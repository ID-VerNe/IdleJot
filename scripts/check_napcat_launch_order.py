"""Pin the launch order inside bootstrap.ensure_napcat: direct injection first.

issue #58 / PR-2. The .bat launcher resolves QQ.exe through `reg query`, which is
denied on this box, so it always failed -- but only *after* spawning a cmd.exe, and
that cmd is the black box that flashed on the desktop (#39, 53 hits of
"QQ installation was not found" in napcat.log). The fix is an ordering change: it
never shows up in a diff as anything but moved lines, and it is trivial to undo by
accident, so it gets a gate instead of a comment.

Cases:

  C1  QQ.exe found              -> inject directly, never spawns cmd
  C2  direct injection fails    -> returns False, still never spawns cmd
  C3  QQ.exe missing            -> falls back to the .bat, note says 降级
  C4  TULPA_NAPCAT_LAUNCHER=bat -> .bat wins even when QQ.exe is found
  C5  QQ.exe missing, no .bat   -> honest error naming launcher-user.bat

Everything the function touches is stubbed: this never starts a process, never
opens a port, and never reads the real config file or NAPCAT_DIR.
"""
import contextlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from water_group import bootstrap

failures = []


def expect(label, got, want):
    if got == want:
        print(f"✓ {label}")
    else:
        print(f"✗ {label}：期望 {want!r}，实际 {got!r}")
        failures.append(label)


class FakeProc:
    """Stands in for the launcher Popen; already exited."""

    def poll(self):
        return 0


@contextlib.contextmanager
def stubbed(**overrides):
    names = dict(
        is_port_open=lambda port: False,
        # issue #51：ensure_napcat 现在会先 ensure_config()（缺失时从模板重建
        # 配置）。门禁在干净 checkout 上跑，不打桩就会把 config.example.json
        # 拷进仓库工作区——那是真实副作用，必须桩掉。
        ensure_config=lambda: None,
        wait_napcat_ports=lambda seconds=12: True,
        kill_stale_instances=lambda: 0,
        configured_account=lambda: "10001",
        qq_extra_args=lambda: [],
        resolve_qq_exe=lambda: None,
        launch_napcat_direct=lambda exe, account="": None,
        start_process_detached=lambda cmd, **kw: FakeProc(),
    )
    names.update(overrides)
    saved = {name: getattr(bootstrap, name) for name in names}
    try:
        for name, value in names.items():
            setattr(bootstrap, name, value)
        yield
    finally:
        for name, value in saved.items():
            setattr(bootstrap, name, value)


def run_case(qq_exe, boot_ok=True, bat_exists=True, launcher=None):
    """Run ensure_napcat once with everything stubbed; return (ok, note, calls)."""
    calls = []
    tmp = tempfile.TemporaryDirectory(prefix="napcat-order-")
    napcat_dir = Path(tmp.name)
    if bat_exists:
        (napcat_dir / "launcher-user.bat").write_text("@echo off\r\n", encoding="ascii")

    previous = os.environ.pop("TULPA_NAPCAT_LAUNCHER", None)
    if launcher is not None:
        os.environ["TULPA_NAPCAT_LAUNCHER"] = launcher
    try:
        with stubbed(
            NAPCAT_DIR=napcat_dir,
            resolve_qq_exe=lambda: qq_exe,
            wait_napcat_ports=lambda seconds=12: boot_ok,
            launch_napcat_direct=lambda exe, account="": calls.append(("inject", exe)),
            start_process_detached=lambda cmd, **kw: calls.append(("bat", list(cmd))) or FakeProc(),
        ):
            ok, note = bootstrap.ensure_napcat()
    finally:
        if previous is not None:
            os.environ["TULPA_NAPCAT_LAUNCHER"] = previous
        else:
            os.environ.pop("TULPA_NAPCAT_LAUNCHER", None)
        tmp.cleanup()
    return ok, note, calls


def bat_calls(calls):
    return [call for call in calls if call[0] == "bat"]


def main():
    # C1：QQ.exe 找得到就只注入，一个 cmd 都不起
    ok, note, calls = run_case("C:/Program Files/QQ/QQ.exe")
    expect("C1 直接注入成功", (ok, "直接注入" in note), (True, True))
    expect("C1 没有起 cmd", bat_calls(calls), [])
    expect("C1 注入了一次", [c[1] for c in calls if c[0] == "inject"],
           ["C:/Program Files/QQ/QQ.exe"])

    # C2：注入超时不许退回 .bat —— 两个启动器抢同一份 user-data-dir 比报错更糟
    ok, note, calls = run_case("C:/Program Files/QQ/QQ.exe", boot_ok=False)
    expect("C2 注入超时如实返回 False", ok, False)
    expect("C2 超时文案指向 napcat.log", "napcat.log" in note, True)
    expect("C2 失败后没有降级到 .bat", bat_calls(calls), [])

    # C3：连 QQ.exe 都找不到才用 .bat（reg query 是它唯一剩下的价值）
    ok, note, calls = run_case(None)
    expect("C3 降级后启动成功", (ok, "bat 降级" in note), (True, True))
    expect("C3 确实起了 .bat", [c[1][:3] for c in bat_calls(calls)],
           [["cmd.exe", "/c", "launcher-user.bat"]])
    expect("C3 注入了一条路径都没走", [c for c in calls if c[0] == "inject"], [])

    # C4：逃生阀，线上不用改代码就能回旧顺序
    ok, note, calls = run_case("C:/Program Files/QQ/QQ.exe", launcher="bat")
    expect("C4 TULPA_NAPCAT_LAUNCHER=bat 生效", [c[1][:3] for c in bat_calls(calls)],
           [["cmd.exe", "/c", "launcher-user.bat"]])
    expect("C4 此时不注入", [c for c in calls if c[0] == "inject"], [])
    expect("C4 成功", ok, True)

    # C5：两条路都不可用时，报错要点名 .bat，别只说"找不到 QQ.exe"
    ok, note, calls = run_case(None, bat_exists=False)
    expect("C5 无 QQ 也无 .bat 时失败", ok, False)
    expect("C5 错误里提到 launcher-user.bat", "launcher-user.bat" in note, True)
    expect("C5 什么都没起", calls, [])

    print()
    if failures:
        print(f"FAIL: {len(failures)} 项未通过 —— {', '.join(failures)}")
        return 1
    print("PASS: NapCat 启动顺序门禁通过（直接注入优先，注入失败不降级）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
