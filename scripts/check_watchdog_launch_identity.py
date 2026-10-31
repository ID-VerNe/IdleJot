"""注入实例的身份识别：记录、比对与计数（issue #46）。

故障：watchdog 日志连续出现 `unresponsive pid=..., killed 0 injected instance(s)`，
而上一实例其实还活着——重拉出来的 QQ 主进程命令行只有 `--disable-gpu`，没有
`-q <账号>`，按进程表 + 命令行判定认不出它，于是第二个实例照常注入，两棵实例树
抢同一份 user-data-dir（2026-10-24 实测：51700 / 61256 两棵并存）。

修复的判据不是「更聪明的命令行指纹」，而是「记住这次拉起的是谁」：spawn 启动器时
落一份 `state/napcat_launch.json`，下一轮按 `ParentProcessId` 找回本次注入的 QQ
主进程，并用进程创建时间挡住 pid 回收。门禁据此断言：

  C1 记录写读往返；启动器已退出、QQ 子进程健在 -> 收掉 1 棵（本例核心）
  C2 启动即死（QQ 从未进进程表）-> 收 0，但记录明确报「实例已消失」
  C3 没有记录 -> 回落到 `-q <账号>` 标记，旧行为不回归
  C4 桌面 QQ（无记录、无标记）-> 一个都不动
  C5 pid 被回收（父 pid 对得上但创建时间早得多）-> 不算本次实例
  C6 两个判据同时命中 -> 只计一次，不重复杀
  C7 watchdog 日志：`relaunch` 带本次 launcher pid；未注入时明说

全程离线：WMI 出口（`bootstrap._qq_processes`）、`taskkill`（`winconsole`）与
`sleep` 全部换成替身，`state/` 落在临时 `WATER_GROUP_HOME`，不碰真机。

    .venv/Scripts/python.exe scripts/check_watchdog_launch_identity.py
"""
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_fixture_support import isolated_env, temp_dir

WATCHDOG = ROOT / "water-group" / "daemon" / "watchdog.py"
ACCOUNT = "10001"


class Completed:
    def __init__(self, stdout=b""):
        self.stdout = stdout
        self.returncode = 0


class FakeConsole:
    """记录 taskkill，其余一律空回；不启动任何控制台子进程。"""

    def __init__(self):
        self.kills = []

    def run_hidden(self, cmd, *args, **kwargs):
        if cmd and cmd[0] == "taskkill":
            self.kills.append(cmd[cmd.index("/PID") + 1])
        return Completed()

    def popen_hidden(self, *args, **kwargs):
        raise AssertionError("门禁里不允许真的 spawn 进程")


class World:
    """一张假进程表：QQ 主进程 + 可选还活着的启动器。"""

    def __init__(self, now=None):
        self.now = now or time.time()
        self.qq = []
        self.launcher = None

    def processes(self, launcher_pid=None):
        rows = list(self.qq)
        if self.launcher and launcher_pid and self.launcher["pid"] == launcher_pid:
            rows.append(self.launcher)
        return rows

    def qq_row(self, pid, parent, command_line="QQ.exe --disable-gpu", age=30):
        row = {"pid": pid, "parent": parent, "created": self.now - age,
               "name": "QQ.exe", "command_line": command_line}
        self.qq.append(row)
        return row

    def launcher_row(self, pid, name="NapCatWinBootMain.exe"):
        self.launcher = {"pid": pid, "parent": 1, "created": self.now - 40,
                         "name": name, "command_line": "NapCatWinBootMain.exe"}
        return self.launcher


