"""钉住 `up.py --status` 的 NapCat 行：印真账号，不印字段名（issue #156）。

`wg_up_wake.login_info()` 返回的是 OneBot 11 `get_login_info` 的 `data` 字典
（``{'user_id': ..., 'nickname': ...}``），`report()` 却按元组解包：

    account, nickname = info        # dict 迭代 -> 两个 key

于是状态行永远印「在线 (user_id nickname)」——两段都是字段名，看起来却完全像
正常输出，把真账号盖了整整一个重构期（issue #37 拆 `wg_up_wake` 时引入）。

这条门禁钉六组契约，任一处退回旧行为都红：

1. **解析口径**（`login_account`）：int 账号归一成 str、首尾空白裁掉、`None`/空串
   留 `None`、`{}` 与非 dict 一律 `(None, None)`。布尔是 `int` 的子类，但它只可能是
   坏数据，不当数字放行。
2. **真实现行**：`report()` 打桩后取 NapCat 行，括号内文本**严格等于**
   ``f"{account} {nickname}"``。这里用正则捕获组而不是子串断言——子串挡不住
   「两段顺序颠倒」（`gate-bot 10001` 里两个子串都在），也挡不住「行尾补一句正确
   账号」而头字段仍然错。
3. **反例对照（旧实现）**：把 `up.login_account` 换回 `lambda info: tuple(info)`
   （正是 issue #156 的坏实现），同一夹具上第 2 组用的那条判据必须翻成假。没有这条
   对照，「门禁绿」只说明两处用了同一份实现，不说明它算对了。
4. **反例对照（顺序颠倒）**：换成 `(nickname, account)` 也必须红。子串断言在这条
   上会假绿，捕获组不会——这正是第 2 组不用子串的理由。
5. **只有一个字段**：行内只印那个，不出现 `None`；两个字段都没有（`{}`）时明说
   「应答但未报账号信息」，**不**说「无响应」——答了与没答是两件事。
6. **`None`**：仍说「端口在，OneBot 无响应」。防的是「恒说在线」的实现：那类实现
   会让 5、6 两组同时绿在错误的一侧。

全程临时 `WATER_GROUP_HOME` / `IDLEJOT_WG_STATE`，不碰实机状态：

    .venv/Scripts/python.exe scripts/check_status_napcat_line.py

已知边界：本门禁只覆盖 `report()` 里 NapCat 那一行。`bootstrap.wait_napcat_login()`
与 `wg_up_stack.ensure_napcat()` 对 `login_info()` 只做真值判断（dict 非空即真），
形状变化不影响它们，因此不在这里重复钉。
"""
import atexit
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 数据根在 import 前隔离：up / wg_up_wake 都在模块级求 state_dir()。
HOME = Path(tempfile.mkdtemp(prefix="tulpa-napcat-line-gate-"))
atexit.register(shutil.rmtree, HOME, True)
os.environ["WATER_GROUP_HOME"] = str(HOME)
os.environ["IDLEJOT_WG_STATE"] = str(HOME / "state")

sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

import up  # noqa: E402
import wg_up_wake  # noqa: E402

ACCOUNT = "10001"
NICK = "gate-bot"
IDENTITY = f"{ACCOUNT} {NICK}"

# 总览屏上会碰真机的探针。漏掉一个，实机正在跑的 collector / 监听就会顶掉用例
# 给定的那一格，断言测的就不再是夹具（与 check_listener_rehang 同一份清单口径）。
PROBES = ("listener_specs", "listener_pids", "supervisor_pids", "commands_pids",
          "port_open", "config_problems", "collector_pids", "heartbeat_age",
          "heartbeat_time")

LINE_RE = re.compile(r"^NapCat\s*:\s*(?P<body>.*)$")
ONLINE_RE = re.compile(r"^在线 \((?P<identity>[^()]*)\)$")
SILENT_RE = re.compile(r"端口在，OneBot 无响应")


