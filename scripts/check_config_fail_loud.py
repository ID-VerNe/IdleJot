"""issue #51 gate: 配置缺失必须 fail-loud——注入拒绝、监听拒绝、状态说出来。

config.json 丢失曾经让整条水群链路**静默**退化（2026-10-24 实测，issue #51）：

* ``self_account`` 读空 → NapCat 注入命令少传账号参数 → 只能弹二维码 → 扫码
  授权被拒 50 次（issue #50 的 ``Login Error ErrCode:3``）；
* ``onebot_token`` 读空 → ``up.py --status`` 的 OneBot 探针被拒 → 误报
  「OneBot 无响应」，值守 Agent 据此差点做出错误处置；
* ``wake_reason`` 的 @ 判定（``str(at.qq) == ""``）永假 → 监听挂着、collector
  在收、事件在落盘、游标在推进，只有「Agent 不回应」一个症状——Agent 侧看到
  的「没有唤醒」与「群里没人说话」完全无法区分。

三者表面都「正常」，所以修复的核心不是补默认值，而是**把缺什么说出来**：
``wg_config.problems()`` 是单一事实来源，注入前拒绝、监听启动拒绝、``--status``
配置行三处消费。本门禁钉住四个契约：

  1. problems() 逐项点名：缺失 / 非法 JSON / self_account 未设置 / 占位符
     （``YOUR_QQ_ACCOUNT`` 之类）/ onebot_token 未设置（env 覆盖不计）；
     健康配置返回空列表；
  2. ``bootstrap.ensure_napcat()`` 账号为空时**拒绝注入**——不起任何进程，
     note 点名 self_account 与恢复路径（config.example.json）；
  3. ``watch.require_wake_identity()`` 在 self_account 为空时返回 1（调用方
     拒绝启动），在位时返回 0——永远唤不醒的监听比没有监听更糟；
  4. ``up.py report()`` 消费 config_problems（AST 钉住），且门面导出与
     ``wg_up_wake`` 同一对象（补丁不打空）。

未打补丁的 base 上本门禁红在：ensure_napcat 在缺账号时照样发起注入、
require_wake_identity 不存在（AttributeError）、report 不读配置健康度。

    .venv/Scripts/python.exe scripts/check_config_fail_loud.py
"""
import ast
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import check_fixture_support as fs  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)
fs.bootstrap_paths(scripts=False)

from water_group import bootstrap  # noqa: E402

import up  # noqa: E402
import watch  # noqa: E402
import wg_config  # noqa: E402
import wg_up_wake  # noqa: E402

FAILURES = []


def expect(label, got, want):
    if got == want:
        print(f"✓ {label}")
    else:
        print(f"✗ {label}：期望 {want!r}，实际 {got!r}")
        FAILURES.append(label)


def write_config(path, **fields):
    path.write_text(json.dumps(fields, ensure_ascii=False), encoding="utf-8")


def case_problems(tmp):
    """problems() 逐项点名；config() 的 {} 兜底保持不变。"""
    missing = tmp / "missing.json"
    real = wg_config.CONFIG
    try:
        wg_config.CONFIG = missing
        expect("problems: 文件缺失被点名（带路径与下游症状）", wg_config.problems(),
               [f"config.json 缺失（{missing} 不存在：self_account 与 onebot_token"
                " 随同缺失——注入只能弹二维码、@ 判不中、--status 误报）"])
        # config() 兜底语义不变：依赖默认值的调用方不被这次修复连带回归。
        expect("config() 缺失仍返回 {}（兜底不变）", wg_config.config(), {})

        broken = tmp / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        wg_config.CONFIG = broken
        expect("problems: 非法 JSON 被点名", wg_config.problems(),
               ["config.json 不是合法 JSON（解析失败）"])

        placeholder = tmp / "placeholder.json"
        write_config(placeholder, self_account="YOUR_QQ_ACCOUNT", onebot_token="t")
        wg_config.CONFIG = placeholder
        expect("problems: 占位符账号被点名", wg_config.problems(),
               ["self_account 不是 QQ 号（'YOUR_QQ_ACCOUNT'，疑似占位符）"])

        no_token = tmp / "no-token.json"
        write_config(no_token, self_account="10001")
        wg_config.CONFIG = no_token
        expect("problems: 缺 token 被点名", wg_config.problems(),
               ["onebot_token 未设置（OneBot 探针会被拒，--status 误报无响应）"])

        healthy = tmp / "healthy.json"
        write_config(healthy, self_account="10001", onebot_token="t")
        wg_config.CONFIG = healthy
        expect("problems: 健康配置零问题", wg_config.problems(), [])
        # env 覆盖在场时不算「token 未设置」——与 watchdog 的 #17 契约一致。
        with patch.dict("os.environ", {"IDLEJOT_ONEBOT_TOKEN": "env-token"}):
            no_token2 = tmp / "no-token2.json"
            write_config(no_token2, self_account="10001")
            wg_config.CONFIG = no_token2
            expect("problems: env token 覆盖不误报", wg_config.problems(), [])
        wg_config.CONFIG = healthy
    finally:
        wg_config.CONFIG = real


