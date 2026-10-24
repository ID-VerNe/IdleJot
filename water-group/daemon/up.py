"""Bring the whole water-group stack up with one command, and keep it up.

An agent told to "go water the group" runs this and then only has to write replies.

Why this process stays in the foreground instead of detaching: on Windows a child
started with DETACHED_PROCESS still dies with the job object of the shell that
created it, and this host kills the whole tree when a command finishes. Measured
here: both DETACHED_PROCESS and CREATE_BREAKAWAY_FROM_JOB children were gone before
they could write a single line. The only thing that reliably keeps the stack alive
is a parent that never exits, so up.py *becomes* the supervisor: it ensures NapCat,
starts the children, reports, and then blocks in the watch loop.

Usage:
  up.py            bring everything up and stay resident (run it in the background)
  up.py --status   report only, touch nothing
  up.py --stop     take the whole stack down (the only stop path; stop.ps1 delegates)
  up.py --detach   legacy: spawn the supervisor and return (unreliable under a job)
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
import winconsole  # noqa: E402  (stdout 编码 + 控制台代码页，一次收口)
import wg_clock  # noqa: E402  (「现在」的唯一出口，issue #95)
import wg_config  # noqa: E402  (config.json 的唯一读取口，issue #4 PA0)
import wg_images  # noqa: E402  (图片标记渲染，与 reply/look 同一份实现)
from wg_paths import logs_dir, state_dir
STATE = state_dir()
CONFIG = wg_config.CONFIG

NAPCAT_PORTS = (43000, 43001)
BACKEND_PORTS = (47860, 48777)
STOP_PORTS = NAPCAT_PORTS + BACKEND_PORTS

# Command-line fingerprints of every process that belongs to the stack. Matching is
# by script path on purpose: a name-based kill (`python.exe`, `QQ.exe`) takes the
# agent host and the user's own desktop QQ with it.
#
# listen.py is deliberately absent: it is the agent's one-shot loop, not part of the
# stack, and it exits on its own after a wake. Killing it here would also kill a
# listener that is mid-`--wait` in some other turn.
#
# Multi-group (`--stop --group A`) is not implemented yet; when it is, these
# fingerprints have to carry the group argument. See doc/MULTI_GROUP_PLAN.md.
STACK_SCRIPTS = (
    "supervisor\\.py",
    "daemon[\\\\/]up\\.py",
    "daemon[\\\\/]watchdog\\.py",
    "daemon[\\\\/]collector\\.py",
    "daemon[\\\\/]commands\\.py",
    "daemon[\\\\/]db_keeper\\.py",
    "server_wg\\.py",
)

PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")

# 待回唤醒的时效上限（秒）：超过它的 @ /关键词不再诱导回复。冷启动后
# wg_events.jsonl 里从未回复过的老唤醒会永远挂 [待回]，Agent 据此会回头
# 回复早已过时的话题——这个上限就是那条时间维度上的刀。
DEFAULT_PENDING_REPLY_TTL = 7200

# 合并转发的唤醒判定预算（issue #9）。转发体只用来回答「叫不叫得醒」，
# 不承载正文：节点数与字符数都封顶，避免一条 100 条转发把 --status / watch
# 的每次扫描拖成毫秒级长尾。与 chatlocal 的 MAX_NODES 对齐（QQ 单条上限）。
FORWARD_WAKE_NODES = 100
FORWARD_WAKE_CHARS = 4000

# 探针和 taskkill 都是控制台程序，而这个进程本身可能没有可见的控制台（经 pythonw/
# 无窗口控制台起出来），于是每跑一次探针就闪一个黑框。开窗抑制统一走
# winconsole.run_hidden（issue #39）。


def strip_proxy():
    """The host proxy intercepts loopback and NapCat only answers on 127.0.0.1.

    Mutating os.environ instead of only building a child env means every module
    imported afterwards — mcp_client, session, bootstrap — inherits a clean one.
    """
    for key in list(os.environ):
        if key.upper() in PROXY_KEYS:
            os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"


def python_exe():
    candidate = ROOT / ".venv" / "Scripts" / "python.exe"
    return str(candidate) if candidate.exists() else sys.executable


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.8):
            return True
    except OSError:
        return False


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""


def config():
    """委托 ``wg_config``：配置只有一份实现（issue #4 PA0）。"""
    return wg_config.config()


def onebot_token():
    return config().get("onebot_token", "")


