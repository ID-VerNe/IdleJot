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
  C6  端口在且登录态在          -> 早退「在线」（不误拒）
  C7  端口在但登录态死          -> 不说「在线」，不起任何进程（issue #50）
  C8  注入后端口起、登录没确认  -> 如实 False，note 指向扫码/napcat.log（issue #50）

C7/C8 钉的是 issue #50 的核心：端口在从来不是登录态在——node sidecar 在 QQ
主进程登不上世时仍然占着 43000/43001，只看端口的早退会返回「在线」，而
napcat.log 里 Login Error 正在循环（实测 99 条期间 watchdog 日志零行）。

Everything the function touches is stubbed: this never starts a process, never
opens a port, and never reads the real config file or NAPCAT_DIR. `time` is
replaced by a virtual clock (NoSleep) -- the issue #50 early exit asks login
state three times and the post-injection wait polls to a 30s timeout, and a gate
that really waits on the wall clock is a gate nobody runs.
"""
import contextlib
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from water_group import bootstrap
from check_fixture_support import temp_dir  # noqa: E402

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


class NoSleep:
    """bootstrap 里 ``time`` 的替身：虚拟钟 + 空转 sleep（issue #50 门禁）。

    早退路径三问登录态（3×5s）、注入后的 wait_napcat_login 轮询到 30s 超时，
    都是墙上时间——只把 sleep 空转的话，死循环会按真钟忙等 30 秒。虚拟钟让
    ``sleep(n)`` 直接把时钟推前 n，两种等待都瞬间完成，且超时逻辑照旧生效。
    """

    def __init__(self):
        self._now = time.time()

    def sleep(self, seconds):
        self._now += seconds

    def time(self):
        return self._now


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
        # issue #50：登录态探针与等待。默认「已登录」，C7/C8 覆盖成登不上；
        # wait_napcat_login 保持真实现（配 NoSleep 虚拟钟，轮询到超时也是瞬间），
        # 这样钉的是真函数而不是又一层桩。
        # 形状按真实现给（issue #156）：`login_info()` 返回 OneBot 的 `data` 字典，
        # 元组只是修复前那段时间门禁自己造出来的假契约。消费方只判真值，形状换了
        # 用例不变，但门禁从此不会再为一份不存在的返回类型背书。
        login_info=lambda: {"user_id": "10001", "nickname": "gate-bot"},
        time=NoSleep(),
    )
    names.update(overrides)
    # base 上没有 issue #50 新增的 login_info 等属性：用哨兵区分「原来是 None」
    # 和「本来不存在」——还原时把桩掉的属性删掉，别把名字留在模块上。少了这一步，
    # 基线验证会红在 AttributeError 裸奔上，而不是红在行为签名上。
    missing = object()
    saved = {name: getattr(bootstrap, name, missing) for name in names}
    try:
        for name, value in names.items():
            setattr(bootstrap, name, value)
        yield
    finally:
        for name, value in saved.items():
            if value is missing:
                if hasattr(bootstrap, name):
                    delattr(bootstrap, name)
            else:
                setattr(bootstrap, name, value)


def run_case(qq_exe, boot_ok=True, bat_exists=True, launcher=None,
             ports_open=False, logged_in=True):
    """Run ensure_napcat once with everything stubbed; return (ok, note, calls).

    ``ports_open`` 让早退路径成立（43000/43001 都在）；``logged_in`` 决定登录态
    探针答不答——issue #50 的两个新用例（C7/C8）一个只要端口、一个只要登录死。
    """
    calls = []
    with temp_dir(prefix="napcat-order-") as napcat_dir:
        if bat_exists:
            (napcat_dir / "launcher-user.bat").write_text("@echo off\r\n", encoding="ascii")

        previous = os.environ.pop("TULPA_NAPCAT_LAUNCHER", None)
        if launcher is not None:
            os.environ["TULPA_NAPCAT_LAUNCHER"] = launcher
        try:
            with stubbed(
                NAPCAT_DIR=napcat_dir,
                resolve_qq_exe=lambda: qq_exe,
                is_port_open=(lambda port: True) if ports_open else (lambda port: False),
                wait_napcat_ports=lambda seconds=12: boot_ok,
                login_info=(lambda: None) if not logged_in else (
                    lambda: {"user_id": "10001", "nickname": "gate-bot"}),
                launch_napcat_direct=lambda exe, account="": calls.append(("inject", exe)),
                start_process_detached=lambda cmd, **kw: calls.append(("bat", list(cmd))) or FakeProc(),
            ):
                ok, note = bootstrap.ensure_napcat()
        finally:
            if previous is not None:
                os.environ["TULPA_NAPCAT_LAUNCHER"] = previous
            else:
                os.environ.pop("TULPA_NAPCAT_LAUNCHER", None)
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

    # C6：端口在且登录态也在——早退路径不许误拒（issue #50 的另一半）
    ok, note, calls = run_case("C:/Program Files/QQ/QQ.exe", ports_open=True)
    expect("C6 端口在且登录态在时早退在线", (ok, note), (True, "在线"))
    expect("C6 在线不起任何进程", calls, [])

    # C7：端口在但登录态死——不许说「在线」，也不许注入（issue #50）
    # 这正是 watchdog 日志里 `launch -> True 在线` 的谎：sidecar 占着端口，
    # QQ 主进程在 Login Error 循环里，只看端口的早退一路放行。
    ok, note, calls = run_case("C:/Program Files/QQ/QQ.exe", ports_open=True,
                               logged_in=False)
    expect("C7 端口在登录死不说在线", (ok, "在线" != note), (False, True))
    expect("C7 失败文案点名登录态未确认", "登录态未确认" in note, True)
    expect("C7 失败文案指向 napcat.log 的 Login Error",
           ("napcat.log" in note and "Login Error" in note), True)
    expect("C7 没注入没起 bat", calls, [])

    # C8：注入后端口起来了、登录态没确认——如实 False 且说清下一步
    # （issue #50）。注入确实发生了，所以 calls 里应有 inject。
    ok, note, calls = run_case("C:/Program Files/QQ/QQ.exe", logged_in=False)
    expect("C8 端口起登录没确认时如实 False", ok, False)
    expect("C8 文案点名登录态未确认", "登录态未确认" in note, True)
    expect("C8 文案指向扫码/冲突与 napcat.log",
           ("napcat.log" in note and "NAPCAT_TROUBLESHOOTING" in note), True)
    expect("C8 注入确实发生过", [c[0] for c in calls], ["inject"])

    print()
    if failures:
        print(f"FAIL: {len(failures)} 项未通过 —— {', '.join(failures)}")
        return 1
    print("PASS: NapCat 启动顺序门禁通过（直接注入优先，注入失败不降级）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