def stub_probes(napcat_open=True):
    """把总览屏的机器层整个挡住，返回还原函数。"""
    stubs = {
        "listener_specs": lambda *a, **k: [],
        "listener_pids": lambda *a, **k: [],
        "supervisor_pids": lambda *a, **k: [],
        "commands_pids": lambda *a, **k: [],
        "port_open": (lambda *a, **k: True) if napcat_open else (lambda *a, **k: False),
        "config_problems": lambda *a, **k: [],
        "collector_pids": lambda *a, **k: [],
        "heartbeat_age": lambda *a, **k: None,
        "heartbeat_time": lambda *a, **k: None,
    }
    saved = {name: getattr(up, name) for name in PROBES}
    for name, value in stubs.items():
        setattr(up, name, value)

    def restore():
        for name, value in saved.items():
            setattr(up, name, value)

    return restore


def napcat_body(info, account_impl=None, napcat_open=True):
    """跑一次 `report()`，返回 NapCat 行的正文部分（冒号之后）。

    `account_impl` 给了就替掉 `up.login_account`——反例对照靠它把「解析」这一层
    单独换掉，其余一切照旧，于是红/绿只可能来自解析。
    """
    restore_probes = stub_probes(napcat_open=napcat_open)
    saved_info, saved_account = up.login_info, up.login_account
    up.login_info = lambda *a, **k: info
    if account_impl is not None:
        up.login_account = account_impl
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            up.report()
    finally:
        up.login_info, up.login_account = saved_info, saved_account
        restore_probes()
    lines = [ln for ln in buf.getvalue().splitlines() if ln.startswith("NapCat")]
    assert len(lines) == 1, f"总览屏的 NapCat 行不唯一：{lines}"
    match = LINE_RE.match(lines[0])
    assert match, f"NapCat 行不是 `NapCat : <正文>` 形态：{lines[0]!r}"
    return match.group("body")


def online_identity(body):
    """`在线 (…)` 的括号内文本；不是这个形态就 None。"""
    match = ONLINE_RE.match(body)
    return match.group("identity") if match else None


# ---------- 契约 1：解析口径 ----------


def test_account_parsing():
    cases = [
        ({"user_id": 10001, "nickname": NICK}, (ACCOUNT, NICK)),          # int 账号
        ({"user_id": f" {ACCOUNT} ", "nickname": f"  {NICK}  "},
         (ACCOUNT, NICK)),                                                # 首尾空白
        ({"user_id": ACCOUNT, "nickname": ""}, (ACCOUNT, None)),          # 空昵称
        ({"user_id": ACCOUNT, "nickname": "   "}, (ACCOUNT, None)),       # 全空白
        ({"nickname": NICK}, (None, NICK)),                               # 缺账号
        ({"user_id": ACCOUNT}, (ACCOUNT, None)),                          # 缺昵称
        ({}, (None, None)),                                               # 空应答
        (None, (None, None)),                                             # 没答
        (("10001", NICK), (None, None)),                                  # 元组不是契约
        ({"user_id": True, "nickname": NICK}, (None, NICK)),              # 布尔不是账号
        ({"user_id": None, "nickname": None}, (None, None)),
    ]
    for info, expected in cases:
        got = wg_up_wake.login_account(info)
        assert got == expected, f"login_account({info!r}) = {got!r}，应为 {expected!r}"
    print(f"✓ 1. 解析口径：{len(cases)} 组输入（int/空白/缺失/空/非 dict/布尔）")


# ---------- 契约 2：真实现行印真账号 ----------


def test_line_shows_real_account():
    body = napcat_body({"user_id": 10001, "nickname": NICK})
    identity = online_identity(body)
    assert identity == IDENTITY, (
        f"NapCat 行括号内是 {identity!r}，应为 {IDENTITY!r}——整行 {body!r}")
    # 字段名不许出现在行里：旧实现印的正是这两个词。
    assert "user_id" not in body and "nickname" not in body, (
        f"NapCat 行把字段名印出来了：{body!r}")
    print(f"✓ 2. 真实现行：括号内严格等于 {IDENTITY!r}，且无字段名")


