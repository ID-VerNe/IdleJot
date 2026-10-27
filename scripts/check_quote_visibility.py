"""issue #94 gate: 引用原文必须到达 Agent，缺了也要说清楚为什么。

症状是唤醒行只有「[引用 QQ 消息 543368894] 是什么？」——编号有、原文没有，Agent
得自己反查，而反查路径（字段名不对、消息已滚出缓存）都不稳。三处各漏一点：入库的
`content` 只写占位；`enrich` 只信 `details` 列，列空就把 `referenced_messages` 整个
省掉；daemon 侧两条读消息的路（wg_events / chat_inbox）都没读这个字段。

所以断言都落在「Agent 能不能看出来」上，而不是某个字段名：

  1. 带 reply 段、`details` 列为空 → `enrich` 仍产出 `referenced_messages`，且带 state；
  2. 解析成功 → 原文进字段（`content` 与 `segments`）；
  3. 段上已有显式 state（第 9 条引用）→ 不重复解析、不合成垃圾条目；
  4. 入库行被缓存挤掉 → 消息不消失，引用标 unavailable；
  5. wg_events 那条路：有 `referenced_messages` 印原文，没有则印编号 +「未解析」；
  6. chat_inbox 那条路：父消息在本地就印原文，滚出缓存则印「不在本地缓存」；
  7. `reply.py --context` 的输出里能看到它。

    .venv/Scripts/python.exe scripts/check_quote_visibility.py
"""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import check_fixture_support as fs  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)
fs.bootstrap_paths(scripts=False)

TMP = Path(tempfile.mkdtemp(prefix="wg_quote_"))
DB = TMP / "fixture.sqlite3"
STATE = TMP / "state"
STATE.mkdir(parents=True, exist_ok=True)
SID = "session-fixture"
PARENT_MID = "543368894"
QUOTE_MID = "1680381396"
PARENT_TEXT = "上一句原话"

os.environ["TULPA_WG_DB"] = str(DB)
os.environ["TULPA_WG_STATE"] = str(STATE)

import reply  # noqa: E402
import wg_quotes  # noqa: E402
from chatlocal.mcp_chat_details import (  # noqa: E402
    ChatDetails,
    reference_refs,
    unresolved_refs,
)

GRANT = dict(id="grant-fixture", scope={})
ROW = dict(id=SID)

PARENT = {"sender": "群友甲", "content": PARENT_TEXT,
          "timestamp": 1792759758000, "onebot_message_id": PARENT_MID}
QUOTED = {
    "sender": "乙", "content": "[引用 QQ 消息 543368894]@10001 是什么？",
    "timestamp": 1792759760000,
    "segments": [{"type": "reply", "onebot_message_id": PARENT_MID},
                 {"type": "at", "qq": "10001"}, {"type": "text", "text": " 是什么？"}],
}
EVENT_QUOTED = dict(QUOTED, id=5629, referenced_messages=[{
    "type": "reply", "onebot_message_id": PARENT_MID, "state": "available",
    "sender": PARENT["sender"], "timestamp": PARENT["timestamp"],
    "content": PARENT_TEXT, "segments": [{"type": "text", "text": PARENT_TEXT}],
}])


def build():
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE chat_sessions (id TEXT, conversation_id TEXT, active INTEGER)")
    db.execute("CREATE TABLE chat_inbox (id INTEGER PRIMARY KEY, session_id TEXT, message_id TEXT,"
               " payload TEXT, media TEXT NOT NULL DEFAULT '[]', details TEXT NOT NULL DEFAULT '[]',"
               " delivered INTEGER NOT NULL DEFAULT 0)")
    db.execute("CREATE TABLE chat_detail_cache (session_id TEXT, cache_key TEXT, body TEXT, at REAL,"
               " PRIMARY KEY(session_id,cache_key))")
    db.execute("INSERT INTO chat_sessions VALUES (?,?,1)", (SID, "10001:group:123"))
    db.execute("INSERT INTO chat_inbox (id,session_id,message_id,payload) VALUES (1,?,?,?)",
               (SID, PARENT_MID, json.dumps(PARENT, ensure_ascii=False)))
    db.execute("INSERT INTO chat_inbox (id,session_id,message_id,payload,details) VALUES (2,?,?,?,?)",
               (SID, QUOTE_MID, json.dumps(QUOTED, ensure_ascii=False),
                json.dumps([{"type": "reply", "id": PARENT_MID}])))
    db.commit()
    db.close()


