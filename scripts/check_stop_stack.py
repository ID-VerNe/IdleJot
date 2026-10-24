"""钉住「一键停栈」的完整性：栈进程一个不留，官方 QQ 一个不碰。

2026-10-22 的真实故障（issue #11）：`up.py --stop` 只树杀 supervisor / watchdog，
`server_wg.py` 与 `collector.py` 常常原地活着继续占 47860 / 48777；而 `start-bot.pyw`
是按端口判幂等的，于是「停了但其实没停」，重启还静默空转。根因不是缺一个入口脚本，
是停止逻辑本身漏了「树外残留」这一层。

本门禁全程离线：`subprocess.run`（taskkill / netstat）与两个模块的 `time` 都换成记录器，
日志写进临时 `WATER_GROUP_HOME`。它不碰真栈，也不会杀任何真实进程。

    .venv/Scripts/python.exe scripts/check_stop_stack.py
"""
import ast
import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

# up.py 在 import 时就把 state_dir() 求值了，隔离必须发生在 import 之前。
HOME = Path(tempfile.mkdtemp(prefix="tulpa-stop-gate-"))
atexit.register(shutil.rmtree, HOME, True)
os.environ["WATER_GROUP_HOME"] = str(HOME)

import up  # noqa: E402
import wg_up_probe  # noqa: E402  (issue #37 PA: finder 与端口探测的真实调用点)
import wg_up_stack  # noqa: E402  (issue #37 PB: stop_stack 的真实调用点)

SELF = {str(os.getpid()), str(os.getppid())}


class Clock:
    """`wg_up_*` 的 `time` 替身：sleep 不真睡，time() 每次前进，保证等待循环会收敛。"""

    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        self.now += 1
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def strftime(self, fmt, *args):
        import time as real

        return real.strftime(fmt, *args)


class FakeOS:
    """记录 taskkill，并按脚本给出的表回答 netstat。

    `listening` 是 {端口: pid}；`stubborn` 里的 pid 杀不掉，用来模拟清不掉的端口。
    `names` 是 {pid: 进程名}，端口兜底据此判断占用者是不是栈自己的进程。
    """

    def __init__(self, listening=None, stubborn=(), names=None):
        self.kills = []
        self.listening = dict(listening or {})
        self.stubborn = set(stubborn)
        self.names = dict(names or {})

    def run(self, cmd, *args, **kwargs):
        if cmd[0] == "taskkill":
            pid = cmd[cmd.index("/PID") + 1]
            self.kills.append(pid)
            if pid not in self.stubborn:
                for port, owner in list(self.listening.items()):
                    if owner == pid:
                        del self.listening[port]
            return Completed(b"")
        if cmd[0] == "netstat":
            return Completed(self.netstat())
        if cmd[0] == "powershell":
            return Completed(self.names_of(cmd[-1]))
        return Completed(b"")

    def names_of(self, script):
        """回答 `_process_names` 的 Get-CimInstance 查询。"""
        rows = [f"{pid} {name}" for pid, name in self.names.items()]
        return ("\r\n".join(rows) + "\r\n").encode("utf-8") if rows else b""

    def netstat(self):
        rows = ["  Proto  Local Address          Foreign Address        State           PID"]
        for port, pid in self.listening.items():
            rows.append(f"  TCP    127.0.0.1:{port}        0.0.0.0:0              LISTENING       {pid}")
        return ("\r\n".join(rows) + "\r\n").encode("utf-8")

    def killed(self, pid):
        return self.kills.count(pid)


class Completed:
    def __init__(self, stdout):
        self.stdout = stdout
        self.returncode = 0