class Harness:
    """把 bootstrap 的外部边界整体换掉，退出时还原。"""

    def __init__(self, bootstrap, world, console, launch_file):
        self.bootstrap = bootstrap
        self.world = world
        self.console = console
        self.launch_file = launch_file

    def __enter__(self):
        b = self.bootstrap
        # issue #277 R1 拆分后，_qq_processes / winconsole / time / LAUNCH_FILE /
        # LAST_LAUNCH 移到 water_group.bootstrap_napcat，那边的函数读的是
        # bootstrap_napcat 自己的命名空间，所以桩要打在 bootstrap_napcat 上；
        # configured_account 留在 bootstrap（bootstrap_napcat 经 _shared() 反向取），
        # ensure_napcat 也桩在 bootstrap（watchdog.relaunch 调 bootstrap.ensure_napcat）。
        from water_group import bootstrap_napcat
        self.napcat = bootstrap_napcat
        self.saved = {name: getattr(b, name) for name in (
            "configured_account", "ensure_napcat")}
        self.saved_napcat = {name: getattr(bootstrap_napcat, name) for name in (
            "_qq_processes", "winconsole", "time", "LAUNCH_FILE", "LAST_LAUNCH")}
        bootstrap_napcat._qq_processes = self.world.processes
        bootstrap_napcat.winconsole = self.console
        bootstrap_napcat.time = SimpleNamespace(
            sleep=lambda seconds: None,
            time=time.time, strftime=time.strftime,
            mktime=time.mktime, strptime=time.strptime,
        )
        b.configured_account = lambda: ACCOUNT
        bootstrap_napcat.LAUNCH_FILE = self.launch_file
        bootstrap_napcat.LAST_LAUNCH = None
        return self

    def __exit__(self, *exc):
        for name, value in self.saved.items():
            setattr(self.bootstrap, name, value)
        for name, value in self.saved_napcat.items():
            setattr(self.napcat, name, value)
        return False

    def stop(self, watchdog):
        with self:
            return watchdog.kill_injected_instances()


def load_watchdog():
    spec = importlib.util.spec_from_file_location("watchdog_launch_under_test", WATCHDOG)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_record(path, launcher_pid, started_epoch, mode="direct"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "launcher_pid": launcher_pid,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started_epoch)),
        "started_epoch": int(started_epoch),
        "account": ACCOUNT,
        "mode": mode,
    }), encoding="utf-8")


