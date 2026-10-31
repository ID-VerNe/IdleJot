"""引用消息的可见性：唤醒行和上下文都得看得出被引用的是什么（issue #94）。

服务端解析好的原文一直在 `referenced_messages` 里，但 Agent 真正读消息的两处都没用它：
`watch.py` 只印 `content`，而 `content` 里的引用只是 `[引用 QQ 消息 <id>]` 占位
（`chatlocal/mcp_chat.py` 的入库段）；`reply.py --context` 走 `chat_inbox.payload`，
那条路连这个字段都没有。于是 Agent 拿到的是一串它查不到的编号，只能自己反查，
而反查路径（字段名、消息滚出 1000 条缓存）都不稳。

归属：渲染只在这里。解析交给调用方——谁手里有数据谁查：
`render_event` 读 wg_events 行自带的 `referenced_messages`；
`render_payload` 只有编号，按 message_id 在同一会话的库里反查。
两条路最后都走 `_line`，输出同形；「没有原文」必须由状态说出来，不能靠不印。
"""
import json
import sqlite3

QUOTE_CHARS = 60   # 唤醒行是给人扫的，原文留够判断「在说什么」就行
SENDER_CHARS = 24
MAX_QUOTES = 3     # 一条消息最多挂 3 个引用，全印出来会把唤醒行撑成一屏

REASONS = {
    "unavailable": "原文不可得",
    "unverified": "原文核验失败",
    "outside_scope": "原文超出会话范围",
    "budget_exhausted": "取原文超时",
    "cancelled": "取原文已取消",
    "not_local": "原文不在本地缓存",
    "unresolved": "原文未解析",
    "detail_limit": "引用超过单条上限，未展开",
}

# 合并转发的后缀（issue #9）。唤醒行只印 `content` 时这里是 `[合并转发]` 五个字，
# 看不出几条、说了什么；段在就必须给出一行可判断的信息，拿不到就明说状态。
FORWARD_CHARS = 60
MAX_FORWARDS = 2
FORWARD_REASONS = {
    "unavailable": "正文不可得",
    "budget_exhausted": "取正文超时",
    "cancelled": "取正文已取消",
    "detail_limit": "超过单条上限，未展开",
    "unresolved": "正文未解析",
    "not_local": "正文不在本地缓存",
}


def _fit(value, limit):
    """显示用截断：折叠空白再按字数裁。截断只影响这一行，不回流到数据。

    截断本身走 `wg_text.render_fitted`（issue #32）：引用原文、转发摘要、
    发送者名这三种短显示曾在这里手写 `text[:limit] + "…"`，是 daemon 侧
    第五份截断口径。
    """
    import wg_text

    return wg_text.render_fitted(value, limit)


def _replies(payload):
    """这条消息引用了谁——以消息段为准，`referenced_messages` 只是内容来源。

    issue #94 里 Agent 看到的 `[引用 QQ 消息 543368894]` 就是段渲染出来的，
    而字段当时不在。段在 = 有引用，这是渲染的前提。
    """
    return [str(s.get("onebot_message_id") or "") for s in payload.get("segments") or []
            if s.get("type") == "reply"][:MAX_QUOTES]


def _line(ref_id, state, sender="", text=""):
    """一条引用的后缀。state 决定原文在哪，绝不靠「没印」表达失败。"""
    body = _fit(text, QUOTE_CHARS)
    who = _fit(sender, SENDER_CHARS)
    if body:
        line = f"↪ 引用 {who}: {body}" if who else f"↪ 引用 {body}"
        return line + ("（已撤回）" if state == "recalled" else "")
    reason = "原文为空" if state == "available" else REASONS.get(state) or f"状态 {state}"
    return f"↪ 引用 {ref_id or '?'}（{reason}）"


def _forwards(payload):
    """这条消息带了几个合并转发——与 `_replies` 同一前提：段在 = 有转发。

    超过 `MAX_FORWARDS` 的只印一句汇总，别让唤醒行被转发撑成一屏。
    """
    found = [s for s in payload.get("segments") or [] if s.get("type") == "forward"]
    return found[:MAX_FORWARDS], max(0, len(found) - MAX_FORWARDS)


