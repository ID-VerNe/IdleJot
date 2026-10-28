"""`reply.py --context` 的读侧：回之前先看群里在聊什么（issue #31/#94/#163）。

`--list` 只认识唤醒——`mention_only` 在服务端就把闲聊滤掉了，`wg_events.jsonl`
里一条都没有。所以上下文只能从 `chat_inbox` 取，而后端在决定要不要推送之前
就已经把每条消息写进去了。

从 `reply.py` 拆出来的是同一条读链（`check_module_size` 的 1000 行红线，
与 `wg_gap` / `wg_send_failure` 同一套姿势）：

- :func:`active_session_id`：会话 id 以库里的 `chat_sessions.active` 为准，
  `wg_session.txt` 会在会话重建后落后；
- :func:`recent_chatter`：**每个**群成员说的话，不只是叫醒我们的；
- :func:`show_context`：把这段闲聊印成一行一条，挂上引用原文、图片标记、人名
  指向与画像（唤醒行 / `--around` 那一行才挂画像，见 issue #163）。

`reply.py` 仍按 `reply.recent_chatter` / `reply.show_context` 再导出：门禁
（`check_reply_context` / `check_alias_visibility` / `check_quote_visibility`）
和 `--context` 入口一直这么调，换路径等于让判据盯着一个搬走的旧地址。
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import wg_aliases  # noqa: E402  (群号 → 别名视图，issue #30)
import wg_clock  # noqa: E402  (「现在」的唯一出口，issue #95)
import wg_dossiers  # noqa: E402  (群友画像，issue #163)
import wg_images  # noqa: E402  (图片标记渲染，issue #27)
import wg_quotes  # noqa: E402  (引用原文渲染，issue #94)
import wg_text  # noqa: E402  (正文渲染：段占位不占额度，issue #32)
from wg_paths import data_dir

DB = Path(os.environ.get("IDLEJOT_WG_DB")
          or os.environ.get("TULPA_WG_DB", data_dir() / "mcp-access.sqlite3"))


def active_session_id():
    """The session the collector is holding, read from the database.

    wg_session.txt can lag behind a rebuilt session, and the context query has to
    follow whichever session the messages were actually written under.
    """
    try:
        db = wg_text.connect_ro(DB, timeout=5)
    except sqlite3.Error:
        return ""
    try:
        row = db.execute(
            "SELECT id FROM chat_sessions WHERE active=1 ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        db.close()
    return row[0] if row else ""


def recent_chatter(limit, around=None):
    """Every message the group sent, not just the ones that woke us.

    mention_only keeps ordinary chatter out of wg_events.jsonl (natural groups now
    let it in, #188, but mention_only groups still do not), so the daemon files
    cannot answer "what were they talking about just now". chat_inbox can:
    the backend stores each event before deciding whether to deliver it.
    """
    # 延迟 import：reply.py 在模块级再导出本模块，顶层反向 import 会成环。
    # `session_id()` 读的是 reply 那边 `wg_session.txt` 的会话 id，是兜底。
    import reply

    sid = active_session_id() or reply.session_id()
    if not sid:
        return []
    try:
        db = wg_text.connect_ro(DB, timeout=5)
    except sqlite3.Error:
        return []
    sql = "SELECT id, payload, media FROM chat_inbox WHERE session_id=?"
    params = [sid]
    if around:
        sql += " AND id<=?"
        params.append(int(around))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    out = []
    try:
        for event_id, payload, raw_media in reversed(db.execute(sql, params).fetchall()):
            try:
                data = json.loads(payload)
            except ValueError:
                continue
            if data.get("synthetic"):
                continue
            try:
                media = json.loads(raw_media or "[]")
            except ValueError:
                media = []
            out.append({
                "id": event_id,
                "sender": data.get("sender") or "",
                "text": " ".join((data.get("content") or "").split()),
                "ts": data.get("timestamp"),
                "self": bool(data.get("is_self")),
                "recalled": bool(data.get("recalled")),
                # 群号：别名指向按群取覆盖行并集（issue #30）。
                "conversation_id": data.get("conversation_id") or "",
                # 引用原文：payload 里只有编号，父消息在同一会话的库里就查得到（issue #94）。
                "quote": wg_quotes.render_payload(db, sid, data),
                # 图片标记让 Agent 知道这条能看图，以及是否已经落盘（issue #27）。
                "images": wg_images.render(dict(data, id=event_id), media),
            })
    except sqlite3.Error:
        pass
    finally:
        db.close()
    return out


def show_context(limit, around=None):
    """Print recent chatter so a reply can follow the thread, not just the ping.

    A wake line says "someone @'d you". It does not say what the group had been
    talking about for the last twenty minutes, and replying off the ping alone is
    how a bot answers a question that was already settled three messages later.
    """
    import up

    rows = recent_chatter(limit, around)
    if not rows:
        # 读不到记录时「现在」更要给：Agent 至少知道该不该追问、隔了多久。
        print(wg_clock.now_line())
        print("读不到聊天记录：会话没建立，或数据库里还没有这批消息")
        return 1
    wakes = {e.get("id") for e in up.events() if up.wake_reason(e)}
    # 画像只挂在这两类行上：叫醒我们的那条、和 Agent 要回的那条。每条闲聊都挂
    # 一张卡，这一屏就从「在聊什么」变成「他们是谁」（issue #163）。
    hot = wakes | ({int(around)} if around else set())
    span = f"{around} 之前" if around else "最近"
    # 每行只有 HH:MM:SS，表头不带日期的话跨零点就没法判断「昨天还是今天」。
    print(f"# {span} {len(rows)} 条（含闲聊，不只是 @）· {wg_clock.now_line()}")
    for row in rows:
        stamp = wg_clock.clock_ms(row["ts"])
        who = "我" if row["self"] else (row["sender"] or "?")
        # 上下文行同一份渲染口径（issue #32）：段占位不占额度，截断了说被切过。
        text, _, _ = wg_text.render_body(row["text"], wg_text.CONTEXT_BODY_CHARS,
                                         row["id"])
        # 人名指向与唤醒行同款（issue #30）；自己说的话不指、不挂画像。
        tag, hits, cards = wg_dossiers.anchors(
            "" if row["self"] else row["sender"],
            "" if row["self"] else row["text"],
            wg_aliases.group_of(row.get("conversation_id")),
            with_card=not row["self"] and row["id"] in hot)
        marks = []
        if row["recalled"]:
            marks.append("撤回")
        if row["id"] in wakes:
            marks.append("唤醒")
        if around and row["id"] == int(around):
            marks.append("回这条")
        tail = f"  <- {'、'.join(marks)}" if marks else ""
        quote = ("  " + row["quote"]) if row.get("quote") else ""
        alias_tail = "".join("  " + h for h in hits)
        print(f"{row['id']} {stamp} {who}{tag}: {text}{quote}{('  ' + row['images']) if row.get('images') else ''}{alias_tail}{cards}{tail}")
    return 0