def connect():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    return db


class _Access:
    """Stand-in for the real access layer: one connection, commit on exit."""

    def __init__(self, db):
        self.db = db

    @contextlib.contextmanager
    def connect(self):
        try:
            yield self.db
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise


class _Turns:
    @staticmethod
    def policy():
        return {"detail_budget_seconds": 1}


class _Chat:
    turns = _Turns()

    @staticmethod
    def validate(session_id, grant_id):
        return dict(GRANT, id=grant_id), dict(id=session_id)


def details(db, reply_result=None):
    """`enrich` with the OneBot round trips stubbed: 这里问的是引用清单，不是取数。"""
    item = ChatDetails.__new__(ChatDetails)
    item.access = _Access(db)
    item.chat = _Chat()
    item.reply = lambda *a, **k: dict(reply_result or {"state": "unavailable"})
    item.forward = lambda *a, **k: dict(state="unavailable", nodes=[], truncated=False)
    return item


def item_of(event_id, payload, **extra):
    return dict(payload, id=event_id, **extra)


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def 段在就算有引用_不靠_details_列():
    refs = reference_refs([{"type": "reply", "onebot_message_id": PARENT_MID}], [])
    return refs == [{"type": "reply", "id": PARENT_MID}]


@case
def 入库记录齐全时原样返回():
    stored = [{"type": "reply", "id": "1"}, {"type": "forward", "id": "f", "nodes": []}]
    segs = [{"type": "reply", "onebot_message_id": "1"}, {"type": "forward", "forward_index": 1}]
    return reference_refs(segs, stored) == stored


@case
def 段上已有显式状态的不再合成():
    return reference_refs([{"type": "reply", "onebot_message_id": "9", "state": "detail_limit"}], []) == []


@case
def payload_段被清空时回落到入库记录():
    stored = [{"type": "reply", "id": "1"}]
    return reference_refs([], stored) == stored


@case
def 缺_details_列也要产出显式引用():
    db = connect()
    db.execute("INSERT INTO chat_inbox (id,session_id,message_id,payload,details) VALUES (10,?,?,?,'[]')",
               (SID, "800000001", json.dumps(QUOTED, ensure_ascii=False)))
    db.commit()
    out = details(db).enrich(GRANT, ROW, [item_of(10, QUOTED)])
    refs = out[0].get("referenced_messages") or []
    return (len(refs) == 1 and refs[0]["state"] == "unavailable"
            and refs[0]["onebot_message_id"] == PARENT_MID)


@case
def 解析成功时原文进字段():
    db = connect()
    db.execute("INSERT INTO chat_inbox (id,session_id,message_id,payload,details) VALUES (11,?,?,?,'[]')",
               (SID, "800000002", json.dumps(QUOTED, ensure_ascii=False)))
    db.commit()
    answer = {"state": "available", "source": "inbox", "sender": PARENT["sender"],
              "sender_id": "10002", "timestamp": PARENT["timestamp"],
              "segments": [{"type": "text", "text": PARENT_TEXT}], "truncated": False}
    out = details(db, answer).enrich(GRANT, ROW, [item_of(11, QUOTED)])
    refs = out[0].get("referenced_messages") or []
    return (len(refs) == 1 and refs[0]["state"] == "available"
            and refs[0]["content"] == PARENT_TEXT and refs[0]["onebot_message_id"] == PARENT_MID)


@case
def 入库行消失也不丢消息():
    out = details(connect()).enrich(GRANT, ROW, [item_of(9999, QUOTED)])
    refs = (out[0].get("referenced_messages") or []) if out else []
    return (len(out) == 1 and out[0]["id"] == 9999 and len(refs) == 1
            and refs[0]["state"] == "unavailable" and refs[0]["onebot_message_id"] == PARENT_MID)


