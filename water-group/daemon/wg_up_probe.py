"""进程/端口探测——栈的观测层，不启不停（issue #37 分节 PA）。

issue #37：`up.py` 一个文件混了四件事——探测栈、启停栈、读配置、判断一条消息该不该
叫醒 Agent——1050 行，也是 `check_module_size` 豁免名单里仅剩的一条。本模块是其中的
**探测**四分之一：只回答「哪些 pid 属于我们」「哪个端口还被占着」，不认识配置、不认识
回复、也不认识栈是怎么起来的。所以它既能被启停层 import，也能被只想看机器的测试单独
import。

匹配一律按**脚本路径**，不按进程名：`python.exe` / `QQ.exe` 这种名字匹配会把 Agent
宿主和用户自己的桌面 QQ 一起带走。

探针既然已经在读命令行，就把命令行一并带回来（issue #86）：**运行中的进程只在自己的
命令行里写着自己用了什么参数**，而状态行只报 pid 时，一个 `--timeout 300` 的监听和一个
`--timeout 90` 的监听在诊断屏上长得一模一样。

Windows only（和 daemon 其它成员一致）：finder 走 WMI 读命令行，端口兜底走 `netstat`。
"""
import os
import re
import socket
import subprocess
import time
from pathlib import Path

import winconsole  # noqa: E402  (探针与 taskkill 都是控制台程序，开窗抑制走它，issue #39)

# 本 checkout 的仓库根（issue #94）：监听探针用它把「别的 worktree / 门禁临时起的
# listen.py」排除出去。探针层不认识配置，但认识自己住在哪棵树里是分内事。
ROOT = Path(__file__).resolve().parents[2]

NAPCAT_PORTS = (43000, 43001)
BACKEND_PORTS = (47860, 48777)
STOP_PORTS = NAPCAT_PORTS + BACKEND_PORTS

# Command-line fingerprints of every process that belongs to the stack. Matching is
# by script path on purpose: a name-based kill (`python.exe`, `QQ.exe`) takes the
# agent host and the user's own desktop QQ with it.
#
# listen.py is deliberately absent: it is the agent's one-shot loop, not part of the
# stack, and it exits on its own after a wake. Killing it here would also kill a
# listener that is mid-`--wait` in some other turn. Consistently, `listener_pids()`
# answers for *this* checkout only (issue #94) -- it never reports another tree's
# listener, so a stop here neither sees nor touches one. This tree's own gate
# listeners are skipped for the same reason (they carry `--probe`, issue #94 PB):
# a stop -- or a `--status` reader -- has no business with a throwaway probe.
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

# Process names the port fallback is allowed to kill. A bound port is not proof
# that the holder is ours: if the user's own tool happens to sit on 47860, killing
# it because it holds "our" port is worse than reporting it and letting a human
# decide. QQ/node are the injected instance's two shapes, python* is the stack's.
KILLABLE_NAMES = {"python.exe", "pythonw.exe", "qq.exe", "node.exe"}

# The wait budget a live process was started with (issue #86). Only the value is
# read here; whether it is too big is the caller's business (`wg_budget`).
TIMEOUT_ARG = re.compile(r"--timeout[ =](\d+)")

# A gate/test throwaway listener says so on its own command line (issue #94
# section PB). Root scoping (PA) keeps *other* trees out, but a gate running in
# *this* tree starts its temporary listeners with this checkout's own absolute
# script path -- indistinguishable from a real listener by shape alone. `--probe`
# is carried by nothing on the duty path (reply.py's re-hang, the `RESTART:`
# hint, a hand-hung loop), so skipping it costs no real listener. Root scoping
# itself needed one more boundary (issue #101): §2.7 puts each issue's worktree
# at `<ROOT>/.tmp/wt-<issue>`, *inside* the root, so "under ROOT" alone would
# count another tree's listener as ours -- see `_own_checkout`.
PROBE_ARG = re.compile(r"(?:^|\s)--probe(?:\s|$)")


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.8):
            return True
    except OSError:
        return False


def self_pids():
    """Our own pid plus the redirector that launched us.

    `python.exe` in the venv is a shim: it carries the same command line as the
    interpreter it starts, so a `--status` run would otherwise find and report
    itself as a running guard.
    """
    return {str(os.getpid()), str(os.getppid())}