def case_ensure_napcat_refuses():
    """账号为空：拒绝注入、零进程、note 说人话。"""
    calls = []
    saved = {name: getattr(bootstrap, name) for name in
             ("is_port_open", "ensure_config", "configured_account",
              "kill_stale_instances", "launch_napcat_direct", "start_process_detached")}
    try:
        bootstrap.is_port_open = lambda port: False
        bootstrap.ensure_config = lambda: None
        bootstrap.configured_account = lambda: ""
        bootstrap.kill_stale_instances = lambda: calls.append("kill") or 0
        bootstrap.launch_napcat_direct = lambda exe, account="": calls.append("inject")
        bootstrap.start_process_detached = lambda cmd, **kw: calls.append("spawn")
        ok, note = bootstrap.ensure_napcat()
    finally:
        for name, value in saved.items():
            setattr(bootstrap, name, value)
    expect("ensure_napcat: 缺账号返回 False", ok, False)
    expect("ensure_napcat: note 点名 self_account 与恢复路径",
           ("self_account" in note and "config.example.json" in note), True)
    expect("ensure_napcat: 零进程调用（不注入不起 bat）", calls, [])


def case_watch_refuses():
    """监听启动校验：缺 self_account 拒绝，在位放行。"""
    real = wg_config.CONFIG
    try:
        with tempfile.TemporaryDirectory(prefix="cfg-watch-") as tmp:
            missing = Path(tmp) / "config.json"
            wg_config.CONFIG = missing
            expect("watch: self_account 读空", up.self_account(), "")
            expect("watch: 缺身份时拒绝启动（exit 1）", watch.require_wake_identity(), 1)
            # @ 事件在缺账号时 wake_reason 静默返回 ""——所以拒绝必须发生在
            # 调用方（watch 启动），这是 issue 候选 6 的「或」分支。
            expect("watch: @ 事件此时永判不中（静默）",
                   up.wake_reason({"is_self": False, "content": "@10001 活了吗",
                                   "segments": [{"type": "at", "qq": "10001"}]}), "")
            expect("watch: 配置行把「缺 self_account」说出来",
                   any("self_account" in p for p in up.config_problems()), True)

            healthy = Path(tmp) / "healthy.json"
            write_config(healthy, self_account="10001", onebot_token="t")
            wg_config.CONFIG = healthy
            expect("watch: 身份在场时放行", watch.require_wake_identity(), 0)
    finally:
        wg_config.CONFIG = real


def case_report_consumes_problems():
    """--status 的配置行不是可选项：report() 必须消费 config_problems。"""
    source = (ROOT / "water-group" / "daemon" / "up.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    report = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "report"), None)
    assert report is not None, "up.report() not found"
    calls = [n for n in ast.walk(report)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "config_problems"]
    expect("up.report() 调用 config_problems（AST）", bool(calls), True)
    expect("门面导出与 wg_up_wake 同一对象（补丁不打空）",
           up.config_problems is wg_up_wake.config_problems, True)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="cfg-fail-loud-") as tmp:
        cases = [
            ("problems 逐项点名", lambda: case_problems(Path(tmp))),
            ("ensure_napcat 缺账号拒绝注入", case_ensure_napcat_refuses),
            ("watch 缺身份拒绝启动", case_watch_refuses),
            ("report 消费配置健康度", case_report_consumes_problems),
        ]
        for label, fn in cases:
            # 逐案捕获：base 上没有 problems()/require_wake_identity() 时，门禁
            # 要红在这一案的行为签名上，而不是整个脚本 AttributeError 裸奔。
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                print(f"✗ {label}：{type(exc).__name__}: {exc}")
                FAILURES.append(label)
    print()
    if FAILURES:
        print(f"FAIL: {len(FAILURES)} 项未通过 —— {', '.join(FAILURES)}")
        return 1
    print("PASS: 配置缺失 fail-loud（problems 点名 / 注入拒绝 / 监听拒绝 / status 可见）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