def login_info():
    """(account, nickname) from OneBot, or None when NapCat is not answering.

    Port open is not enough: the port stays bound while the renderer behind it is
    already wedged, so only an HTTP answer proves the bridge is alive.
    """
    body = json.dumps({}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:43000/get_login_info", data=body,
        headers={"Authorization": f"Bearer {onebot_token()}",
                 "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if data.get("status") != "ok":
        return None
    info = data.get("data") or {}
    return info.get("user_id"), info.get("nickname")


def self_pids():
    """Our own pid plus the redirector that launched us.

    `python.exe` in the venv is a shim: it carries the same command line as the
    interpreter it starts, so a `--status` run would otherwise find and report
    itself as a running guard.
    """
    return {str(os.getpid()), str(os.getppid())}


def _match_pids(pattern, exclude=None):
    """PIDs whose command line matches, with launcher shims collapsed.

    The venv python.exe is a redirector: it shows up in WMI with the same command
    line as the interpreter it spawns, so a naive match reports one guard as two
    pids. A pid whose parent is also matched is that second layer, so it is dropped.
    """
    if os.name != "nt":
        # console-gate: posix-only -- pgrep has no window to open.
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True)
        found = [line.strip() for line in out.stdout.decode().splitlines() if line.strip()]
        return [p for p in found if p not in exclude] if exclude else found
    script = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | "
              f"Where-Object {{ $_.CommandLine -match '{pattern}' }} | "
              "ForEach-Object { \"$($_.ProcessId) $($_.ParentProcessId)\" }")
    try:
        out = winconsole.run_hidden(["powershell", "-NoProfile", "-NonInteractive",
                                     "-Command", script], capture_output=True, timeout=30)
    except Exception:  # noqa: BLE001
        return []
    pairs = []
    for line in out.stdout.decode("utf-8", "ignore").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit():
            pairs.append((parts[0], parts[1]))
    matched = {pid for pid, _ in pairs}
    found = [pid for pid, parent in pairs if parent not in matched]
    return [p for p in found if p not in exclude] if exclude else found


def supervisor_pids(exclude=None):
    """PIDs of a live guard loop: either supervisor.py or this script resident.

    One-shot `up.py --status` / `--stop` runs are subtracted: they share the
    `daemon/up.py` command line but are not the guard, and reporting a concurrent
    probe as "guard running" is how a stopped stack looks alive.
    """
    transient = set(transient_pids(exclude))
    pids = _match_pids("supervisor\\.py|daemon[\\\\/]up\\.py", exclude)
    return [pid for pid in pids if pid not in transient]


def watchdog_pids(exclude=None):
    """PIDs of the NapCat watchdog. It is what re-injects a dead instance."""
    return _match_pids("daemon[\\\\/]watchdog\\.py", exclude)


def transient_pids(exclude=None):
    """Short-lived `up.py` invocations: `--status` and `--stop` themselves.

    Neither is the resident guard, and a stop must not tree-kill either. `--status`
    is documented as side-effect free and is what an operator (or an agent) runs to
    find out what is going on; matching `daemon/up.py` alone made a concurrent stop
    kill the diagnostic that was asked to explain the stack -- observed on
    2026-10-22, where a `--stop` in one shell left a `--status` in another with an
    empty report. `--stop` is excluded for the same reason: two stops racing should
    not kill each other's bookkeeping.
    """
    return _match_pids("daemon[\\\\/]up\\.py.*--(?:status|stop)", exclude)


def listener_pids(exclude=None):
    """PIDs of the agent's listen.py loop.

    That loop is the only thing that turns "a message arrived" into "wake the
    agent", and it lives for exactly one wake. An empty answer here is not a
    harmless idle state: the collector keeps writing events and nobody ever reads
    them, which looks to the group like the bot stopped hearing them.
    """
    return _match_pids("daemon[\\\\/]listen\\.py", exclude)


def commands_pids(exclude=None):
    """PIDs of the slash-command daemon: # keywords answered without an agent.

    Its absence is quiet rather than loud: # messages were never delivered to the
    agent anyway (mention_only drops them server-side), so a dead daemon looks
    exactly like "the bot ignores commands" — worth its own status line.
    """
    return _match_pids("daemon[\\\\/]commands\\.py", exclude)


def stack_pids(exclude=None):
    """PIDs of every stack process, found by script path rather than by tree.

    The tree kill in stop_stack only reaches children of a live guard. Anything
    that outlived its parent -- the `server_wg.py` and `collector.py` left holding
    47860/48777 after `--stop` on 10-20 -- is invisible to it, and those leftovers
    are exactly what makes `start-bot.pyw` think the stack is still up.
    """
    return _match_pids("|".join(STACK_SCRIPTS), exclude)