class Stubs:
    """把 OS 边界整体换掉，退出时还原（issue #37：补丁打到真实调用点所在的模块）。

    `pids` 按「哪一类」回答：`guard` / `watchdog` / `stack`。三者共用一个
    `_match_pids` 入口，所以打这一个点就覆盖了全部 finder，不必重写它们——但它现在
    住在 `wg_up_probe`，打 `up._match_pids` 只会改门面那份引用，finders 照旧走真机器。

    `time` 要打两处：`stop_stack` 自己的 sleep 在 `wg_up_stack`，端口等待循环在
    `wg_up_probe`。只打一处等于放一半真等待进来。
    """

    def __init__(self, pids, os_stub=None, instances=0):
        self.pids = pids
        self.os = os_stub or FakeOS()
        self.clock = Clock()
        self.instances = instances

    def match(self, pattern, exclude=None):
        found = list(self.pids.get(self._kind(pattern), []))
        return [pid for pid in found if not exclude or pid not in exclude]

    @staticmethod
    def _kind(pattern):
        # 只有 stack_pids 会把整张指纹表拼成一个正则，而整表里唯一独有的名字是
        # collector。按它分流，比看 pattern 里有没有 "|" 可靠——supervisor 那条
        # 自己就带着一个 "|"。
        if "--" in pattern:
            # transient_pids 是唯一带长选项的那条指纹（--status / --stop）。
            return "transient"
        if "collector" in pattern:
            return "stack"
        if "watchdog" in pattern:
            return "watchdog"
        return "guard"

    def __enter__(self):
        self.saved = (
            subprocess.run, wg_up_probe.time, wg_up_stack.time,
            wg_up_probe._match_pids, wg_up_stack._kill_instances,
        )
        subprocess.run = self.os.run
        wg_up_probe.time = self.clock
        wg_up_stack.time = self.clock
        wg_up_probe._match_pids = self.match
        wg_up_stack._kill_instances = lambda: self.instances
        return self

    def __exit__(self, *exc):
        (subprocess.run, wg_up_probe.time, wg_up_stack.time,
         wg_up_probe._match_pids, wg_up_stack._kill_instances) = self.saved
        return False

    def stop(self):
        with self:
            return up.stop_stack()


def case_orphans_are_killed():
    """树杀之外的残留必须逐个精确 PID 杀掉——这是 issue #11 的要害。"""
    stubs = Stubs({"guard": ["100"], "watchdog": ["101"], "stack": ["200", "201", "202"]})
    result = stubs.stop()
    assert result.guard == ["100", "101"], result.guard
    assert result.orphans == ["200", "201", "202"], result.orphans
    for pid in ("100", "101", "200", "201", "202"):
        assert stubs.os.killed(pid) == 1, f"pid {pid} 被杀了 {stubs.os.killed(pid)} 次"
    assert result.ok, result.ports_left


def case_self_is_never_killed():
    """stop_stack 自己（及其 launcher shim）永远不在杀名单里。"""
    stubs = Stubs({"guard": [], "watchdog": [], "stack": sorted(SELF)})
    result = stubs.stop()
    assert stubs.os.kills == [], f"误杀自己: {stubs.os.kills}"
    assert result.orphans == [], result.orphans


def case_nothing_running_kills_nothing():
    """全空时必须零次 taskkill：不允许任何「按名字兜底」的通配。"""
    stubs = Stubs({"guard": [], "watchdog": [], "stack": []})
    result = stubs.stop()
    assert stubs.os.kills == [], f"空栈却动了刀: {stubs.os.kills}"
    assert result.ok and not result.guard and not result.orphans


def case_port_owner_fallback():
    """命令行已认不出的进程仍占着端口时，按 LISTENING owner 兜底并如实上报。"""
    stubs = Stubs({"guard": [], "watchdog": [], "stack": []},
                  os_stub=FakeOS(listening={47860: "999"}, names={"999": "python.exe"}))
    result = stubs.stop()
    assert stubs.os.killed("999") == 1, f"端口占用者没被清: {stubs.os.kills}"
    assert result.port_owners == ["999"], result.port_owners
    assert result.ok, result.ports_left


def case_foreign_port_holder_is_spared():
    """端口被非栈进程占着时不许强杀——端口撞车不能拿别人的进程抵命。"""
    stubs = Stubs({"guard": [], "watchdog": [], "stack": []},
                  os_stub=FakeOS(listening={47860: "888"}, names={"888": "Code.exe"}))
    result = stubs.stop()
    assert stubs.os.killed("888") == 0, f"误杀外部进程: {stubs.os.kills}"
    assert result.refused == ["888"], result.refused
    assert not result.ok, "端口仍被占着却报告成功"
    assert "非栈进程" in result.summary(), result.summary()


