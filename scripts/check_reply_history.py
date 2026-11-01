"""钉住 `reply.py --history`：从我上次发言到现在，群里都说了些什么（issue #130）。

`--context` 回答的是「最近 N 条」，回答不了「我上次说完之后他们又聊了什么」——
窗口的一头是「上次发言」，这个边界只能从数据里现找，不能由 Agent 猜。

这个脚本用一份临时库固定四件事：

1. 边界选的是**最后一条**本机发言，打印的是它**之后**的全部行（边界行自己也留着，
   读者才知道窗口从哪开始）；窗口里没有本机发言时全量打印并明说；
2. `--sender` 把边界换成别人，且是**按 QQ**（`payload.sender_id`）认，不按显示名；
3. `--since/--until/--before` 收窄窗口，`--until` 与 `--before` 同时给时取更早的；
4. 截断、`dropped_through`、时间口径这些「读者有权知道」的事都在表头里说出来。

    .venv/Scripts/python.exe scripts/check_reply_history.py
"""
import contextlib
import io
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import check_fixture_support as fs  # noqa: E402  (issue #169 PE: path 注入收口)
from check_fixture_support import pin_state_env, scratch_dir  # noqa: E402
fs.bootstrap_paths(scripts=False, root=False)

TMP = scratch_dir(prefix="wg_hist_")
DB = TMP / "fixture.sqlite3"
SID = "session-fixture"

os.environ["TULPA_WG_DB"] = str(DB)
pin_state_env(TMP)  # 空的，up.events() 读不到唤醒文件；两腿都钉（#281）

import wg_context  # noqa: E402

ME = "90001"      # 本机（夹具自封的账号，见 build() 的 patch）
甲 = "10001"
乙 = "10002"

# (rowid, payload)。时间戳是**毫秒**（`payload.timestamp` 就是这个单位）。
# 边界故事：本机在 5 说了话，接着大家又聊了 6/7/8；后面 9 本机再发言，
# 所以「我上次发言」应当是 9，窗口是 10 起的那些。
MESSAGES = [
    (1, {"sender": "甲", "sender_id": 甲, "content": "早上好", "timestamp": 1792500000000}),
    (2, {"sender": "乙", "sender_id": 乙, "content": "早", "timestamp": 1792500001000}),
    (3, {"sender": "甲", "sender_id": 甲, "content": "领带别了别", "timestamp": 1792500002000}),
    (4, {"sender": "甲", "sender_id": 甲, "content": "系统提示", "timestamp": 1792500003000,
         "synthetic": True}),
    (5, {"sender": "我", "sender_id": ME, "content": "我也在", "timestamp": 1792500004000,
         "is_self": True}),
    (6, {"sender": "乙", "sender_id": 乙, "content": "数字不对吧", "timestamp": 1792500005000}),
    (7, {"sender": "甲", "sender_id": 甲, "content": "他算错了", "timestamp": 1792500006000}),
    (8, {"sender": "乙", "sender_id": 乙, "content": "笑死", "timestamp": 1792500007000}),
    (9, {"sender": "我", "sender_id": ME, "content": "行吧", "timestamp": 1792500008000,
         "is_self": True}),
    (10, {"sender": "甲", "sender_id": 甲, "content": "那就这样", "timestamp": 1792500009000}),
    (11, {"sender": "乙", "sender_id": 乙, "content": "明儿见", "timestamp": 1792500010000}),
]


def build():
    db = sqlite3.connect(DB)
    # `dropped_through` 只在 `active_session()` 里读；夹具给一个原值，
    # 断言只看它有没有被**原样**打出来（不做减法，见 wg_context 模块 docstring）。
    db.execute("CREATE TABLE chat_sessions "
               "(id TEXT, conversation_id TEXT, active INTEGER, dropped_through INTEGER)")
    db.execute("CREATE TABLE chat_inbox "
               "(id INTEGER PRIMARY KEY, session_id TEXT, payload TEXT, media TEXT NOT NULL DEFAULT '[]')")
    db.execute("INSERT INTO chat_sessions VALUES (?, ?, 1, ?)", (SID, "10001:group:123", 4096))
    db.execute("INSERT INTO chat_sessions VALUES ('dead-session', '10001:group:123', 0, NULL)")
    for row_id, payload in MESSAGES:
        db.execute("INSERT INTO chat_inbox (id, session_id, payload) VALUES (?, ?, ?)",
                   (row_id, SID, json.dumps(payload, ensure_ascii=False)))
    # 另一个会话的消息不该混进来
    db.execute("INSERT INTO chat_inbox (id, session_id, payload) VALUES (99, 'dead-session', ?)",
               (json.dumps({"sender": "外人", "sender_id": "10003", "content": "别的会话",
                            "timestamp": 1792500009500}, ensure_ascii=False),))
    db.commit()
    db.close()

    # 本机账号：不读真配置（门禁不能依赖实机 `config.json`），直接钉死。
    wg_context._own_qq = lambda: ME


