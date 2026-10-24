"""启停链路——把栈拉起来、按四遍扫描停干净（issue #37 分节 PB）。

从 `up.py` 搬出来的四分之一。这一层是唯一会「动手」的地方：`taskkill`、注入 QQ、
清死锁、等端口。它依赖 `wg_up_probe`（谁是我的进程）与 `wg_up_wake`（OneBot 答不答），
不反向依赖——`up.py` 只剩门面、`report` 与 `main`。

`--stop` 的契约没变：四遍扫描（树杀守护/看护 → 按脚本指纹扫树外残留 → 按 `-q <账号>`
杀注入实例 → 端口兜底），全部结果写进 `StopResult`，`stopper.log` 一行。`ok` 只在
端口全释放时为真——半停不许记成干净。

DRY 说明（issue #37 明确不做）：`reload_exec._port_open` / `_wait_ports_free` 与
`wg_up_probe.port_open` / `wait_ports_free` 看着重复，但那是**子进程**执行重启时用的
探测，不该 import guard-owned 模块；且语义不同（0.8s 超时、返未释放端口列表 vs 0.4s
超时、返 bool）。合并要先改行为，不属于本节。
"""
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import winconsole  # noqa: E402  (taskkill / 注入都不想闪黑框，issue #39)
from wg_paths import logs_dir, state_dir  # noqa: E402
from wg_up_probe import (  # noqa: E402  (探测层，分节 PA)
    KILLABLE_NAMES,
    NAPCAT_PORTS,
    _process_names,
    port_open,
    port_owners,
    self_pids,
    stack_pids,
    supervisor_pids,
    transient_pids,
    wait_ports_free,
    watchdog_pids,
)
from wg_up_wake import login_info  # noqa: E402  (OneBot 账号探测，分节 PC)

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]
STATE = state_dir()


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
    # 收掉的注入实例数（收前只读计数与直接收掉取较大者，issue #46）。
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
    """Injected QQ instances only, identified by the launch record or `-q <account>`.

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


def _count_instances():
    """只读预扫描：停止动作开始前，现在有几个注入实例（issue #46 计数口径）。

    注入实例通常挂在守护树下，第 1 遍树杀就一并带走了；只在事后数「按标记
    杀掉的」，会把「整棵树收干净」报成「注入实例 0 个」。所以先数，收完再取
    较大值。None 仍表示 bootstrap 导不进来（未统计），不能混成 0。
    """
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from water_group import bootstrap

        return bootstrap.count_injected_instances()
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

    # 先数再收：实例通常挂在守护树下，树杀一过就再也数不到了（issue #46）。
    instances_before = _count_instances()

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
    if instances_before is not None and result.instances is not None:
        # 树杀已经收掉的也算数：报告要说明「这次停栈收掉了几个」，而不是
        # 只说明「事后按标记又抓到几个」（issue #46）。
        result.instances = max(instances_before, result.instances)
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
    # do that (job objects decide, see up.py's module docstring) -- it only takes
    # the console away, which makes every console grandchild allocate a visible one.
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