def case_unknown_name_is_spared():
    """读不到进程名时也按「不动」处理：宁可半停也不赌。"""
    stubs = Stubs({"guard": [], "watchdog": [], "stack": []},
                  os_stub=FakeOS(listening={47860: "777"}, names={}))
    result = stubs.stop()
    assert stubs.os.killed("777") == 0, f"名字未知仍动了刀: {stubs.os.kills}"
    assert result.refused == ["777"], result.refused


def case_leftover_ports_are_reported():
    """端口清不掉时 ok 必须为 False 并列出端口——不能把半停记成干净。"""
    stubs = Stubs({"guard": [], "watchdog": [], "stack": []},
                  os_stub=FakeOS(listening={47860: "999"}, stubborn={"999"},
                                 names={"999": "python.exe"}))
    result = stubs.stop()
    assert not result.ok, "端口没释放却报告成功"
    assert any(addr.endswith(":47860") for addr in result.ports_left), result.ports_left
    assert "未释放" in result.summary(), result.summary()


def case_instances_uncounted_is_not_zero():
    """bootstrap 导不进来时必须是「未统计」，不能悄悄当 0 个。"""
    result = Stubs({"guard": [], "watchdog": [], "stack": []}, instances=None).stop()
    assert result.instances is None, result.instances
    assert "未统计" in result.summary(), result.summary()


def case_log_line_written():
    """每次停止都要在 stopper.log 留一行，双击 .pyw 之后才有据可查。"""
    log = HOME / "logs" / "stopper.log"
    if log.exists():
        log.unlink()
    Stubs({"guard": ["100"], "watchdog": [], "stack": ["200"]}, instances=3).stop()
    text = log.read_text(encoding="utf-8")
    assert "守护/看护 pid=100" in text, text
    assert "树外残留 pid=200" in text, text
    assert "注入实例 3 个" in text, text


def case_dead_commands_lock_is_cleared():
    """强杀留下的死锁必须清掉：停止之后 state/ 里不许有指向尸体的锁。"""
    import commands

    commands.LOCK.parent.mkdir(parents=True, exist_ok=True)
    # 一个绝不存在的 pid：Windows 上 OpenProcess 会失败，pid_alive 返回 False。
    dead = 999_999_999
    commands.LOCK.write_text(str(dead), encoding="utf-8")
    result = Stubs({"guard": [], "watchdog": [], "stack": []}).stop()
    assert not commands.LOCK.exists(), "死锁没被清掉，state/ 里还躺着指向尸体的锁"
    assert result.stale_lock == dead, result.stale_lock


def case_live_commands_lock_is_kept():
    """持锁进程还活着时绝不动它——两实例抢锁正是这把锁存在的理由。"""
    import commands

    commands.LOCK.parent.mkdir(parents=True, exist_ok=True)
    commands.LOCK.write_text(str(os.getpid()), encoding="utf-8")
    try:
        result = Stubs({"guard": [], "watchdog": [], "stack": []}).stop()
        assert commands.LOCK.exists(), "活着的持锁者被误删了锁"
        assert result.stale_lock is None, result.stale_lock
    finally:
        commands.LOCK.unlink(missing_ok=True)


def case_stale_lock_is_logged():
    """清掉死锁要落进 stopper.log，否则事后无法确认它是被清的还是没写。"""
    import commands

    log = HOME / "logs" / "stopper.log"
    if log.exists():
        log.unlink()
    commands.LOCK.parent.mkdir(parents=True, exist_ok=True)
    commands.LOCK.write_text("999999999", encoding="utf-8")
    Stubs({"guard": [], "watchdog": [], "stack": []}).stop()
    text = log.read_text(encoding="utf-8")
    assert "清掉死锁 pid=999999999" in text, text


def code_only(source):
    """源码去掉注释与字符串字面量，只留真正会执行的记号。

    注释和文档字符串里出现 `taskkill` / `server_wg.py` 是在解释历史，不是在执行；
    把它们算进来只会逼着后来者别写注释。
    """
    import io
    import tokenize

    skip = {tokenize.COMMENT, tokenize.STRING}
    parts = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in skip:
            continue
        parts.append(token.string)
    return " ".join(parts)


