"""钉住待回时效：超时的唤醒不再算 [待回]，回复默认拒绝。

2026-10-22 的真实故障：隔了很久第一次启动水群栈，`reply.py --list` 把昨天及更早
从未回复过的 @ 全部翻出来标 [待回]，Agent 据此回复早已过时的话题。根因是待回
判定链路（wake_reason + replied_after）没有任何时间维度，而 wg_events.jsonl
只增不减——旧唤醒永远挂 [待回]。

现在按事件时间实时判定（up.wake_expired，TTL 取 config.json 的
pending_reply.max_age_seconds，默认 7200）：

  - `--list` / `up.py --status`：过期显示 [过期]，不再诱导回复
  - `unanswered()`：过期不进发送前 NOTE，不被陈年旧账刷屏
  - `reply.py <id>`：目标是过期唤醒时默认拒绝，--force 显式放行
  - `watch.py`：冷启动游标归零后，过期唤醒不当新唤醒打印

过期不等于已回：不写 wg_replied.jsonl，每次现场算，改 TTL 立即对历史生效。

    .venv/Scripts/python.exe scripts/check_pending_reply.py
"""
import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import check_fixture_support as fs  # noqa: E402  (issue #169 PE: path 注入收口)
from check_fixture_support import pin_conversation, pin_state_env, scratch_dir  # noqa: E402
fs.bootstrap_paths(scripts=False, root=False)

# 夹具事件与会话都钉在这个群上：不钉的话，本机 config.json 恰好只有一个启用群时
# `wg_isolation.resolve()` 落到 `config:唯一启用群`（`wg_isolation.py:84`），夹具的
# 行全被判成外群——读侧 `rows_of()` 滤空、写侧 `target_group()` 抛 `CrossGroup`，
# 随机器上的私有配置从全绿翻成 5 红（issue #334，#316/#322 是同一个坑）。
FIXTURE_CONV = "10001:group:123"

# state 目录必须在 import reply 之前钉走：`wg_send_failure.LEDGER` 是 import 期
# 求值的（`state_dir() / "wg_send_failure.json"`），而 issue #145 之后
# `reply.list_wakes()` 会顺手复查那份账本——不钉的话，本机恰好有一条过期禁言记录时
# 这个门禁会去问**实机** OneBot 并把结论写回实机账本（测试写生产状态）。
# state 两腿都钉（#281）：IDLEJOT 先于 TULPA，单钉 IDLEJOT 时外部 TULPA 不穿透，
# 但两腿都钉才与全仓其余门禁同口径，priority 规则进代码不进注释。
_PINNED_STATE = scratch_dir(prefix="wg_pending_state_")
pin_state_env(_PINNED_STATE)
# 会话身份同位置钉住（issue #334）：`reply.list_wakes()` / `unanswered()` 经
# `wg_isolation.rows_of()` 只认本会话群，没钉死时读侧不过滤、钉错群时全滤空——
# 两种都让这个门禁测的不是被测行为，而是「这台机器恰好有几个启用群」。
pin_conversation(FIXTURE_CONV, _PINNED_STATE)

import up  # noqa: E402  (门面；补丁见下，issue #37 起要打到 wg_up_wake)
import wg_up_wake  # noqa: E402  (issue #37 PC: 唤醒判定的真实调用点)

import reply  # noqa: E402

NOW = time.time()
FRESH = (NOW - 5 * 60) * 1000        # 5 分钟前：任何合理 TTL 内都算新鲜
OLD = (NOW - 3 * 3600) * 1000        # 3 小时前：默认 TTL 7200 下已过期
TTL = 7200

# 哪些补丁必须同时打到门面：消费方（watch.py / reply.py）是经 `up.<name>` 调用的，
# 只改归属模块的话它们读到的还是导入时绑定的旧对象。
CONSUMED_THROUGH_FACADE = {"events", "replied_ids", "replied_after"}

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def rows():
    return [
        {"id": 9001, "timestamp": FRESH, "is_self": False,
         "sender": "甲", "content": "@小鲸鱼 在吗", "conversation_id": FIXTURE_CONV},
        {"id": 9002, "timestamp": OLD, "is_self": False,
         "sender": "乙", "content": "@小鲸鱼 旧话题", "conversation_id": FIXTURE_CONV},
        {"id": 9003, "timestamp": 0, "is_self": False,
         "sender": "丙", "content": "@小鲸鱼 没有时间戳", "conversation_id": FIXTURE_CONV},
    ]


