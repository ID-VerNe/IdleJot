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
MAX_QUOTES = 3     # 一条消息最多挂 8 个引用，全印出来会把唤醒行撑成一屏

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


def _fit(value, limit):
    """显示用截断：折叠空白再按字数裁。截断只影响这一行，不回流到数据。"""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


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


def render_event(event):
    """wg_events 那条路：服务端已展开，按 onebot_message_id 对齐到段。"""
    details = {str(d.get("onebot_message_id") or ""): d
               for d in event.get("referenced_messages") or [] if isinstance(d, dict)}
    parts = []
    for ref_id in _replies(event):
        detail = details.get(ref_id)
        if detail is None:
            # 有引用段、字段却不在：正是本 issue 报的那一屏，明说未解析。
            parts.append(_line(ref_id, "unresolved"))
        else:
            parts.append(_line(ref_id, str(detail.get("state") or "unavailable"),
                               detail.get("sender") or "", detail.get("content") or ""))
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
    return "".join(parts)
