"""钉住 `up.py --status` 的 NapCat 行：印真账号，不印字段名（issue #156）。

`wg_up_wake.login_info()` 返回的是 OneBot 11 `get_login_info` 的 `data` 字典
（``{'user_id': ..., 'nickname': ...}``），`report()` 却按元组解包：

    account, nickname = info        # dict 迭代 -> 两个 key

于是状态行永远印「在线 (user_id nickname)」——两段都是字段名，看起来却完全像
正常输出，把真账号盖了整整一个重构期（issue #37 拆 `wg_up_wake` 时引入）。

这条门禁钉七组契约，任一处退回旧行为都红：

1. **生产者形状**（issue #159）：只桩最底层的 `wg_up_wake._onebot_call`，让真
   `login_info()` 走完整条路，断言它返回 dict、打的 action 是 `get_login_info`、
   「没答」时返回 `None` 而不是 `{}`，且 `up.py` 用的正是这一个函数。以前
   `napcat_body()` 第一件事就是换掉 `up.login_info`，生产者整个被替身顶掉——
   「返回什么类型」这个维度零断言，退化成元组时六组全绿。对照组：元组形状必须
   被同一判据判红，且渲染路径确实降级成「未报账号」，否则第 3 组的绿只是碰巧。
   `None` 与 `{}` 在 `report()` 里分属「无响应」与「应答但未报账号」两个分支，
   形状判据对两者都放行，所以那条单独钉——中间层把 `None` 吞成 `{}` 就是把
   「没答」说成「答了」，而第 6 组桩的是 `up.login_info`，盖不到这一层。
2. **解析口径**（`login_account`）：int 账号归一成 str、首尾空白裁掉、`None`/空串
   留 `None`、`{}` 与非 dict 一律 `(None, None)`。布尔是 `int` 的子类，但它只可能是
   坏数据，不当数字放行。
3. **整条真链印真账号**：不桩生产者，只桩 `_onebot_call`，整条真链跑 `report()`
   取 NapCat 行，括号内文本**严格等于** ``f"{account} {nickname}"``。这里用正则
   捕获组而不是子串断言——子串挡不住「两段顺序颠倒」（`gate-bot 10001` 里两个
   子串都在），也挡不住「行尾补一句正确账号」而头字段仍然错。
4. **反例对照（解析层）**：把 `up.login_account` 换回 `lambda info: tuple(info)`
   （正是 issue #156 的坏实现）与顺序颠倒版，同一夹具上第 3 组用的那条判据必须
   翻成假。没有这条对照，「门禁绿」只说明两处用了同一份实现，不说明它算对了。
5. **只有一个字段**：行内只印那个，不出现 `None`；两个字段都没有（`{}`）时明说
   「应答但未报账号信息」，**不**说「无响应」——答了与没答是两件事。
6. **`None`**：仍说「端口在，OneBot 无响应」。防的是「恒说在线」的实现：那类实现
   会让 5、6 两组同时绿在错误的一侧。
7. **端口不在**：行是「离线」，不印账号。防的是「只要有账号就说在线」的实现。

全程临时 `WATER_GROUP_HOME` / `IDLEJOT_WG_STATE`，不碰实机状态：

    .venv/Scripts/python.exe scripts/check_status_napcat_line.py

已知边界：本门禁只覆盖 `report()` 里 NapCat 那一行。`bootstrap.wait_napcat_login()`
与 `wg_up_stack.ensure_napcat()` 对 `login_info()` 只做真值判断（dict 非空即真），
形状变化不影响它们，因此不在这里重复钉。生产者形状只钉「dict / None 二选一」，
不钉 dict 里有哪些键——第 2 组的夹具覆盖了缺字段与空字典。
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


def _napcat_line_body(text):
    """从 `report()` 的整段输出里取 NapCat 行的正文部分（冒号之后）。"""
    lines = [ln for ln in text.splitlines() if ln.startswith("NapCat")]
    assert len(lines) == 1, f"总览屏的 NapCat 行不唯一：{lines}"
    match = LINE_RE.match(lines[0])
    assert match, f"NapCat 行不是 `NapCat : <正文>` 形态：{lines[0]!r}"
    return match.group("body")


def report_body(napcat_open=True):
    """跑一次 `report()`（只桩机器层探针，**不碰生产者**），返回 NapCat 行正文。

    生产者留给调用方决定：真链（第 1、3 组）只桩更底层的
    `wg_up_wake._onebot_call`；要构造坏形状时由 `napcat_body()` 换掉
    `up.login_info`。分成两层是为了让「谁被桩掉了」在调用点看得见。
    """
    restore_probes = stub_probes(napcat_open=napcat_open)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            up.report()
    finally:
        restore_probes()
    return _napcat_line_body(buf.getvalue())


def napcat_body(info, account_impl=None, napcat_open=True):
    """桩掉生产者 `up.login_info`，用给定的 info 跑一次 `report()`。

    `account_impl` 给了就替掉 `up.login_account`——反例对照靠它把「解析」这一层
    单独换掉，其余一切照旧，于是红/绿只可能来自解析。
    """
    saved_info, saved_account = up.login_info, up.login_account
    up.login_info = lambda *a, **k: info
    if account_impl is not None:
        up.login_account = account_impl
    try:
        return report_body(napcat_open=napcat_open)
    finally:
        up.login_info, up.login_account = saved_info, saved_account


def online_identity(body):
    """`在线 (…)` 的括号内文本；不是这个形态就 None。"""
    match = ONLINE_RE.match(body)
    return match.group("identity") if match else None


def producer_shape_problem(info):
    """`login_info()` 的返回形状是否仍是消费者认的契约；合规返回 None。

    返回问题描述而不是布尔，是为了让失败信息自带「为什么」。判据只认两态：
    OneBot 的 `data` 字典（答了）与 `None`（没答）。元组是 issue #156 之前的
    历史形状，此后不再是契约——`login_account()` 对非 dict 返回 `(None, None)`，
    于是元组会静默降级成「应答但未报账号信息」。
    """
    if info is None or isinstance(info, dict):
        return None
    return (f"login_info() 返回 {type(info).__name__}（{info!r}），应为 dict 或 None"
            f"——login_account() 只认 dict，其它形状会静默降级成「应答但未报账号信息」")


# ---------- 契约 1：生产者形状（不桩生产者） ----------


def test_producer_shape():
    assert up.login_info is wg_up_wake.login_info, (
        "up.py 用的不是 wg_up_wake.login_info——账号探测长出第二份实现，"
        "形状契约只盖住一半")
    saved = wg_up_wake._onebot_call
    seen = []

    def fake(action, params):
        seen.append((action, params))
        return {"user_id": 10001, "nickname": NICK}

    wg_up_wake._onebot_call = fake
    try:
        info = wg_up_wake.login_info()
    finally:
        wg_up_wake._onebot_call = saved

    problem = producer_shape_problem(info)
    assert problem is None, problem
    assert seen == [("get_login_info", {})], (
        f"login_info() 打的不是 get_login_info：{seen!r}——形状对了、问的却是别的"
        f"接口，只钉返回值的门禁会假绿")
    # 「没答」必须是 None，不能是 {}：两者在 `report()` 里分属两个分支（「无响应」/
    # 「应答但未报账号」），而形状判据对两者都放行。契约 6 桩的是 `up.login_info`，
    # 盖不到「中间那层把 None 吞成 {}」——那正是把「没答」说成「答了」的假话。
    wg_up_wake._onebot_call = lambda action, params: None
    try:
        silent = wg_up_wake.login_info()
    finally:
        wg_up_wake._onebot_call = saved
    assert silent is None, (
        f"OneBot 没答时 login_info() 返回 {silent!r}，应为 None——它会被渲染成"
        f"「应答但未报账号信息」，把「没答」说成「答了」")
    # 对照组：元组（退化生产者）必须被同一判据判红，且渲染路径确实降级成
    # 「未报账号」——否则第 3 组的绿只是碰巧，判据根本没在判形状。
    degenerate = (ACCOUNT, NICK)
    problem = producer_shape_problem(degenerate)
    assert problem, "元组形状被判成合规——判据没在判形状，整组是假绿"
    body = napcat_body(degenerate)
    assert online_identity(body) != IDENTITY, (
        f"喂给 up.login_info 的元组印出了正确账号（{body!r}）——要么 report() 没读"
        f" up.login_info（账号探测长出第二份实现），要么 login_account() 又肯收"
        f"元组了（第 3 组的端到端断言抓不住形状漂移）")
    assert "未报账号" in body, (
        f"元组生产者没有降级成「未报账号」，症状与 issue #159 不符：{body!r}")
    print("✓ 1. 生产者形状：真 login_info() 返回 dict 且打 get_login_info，元组被判红")


# ---------- 契约 2：解析口径 ----------


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
    print(f"✓ 2. 解析口径：{len(cases)} 组输入（int/空白/缺失/空/非 dict/布尔）")


# ---------- 契约 3：整条真链印真账号 ----------


def test_line_shows_real_account():
    saved = wg_up_wake._onebot_call
    wg_up_wake._onebot_call = lambda action, params: {"user_id": 10001, "nickname": NICK}
    try:
        body = report_body()
    finally:
        wg_up_wake._onebot_call = saved
    identity = online_identity(body)
    assert identity == IDENTITY, (
        f"NapCat 行括号内是 {identity!r}，应为 {IDENTITY!r}——整行 {body!r}")
    # 字段名不许出现在行里：旧实现印的正是这两个词。
    assert "user_id" not in body and "nickname" not in body, (
        f"NapCat 行把字段名印出来了：{body!r}")
    print(f"✓ 3. 真实现行（只桩 OneBot）：括号内严格等于 {IDENTITY!r}，且无字段名")


# ---------- 契约 4：反例对照，确认第 3 组红的是「解析」 ----------


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
            f"第 3 组的判据抓不住它，门禁是假绿")
        assert identity, f"{label} 的坏实现没走 `在线 (…)` 分支：{body!r}"
    print(f"✓ 4. 反例对照：{len(counter_examples)} 种坏解析都被同一判据判红")


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
    print("✓ 5. 单字段不带 None；空应答说「应答但未报账号」，不说「无响应」")


# ---------- 契约 6：None 仍说无响应 ----------


def test_silent_bridge_still_reported():
    body = napcat_body(None)
    assert SILENT_RE.search(body), (
        f"OneBot 没答时 NapCat 行没说「无响应」（恒说在线的实现会漏这一格）：{body!r}")
    assert online_identity(body) is None, f"没答却印了账号：{body!r}"
    print("✓ 6. OneBot 没答：仍说「端口在，OneBot 无响应」")


# ---------- 契约 7：端口不在时是「离线」 ----------


def test_offline_when_ports_closed():
    body = napcat_body({"user_id": 10001, "nickname": NICK}, napcat_open=False)
    assert body == "离线", f"端口不在时应是「离线」，实际 {body!r}"
    print("✓ 7. 端口不在：行是「离线」")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 72)
    print("        [测试] issue #156/#159 NapCat 状态行：印真账号，不印字段名")
    print("=" * 72)
    checks = (test_producer_shape, test_account_parsing,
              test_line_shows_real_account, test_bad_parsers_turn_it_red,
              test_partial_and_empty_answer, test_silent_bridge_still_reported,
              test_offline_when_ports_closed)
    bad = 0
    for fn in checks:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            bad += 1
            print(f"✗ {fn.__name__}: {type(exc).__name__}: {exc}")
    print("=" * 72)
    print("✓ 全部 7 组契约测试通过！" if not bad else f"{bad} 组失败")
    print("=" * 72)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