def patch_up(event_rows, ledger=()):
    """固定唤醒判定的输入：事件流、账本、唤醒词、已回判定。

    issue #37：这些函数搬到了 `wg_up_wake`。补丁要装两处才算完整——`watch.py` /
    `reply.py` 经 `up.<name>` 读，`wg_up_wake` 内部互相调用读自己的 globals；
    只打一处，另一半仍走真实现（门禁照绿，测的却是真账本/真配置）。
    """
    stubs = (
        ("events", lambda: list(event_rows)),
        ("replied_ids", lambda: set(ledger)),
        ("replied_after", lambda *a, **k: False),
        ("self_account", lambda: "10001"),
        ("wake_words", lambda: ["小鲸鱼"]),
    )
    for name, value in stubs:
        setattr(wg_up_wake, name, value)
        if name in CONSUMED_THROUGH_FACADE:  # 消费方走 `up.<name>`，门面也要换
            setattr(up, name, value)


_real_config = wg_up_wake.config


def restore_config():
    wg_up_wake.config = _real_config


# ---------- wake_expired：时效判定的边界 ----------


@case
def 新鲜唤醒不过期():
    return up.wake_expired({"timestamp": FRESH}) is False


@case
def 超龄唤醒过期():
    return up.wake_expired({"timestamp": OLD}) is True


@case
def 卡在边界上不算过期():
    # 严格大于才算过期：恰好等于 TTL 的那一刻还新鲜，再挪一微秒就过期。
    # 用小整数时间戳配显式 TTL，不用真实 time.time() 反推边界——纪元秒量级下
    # (x*1000)/1000 的浮点来回有约 1e-7 秒误差，实测约 1.2% 概率把「恰好
    # 等于」舍成「大于」，CI 上随机红过一次；这里全部取值精确可表示。
    ttl = 1.0
    wg_up_wake.config = lambda: {"pending_reply": {"max_age_seconds": ttl}}
    try:
        edge = 1_000_000                       # ms，精确
        now = edge / 1000 + ttl                # 1001.0，精确
        return (up.wake_expired({"timestamp": edge}, now=now) is False
                and up.wake_expired({"timestamp": edge}, now=now + 0.001) is True)
    finally:
        restore_config()


@case
def 没有时间戳不判过期():
    # 数据异常宁可多显示一条，也不悄悄藏成「没问题」
    return (up.wake_expired({"timestamp": 0}) is False
            and up.wake_expired({}) is False)


@case
def 过期判定读配置():
    wg_up_wake.config = lambda: {"pending_reply": {"max_age_seconds": 60}}
    two_min_old = {"timestamp": (time.time() - 120) * 1000}
    try:
        got = up.wake_expired(two_min_old)
    finally:
        restore_config()
    return got is True


@case
def TTL_配置非法回落默认():
    results = []
    for raw in (None, "abc", 0, -5, ""):
        wg_up_wake.config = lambda raw=raw: {"pending_reply": {"max_age_seconds": raw}}
        results.append(up.pending_reply_ttl())
    wg_up_wake.config = lambda: {}  # 整段缺失
    results.append(up.pending_reply_ttl())
    restore_config()
    return results == [up.DEFAULT_PENDING_REPLY_TTL] * 6


_real_config = wg_up_wake.config


# ---------- wake_state / --list：三态标记 ----------


@case
def 三态标记齐全():
    patch_up(rows())
    by_id = {e["id"]: e for e in rows()}
    fresh = up.wake_state(by_id[9001], rows(), set())
    old = up.wake_state(by_id[9002], rows(), set())
    return fresh == "待回" and old == "过期"