def case_shell_has_no_logic():
    """stop-bot.pyw 只许是壳：出现 taskkill/netstat 就说明规则被复制了第二份。"""
    source = (ROOT / "water-group" / "stop-bot.pyw").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = {node.func.attr for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert "stop_stack" in calls, "壳没有调用 up.stop_stack()"
    assert "run" not in calls, "壳里出现了 subprocess.run —— 停止规则必须只在 up.py 一处"
    code = code_only(source)
    for forbidden in ("taskkill", "netstat"):
        assert forbidden not in code, f"壳里出现 {forbidden}"


def case_stop_ps1_delegates():
    """stop.ps1 必须委托 up.py --stop，否则又会长出第二套停止规则。"""
    text = (ROOT / "stop.ps1").read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith("#"))
    assert "--stop" in code, text
    assert "up.py" in code, text
    assert "server_wg.py" not in code, "stop.ps1 又回去只杀后端了"


def case_status_probe_survives():
    """`--status` 是只读探针，stop 不能连它一起杀。

    2026-10-22 实测：一个 shell 跑 `stop.ps1`，另一个同时在跑 `up.py --status`，
    stop 的树杀把 status 也带走了，输出是空的。一个「看看到底怎么回事」的命令
    被「正在停止」的东西杀掉，比不输出更糟。`--stop` 自身同理。
    """
    stubs = Stubs({"guard": ["100"], "watchdog": [], "stack": ["300"],
                   "transient": ["400", "401"]})
    result = stubs.stop()
    assert stubs.os.killed("400") == 0, f"--status 被误杀: {stubs.os.kills}"
    assert stubs.os.killed("401") == 0, f"并发的 --stop 被误杀: {stubs.os.kills}"
    assert result.guard == ["100"] and result.orphans == ["300"], result


def case_probe_not_reported_as_guard():
    """并发的 `--stop` / `--status` 不能被 `--status` 报成「守护运行中」。

    2026-10-22 实测：`--status` 与 `--stop` 并行时，status 把 stop 那个进程
    （命令行同样是 `daemon/up.py`）当成守护，报出 `运行中 pid=4012`，而栈其实是停的。
    """
    stubs = Stubs({"guard": ["100", "401"], "transient": ["401"]})
    with stubs:
        assert up.supervisor_pids(exclude=set()) == ["100"], up.supervisor_pids(exclude=set())


CASES = [
    ("树外残留被逐个精确杀掉", case_orphans_are_killed),
    ("自己永不被杀", case_self_is_never_killed),
    ("空栈不动刀", case_nothing_running_kills_nothing),
    ("只读 --status 不被误杀", case_status_probe_survives),
    ("探针不被报成守护", case_probe_not_reported_as_guard),
    ("端口占用者兜底", case_port_owner_fallback),
    ("外部进程占端口不许杀", case_foreign_port_holder_is_spared),
    ("进程名未知不许杀", case_unknown_name_is_spared),
    ("端口清不掉必须报失败", case_leftover_ports_are_reported),
    ("注入实例未统计 != 0", case_instances_uncounted_is_not_zero),
    ("停止结果落 stopper.log", case_log_line_written),
    ("强杀留下的死锁被清掉", case_dead_commands_lock_is_cleared),
    ("活着的锁不许动", case_live_commands_lock_is_kept),
    ("清死锁要落 stopper.log", case_stale_lock_is_logged),
    ("stop-bot.pyw 只是壳", case_shell_has_no_logic),
    ("stop.ps1 委托 up.py", case_stop_ps1_delegates),
]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    bad = 0
    for name, case in CASES:
        try:
            case()
        except AssertionError as exc:
            bad += 1
            print(f"✗ {name}: {exc}")
        else:
            print(f"✓ {name}")

    if bad:
        print(f"\nFAIL: {bad} 项未通过")
        return 1
    print("\nPASS: 一键停栈门禁通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