@case
def 引用里的合并转发仍按位置读得回来():
    """`read()` 的引用清单也必须来自同一份推导，否则位置会和 Agent 看到的错开。"""
    nodes = [dict(sender="甲", sender_id="444", timestamp=1792759000,
                  segments=[{"type": "text", "text": f"node {i}"}], truncated=False) for i in range(25)]
    db = connect()
    payload = dict(QUOTED, segments=[{"type": "reply", "onebot_message_id": PARENT_MID}])
    db.execute("INSERT INTO chat_inbox (id,session_id,message_id,payload,details,delivered)"
               " VALUES (12,?,?,?,?,1)",
               (SID, "800000003", json.dumps(payload, ensure_ascii=False),
                json.dumps([{"type": "reply", "id": PARENT_MID}])))
    db.commit()
    item = details(db)
    item.reply = lambda *a, **k: dict(state="available", source="inbox", sender=PARENT["sender"],
                                      sender_id="10002", timestamp=PARENT["timestamp"],
                                      segments=[{"type": "forward", "id": "fwd-1"}], truncated=False)
    item.forward = lambda *a, **k: dict(state="available", nodes=nodes, truncated=False)
    out = item.read(GRANT, dict(session_id=SID, event_id=12, reply_index=0, forward_index=0,
                                offset=20, limit=20), threading.Event())
    return (out["state"] == "available" and out["total_nodes"] == 25
            and len(out["nodes"]) == 5 and not out["has_more"])


@case
def 唤醒行印出被引用原文():
    return PARENT_TEXT in wg_quotes.render_event(EVENT_QUOTED)


@case
def 唤醒行缺字段时明说未解析():
    text = wg_quotes.render_event(dict(QUOTED, id=5629))
    return "未解析" in text and PARENT_MID in text


@case
def 唤醒行不靠不印来表达失败():
    """一条没有引用的消息不该凭空长出一个引用标记。"""
    plain = {"segments": [{"type": "text", "text": "在吗"}]}
    return wg_quotes.render_event(plain) == ""


@case
def 唤醒行印出转发条数与首条摘要():
    """issue #9：转发段必须给出「几条 + 谁说了什么」，不能只留 [合并转发]。"""
    event = {"segments": [{"type": "forward", "forward_index": 0}],
             "referenced_messages": [{"type": "forward", "state": "available", "forward_index": 0,
                                      "total_nodes": 5, "nodes": [
                                          {"sender": "甲", "content": "第一条正文"},
                                          {"sender": "乙", "content": "第二条正文"}]}]}
    text = wg_quotes.render_event(event)
    return "转发 5 条" in text and "甲" in text and "第一条正文" in text


@case
def 转发不可得时明说状态():
    """拿不到正文时印状态词，而不是空串或 0 条（#94 的同一口径）。"""
    event = {"segments": [{"type": "forward", "forward_index": 0}],
             "referenced_messages": [{"type": "forward", "state": "unavailable",
                                      "forward_index": 0, "nodes": []}]}
    text = wg_quotes.render_event(event)
    return "转发" in text and "正文不可得" in text and "0 条" not in text


@case
def 上下文能本地反查原文():
    return PARENT_TEXT in wg_quotes.render_payload(connect(), SID, QUOTED)


@case
def 上下文父消息不在本地时明说():
    payload = {"segments": [{"type": "reply", "onebot_message_id": "123456789"},
                            {"type": "text", "text": " 是什么？"}]}
    text = wg_quotes.render_payload(connect(), SID, payload)
    return "123456789" in text and "不在本地缓存" in text


@case
def context_输出带引用原文():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = reply.show_context(10)
    return code == 0 and PARENT_TEXT in buf.getvalue()


@case
def context_父消息滚出缓存后仍明说():
    db = sqlite3.connect(DB)
    db.execute("DELETE FROM chat_inbox WHERE id=1")
    db.commit()
    db.close()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = reply.show_context(10)
    text = buf.getvalue()
    return code == 0 and "不在本地缓存" in text and PARENT_MID in text


def main():
    return fs.run_cases(CASES, setup=build, prefix=False)


if __name__ == "__main__":
    sys.exit(main())