@case
def 已回仍然优先():
    patch_up(rows())
    wg_up_wake.replied_after = lambda *a, **k: True
    by_id = {e["id"]: e for e in rows()}
    got = up.wake_state(by_id[9002], rows(), set())  # 过期但回过
    wg_up_wake.replied_after = lambda *a, **k: False
    return got == "已回"


@case
def list_输出区分待回和过期():
    patch_up(rows())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        reply.list_wakes()
    lines = {ln.split()[0]: ln for ln in buf.getvalue().splitlines() if ln.startswith("id=")}
    return ("[待回]" in lines.get("id=9001", "")
            and "[过期]" in lines.get("id=9002", "")
            and "[过期]" not in lines.get("id=9003", ""))


@case
def unanswered_滤掉过期唤醒():
    patch_up(rows())
    got = reply.unanswered([9001])
    ids = [e["id"] for e in got]
    # 9001 被这条回复覆盖；9002 过期不该再进 NOTE；9003 无时间戳不过期，仍在
    return ids == [9003]


# ---------- reply.py 主路径：拒绝 / 放行 / 不误伤 ----------


def run_reply(args, state_dir):
    env = dict(os.environ)
    pin_state_env(state_dir, env=env)  # state 两腿都钉给子进程（#281）
    # 会话身份也要钉给子进程（issue #334）：子进程里 `wg_isolation.resolve()` 同样会落到
    # `config:唯一启用群`，于是 `target_group()`（`wg_isolation.py:186`）把夹具的 9001/9002
    # 判成跨群、把 9999 判成「不能确认属于本群」，三条用例全红且原因与待回时效无关。
    pin_conversation(FIXTURE_CONV, state_dir, env=env)
    # reply.py 走 ensure_utf8_stdio，写出的是 UTF-8；解码钉在 run_python 里
    # （issue #240：裸 text=True 按宿主 locale=cp936 解，stdout 变成 None）。
    return fs.run_python([str(ROOT / "water-group" / "daemon" / "reply.py"), *args],
                         cwd=ROOT, env=env, timeout=90)


def make_state():
    tmp = scratch_dir(prefix="wg_pending_")
    state = tmp
    (state / "wg_session.txt").write_text("session-fixture", encoding="utf-8")
    lines = [
        {"id": 9001, "timestamp": FRESH, "is_self": False, "sender": "甲",
         "content": "新的 @", "conversation_id": FIXTURE_CONV,
         "onebot_message_id": "555001"},
        {"id": 9002, "timestamp": OLD, "is_self": False, "sender": "乙",
         "content": "旧的 @", "conversation_id": FIXTURE_CONV,
         "onebot_message_id": "555002"},
    ]
    (state / "wg_events.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in lines) + "\n", encoding="utf-8")
    return state


def write_fixture_events():
    """把夹具的三条事件写到磁盘事件流上（issue #334）。

    `run_reply` 的流程里，`reply.event_target()` 走的是 `wg_event_ref._ref_from_events()`
    ——它读**磁盘**的 `wg_events.jsonl`（`reply.read_events()`），不是 `patch_up` 换掉的
    `up.events` 桩。改前这个门禁没发现，因为会话未钉死时 `wg_isolation.target_group()`
    直接放行 `found=None`，`event_target` 一次都没被真正问过（假绿）。钉死之后它才被问到，
    于是「事件流里查不到这条」= 拒绝，三条用例翻红。夹具两边必须是同一份。

    返回写出的路径。调用方不需要它，返回值只是为了让「写到了钉死的 state 里、
    不写实机 `~/.water-group`」这件事在需要时断言得出来。
    """
    path = _PINNED_STATE / "wg_events.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows()) + "\n",
                    encoding="utf-8")
    return path


@case
def 回复过期唤醒被拒绝():
    # --no-quote 让流程停在计划通道（临时 STATE 没有批次），任何情况下都不会真发
    proc = run_reply(["9002", "测试", "--no-quote"], make_state())
    return proc.returncode == 1 and "拒绝" in proc.stdout and "--force" in proc.stdout