def _match_rows(pattern, exclude=None):
    """`[(pid, command_line)]` whose command line matches, launcher shims collapsed.

    The matching rule is unchanged (by script path, never by process name); what is
    new is that the command line comes back too (issue #86). It is the only place a
    running process states its own parameters, and `--status` needs it to tell a
    listener on the old budget from one on the current one.

    The venv python.exe is a redirector: it shows up in WMI with the same command
    line as the interpreter it spawns, so a naive match reports one guard as two
    pids. A pid whose parent is also matched is that second layer, so it is dropped.
    """
    rows = []
    if os.name != "nt":
        # console-gate: posix-only -- ps has no window to open.
        out = subprocess.run(["ps", "-ww", "-eo", "pid=,ppid=,args="], capture_output=True)
        for line in out.stdout.decode("utf-8", "ignore").splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
                continue
            if re.search(pattern, parts[2]):
                rows.append((parts[0], parts[1], parts[2]))
    else:
        # `|` separates the fields because a command line contains spaces (and may
        # contain `|` itself): `split("|", 2)` keeps the tail intact either way.
        script = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | "
                  f"Where-Object {{ $_.CommandLine -match '{pattern}' }} | "
                  "ForEach-Object { \"$($_.ProcessId)|$($_.ParentProcessId)|$($_.CommandLine)\" }")
        try:
            out = winconsole.run_hidden(["powershell", "-NoProfile", "-NonInteractive",
                                         "-Command", script], capture_output=True, timeout=30)
        except Exception:  # noqa: BLE001
            out = None
        for line in (getattr(out, "stdout", None) or b"").decode("utf-8", "ignore").splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                rows.append((parts[0], parts[1], parts[2]))
    matched = {pid for pid, _, _ in rows}
    found = [(pid, cmdline) for pid, parent, cmdline in rows if parent not in matched]
    return [(pid, cmdline) for pid, cmdline in found if pid not in exclude] if exclude else found


def _match_pids(pattern, exclude=None):
    """PIDs whose command line matches, with launcher shims collapsed.

    Thin wrapper over `_match_rows`: the pids are all some callers want, and
    `scripts/check_stop_stack.py` stubs this one entry point to fake the whole
    machine -- moving the implementation elsewhere would silently un-stub it.
    """
    return [pid for pid, _ in _match_rows(pattern, exclude)]


# A listen.py script path written as an absolute path (issue #94). Only the drive
# letter anchors the match, so mixed separators (`water-group\daemon/listen.py`)
# still count; the venv interpreter path never ends in `listen.py` and cannot
# match. A command line without one was launched relative to the repo root -- the
# only way the daemon is ever started relatively (`reply.py` passes `cwd=ROOT`,
# the `RESTART:` hint assumes the same) -- so it belongs to this tree.
_ABS_LISTEN = re.compile(r"[A-Za-z]:[\\/][^\s|]*listen\.py")


def _registered_worktrees():
    """Every worktree root registered in this repo (issue #101), normcased.

    `git worktree list --porcelain` is the one authority on worktree boundaries
    that does not depend on a naming convention: it answers "which directories
    are checkouts of this repo", nested ones included. It is only consulted for
    a path already known to sit under ROOT, so a listener elsewhere costs no
    subprocess. Any failure (no git, not a repo, timeout) reads as "no extra
    roots": the `.tmp/wt-*` convention in `_own_checkout` still covers the
    documented layout, and a missed boundary is the pre-#101 behaviour, not a
    crash on the wake path.
    """
    try:
        out = winconsole.run_hidden(
            ["git", "-c", "core.quotepath=false", "worktree", "list", "--porcelain"],
            cwd=str(ROOT), capture_output=True, timeout=30)
        roots = []
        for line in (getattr(out, "stdout", None) or b"").decode("utf-8", "ignore").splitlines():
            if line.startswith("worktree "):
                roots.append(Path(os.path.normcase(line[len("worktree "):].strip())))
        return roots
    except Exception:  # noqa: BLE001
        return []


