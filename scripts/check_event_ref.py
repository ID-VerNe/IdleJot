"""钉住「事件 id 只有一个权威解析入口」（issue #216）。

issue #216 的现场：`reply.py --context` / `--show` 读得出一条闲聊的全文，
`reply.py <id>` 却回 `None` 并抛 `CrossGroup`「查不到所属群」——同一 id 两个入口
两种结论。根因是四份各自实现的扫描：`reply.event_target` 先扫事件流、再让追问
插件解析 payload 里的 `onebot_message_id`；`followup._group_of` 只认那两个键；
`wg_text.show_event` 只认行；`look.event_by_id` 只扫 `wg_events`。payload 少一个键
就静默 `None`，而群号与消息编号在库的**列**上本来就有。

本门禁钉三件事：

1. 解析：字段名兼容（`onebot_message_id` / `message_id`）、列兜底
   （`chat_sessions.conversation_id` / `chat_inbox.message_id`）、来源顺序
   （`wg_events` 优先，撞号时以事件流为准）；
2. 说法：`explain()` 与 `target_group()` 把「不在任何来源」与「在某来源里但取不到
   群号」分开说，不再一律「查不到所属群」；
3. 闭环：`--context` 列出的一行（只在 `chat_inbox`）必须能被 `reply.py <id>` 解析
   到本群、`target_group()` 不得抛 `CrossGroup`，且该行自带 `src=chat_inbox`。

issue #219 加第四件：**批量读点同一口径**。`followup.since` / `wg_context.recent_chatter`
/ `wg_gap._local_state` 在 #216 之后仍各读一次 payload 的 `conversation_id`——写完字一
缺这个键，追问行与 `--context` 行就退回跨群主档挂别名指向，缺口账本还把那些行判成
「不是本群」。三处现在都走 ``wg_event_ref.row_conversation``（payload 优先、会话行
兜底）；门禁钉住三条读点的结果、payload 优先的反例、非对象 payload 不拖垮整批，
另有一条自检证明「只认 payload」的写法在这个判据下必须红。

基线验证（未打补丁的 base 上必须红，且红在原症状）：
`--context` 列出 id 11 → `reply.event_target(11)` 为 `None` →
`target_group` 抛 `CrossGroup('id=11 查不到所属群…')`；
批量读点：`followup.since(0, SID)[12]["conversation_id"] == ""`（会话行上有群号）。

    .venv/Scripts/python.exe scripts/check_event_ref.py
"""
import contextlib
import io
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import check_fixture_support as fs  # noqa: E402  (issue #169 PE: path 注入收口)
from check_fixture_support import scratch_dir  # noqa: E402
fs.bootstrap_paths(scripts=False, root=False)

TMP = scratch_dir(prefix="wg_event_ref_")
DB = TMP / "fixture.sqlite3"
STATE = TMP / "state"
STATE.mkdir(parents=True, exist_ok=True)
os.environ["TULPA_WG_DB"] = str(DB)
os.environ["TULPA_WG_STATE"] = str(STATE)
# 钉死群：本会话只管 123，跨群/查不到群号的拒绝路径才走得到（issue #164）。
os.environ["IDLEJOT_WG_CONVERSATION"] = "10001:group:123"

import look  # noqa: E402
import reply  # noqa: E402
import wg_event_ref  # noqa: E402
import wg_isolation  # noqa: E402

GROUP = "123"
SID = "session-fixture"
EVENTS = STATE / "wg_events.jsonl"