def _forward_line(detail):
    """一个转发的后缀：几条 + 首条谁说了什么；拿不到就明说状态（issue #9）。

    `detail` 可能是 `referenced_messages` 里已展开的条目（带 state/total_nodes），
    也可能是 `chat_inbox.details` 里的原始 ref（只有 id / nodes），两种都认。
    """
    if not isinstance(detail, dict):
        return "⤴ 转发（正文未展开）"
    nodes = detail.get("nodes") or []
    state = str(detail.get("state") or ("available" if nodes else "unavailable"))
    if state == "available" and nodes:
        first = nodes[0] if isinstance(nodes[0], dict) else {}
        total = detail.get("total_nodes")
        count = total if isinstance(total, int) and total > 0 else len(nodes)
        body = _fit(first.get("content") or "", FORWARD_CHARS)
        who = _fit(first.get("sender") or "", SENDER_CHARS)
        if not body:
            return f"⤴ 转发 {count} 条（正文为空）"
        return f"⤴ 转发 {count} 条：{who}: {body}" if who else f"⤴ 转发 {count} 条：{body}"
    if state == "available":
        return "⤴ 转发（正文为空）"
    return f"⤴ 转发（{FORWARD_REASONS.get(state) or '正文未展开'}）"


def render_event(event):
    """wg_events 那条路：服务端已展开，按 onebot_message_id 对齐到段。"""
    details = {str(d.get("onebot_message_id") or ""): d
               for d in event.get("referenced_messages") or [] if isinstance(d, dict)}
    forward_details = {d.get("forward_index"): d
                       for d in event.get("referenced_messages") or []
                       if isinstance(d, dict) and d.get("type") == "forward"}
    parts = []
    for ref_id in _replies(event):
        detail = details.get(ref_id)
        if detail is None:
            # 有引用段、字段却不在：正是本 issue 报的那一屏，明说未解析。
            parts.append(_line(ref_id, "unresolved"))
        else:
            parts.append(_line(ref_id, str(detail.get("state") or "unavailable"),
                               detail.get("sender") or "", detail.get("content") or ""))
    forwards, extra = _forwards(event)
    for seg in forwards:
        parts.append(_forward_line(forward_details.get(seg.get("forward_index"))))
    if extra:
        parts.append(f"（另有 {extra} 个转发未展开）")
    return "".join(parts)


def render_payload(db, session, payload):
    """chat_inbox 那条路：段里只有编号，原文在同一会话的库里就查得到。"""
    parts = []
    for ref_id in _replies(payload):
        try:
            row = db.execute("SELECT payload FROM chat_inbox WHERE session_id=? AND message_id=?",
                             (session, ref_id)).fetchone()
            parent = json.loads(row[0]) if row else None
        except (sqlite3.Error, ValueError, TypeError):
            parent = None
        if not isinstance(parent, dict):
            parts.append(_line(ref_id, "not_local"))
            continue
        state = "recalled" if parent.get("recalled") else "available"
        parts.append(_line(ref_id, state, parent.get("sender") or "", parent.get("content") or ""))
    # 转发：details 列里按 forward_index 取（顺序与入库时 details.append 一致）；
    # 行已滚出缓存或 details 为空时，_forward_line 会明说「正文未展开」。
    stored = []
    try:
        mid = str(payload.get("onebot_message_id") or "")
        if mid:
            row = db.execute("SELECT details FROM chat_inbox WHERE session_id=? AND message_id=?",
                             (session, mid)).fetchone()
            stored = json.loads(row[0]) if row else []
    except (sqlite3.Error, ValueError, TypeError):
        stored = []
    refs = {i: d for i, d in enumerate(stored) if isinstance(d, dict) and d.get("type") == "forward"}
    forwards, extra = _forwards(payload)
    for seg in forwards:
        parts.append(_forward_line(refs.get(seg.get("forward_index"))))
    if extra:
        parts.append(f"（另有 {extra} 个转发未展开）")
    return "".join(parts)