def main():
    failures = []

    def expect(label, got, want):
        if got == want:
            print(f"✓ {label}")
        else:
            print(f"✗ {label}：期望 {want!r}，实际 {got!r}")
            failures.append(label)

    with temp_dir(prefix="watchdog-identity-") as tmp:
        home = tmp / "home"
        launch_file = home / "state" / "napcat_launch.json"
        with patch.dict("os.environ", isolated_env(home), clear=True):
            from water_group import bootstrap

            watchdog = load_watchdog()
            now = time.time()

            # C1：记录写读往返 + 启动器已退出、QQ 子进程健在 -> 收掉 1 棵
            world = World(now)
            world.qq_row(51700, 59488)
            console = FakeConsole()
            write_record(launch_file, 59488, now - 90)
            with Harness(bootstrap, world, console, launch_file):
                record = bootstrap.read_launch_record()
                expect("C1 记录读得回", record and record["launcher_pid"], 59488)
                expect("C1 launcher 已退出", record["launcher_alive"], False)
                expect("C1 按父 pid 认出实例", record["instance_pids"], ["51700"])
                expect("C1 kill 收掉 1 棵", watchdog.kill_injected_instances(), 1)
                expect("C1 taskkill 只打实例 pid", console.kills, ["51700"])
                world.qq.clear()
                expect("C1 再收一次为 0（幂等）", watchdog.kill_injected_instances(), 0)
                expect("C1 没有多打 taskkill", console.kills, ["51700"])

            # C2：启动即死 —— 记录在、实例从未进进程表
            world = World(now)
            console = FakeConsole()
            write_record(launch_file, 61232, now - 120)
            with Harness(bootstrap, world, console, launch_file):
                expect("C2 kill 收 0", watchdog.kill_injected_instances(), 0)
                expect("C2 没有误杀", console.kills, [])
                record = bootstrap.read_launch_record()
                expect("C2 实例列表为空", record["instance_pids"], [])
                expect("C2 启动器也没了", record["launcher_alive"], False)
                trace = watchdog.launch_trace()
                expect("C2 日志说「启动即死」", "gone before settling" in trace, True)
                expect("C2 日志带 launcher pid", "pid=61232" in trace, True)

            # C3：没有记录 -> 回落到 `-q <账号>` 标记
            launch_file.unlink()
            world = World(now)
            world.qq_row(60001, 1, command_line=f"QQ.exe --disable-gpu -q {ACCOUNT}")
            console = FakeConsole()
            with Harness(bootstrap, world, console, launch_file):
                expect("C3 无记录也能按标记认", bootstrap.injected_instance_pids(), ["60001"])
                expect("C3 kill 收 1 棵", watchdog.kill_injected_instances(), 1)
                expect("C3 taskkill 命中", console.kills, ["60001"])

            # C4：桌面 QQ —— 无记录、无标记，一个都不许动
            launch_file.unlink(missing_ok=True)
            world = World(now)
            world.qq_row(70002, 1, command_line="")
            console = FakeConsole()
            with Harness(bootstrap, world, console, launch_file):
                expect("C4 认不出就不算", bootstrap.injected_instance_pids(), [])
                expect("C4 kill 收 0", watchdog.kill_injected_instances(), 0)
                expect("C4 桌面 QQ 没被碰", console.kills, [])

            # C5：pid 被回收 —— 父 pid 对得上，但创建时间远早于本次注入
            world = World(now)
            world.qq_row(80001, 80000, age=10 * 86400)
            console = FakeConsole()
            write_record(launch_file, 80000, now - 60)
            with Harness(bootstrap, world, console, launch_file):
                expect("C5 创建时间不符不算实例", bootstrap.injected_instance_pids(), [])
                expect("C5 kill 收 0", watchdog.kill_injected_instances(), 0)
                expect("C5 没有误杀回收的 pid", console.kills, [])

            # C6：两个判据同时命中 -> 只计一次
            world = World(now)
            world.qq_row(90001, 90000, command_line=f"QQ.exe --disable-gpu -q {ACCOUNT}")
            console = FakeConsole()
            write_record(launch_file, 90000, now - 30)
            with Harness(bootstrap, world, console, launch_file):
                expect("C6 两判据去重", bootstrap.injected_instance_pids(), ["90001"])
                expect("C6 计数 1", bootstrap.count_injected_instances(), 1)
                expect("C6 kill 收 1 棵", watchdog.kill_injected_instances(), 1)

            # C7：watchdog 日志 —— relaunch 记本次 launcher，未注入时明说
            log_file = Path(watchdog.LOG)
            if log_file.exists():
                log_file.unlink()
            with Harness(bootstrap, World(now), FakeConsole(), launch_file):
                bootstrap.ensure_napcat = lambda: (False, "注入超时")
                # LAST_LAUNCH 移到 water_group.bootstrap_napcat（issue #277 R1）：
                # bootstrap_napcat.last_launch 读的是 bootstrap_napcat.LAST_LAUNCH，
                # 桩必须打在那边，bootstrap.LAST_LAUNCH 这条 re-export 只是 import
                # 时刻的快照，改它不会回写。
                from water_group import bootstrap_napcat
                bootstrap_napcat.LAST_LAUNCH = {"launcher_pid": 61232,
                                         "started_at": "2026-10-24 20:33:56"}
                watchdog.relaunch()
                bootstrap_napcat.LAST_LAUNCH = None
                watchdog.relaunch()
            text = log_file.read_text(encoding="utf-8", errors="ignore")
            expect("C7 本次 launcher pid 进日志",
                   "launcher pid=61232 at 2026-10-24 20:33:56" in text, True)
            expect("C7 未注入时明说", "(no injection this round)" in text, True)

            launch_file.unlink(missing_ok=True)

    print()
    if failures:
        print(f"FAIL: {len(failures)} 项未通过 —— {', '.join(failures)}")
        return 1
    print("PASS: 注入实例按启动记录认领（启动器已退 / 启动即死 / pid 回收都不误判），"
          "无记录时回落 -q 标记，桌面 QQ 不动。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