# (id, session_id, message_id 列, payload)
INBOX = [
    # 7 与事件流同号：来源顺序必须以 wg_events 为准（撞号时事件流是投递过的那条）。
    (7, SID, "7007", {"conversation_id": f"10001:group:222", "onebot_message_id": "7007",
                      "sender": "甲", "content": "事件流那条", "timestamp": 1792500000000}),
    # 10 只在 chat_inbox，编号用的是图片索引那一路的键名 `message_id`。
    (10, SID, "", {"conversation_id": f"10001:group:{GROUP}", "message_id": "1010",
                   "sender": "乙", "content": "只带 message_id", "timestamp": 1792500001000}),
    # 11 是 issue 的复现形态：payload 没有 conversation_id，群号在会话行上，
    # 编号哪儿都没有 —— 能回，只是挂不上引用。
    (11, SID, "", {"sender": "丙", "content": "群里在聊作业", "timestamp": 1792500002000}),
    # 12 的编号只在 `chat_inbox.message_id` 列上（payload 一个键都没有）。
    (12, SID, "1212", {"sender": "丁", "content": "编号在列上", "timestamp": 1792500003000}),
    # 13 有群号、没有编号：群号解析得出来，编号为空串。
    (13, SID, "", {"conversation_id": f"10001:group:{GROUP}", "sender": "戊",
                   "content": "只有群号", "timestamp": 1792500004000}),
    # 14 挂在没有 conversation_id 的会话上：行在，但群号取不到。
    (14, "orphan-session", "", {"sender": "己", "content": "谁的群？",
                                "timestamp": 1792500005000}),
]


def build():
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE chat_sessions (id TEXT, conversation_id TEXT, active INTEGER)")
    db.execute("CREATE TABLE chat_inbox (id INTEGER PRIMARY KEY, session_id TEXT,"
               " message_id TEXT, payload TEXT, at REAL,"
               " media TEXT NOT NULL DEFAULT '[]')")
    db.execute("INSERT INTO chat_sessions VALUES (?, ?, 1)", (SID, f"10001:group:{GROUP}"))
    # 这条会话没结束、但没有 conversation_id：行在、群号取不到（14 用）。active=0
    # 是为了让 `--context` 的「当前会话」仍然只有一个，不然最近 N 条会挑到它。
    db.execute("INSERT INTO chat_sessions VALUES ('orphan-session', '', 0)")
    for event_id, session_id, message_id, payload in INBOX:
        db.execute("INSERT INTO chat_inbox (id, session_id, message_id, payload, at)"
                   " VALUES (?, ?, ?, ?, ?)",
                   (event_id, session_id, message_id,
                    json.dumps(payload, ensure_ascii=False), 1792500000.0 + event_id))
    db.commit()
    db.close()
    # 事件流只放 7：同号时它必须赢（来源顺序）。
    EVENTS.write_text(json.dumps({
        "id": 7, "conversation_id": f"10001:group:{GROUP}",
        "onebot_message_id": "7007", "sender": "甲",
        "content": "事件流那条", "timestamp": 1792500000000,
    }, ensure_ascii=False) + "\n", encoding="utf-8")


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def 同号时事件流优先():
    ref = wg_event_ref.resolve_id(7)
    return (ref is not None and ref.source == wg_event_ref.SRC_EVENTS
            and ref.group_id == GROUP and ref.message_id == "7007"
            and ref.payload.get("content") == "事件流那条")


@case
def 只带_message_id_的_payload_也解析得出():
    # issue 复现命令的形态：`followup._group_of` 以前对这种 payload 回 None。
    import followup
    ref = wg_event_ref.resolve_id(10)
    return (ref is not None and ref.source == wg_event_ref.SRC_INBOX
            and ref.target == (GROUP, "1010")
            and followup._group_of({"conversation_id": "SELF:group:111",
                                    "message_id": "8888"}) == ("111", "8888"))


@case
def payload_没群号时回查会话行():
    ref = wg_event_ref.resolve_id(11)
    return (ref is not None and ref.group_id == GROUP and ref.message_id == ""
            and ref.target == (GROUP, ""))


@case
def payload_没编号时回查消息列():
    ref = wg_event_ref.resolve_id(12)
    return ref is not None and ref.target == (GROUP, "1212")


@case
def 群号有编号空也算解析成功():
    ref = wg_event_ref.resolve_id(13)
    return (ref is not None and ref.resolvable
            and reply.event_target(13) == (GROUP, ""))