# ---------- 契约 3/4：反例对照，确认第 2 组红的是「解析」 ----------


def test_bad_parsers_turn_it_red():
    info = {"user_id": 10001, "nickname": NICK}
    field = wg_up_wake._field_text
    counter_examples = {
        # issue #156 的坏实现：dict 迭代出两个 key
        "元组解包（旧实现）": lambda value: tuple(value),
        # 两段都在、只是顺序颠倒：子串断言在这条上假绿
        "顺序颠倒": lambda value: (field(value.get("nickname")),
                                   field(value.get("user_id"))),
    }
    for label, impl in counter_examples.items():
        body = napcat_body(info, account_impl=impl)
        identity = online_identity(body)
        assert identity != IDENTITY, (
            f"{label} 的坏实现居然印出了正确账号 {identity!r}——"
            f"第 2 组的判据抓不住它，门禁是假绿")
        assert identity, f"{label} 的坏实现没走 `在线 (…)` 分支：{body!r}"
    print(f"✓ 3. 反例对照：{len(counter_examples)} 种坏解析都被同一判据判红")


# ---------- 契约 5：只有一个字段 / 空应答 ----------


def test_partial_and_empty_answer():
    only_account = napcat_body({"user_id": 10001})
    assert online_identity(only_account) == ACCOUNT, (
        f"只报账号时括号内应为 {ACCOUNT!r}：{only_account!r}")
    assert "None" not in only_account, f"缺失字段被印成 None：{only_account!r}"

    only_nick = napcat_body({"nickname": NICK})
    assert online_identity(only_nick) == NICK, (
        f"只报昵称时括号内应为 {NICK!r}：{only_nick!r}")
    assert "None" not in only_nick, f"缺失字段被印成 None：{only_nick!r}"

    empty = napcat_body({})
    assert not SILENT_RE.search(empty), (
        f"空应答被说成「OneBot 无响应」——答了与没答是两件事：{empty!r}")
    assert "在线" in empty, f"空应答仍应算在线（端口在且桥答了）：{empty!r}"
    assert "user_id" not in empty and "nickname" not in empty, (
        f"空应答把字段名印出来了：{empty!r}")
    assert online_identity(empty) is None, (
        f"没有任何账号信息却印出了括号账号：{empty!r}")
    print("✓ 4. 单字段不带 None；空应答说「应答但未报账号」，不说「无响应」")


# ---------- 契约 6：None 仍说无响应 ----------


def test_silent_bridge_still_reported():
    body = napcat_body(None)
    assert SILENT_RE.search(body), (
        f"OneBot 没答时 NapCat 行没说「无响应」（恒说在线的实现会漏这一格）：{body!r}")
    assert online_identity(body) is None, f"没答却印了账号：{body!r}"
    print("✓ 5. OneBot 没答：仍说「端口在，OneBot 无响应」")


# ---------- 契约 7：端口不在时是「离线」 ----------


def test_offline_when_ports_closed():
    body = napcat_body({"user_id": 10001, "nickname": NICK}, napcat_open=False)
    assert body == "离线", f"端口不在时应是「离线」，实际 {body!r}"
    print("✓ 6. 端口不在：行是「离线」")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 72)
    print("        [测试] issue #156 NapCat 状态行：印真账号，不印字段名")
    print("=" * 72)
    checks = (test_account_parsing, test_line_shows_real_account,
              test_bad_parsers_turn_it_red, test_partial_and_empty_answer,
              test_silent_bridge_still_reported, test_offline_when_ports_closed)
    bad = 0
    for fn in checks:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            bad += 1
            print(f"✗ {fn.__name__}: {type(exc).__name__}: {exc}")
    print("=" * 72)
    print("✓ 全部 6 组契约测试通过！" if not bad else f"{bad} 组失败")
    print("=" * 72)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