def port_owners(ports=None):
    """`{pid: [addr, ...]}` for LISTENING sockets on the stack's ports.

    The last line of defence after the tree kill and the orphan sweep: a process
    whose command line no longer says what it is (a restarted child that re-exec'd,
    a wrapper) still holds the port, and the port is what `start-bot.pyw` uses to
    decide the stack is alive.
    """
    try:
        raw = winconsole.run_hidden(["netstat", "-ano"], capture_output=True,
                                    timeout=15).stdout or b""
    except Exception:  # noqa: BLE001
        return {}
    wanted = tuple(f":{port}" for port in (ports or STOP_PORTS))
    owners = {}
    for line in raw.decode("utf-8", "ignore").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(wanted):
            if parts[4].isdigit():
                owners.setdefault(parts[4], []).append(parts[1])
    return owners


def wait_ports_free(seconds, ports=None):
    """Wait up to `seconds` for the stack ports to stop listening.

    Returns the ports still bound at the deadline, so the caller can tell a clean
    stop from a half stop instead of reporting success either way.
    """
    deadline = time.time() + seconds
    while True:
        owners = port_owners(ports)
        if not owners:
            return []
        if time.time() >= deadline:
            return sorted({addr for addrs in owners.values() for addr in addrs})
        time.sleep(1)


# Process names the port fallback is allowed to kill. A bound port is not proof
# that the holder is ours: if the user's own tool happens to sit on 47860, killing
# it because it holds "our" port is worse than reporting it and letting a human
# decide. QQ/node are the injected instance's two shapes, python* is the stack's.
KILLABLE_NAMES = {"python.exe", "pythonw.exe", "qq.exe", "node.exe"}


def _process_names(pids):
    """`{pid: exe name}` for the given pids. Missing keys mean the pid is gone."""
    wanted = [pid for pid in pids if str(pid).isdigit()]
    if not wanted:
        return {}
    filt = " or ".join(f"ProcessId={pid}" for pid in wanted)
    script = (f"Get-CimInstance Win32_Process -Filter \"{filt}\" | "
              "ForEach-Object { \"$($_.ProcessId) $($_.Name)\" }")
    try:
        out = winconsole.run_hidden(["powershell", "-NoProfile", "-NonInteractive",
                                     "-Command", script], capture_output=True, timeout=30)
    except Exception:  # noqa: BLE001
        return {}
    names = {}
    for line in out.stdout.decode("utf-8", "ignore").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit():
            names[parts[0]] = parts[1]
    return names


@dataclass
class StopResult:
    """What a stop actually did. Every field is reported, none is inferred.

    `instances` is None (not 0) when bootstrap could not be imported: "we could not
    count them" and "there were none" are different facts, and collapsing them is
    how a half stop gets logged as a clean one.
    """

    guard: list = field(default_factory=list)
    orphans: list = field(default_factory=list)
    port_owners: list = field(default_factory=list)
    refused: list = field(default_factory=list)
    instances: int | None = 0
    ports_left: list = field(default_factory=list)
    # The `#` daemon's lock file, cleared when its holder turned out to be dead.
    # None means there was nothing to clear; the pid is reported so a stale lock
    # that came back is visible in stopper.log instead of silently swept.
    stale_lock: int | None = None

    @property
    def ok(self):
        return not self.ports_left

    def summary(self):
        def pids(values):
            return ",".join(values) if values else "无"

        instances = "未统计" if self.instances is None else f"{self.instances} 个"
        line = (f"[停止] 守护/看护 pid={pids(self.guard)}；树外残留 pid={pids(self.orphans)}；"
                f"注入实例 {instances}")
        if self.stale_lock:
            line += f"；清掉死锁 pid={self.stale_lock}"
        if self.port_owners:
            line += f"；端口占用者 pid={pids(self.port_owners)}（已强杀）"
        if self.refused:
            line += f"；端口被非栈进程占着，未动：pid={pids(self.refused)}"
        if self.ports_left:
            line += f"；⚠ 端口仍未释放：{','.join(self.ports_left)}"
        else:
            line += "；端口已全部释放"
        return line


def log_stop(result):
    """One line per stop, in the same style as wg-supervisor.log.

    A .pyw stop has no console, so this file is the only place the outcome of a
    double-click is readable afterwards.
    """
    try:
        LOG = logs_dir() / "stopper.log"
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%m-%d %H:%M:%S')} {result.summary()}\n")
    except OSError:
        pass


def _taskkill_tree(pid):
    """`/T` on purpose: killing only the main process leaves the renderer, the
    network utility and the node sidecar running against the same profile, which
    is how a "cleaned" instance kept holding locks and ports."""
    winconsole.run_hidden(["taskkill", "/F", "/T", "/PID", pid],
                          capture_output=True, timeout=20)