@case
def 行在但群号取不到时_resolvable_为假():
    ref = wg_event_ref.resolve_id(14)
    return (ref is not None and not ref.resolvable and ref.target is None
            and reply.event_target(14) is None)


@case
def 两种查不到分开说():
    missing = wg_event_ref.explain(9999)
    orphan = wg_event_ref.explain(14)
    return ("不在 wg_events 也不在 chat_inbox" in missing
            and "取不到群号" in orphan
            and missing != orphan)


@case
def 自检_explain_退化成同一句必须红():
    """判据随实现翻转：把 explain 钉成常量，本门禁的「分开说」必须不成立。"""
    real = wg_event_ref.explain
    wg_event_ref.explain = lambda event_id: "查不到所属群"
    try:
        return not ("不在 wg_events 也不在 chat_inbox" in wg_event_ref.explain(9999)
                    and "取不到群号" in wg_event_ref.explain(14))
    finally:
        wg_event_ref.explain = real


@case
def 拒绝信息不再一律说查不到所属群():
    try:
        wg_isolation.target_group(14, reply.event_target(14))
    except wg_isolation.CrossGroup as exc:
        orphan = str(exc)
    else:
        return False
    try:
        wg_isolation.target_group(9999, reply.event_target(9999))
    except wg_isolation.CrossGroup as exc:
        missing = str(exc)
    else:
        return False
    return ("取不到群号" in orphan and "不在 wg_events 也不在 chat_inbox" in missing
            and "查不到所属群" not in orphan and "查不到所属群" not in missing)