def out(**kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = wg_context.show_history(**kwargs)
    return code, buf.getvalue()


def qids(text):
    """输出里出现过的 `q<n>` 记号，按出现顺序。"""
    seen = []
    for line in text.splitlines():
        head = line.split()[0] if line.split() else ""
        if head.startswith("q") and head[1:].isdigit():
            seen.append(int(head[1:]))
    return seen


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def 窗口从最后一次本机发言之后开始():
    code, text = out(limit=100)
    # 边界是 9，之后是 10、11；边界行自己也打出来（读者要知道从哪开始）。
    return code == 0 and qids(text) == [9, 10, 11]


@case
def 窗口里其他人说的话都在():
    _, text = out(limit=100)
    # 问的是「他们都说了些什么」，所以正文是**所有人**的行，不是本机自己的。
    return "那就这样" in text and "明儿见" in text


@case
def sender_把边界换成别人的_QQ():
    # 乙在 8 和 11 都说过话；取批内**最后一条**（11）作边界，所以窗口是空的。
    # 这一条钉的是「边界按人换」：本机（9）那条不再是边界。
    _, text = out(limit=100, sender=乙)
    return qids(text) == [11] and f"边界：QQ {乙}" in text


@case
def sender_边界之后的行照旧全打出来():
    # 甲在 7 被改名成「我」也不该被认成边界（`is_self` 才认本机）——见下一条用例。
    # 这一条钉的是「边界之后的行整段照打」：甲最后一条是 10，之后是 11。
    _, text = out(limit=100, sender=甲)
    return (qids(text) == [10, 11]
            and f"边界：QQ {甲}" in text
            # 换成别人的边界后，本机自己那几条只是普通行，窗口不为空。
            and "窗口里没有上次发言" not in text)


@case
def 边界按_QQ_认而不是显示名():
    # 本机账号（ME）的显示名也叫「甲」，但 QQ 不是甲：按 `sender=甲` 找边界时，
    # 落在 10 而不是本机那两条 5/9。若按显示名认，边界会跑到 9，窗口就变成 10、11
    # 之外还多出一段——这条用例正是在钉「认 QQ 不认名字」。
    db = sqlite3.connect(DB)
    for row_id in (5, 9):
        row = db.execute("SELECT payload FROM chat_inbox WHERE id=?", (row_id,)).fetchone()
        payload = json.loads(row[0])
        payload["sender"] = "甲"
        db.execute("UPDATE chat_inbox SET payload=? WHERE id=?",
                   (json.dumps(payload, ensure_ascii=False), row_id))
    db.commit()
    db.close()
    _, text = out(limit=100, sender=甲)
    # 按 QQ 认：边界 10，窗口 11。若按显示名认，边界会跑到本机那条 9，窗口变成 10、11。
    return (qids(text) == [10, 11]
            and f"边界：QQ {甲}" in text
            # 边界行是 10 那条，不是本机改名的那条（9）。
            and "q10 " in text
            and "q9 " not in text)


@case
def 窗口里没有上次发言时全量打印并说明():
    # 只看 1..3 这一段：本机没在里面发过言。
    _, text = out(limit=100, until=1792500002000)
    return (qids(text) == [1, 2, 3]
            and "窗口里没有上次发言" in text)


@case
def since_until_收窄窗口():
    _, text = out(limit=100, since=1792500006000, until=1792500010000)
    # 窗口里能看到 7、8、9、10；边界（本机最后一条，9）落在窗口里，之后是 10、11。
    return qids(text) == [9, 10, 11] and "起 2026-10-20" in text


@case
def until_与_before_同时给时取更早的并说明():
    _, text = out(limit=100, until=1792500007000, before=10)
    # until（20:40:07）比 before(10) 更早，所以上界取 until：窗口 1..8，本机边界 5 之后是 6、7、8。
    return (qids(text) == [5, 6, 7, 8]
            and "取更早的 --until" in text
            and "上界 q10" in text)


@case
def 截断与_dropped_through_都在表头说出来():
    code, text = out(limit=2)
    # 边界之后共 3 条（10、11… 加上被砍掉的），limit=2 只打最近 2 条：
    # 要明说砍了几条，且边界行**仍然**在最前面（不跟着被砍）。
    return (code == 0
            and "# 保留上限：chat_sessions.dropped_through=4096" in text
            and "此值及之前的消息已被裁掉" in text
            and "边界之后共 3 条，只打了最近 2 条" in text
            # 边界找得到，就不该报「没有上次发言」，且要说明那条边界行也打出来了。
            and "窗口里没有上次发言" not in text
            and "另含边界行本身 1 条" in text
            and wg_context.wg_clock.now_line() in text)


def context(**kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = wg_context.show_context(**kwargs)
    return code, buf.getvalue()


@case
def context_要多了要说只取到几条():
    # issue #130 的复现命令 2：`--context 2000` 以前表头照念入参，读者看不出
    # 实际只拿到几条。夹具里本会话有 11 条，但第 7 条是 `synthetic`（不进
    # `chat_inbox` 的阅读视图，见 `recent_chatter` 的 `data.get("synthetic")`），
    # 另一会话那条也不算——真正能读到的只有 10 条，要 2000 就该如实说 10。
    code, text = context(limit=2000)
    head = text.splitlines()[0] if text else ""
    return (code == 0
            and head.startswith("# 最近 10 条")
            and "要了 2000 条只取到 10 条" in head
            and f"会话只留最近 {wg_context.CONTEXT_SCAN} 条或更少" in head)


@case
def context_要得不多就不念截断披露():
    # 反例对照：要 5 条、库里够 5 条时**不该**出现这句——否则披露会变成噪音，
    # 读者很快学会忽略它，真截断时也就看不见了。
    code, text = context(limit=5)
    head = text.splitlines()[0] if text else ""
    return (code == 0
            and head.startswith("# 最近 5 条")
            and "只取到" not in head)


@case
def 空窗口说清楚而不是打一屏空白():
    code, text = out(limit=100, since=1792509999000, until=1792509999999)
    return code == 1 and "这个窗口里没有任何消息" in text


# 时间用例的期望值一律**现算**，不写死 epoch 字面量（issue #363）：`parse_time`
# 的语义是「按本地时区落到 epoch」，写死的字面量只在 UTC+8 那台机器上对，CI 跑在
# UTC 上就红——那是用例写错了，不是被测代码错了。下面的 ``at`` 就是这条语义。

NOW_S = 1792500000  # 2026-10-20 20:40:00（epoch 秒，与时区无关）


def at(*args):
    """本地时区的日历时刻 -> epoch **毫秒**（``parse_time`` 的入参/出参口径）。"""
    return int(datetime(*args).timestamp() * 1000)


@case
def parse_time_返回毫秒():
    # 时间戳的单位是**毫秒**（`payload.timestamp` 就是这个单位），别返回秒。
    ms, note = wg_context.parse_time("2026-10-19 20:00:00")
    if ms != at(2026, 10, 19, 20, 0, 0) or "2026-10-19 20:00:00" not in note:
        return False
    # 关系式断言：同一时刻按秒算只有它的千分之一，混用单位会让窗口恒空。
    return abs(ms - at(2026, 10, 19, 20, 0, 0) // 1000) > 1_000_000


@case
def parse_time_不带日期按最近一次解释并说出来():
    ms, note = wg_context.parse_time("03:00", now=NOW_S)
    # 03:00 今天已经过了（now 是 20:40），所以指今天那一次，不是明天。
    return (ms == at(2026, 10, 20, 3, 0, 0)
            and ms < NOW_S * 1000
            and "按最近一次解释" in note)


@case
def parse_time_今天还没到的时间回退到昨天():
    # 21:30 在 2026-10-20 20:40 时还没到，只能指昨天——回落到「明天」会把窗口
    # 推到未来，`--since 21:30` 于是恒空，看起来像「窗口里没人说话」。
    # 断言必须包住异常：退化成「抛 ValueError」时前一条用例照绿，只有这里会红。
    try:
        ms, note = wg_context.parse_time("21:30", now=NOW_S)
    except ValueError:
        return False
    yesterday = at(2026, 10, 19, 21, 30, 0)
    # 期望值现算（本地时区），另带一条与时区无关的关系式：回退的是整整一天。
    return (ms == yesterday
            and ms == at(2026, 10, 20, 21, 30, 0) - 24 * 3600 * 1000
            and ms < NOW_S * 1000
            and "2026-10-19 21:30:00" in note)


@case
def parse_time_的本地时区语义与时区无关():
    # 自检：上面几条的期望值与 `parse_time` 必须是**同一条**时区语义，否则换个
    # TZ 跑就只是两条各自偏移的错值碰巧相等。这条直接钉语义，不靠 TZ 环境变量
    # （Windows 上 TZ 基本不吃，换时区验一遍是个假动作，issue #363）。
    got, _ = wg_context.parse_time("2026-10-19 20:00:00")
    local = datetime(2026, 10, 19, 20, 0, 0)
    # 1) 形态：本地日历时刻，且换算回来仍是同一串数字（不是当 UTC 解释）。
    back = datetime.fromtimestamp(got / 1000)
    if back.replace(microsecond=0) != local:
        return False
    # 2) 与「把同一串数字当 UTC 解释」错开正好一个本地偏移；偏移为 0 时两者相等。
    #    方向：本地比 UTC 早 N 小时，同一串数字当 UTC 解释得到的 epoch 就更大，
    #    所以 `got - wall` 是 **-offset**（UTC+8 下 -8h）。写成 `+offset` 只在 UTC 下成真。
    offset_ms = int(local.astimezone().utcoffset().total_seconds() * 1000)
    wall = local.replace(tzinfo=timezone.utc).timestamp() * 1000
    return got - wall == -offset_ms


@case
def parse_time_认不出就报错():
    try:
        wg_context.parse_time("刚才")
    except ValueError as exc:
        return "认不出" in str(exc)
    return False


def main():
    return fs.run_cases(CASES, setup=build, catch=False)


if __name__ == "__main__":
    sys.exit(main())
