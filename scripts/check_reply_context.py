"""钉住 `reply.py --context`：回之前要看得到群里在聊什么。

`--list` 只认识唤醒——`mention_only` 在服务端就把闲聊滤掉了，`wg_events.jsonl`
里一条都没有。所以上下文只能从 `chat_inbox` 取，而后端在决定要不要推送之前
就已经把每条消息写进去了。

这个脚本用一份临时库固定三件事：窗口取的是最近 N 条（不是最早的）、`--around`
截在指定事件之前、系统提示与自己说过的话在输出里能分清。

    .venv/Scripts/python.exe scripts/check_reply_context.py
"""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.mkdtemp(prefix="wg_ctx_")
DB = Path(TMP) / "fixture.sqlite3"
SID = "session-fixture"

os.environ["TULPA_WG_DB"] = str(DB)
os.environ["TULPA_WG_STATE"] = TMP  # 空的，up.events() 读不到唤醒文件

sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

import reply  # noqa: E402

MESSAGES = [
    (1, {"sender": "甲", "content": "早上好", "timestamp": 1792500000000}),
    (2, {"sender": "乙", "content": "早", "timestamp": 1792500001000}),
    (3, {"sender": "小鲸鱼", "content": "早啊", "timestamp": 1792500002000, "is_self": True}),
    (4, {"sender": "甲", "content": "系统提示", "timestamp": 1792500003000, "synthetic": True}),
    (5, {"sender": "丙", "content": "有人吗", "timestamp": 1792500004000}),
    (6, {"sender": "乙", "content": "@10001 出来", "timestamp": 1792500005000}),
    (7, {"sender": "丁", "content": "刚那句撤回", "timestamp": 1792500006000, "recalled": True}),
]


def build():
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE chat_sessions (id TEXT, conversation_id TEXT, active INTEGER)")
    db.execute("CREATE TABLE chat_inbox "
               "(id INTEGER PRIMARY KEY, session_id TEXT, payload TEXT, media TEXT NOT NULL DEFAULT '[]')")
    db.execute("INSERT INTO chat_sessions VALUES (?, ?, 1)", (SID, "10001:group:123"))
    db.execute("INSERT INTO chat_sessions VALUES ('dead-session', '10001:group:123', 0)")
    for event_id, payload in MESSAGES:
        db.execute("INSERT INTO chat_inbox (id, session_id, payload) VALUES (?, ?, ?)",
                   (event_id, SID, json.dumps(payload, ensure_ascii=False)))
    # 另一个会话的消息不该混进来
    db.execute("INSERT INTO chat_inbox (id, session_id, payload) VALUES (99, 'dead-session', ?)",
               (json.dumps({"sender": "外人", "content": "别的会话"}, ensure_ascii=False),))
    db.commit()
    db.close()


def out(limit, around=None):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = reply.show_context(limit, around)
    return code, buf.getvalue()


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def 窗口取最近而不是最早():
    rows = reply.recent_chatter(2)
    return [r["id"] for r in rows] == [6, 7]


@case
def 系统提示不进上下文():
    return 4 not in [r["id"] for r in reply.recent_chatter(20)]


@case
def 别的会话不混进来():
    return 99 not in [r["id"] for r in reply.recent_chatter(20)]


@case
def around_截在指定事件之前():
    rows = reply.recent_chatter(20, around=5)
    return [r["id"] for r in rows] == [1, 2, 3, 5]


@case
def 自己说过的话标成我():
    _, text = out(20)
    return " 我: 早啊" in text


@case
def around_那条标成回这条():
    _, text = out(20, around=6)
    return "回这条" in text


@case
def 撤回的消息标出来():
    _, text = out(20)
    return "撤回" in text


@case
def 空库返回非零():
    db = sqlite3.connect(DB)
    db.execute("DELETE FROM chat_inbox")
    db.commit()
    db.close()
    code, _ = out(5)
    return code == 1


@case
def 换行拆成多条气泡():
    return reply.split_bubbles("第一句\n第二句\n\n第三句") == ["第一句", "第二句", "第三句"]


@case
def 字面量_n_拆成多条气泡():
    # shell 双引号里 "a\nb" 进来的是反斜杠加 n，也得拆（issue #80）
    return reply.split_bubbles("第一句\\n第二句") == ["第一句", "第二句"]


@case
def 字面量_rn_拆成多条气泡():
    return reply.split_bubbles("a\\r\\nb") == ["a", "b"]


@case
def 双反斜杠是逃生门不当分隔符():
    return reply.split_bubbles("a\\\\nb") == ["a\\nb"]


@case
def 真换行和字面量_n_混用也拆():
    return reply.split_bubbles("a\nb\\nc") == ["a", "b", "c"]


@case
def 字面量_n_之间的空行忽略():
    return reply.split_bubbles("a\\n\\nb") == ["a", "b"]


@case
def 单行不拆():
    return reply.split_bubbles("就一句") == ["就一句"]


@case
def 超过三条截断并提示():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = reply.split_bubbles("a\nb\nc\nd")
    return got == ["a", "b", "c"] and "丢掉了 1 行" in buf.getvalue()


@case
def 空文本不炸():
    return reply.split_bubbles("") == [""] and reply.split_bubbles(None) == [""]


@case
def 多气泡首条带引用其余裸发():
    calls = []
    real = reply.onebot_send
    reply.onebot_send = lambda group_id, segments, message_id=None, failure=None: (
        calls.append((segments, message_id)) or True)
    try:
        sent = reply.onebot_send_bubbles(123, [[{"type": "text", "data": {"text": "a"}}],
                                               [{"type": "text", "data": {"text": "b"}}]],
                                         "555")
    finally:
        reply.onebot_send = real
    return sent == 2 and [mid for _, mid in calls] == ["555", None]


@case
def 中途失败不回退不重发():
    calls = []
    real = reply.onebot_send
    reply.onebot_send = lambda group_id, segments, message_id=None, failure=None: (
        calls.append(segments) or len(calls) == 1)
    try:
        sent = reply.onebot_send_bubbles(123, [[{"type": "text", "data": {"text": "a"}}],
                                               [{"type": "text", "data": {"text": "b"}}]],
                                         "555")
    finally:
        reply.onebot_send = real
    return sent == 1 and len(calls) == 2


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    build()
    bad = 0
    for fn in CASES:
        got = fn()
        print(("✓ " if got else "✗ ") + fn.__name__)
        bad += 0 if got else 1
    print("全部通过" if not bad else f"{bad} 项失败")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