@case
def 闭环_context_里的闲聊回得了():
    """issue 的验收项：`--context` 列出的行，`reply.py <id>` 解析得到本群。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = reply.show_context(20)
    text = buf.getvalue()
    if code != 0 or "群里在聊作业" not in text:
        return False
    found = reply.event_target(11)
    if not found or str(found[0]) != GROUP:
        return False
    # 钉死群下不得抛 CrossGroup：这条本来就属于本群，只是 payload 少字段。
    return wg_isolation.target_group(11, found) == found


@case
def context_行自带来源标记():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        reply.show_context(20)
    # 行首编号是 `q11`（issue #203 的库记号），来源标记紧跟在它后面（#216）。
    line = next((ln for ln in buf.getvalue().splitlines() if ln.startswith("q11 ")), "")
    return bool(line) and f"src={wg_event_ref.SRC_INBOX}" in line


@case
def 补读先确认这条在不在():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = look.read_event(9999, 0, "one")
    return code == 1 and "本地没有 id=9999 的记录" in buf.getvalue()


# ---- issue #219：批量读点的群号 ----
#
# 12 的 payload 一个键都没有（群号只在 `chat_sessions.conversation_id` 上），
# 正是 issue 的现场形态。三个批量读点以前各读一次 payload，缺键就静默读成空：
# 追问行与 `--context` 行按跨群主档挂别名指向，缺口账本则把这条判成「不是本群」。
# 13 是反例：payload 自己有群号时必须以 payload 为准，两条路不许颠倒。

@case
def 批量读点缺键时回查会话行():
    """`followup.since` / `wg_context.recent_chatter` / `wg_gap` 三处同一口径。"""
    import followup
    import wg_context
    import wg_gap

    since = {row["id"]: row["conversation_id"] for row in followup.since(0, SID)}
    context = {row["id"]: row["conversation_id"]
               for row in wg_context.recent_chatter(20)}
    want = f"10001:group:{GROUP}"
    # 缺口账本：只认 payload 群号时，11/12 这两条缺键行被整条跳过，窗口左边界
    # 停在 10 上；兜底之后 13（payload 自带群号）才是本群最新一条。
    _, last_at = wg_gap._local_state(GROUP)
    # 会话行没有群号的那条（14）仍旧读空，不许被兜底成任何人的群。
    orphan = wg_event_ref.resolve(14)
    return (since.get(12) == want and context.get(12) == want
            and last_at == int(1792500000.0 + 13)
            and orphan is not None and orphan.group_id == "")


@case
def 批量读点以_payload_的群号为准():
    """13 的 payload 自己写着本群——兜底不许覆盖它。"""
    import followup
    import wg_context

    want = f"10001:group:{GROUP}"
    since = {row["id"]: row["conversation_id"] for row in followup.since(0, SID)}
    context = {row["id"]: row["conversation_id"]
               for row in wg_context.recent_chatter(20)}
    return since.get(13) == want and context.get(13) == want


@case
def 非对象_payload_不拖垮整批():
    """payload 是 JSON 数组/字符串时不是合法事件：跳过这一条，其余照读。"""
    import followup
    import wg_context

    db = sqlite3.connect(DB)
    db.execute("INSERT INTO chat_inbox (id, session_id, message_id, payload, at)"
               " VALUES (?, ?, ?, ?, ?)", (15, SID, "", "[1, 2]", 1792500000.0 + 15))
    db.commit()
    db.close()
    try:
        since = [row["id"] for row in followup.since(0, SID)]
        context = [row["id"] for row in wg_context.recent_chatter(20)]
    finally:
        db = sqlite3.connect(DB)
        db.execute("DELETE FROM chat_inbox WHERE id=15")
        db.commit()
        db.close()
    return 12 in since and 12 in context and 15 not in since and 15 not in context


@case
def 自检_只读_payload_的写法必须红():
    """判据随实现翻转：退回改前「只认 payload」，本门禁必须红。

    模拟的是改前那个读点：payload 没这个键就读空（会话行那一列根本没人看）。
    """
    real = wg_event_ref.row_conversation

    def only_payload(row):
        return wg_event_ref.payload_conversation((row or {}).get("payload"))

    wg_event_ref.row_conversation = only_payload
    try:
        # 只替换 followup 那一路：since() 走 row_conversation，缺口账本与
        # --context 走的是 payload_conversation + 会话行（它们的对照在别的用例里）。
        import followup
        since = {row["id"]: row["conversation_id"] for row in followup.since(0, SID)}
        return since.get(12) != f"10001:group:{GROUP}"
    finally:
        wg_event_ref.row_conversation = real


@case
def group_of_仓里唯一定义在_wg_event_ref():
    """issue #255：`group_of` 三份副本（wg_aliases / wg_event_ref / wg_isolation）
    收口成一份。静态断言钉死：仓里**只**剩 `wg_event_ref` 一处 `def group_of`。

    改前计数：`rg -n 'def group_of' water-group/daemon` 命中三行（aliases/event_ref/isolation）。
    改后必须只命中 `wg_event_ref` 一行——任何第二份副本回归就在这里红。
    """
    import ast

    daemon = ROOT / "water-group" / "daemon"
    defs = []
    for path in daemon.glob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "group_of":
                defs.append(path.name)
    return (defs == ["wg_event_ref.py"]) or f"group_of 定义在 {defs}，期望只在 wg_event_ref.py"


@case
def group_of_外部调用点只走_wg_event_ref():
    """issue #255：`wg_aliases.group_of` / `wg_isolation.group_of` 的外部调用点
    全部改指 `wg_event_ref.group_of`。

    `session.py:41` 用 `wg_isolation.group_of(conversation)` 反解会话号取群号——
    与事件流读点同一口径，同样改指 `wg_event_ref.group_of`。静态断言：daemon 树里
    不许再出现 `wg_aliases.group_of(` 或 `wg_isolation.group_of(` 的调用。
    """
    import ast

    daemon = ROOT / "water-group" / "daemon"
    bad = []
    for path in daemon.glob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr == "group_of"
                    and isinstance(node.value, ast.Name)
                    and node.value.id in ("wg_aliases", "wg_isolation")):
                bad.append(f"{path.name}: wg_{node.value.id.removeprefix('wg_')}.group_of")
    return (bad == []) or f"仍有外部副本调用：{bad}"


def main():
    return fs.run_cases(CASES, setup=build)


if __name__ == "__main__":
    sys.exit(main())