def _own_checkout(cmdline):
    """False when the command line pins listen.py to another checkout (#94/#101).

    Worktrees are the norm here (one per issue), and gate scripts spawn throwaway
    listeners in temp trees, so "matches daemon/listen.py" is not ownership.

    Neither is "under the repo root" (issue #101): §2.7 puts each issue's
    worktree at `<ROOT>/.tmp/wt-<issue>` -- *inside* the root -- so another
    tree's listener path lives under ROOT exactly like ours does. The walk from
    the script up to ROOT therefore stops at the first worktree root it crosses:
    a `.tmp/wt-*` slot (the §2.7 layout, decided without git so a pruned
    registration still counts) or any root registered in this repo
    (`git worktree list`). Only a path that reaches ROOT without crossing one is
    ours.

    Known limit: WMI does not expose a process's cwd, so a foreign listener
    started with a *relative* path is indistinguishable from ours -- every
    existing start path is either absolute or rooted at the repo root.
    """
    found = _ABS_LISTEN.search(cmdline or "")
    if not found:
        return True
    # normcase 抹平大小写与 `/`、`\` 之差；归属判定交给 pathlib，不手工拼分隔符。
    root = Path(os.path.normcase(str(ROOT)))
    script = Path(os.path.normcase(found.group(0)))
    if script == root or root not in script.parents:
        return script == root or root in script.parents
    # script 在 ROOT 之下，还要问「这一路上有没有跨过另一棵 worktree 的根」
    # （issue #101）。`parents` 由近到远，排在 ROOT 前面的那若干级才是候选边界。
    parents = list(script.parents)
    boundary = parents[:parents.index(root)]
    if any(p.parent.name == ".tmp" and p.name.startswith("wt-") for p in boundary):
        return False
    return not any(p in _registered_worktrees() for p in boundary)


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
    """PIDs of *this* checkout's listen.py loop (issue #94 scopes it to the tree).

    That loop is the only thing that turns "a message arrived" into "wake the
    agent", and it lives for exactly one wake. An empty answer here is not a
    harmless idle state: the collector keeps writing events and nobody ever reads
    them, which looks to the group like the bot stopped hearing them.

    The answer is scoped to the tree this probe itself lives in. The bare script
    pattern also matches listen.py processes started from other worktrees and by
    gate scripts, and consumers then misread them as "a listener is already
    up": `reply.py` skipped a necessary re-hang that way, and a `--status` pid
    belonging to another tree invited a `taskkill` that broke the gate mid-run.

    Root scoping (issue #94 PA) covers other trees -- including the ones nested
    *inside* ROOT at `.tmp/wt-<issue>` (§2.7 layout, issue #101); this tree's
    own gate listeners needed a second, explicit line: they run this checkout's
    listen.py by absolute path, so shape alone cannot tell them from a real
    listener. They carry `--probe` (section PB) and are skipped here.
    """
    return [pid for pid, cmdline in _match_rows("daemon[\\\\/]listen\\.py", exclude)
            if _own_checkout(cmdline) and not PROBE_ARG.search(cmdline or "")]


def parse_timeout(cmdline):
    """The `--timeout N` on a command line, or None when it was not passed.

    `--timeout=N` counts too: the value is what matters, not which spelling the
    caller used. Nothing is guessed here -- a listener that never wrote a timeout
    down really is running on whatever default its own code had.
    """
    found = TIMEOUT_ARG.search(cmdline or "")
    return int(found.group(1)) if found else None


def listener_specs(exclude=None):
    """`[{"pid": str, "timeout": int | None}]` for this checkout's listen.py.

    The timeout comes off the command line, because that is the only place a
    running listener says which budget it is on (issue #86): `--status` used to
    report the pid alone, so a listener started before the budget fix looked
    exactly like one started after it -- and the old one dies on the next quiet
    stretch without a word on the way out. Scoped to this tree like
    `listener_pids()` (issue #94, nested worktrees excluded per issue #101):
    another worktree's listener is not ours to report, diagnose, or kill. Gate
    listeners (`--probe`, section PB) are skipped here too: reporting one
    invites the `taskkill` that breaks the gate mid-run.
    """
    return [{"pid": pid, "timeout": parse_timeout(cmdline)}
            for pid, cmdline in _match_rows("daemon[\\\\/]listen\\.py", exclude)
            if _own_checkout(cmdline) and not PROBE_ARG.search(cmdline or "")]


def collector_pids(exclude=None):
    """PIDs of the event collector: without it no message reaches local storage.

    issue #142：`--status` 的监听行曾无条件说「消息照收，但没人会叫醒你」。
    NapCat 离线或 collector 不在时那句话是假的——消息连本地都到不了，不是
    「收到了但没人看」。这条探针只回答「此刻有没有进程在把 OneBot 事件写进
    `wg_events` / `chat_inbox`」，与 `commands_pids()` 同一套脚本指纹匹配。
    """
    return _match_pids("daemon[\\\\/]collector\\.py", exclude)


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