def _kill_instances():
    """Injected QQ instances only, identified by `-q <self_account>`.

    Returns None when bootstrap cannot be imported: the caller must be able to say
    "not counted" rather than "zero", see StopResult.
    """
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from water_group import bootstrap

        return bootstrap.kill_stale_instances()
    except Exception:  # noqa: BLE001
        return None


def _clear_stale_commands_lock():
    """Drop the `#` daemon's single-instance lock when its holder is dead.

    `taskkill /F` skips `release_lock()`, so every forced stop used to leave
    `state/wg_commands.lock` naming a corpse. The next start recovered from it
    (`接管陈锁`), but the file made a stopped stack look locked to anything that
    read state/ directly.

    The path and the alive/dead rule stay in `commands.py`, which owns the lock;
    this only asks it to clean up. A live holder is left alone -- two instances
    racing between the kill and this call is precisely what the lock guards.
    """
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    try:
        import commands
    except Exception:  # noqa: BLE001
        return None
    try:
        return commands.clear_stale_lock()
    except Exception:  # noqa: BLE001
        return None


def stop_stack():
    """Take the whole stack down and report exactly what was left over.

    Four passes, each covering what the previous one cannot see:

      1. tree-kill the guard (supervisor / resident up.py) and the watchdog, which
         takes their children with them;
      2. sweep anything still matching STACK_SCRIPTS by command line -- the
         `server_wg.py` and `collector.py` that survived `--stop` and kept holding
         47860/48777, which is what made a restart silently do nothing;
      3. kill injected QQ instances by `-q <account>`, never by process name, so the
         user's own desktop client is untouched;
      4. if a port is still bound, kill its LISTENING owner and wait again.

    Then clear the `#` daemon's single-instance lock if its holder died in the
    kill. `taskkill /F` gives no chance to `release_lock()`, so without this every
    forced stop left `state/wg_commands.lock` naming a corpse -- harmless to the
    next start (it recovers) but a lie to anything reading state/ directly.

    Only after that is it honest to say the stack is down, so the result carries the
    leftover ports and `ok` is False whenever any remain.
    """
    me = self_pids()
    result = StopResult()
    # `--status` / `--stop` are one-shot probes, not the resident guard. An operator
    # who runs `--status` to watch a stop must get an answer, not get killed by the
    # thing they are watching.
    protected = set(transient_pids(exclude=me)) | set(me)

    for finder in (supervisor_pids, watchdog_pids):
        for pid in finder(exclude=protected):
            _taskkill_tree(pid)
            result.guard.append(pid)

    # The guard is gone; give its children a moment to notice before judging what
    # survived. Without this the sweep sees the tree it just killed and reports it
    # as an orphan. Skipped when nothing was killed, so a double-click on an
    # already-stopped stack stays instant.
    if result.guard:
        time.sleep(2)
    for pid in stack_pids(exclude=protected):
        _taskkill_tree(pid)
        result.orphans.append(pid)

    result.instances = _kill_instances()
    result.stale_lock = _clear_stale_commands_lock()

    result.ports_left = wait_ports_free(12)
    if result.ports_left:
        holders = [pid for pid in port_owners() if pid not in protected]
        names = _process_names(holders)
        for pid in holders:
            # A bound port is not proof the holder is ours. Only the shapes the
            # stack and its injected instance can take are killed; anything else --
            # including a holder whose name could not be read -- is reported and
            # left alone, because killing a stranger's process over a port collision
            # is worse than a half stop a human can finish.
            name = (names.get(pid) or "").lower()
            if name not in KILLABLE_NAMES:
                result.refused.append(pid)
                continue
            _taskkill_tree(pid)
            result.port_owners.append(pid)
        result.ports_left = wait_ports_free(8)

    log_stop(result)
    return result


def spawn(args, log_name):
    STATE.mkdir(parents=True, exist_ok=True)
    log = open(logs_dir() / log_name, "ab")
    # popen_hidden already gives the child a windowless console. DETACHED_PROCESS
    # used to be passed here "so the stack outlives this process", but it does not
    # do that (job objects decide, see the module docstring) -- it only takes the
    # console away, which makes every console grandchild allocate a visible one.
    return winconsole.popen_hidden(args, cwd=str(ROOT), env=dict(os.environ),
                                   stdout=log, stderr=subprocess.STDOUT)


