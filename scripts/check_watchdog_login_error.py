"""issue #50 gate: Login Error 必须进 watchdog 日志，halt 必须说得出病因。

2026-10-24 实测（issue #50）：扫码后 `Login Error,ErrType:  1  ErrCode: 3`
在 napcat.log 里循环了 99 条，而 napcat-watchdog.log 同期零行——两条时间线
分开，「launch -> True 在线」的谎没人看得见（早退路径只看端口，sidecar 在
QQ 登不上世时仍然占着 43000/43001）。本门禁钉四组契约：

  1. ``report_login_errors()`` 把 napcat.log 里新出现的 Login Error 映射进
     watchdog 日志：已映射的码带原因提示，未映射的原文照录并指向 napcat.log
     （编原因比没有原因更糟）；
  2. 同一 (ErrType, ErrCode) 在去重窗口内只记一次——ErrCode 3 约两分钟一条，
     不记重会淹掉日志；
  3. ``unresponsive_note()`` 分型：「无 LISTENING 监听者（实例缺失或已退出）」
     与「端口在但 OneBot 不应答」是两回事；
  4. ``boot_timeout_reason()`` 带最后一次 launch 的真实结果，且 relaunch()
     确实记录它——配置缺失（#51）与登录被拒（#50）在 halt 标记里必须可分辨。

再加两条 AST 钉：main() 每轮真的调用 report_login_errors（函数存在但没人
     调 = 死协议），main() 走 unresponsive_note / boot_timeout_reason。

watchdog 用 importlib 在隔离的 WATER_GROUP_HOME 下加载（LOG / NAPCAT_LOG /
HALT_FILE 都是 import 期求值），不碰实机日志与状态。

    .venv/Scripts/python.exe scripts/check_watchdog_login_error.py
"""
import ast
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_fixture_support import isolated_env, temp_dir  # noqa: E402  (数据根隔离，见 issue #79/#89)

WATCHDOG = ROOT / "water-group" / "daemon" / "watchdog.py"

# 与 napcat.log 实测格式一致（双空格是 NapCat 的原样输出）：
#   10-24 22:33:53 [error] [Core] [Login] Login Error,ErrType:  1  ErrCode: 3
LINE_1_3 = "10-24 22:33:53 [error] [Core] [Login] Login Error,ErrType:  1  ErrCode: 3\n"
LINE_2_20 = "10-24 22:34:01 [error] [Core] [Login] Login Error,ErrType:  2  ErrCode: 20\n"

FAILURES = []


def expect(label, got, want):
    if got == want:
        print(f"✓ {label}")
    else:
        print(f"✗ {label}：期望 {want!r}，实际 {got!r}")
        FAILURES.append(label)


def load_watchdog(home, config):
    """Import watchdog.py the way the supervisor launches it, data root pinned.

    LOG / NAPCAT_LOG / HALT_FILE 都是 import 期求值，所以隔离必须发生在
    exec_module 之前（isolated_env 只清 calling thread 的 environ 不够）。
    """
    spec = importlib.util.spec_from_file_location("watchdog_login_under_test", WATCHDOG)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.CONFIG = config
    return module


def watchdog_log_text(module):
    return module.LOG.read_text(encoding="utf-8", errors="ignore")


def login_error_lines(text):
    return [ln for ln in text.splitlines() if "login error:" in ln]


def case_login_error_mapped(mod, napcat_log):
    """已映射的码：原文 + 原因提示一起进 watchdog 日志。"""
    napcat_log.write_text(LINE_1_3, encoding="utf-8")
    recorded = mod.report_login_errors()
    expect("mapped: 记录了一条", recorded, 1)
    text = watchdog_log_text(mod)
    expect("mapped: 进了 watchdog 日志", len(login_error_lines(text)), 1)
    expect("mapped: napcat 原文照录（含双空格）",
           "Login Error,ErrType:  1  ErrCode: 3" in text, True)
    expect("mapped: 带原因提示（同账号冲突）", "同账号冲突" in text, True)


def case_dedupe(mod, napcat_log):
    """同码在窗口内再出现：静默跳过，不淹日志。"""
    with open(napcat_log, "a", encoding="utf-8") as fh:
        fh.write(LINE_1_3)
    recorded = mod.report_login_errors()
    expect("dedupe: 窗口内同码不重复记录", recorded, 0)
    expect("dedupe: 日志仍只有一条", len(login_error_lines(watchdog_log_text(mod))), 1)