@case
def force_显式放行过期唤醒():
    proc = run_reply(["9002", "测试", "--no-quote", "--force"], make_state())
    return "拒绝" not in proc.stdout and "没有可用批次" in proc.stdout


@case
def 新鲜唤醒不受影响():
    proc = run_reply(["9001", "测试", "--no-quote"], make_state())
    return "拒绝" not in proc.stdout and "没有可用批次" in proc.stdout


@case
def 不在事件流里的id一律拒():
    """「不在事件流里」不等于「可以放行」：钉死群下它归属不明，必须拒。

    这条以前断言 ``"拒绝" not in proc.stdout``，靠的是会话**没钉死**——
    ``target_group``（``wg_isolation.py:178-200``）在未钉死时原样返回 ``found``，
    钉死之后同一条路径会抛 ``CrossGroup``。钉死是把 #164 的护栏真正打开，不是回退，
    所以断言跟着改成护栏的语义：**拒绝、且拒绝理由说的是归属不明（不是过期）**。
    「过期判定不拿没有的行开刀」由其余用例的 id 都在事件流里来保证。
    """
    write_fixture_events()
    proc = run_reply(["9999", "测试", "--no-quote"], make_state())
    return (proc.returncode == 1
            and "拒绝" in proc.stdout
            and "不能确认这条属于本群" in proc.stdout
            and "过期" not in proc.stdout)


# ---------- 会话钉死的真对照（issue #334） ----------


@contextlib.contextmanager
def pinned_to(conversation):
    """临时把本会话钉到 ``conversation``：env 与 <state> 两腿一起改，退出原样还原。"""
    import wg_isolation

    saved = {key: os.environ.get(key) for key in wg_isolation.CONVERSATION_ENV}
    file = _PINNED_STATE / wg_isolation.CONVERSATION_FILE
    existed = file.read_text(encoding="utf-8") if file.exists() else None
    try:
        pin_conversation(conversation, _PINNED_STATE)
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if existed is None:
            file.unlink(missing_ok=True)
        else:
            file.write_text(existed, encoding="utf-8")


@case
def 会话钉死不靠本机配置():
    """issue #334：本会话群必须来自 pin（``env:`` / ``file:``），不能是 ``config:``。

    这条是「真对照」的前半句：断言 ``--list`` 抬头里的群来源。改前在本机实机
    config（恰好一个启用群）下这里是 ``config:唯一启用群``，夹具行全被滤空——
    「输出里恰好没有 WARN」证明不了任何事，来源才证明得了。

    第 2 个断言直接问 ``rows_of()``：夹具三条行一条不少地活过读侧过滤。
    改前在这个位置是「未钉死 → 不过滤 → 也是 3」，所以它单独证明不了什么；
    和上一句的 ``config:`` 缺失、以及下一条钉到别群翻空合起来才是完整的对照。
    """
    import wg_isolation

    patch_up(rows())
    try:
        with pinned_to(FIXTURE_CONV):
            write_fixture_events()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                reply.list_wakes()
            got = wg_isolation.rows_of(reply.read_events())
        text = buf.getvalue()
    finally:
        restore_config()
    pinned_source = any(f"group:{FIXTURE_CONV.rsplit(':', 1)[-1]}（{prefix}" in text
                        for prefix in ("env:", "file:"))
    return pinned_source and "config:" not in text and len(got) == len(rows())


@case
def 钉到别群时唤醒要消失():
    """反向自检：钉到别的群时夹具那条唤醒必须一条都看不到。

    否则等于把 #164 的读侧过滤关掉，前一条用例就永远是绿的——issue #334 的
    整个教训就是「绿」必须能被打翻。
    """
    patch_up(rows())
    try:
        with pinned_to("10001:group:999"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                reply.list_wakes()
            quiet = buf.getvalue()
            got = reply.unanswered([9001])
    finally:
        restore_config()
    return "id=9001" not in quiet and got == []


def main():
    return fs.run_cases(CASES)


if __name__ == "__main__":
    sys.exit(main())