def wait_ports(ports, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if all(port_open(p) for p in ports):
            return True
        time.sleep(2)
    return all(port_open(p) for p in ports)


def ensure_napcat():
    if all(port_open(p) for p in NAPCAT_PORTS):
        # Ports bound but OneBot silent almost always means NapCat is mid-push or
        # mid-reconnect, not dead. Re-injecting here would cost the session and the
        # cursor, so wait it out before concluding anything.
        for _ in range(3):
            if login_info():
                return True, "已在线"
            time.sleep(5)
        return False, "端口在但 OneBot 无响应（正在重连，等 30 秒再跑一次）"

    # The watchdog owns the NapCat lifecycle whenever a guard loop runs. Two
    # launchers racing to inject the same account leaves several instances sharing
    # one user-data-dir, and their GPU processes then fight until QQ dies. Wait for
    # the watchdog instead of starting a second injection.
    #
    # The check is for watchdog.py specifically, not for the guard: at cold start
    # this process *is* the guard, so asking "is a guard running" always answers yes.
    # Injection itself measures ~65 s, so the window has to clear that comfortably.
    if watchdog_pids(exclude=self_pids()):
        print("[1/3] NapCat 离线，守护进程会重新注入，等待…", flush=True)
        for _ in range(24):
            time.sleep(5)
            if all(port_open(p) for p in NAPCAT_PORTS) and login_info():
                return True, "守护已恢复"
        return False, "守护 120 秒内没恢复 NapCat（看 ~/.water-group/logs/napcat-watchdog.log）"

    print("[1/3] NapCat 未启动且守护未运行，直接注入…", flush=True)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from water_group import bootstrap
    try:
        ok, note = bootstrap.ensure_napcat()
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    return ok, note


def load_supervisor():
    """Import the sibling module whether or not this file was run as a script."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import supervisor
    return supervisor


def load_reload():
    """Import the reload planner/executor next to this file."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import reload
    return reload


def events():
    path = STATE / "wg_events.jsonl"
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return rows


def self_account():
    return str(config().get("self_account") or "")


def wake_words():
    words = config().get("self_names") or []
    return [str(w) for w in words if w]


def command_prefix():
    """`#` 指令前缀，读 config.json 的 slash_commands.prefix。

    归一化与 commands.settings_from 一致：缺失/空值回落 "#"。每次现读、不缓存：
    config 是热文件，测试也靠替换 up.config 打桩。
    """
    section = config().get("slash_commands") or {}
    return str(section.get("prefix") or "#")


def is_command(text, prefix=None):
    """这条纯文本是不是 `#` 指令守护的消息（issue #70）。

    接受规则必须与 commands.command_key 完全一致：前缀在行首、且前缀之后还有
    非空白内容。宽一点会吞掉守护也不认的消息，窄一点会让指令混进追问窗口——
    两边都不行。行内出现不算（`C#`、`第#3名`、URL fragment）。
    """
    prefix = command_prefix() if prefix is None else prefix
    if not prefix or not text or not text.startswith(prefix):
        return False
    return bool(text[len(prefix):].strip())


def _forward_at(nodes, account, budget, depth=0):
    """转发体里有没有 @ 我（issue #9）。只读落盘节点，不联网。

    `budget` 是共享的 ``{"nodes": N}`` 计数器：一条消息挂多个转发时，总扫描量
    仍受 ``FORWARD_WAKE_NODES`` 约束。嵌套最多跟两层，与 chatlocal 的
    ``sanitize_nodes`` 展开深度一致。
    """
    if depth >= 2:
        return False
    for node in nodes or []:
        if budget["nodes"] <= 0:
            return False
        budget["nodes"] -= 1
        if not isinstance(node, dict):
            continue
        for seg in node.get("segments") or []:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") == "at" and str(seg.get("qq")) == account:
                return True
            if seg.get("type") == "forward" and _forward_at(seg.get("nodes"), account, budget, depth + 1):
                return True
    return False


def _forward_word(nodes, budget, words, depth=0):
    """转发体里有没有唤醒词，返回命中的那个（issue #9）。"""
    if depth >= 2:
        return ""
    for node in nodes or []:
        if budget["nodes"] <= 0 or budget["chars"] <= 0:
            return ""
        budget["nodes"] -= 1
        if not isinstance(node, dict):
            continue
        for seg in node.get("segments") or []:
            if not isinstance(seg, dict):
                continue
            kind = seg.get("type")
            if kind == "text":
                text = str(seg.get("text") or "")[: max(0, budget["chars"])]
                budget["chars"] -= len(text)
                for word in words:
                    if word in text:
                        return word
            elif kind == "forward":
                word = _forward_word(seg.get("nodes"), budget, words, depth + 1)
                if word:
                    return word
    return ""


def wake_reason(event):
    """Why this message should wake an agent: an explicit @, or our name in the text.

    The server already applies the same three rules (at / reply-to-self / keyword)
    before it returns a batch; repeating them here lets the report say which
    messages still deserve a reply without a second round trip.

    合并转发（issue #9）：正文在 ``referenced_messages[].nodes`` 里，顶层
    ``segments`` 只有 ``{"type":"forward","forward_index":0}``、``content`` 只有
    ``[合并转发]``。判定因此要下沉到节点内——转发里 @ 我、或转发正文里有我的
    名字，都该叫醒。数据全部来自落盘事件，这里不发起网络请求。
    """
    if event.get("is_self"):
        return ""
    account = self_account()
    for seg in event.get("segments") or []:
        if seg.get("type") == "at" and str(seg.get("qq")) == account:
            return "at"
    text = event.get("content") or ""
    # `#` 指令归 commands.py。@ 已经在上面先返回了：ignore_at_self=true 时守护会
    # 把带 @ 的混合消息让给 Agent，所以这里只挡纯指令。闸门放在关键词之前——
    # `#帮助 机器人` 里的名字不该把 Agent 叫醒、和守护抢同一条消息（issue #70）。
    if is_command(text):
        return ""
    # 只认服务端当场展开过的转发：state 不是 available 就没有正文可判，宁可不醒
    # 也不猜（#8 的「状态必须说出来」同一口径）。
    forwards = [d for d in event.get("referenced_messages") or []
                if isinstance(d, dict) and d.get("type") == "forward"
                and d.get("state") == "available"]
    # 转发体内的 @ 与顶层同一优先级：@ 先于关键词。
    budget = {"nodes": FORWARD_WAKE_NODES}
    for detail in forwards:
        if _forward_at(detail.get("nodes"), account, budget):
            return "at"
    for word in wake_words():
        if word in text:
            return f"keyword:{word}"
    words = wake_words()
    budget = {"nodes": FORWARD_WAKE_NODES, "chars": FORWARD_WAKE_CHARS}
    for detail in forwards:
        word = _forward_word(detail.get("nodes"), budget, words)
        if word:
            return f"keyword:{word}"
    return ""


def pending_reply_ttl():
    """待回唤醒的时效上限（秒），读 config.json 的 pending_reply.max_age_seconds。

    与 followup 段平级。缺失或非法（含 0 / 负数）时回落 7200：一个拼错的
    配置不该把时效关掉，那正是这个修复要治的病。
    """
    raw = (config().get("pending_reply") or {}).get("max_age_seconds")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PENDING_REPLY_TTL
    return value if value > 0 else DEFAULT_PENDING_REPLY_TTL


def wake_expired(event, now=None):
    """这条唤醒是否已过待回时效——按事件时间实时计算，不落任何账本。

    过期不等于已回：不写 wg_replied.jsonl（那是「已回复」账本，语义不同），
    也不另存 expired 集合。每次报告现场算，改 TTL 对历史事件立即生效。

    没有时间戳的事件无法判定新旧，一律当没过期：宁可多显示一条，也不把
    数据异常悄悄藏成「没问题」。
    """
    ts = event.get("timestamp") or 0
    if not ts:
        return False
    now = time.time() if now is None else now
    return (now - ts / 1000) > pending_reply_ttl()


def heartbeat_time():
    """采集器最后一次完成循环的墙上时间（秒），没有心跳时返回 None。

    报告要同时给「19s 前」和绝对时刻：只有相对量的话，Agent 读到它时可能已经
    又过了几十秒，换算回去仍然不准（issue #95）。
    """
    raw = read_text(STATE / "wg_heartbeat")
    try:
        return int(raw)
    except ValueError:
        return None


def heartbeat_age():
    beat = heartbeat_time()
    return None if beat is None else time.time() - beat


def replied_ids():
    """Ids reply.py has already answered.

    The collector only delivers a batch when someone mentions the bot, so a reply
    we just sent is usually absent from wg_events.jsonl. Without this ledger the
    next agent would read the report, see [待回] on an answered message, and reply
    to it a second time.
    """
    ids = set()
    path = STATE / "wg_replied.jsonl"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                ids.add(json.loads(line)["id"])
            except (ValueError, KeyError):
                continue
    except OSError:
        pass
    return ids


_LEDGER_START = {"mtime": None, "ts": 0.0}


def ledger_started():
    """Timestamp of the first entry in wg_replied.jsonl, or 0.0 when there is none.

    Everything older than this was answered before the ledger existed, so a later
    self message is the only evidence left for it. Everything newer has to be in
    the ledger itself. Cached on mtime: the file is append-only, so the first
    line never moves.
    """
    path = STATE / "wg_replied.jsonl"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return 0.0
    if _LEDGER_START["mtime"] == mtime:
        return _LEDGER_START["ts"]
    ts = 0.0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            ts = float(json.loads(line)["ts"])
            break
        except (ValueError, KeyError, TypeError):
            continue
    _LEDGER_START.update(mtime=mtime, ts=ts)
    return ts


def replied_after(event_id, rows, ledger=None):
    """Whether the bot spoke after this message — a cheap proxy for "answered".

    The ack cursor only records what reached disk, so it cannot answer this. The
    local ledger is the primary signal; a self message appearing later in the
    stream is the fallback for replies sent before the ledger existed.

    That fallback now only has historical rows to work with: collector stopped
    appending self echoes (#28), so everything after the ledger's first entry is
    judged by the ledger alone. The data it still sees is the pre-ledger tail
    already on disk, which has to keep its old meaning.

    That fallback is fenced to pre-ledger events on purpose. Left open it turns
    "answered" into "any self message came later", so a reply to one message
    marks every @ that arrived while it was being typed as answered — which is
    exactly how a burst gets dropped: a later message swallowed by the
    reply to 1563「？」, because that reply landed two seconds later.
    """
    if ledger and event_id in ledger:
        return True
    started = ledger_started()
    seen = False
    for row in rows:
        if row.get("id") == event_id:
            seen = True
            if started and (row.get("timestamp") or 0) / 1000 >= started:
                # The ledger was already keeping score by then. Not in it, not answered.
                return False
            continue
        if seen and row.get("is_self"):
            return True
    return False


def wake_state(event, rows, ledger):
    """一条唤醒的三态：已回 / 过期 / 待回。报告与 --list 共用这一个判定。

    过期是时间维度上的第二把刀：replied_after 只回答「回过没有」，不回答
    「还值不值得回」。两问都答了，[待回] 才是真正该回复的那批。
    """
    if replied_after(event.get("id"), rows, ledger):
        return "已回"
    if wake_expired(event):
        return "过期"
    return "待回"


def report(exclude_pids=None, resident_pid=None):
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import followup

    account, nickname = (None, None)
    info = login_info()
    if info:
        account, nickname = info

    napcat = "离线"
    if all(port_open(p) for p in NAPCAT_PORTS):
        napcat = f"在线 ({account} {nickname})" if info else "端口在，OneBot 暂未响应"
    backend = "在线" if all(port_open(p) for p in BACKEND_PORTS) else "离线"
    if resident_pid:
        guard = f"运行中 pid={resident_pid}（本进程常驻）"
    else:
        pids = supervisor_pids(exclude=exclude_pids)
        guard = f"运行中 pid={','.join(pids)}" if pids else "未运行"
    age = heartbeat_age()
    beat_at = heartbeat_time()
    at = wg_clock.clock(beat_at) if beat_at else "时刻不可读"
    if age is None:
        beat = "无心跳"
    elif age > 300:
        beat = f"{age:.0f}s 前（{at}，已卡死，supervisor 应会重启）"
    else:
        beat = f"{age:.0f}s 前（{at}，wait 周期 170s，正常）"
    listeners = listener_pids(exclude=exclude_pids)
    listener = (f"已挂 pid={','.join(listeners)}" if listeners
                else "未挂 —— 消息照收，但没人会叫醒你")
    cmds = commands_pids(exclude=exclude_pids)
    command = (f"运行中 pid={','.join(cmds)}（# 指令直回，不过 Agent）" if cmds
               else "未运行（# 指令无自动回复）")
    window = followup.load()
    if window and not followup.stale(window):
        until = window.get("until") or 0
        left = wg_clock.seconds_left(until)
        left_text = f"，还剩 {left}s" if left is not None else ""
        follow = (f"进行中（回复后还可跟 {window.get('budget')} 条，"
                  f"静默至 {wg_clock.clock(until)}{left_text}）")
    else:
        follow = "未开窗（只等 @ 和关键词）"
    rows = events()
    ledger = replied_ids()
    wakes = [e for e in rows if wake_reason(e)][-6:]

    print()
    print(wg_clock.now_line())
    print(f"NapCat    : {napcat}")
    print(f"后端      : {backend}  (47860 / 48777)")
    print(f"守护      : {guard}  心跳 {beat}")
    print(f"监听      : {listener}")
    print(f"#指令     : {command}")
    try:
        import commands as wg_commands  # noqa: PLC0415  只读：拿插件状态与设置
        plugin_line = wg_commands.PLUGINS.status_line(wg_commands.load_settings())
    except Exception as exc:  # noqa: BLE001  status must not fail because a plugin is broken
        plugin_line = f"状态不可读（{type(exc).__name__}: {exc}）"
    print(f"插件      : {plugin_line}")
    try:
        reload_line = load_reload().status_line()
    except Exception as exc:  # noqa: BLE001  status must not fail because reload is broken
        reload_line = f"状态不可读（{type(exc).__name__}: {exc}）"
    print(f"热重载    : {reload_line}")
    print(f"追问      : {follow}")
    print(f"会话      : {read_text(STATE / 'wg_session.txt') or '（尚未建立）'}")
    print(f"游标      : {read_text(STATE / 'wg_ack.txt') or '0'}   事件 {len(rows)} 条")
    if wakes:
        print("最近唤醒  :")
        for e in wakes:
            stamp = wg_clock.clock_ms(e.get("timestamp"))
            state = wake_state(e, rows, ledger)
            images = wg_images.render(e)
            print(f"  id={e.get('id')} {stamp} [{state}] [{wake_reason(e)}] "
                  f"{e.get('sender')}: {(e.get('content') or '')[:40]}"
                  f"{('  ' + images) if images else ''}")
    else:
        print("最近唤醒  : 无")
    print()
    print("下一步    : 对上面 id 里还没回过的，用 reply.py <id> \"文本\" 回复"
          "（内部走 plan_chat_reply + send_chat_reply）；")
    print("            [过期] 是超过待回时效的老唤醒，默认不再回，确实要回加 --force；")
    print("            回完立刻重挂 listen.py --timeout 300，它是唯一会叫醒你的东西。")
    print("            # 开头的指令由 commands.py 直接回复，Agent 不参与；增删关键词改 "
          "water-group/config.json 的 slash_commands.commands（30 秒热生效）。")
    return 0


def main():
    parser = argparse.ArgumentParser(description="一键接管水群：拉起 NapCat、后端与守护并常驻")
    parser.add_argument("--status", action="store_true", help="只报告状态，不启动任何东西")
    parser.add_argument("--detach", action="store_true",
                        help="旧行为：后台拉起 supervisor 后立即返回（沙箱内不可靠）")
    parser.add_argument("--stop", action="store_true",
                        help="停掉常驻守护、看护与注入实例后退出")
    args = parser.parse_args()

    winconsole.ensure_utf8_stdio()
    strip_proxy()

    if args.stop:
        result = stop_stack()
        print(result.summary())
        if result.ports_left:
            print("      仍被占用说明有进程不受脚本指纹管辖，按 pid 精准清后重跑；"
                  "详见 " + str(logs_dir() / "stopper.log"))
        return 0 if result.ok else 1

    if args.status:
        return report(exclude_pids=self_pids())

    if args.detach:
        ok_napcat, note_napcat = ensure_napcat()
        if not all(port_open(p) for p in BACKEND_PORTS):
            spawn([python_exe(), str(HERE / "supervisor.py")], "supervisor.log")
        ok = wait_ports(BACKEND_PORTS, 120)
        report()
        print(f"[结果] NapCat: {note_napcat} | 后端: {'启动成功' if ok else '未就绪'} | "
              f"守护: 已脱离（随启动它的进程一起死）")
        return 0 if (ok_napcat and ok) else 1

    ok_napcat, note_napcat = ensure_napcat()

    supervisor = load_supervisor()
    hot_reload = load_reload()
    procs = supervisor.start_all()
    hot_reload.write_guard_state(os.getpid(), build=hot_reload.build_stamp())
    reloader = hot_reload.Reloader(
        spawn=supervisor.spawn,
        healthy=lambda: bool(login_info()),
        quarantined=supervisor.QUARANTINED,
        config=(config().get("reload") or {}),
    )
    print("[2/3] 后端启动中…", flush=True)
    ok_backend = wait_ports(BACKEND_PORTS, 180)
    print("[3/3] 守护循环接管中…", flush=True)

    report(resident_pid=os.getpid())
    print(f"[结果] NapCat: {note_napcat} | 后端: {'启动成功' if ok_backend else '未就绪'} | "
          f"守护: 本进程 pid={os.getpid()} 常驻")
    print("[常驻] 不要关闭这个任务；关掉它等于关掉整个水群。", flush=True)

    try:
        supervisor.supervise(procs, on_tick=reloader.tick,
                             quarantined=supervisor.QUARANTINED)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