def case_unmapped_code(mod, napcat_log):
    """未映射的码：原文照录 + 指向 napcat.log，不编原因。"""
    with open(napcat_log, "a", encoding="utf-8") as fh:
        fh.write(LINE_2_20)
    recorded = mod.report_login_errors()
    expect("unmapped: 记录了一条", recorded, 1)
    text = watchdog_log_text(mod)
    expect("unmapped: 原文照录", "Login Error,ErrType:  2  ErrCode: 20" in text, True)
    expect("unmapped: 明说未映射并指向 napcat.log",
           ("未映射的错误码" in text and "napcat.log" in text), True)
    expect("unmapped: 没有编造原因提示", "同账号冲突" not in text.split("login error:")[-1], True)


def case_unresponsive_split(mod):
    """pid=None 与 pid 在场是两种文案（issue #50 修复候选 4）。"""
    missing = mod.unresponsive_note(None, 0)
    expect("unresponsive: 无监听者时说实例缺失或已退出",
           ("无 LISTENING 监听者" in missing and "实例缺失或已退出" in missing), True)
    present = mod.unresponsive_note(4242, 1)
    expect("unresponsive: 有 pid 时说端口在但不应答",
           ("pid=4242" in present and "端口在但 OneBot 不应答" in present), True)
    expect("unresponsive: 两种文案不是一句", missing != present, True)


def case_halt_reason(mod):
    """halt reason 带 last launch note；relaunch 真的记录它。"""
    expect("halt reason: 无 note 时保持原样",
           mod.boot_timeout_reason(5), "5 consecutive boot failures, cooling down")
    mod.LAST_LAUNCH_OUTCOME.update(ok=False, note="缺 self_account：config.json 缺失")
    reason = mod.boot_timeout_reason(5)
    expect("halt reason: 带 last launch", "last launch: 缺 self_account" in reason, True)
    expect("halt reason: 原句仍在", "5 consecutive boot failures" in reason, True)

    source = WATCHDOG.read_text(encoding="utf-8")
    tree = ast.parse(source)
    relaunch = next((n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == "relaunch"), None)
    assert relaunch is not None, "watchdog.relaunch() not found"
    touches_last = any(isinstance(n, ast.Name) and n.id == "LAST_LAUNCH_OUTCOME"
                       for n in ast.walk(relaunch))
    expect("AST: relaunch() 记录 LAST_LAUNCH_OUTCOME", touches_last, True)


def case_loop_wiring():
    """main() 必须真的调用这三个函数——存在但没人调 = 死协议。"""
    tree = ast.parse(WATCHDOG.read_text(encoding="utf-8"))
    main = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    assert main is not None, "watchdog.main() not found"
    called = {n.func.id for n in ast.walk(main)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    for name in ("report_login_errors", "unresponsive_note", "boot_timeout_reason"):
        expect(f"AST: main() 调用 {name}", name in called, True)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    with temp_dir(prefix="watchdog-login-") as tmp:
        home = tmp / "home"
        (home / "logs").mkdir(parents=True)
        config = tmp / "config.json"
        with patch.dict("os.environ", isolated_env(home), clear=True):
            config.write_text(json.dumps({"onebot_token": "gate-token"}),
                              encoding="utf-8")
            mod = load_watchdog(home, config)
            napcat_log = home / "logs" / "napcat.log"
            # 门禁自己的日志从零开始：每个用例的条数断言才站得住。
            mod.LOG.write_text("", encoding="utf-8")
            cases = [
                ("Login Error 映射进 watchdog 日志", lambda: case_login_error_mapped(mod, napcat_log)),
                ("同码去重", lambda: case_dedupe(mod, napcat_log)),
                ("未映射码原文照录", lambda: case_unmapped_code(mod, napcat_log)),
                ("unresponsive 文案分型", lambda: case_unresponsive_split(mod)),
                ("halt reason 带 launch note", lambda: case_halt_reason(mod)),
                ("main() 接线（AST）", case_loop_wiring),
            ]
            for label, fn in cases:
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001
                    print(f"✗ {label}：{type(exc).__name__}: {exc}")
                    FAILURES.append(label)
    print()
    if FAILURES:
        print(f"FAIL: {len(FAILURES)} 项未通过 —— {', '.join(FAILURES)}")
        return 1
    print("PASS: Login Error 可见、unresponsive 分型、halt 带病因。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
