"""issue #35 gate: 唤醒行的图片标记必须可执行，不许出现裸 [图片]。

症状是群里发图并 @ 我，唤醒行只给一个 `[图片]`：不落盘、不说明怎么补读，Agent
每次都要现场试错（读 wg_events 段 → 猜 images 目录 → 翻 chat_inbox.media →
试 read_chat_forward）才能想起有个 `look.py`。文档侧同样零说明。

本门禁钉住三件事：

  1. 「可补读」必须带上具体命令（`look.py --event <id> --index <i>`）——状态词不是
     可执行指引；
  2. 段里缺 image_index、但索引里有该事件图片行时（旧数据/部分投递），渲染必须经
     索引反查发现图片，而不是当没看见；
  3. 任何状态都要由标记说出来：路径、无权限、未知状态、无记录，各有各的印法，
     唯独不许「有图却什么都不印」。

    .venv/Scripts/python.exe scripts/check_image_visibility.py
"""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_fixture_support  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

TMP = Path(tempfile.mkdtemp(prefix="wg_image_"))
DB = TMP / "fixture.sqlite3"
STATE = TMP / "state"
(STATE / "images").mkdir(parents=True)
SID = "session-fixture"
SELF = "10001"

os.environ["TULPA_WG_DB"] = str(DB)
os.environ["TULPA_WG_STATE"] = str(STATE)

import followup  # noqa: E402
import up  # noqa: E402
import watch  # noqa: E402
import wg_images  # noqa: E402
import wg_up_wake  # noqa: E402  (issue #37 PC: config 的真实调用点；up.config 只是门面引用)

# 事件 id 段：9001+ 与真实数据不撞车；每条用例一个 id，索引行按需写入。
EID_SEG = 9001        # 段有 image_index、无 pixels（实机 6026/5274 的形状）
EID_NO_SEG = 9002     # 段里什么都没有，只有索引行（裸 [图片] 的直接根因）
EID_LOCAL = 9003      # 已落盘
EID_DENIED = 9004     # 无权限
EID_WEIRD = 9005      # 未知状态（失败必须可见）
EID_NONE = 9006       # 占位在、记录全无
EID_PLAIN = 9007      # 没有图
EID_MEDIA = 9008      # 段缺失、media 兜底（reply.py --context 那条路）
EID_WAKE = 9009       # watch 唤醒行端到端
NOW_MS = int(time.time() * 1000)


def index_row(event_id, state, local_path="", index=0, quote_of=None):
    return dict(event_id=event_id, image_index=index, state=state, sha256="",
                local_path=local_path, files=[local_path] if local_path else [],
                dimensions=[], frame_indices=[], seen=bool(local_path),
                at=time.time(), sender="群友丁", ts=NOW_MS, quote_of=quote_of)


def build():
    wg_images.upsert([
        index_row(EID_SEG, "deferred"),
        index_row(EID_NO_SEG, "deferred"),
        index_row(EID_LOCAL, "pixels_delivered", local_path=str(STATE / "9003-0.jpg")),
        index_row(EID_DENIED, "permission_denied"),
        index_row(EID_WEIRD, "budget_exhausted"),
        index_row(EID_MEDIA, "deferred"),
        index_row(EID_WAKE, "deferred"),
    ])
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE chat_sessions (id TEXT, conversation_id TEXT, active INTEGER)")
    db.execute("CREATE TABLE chat_inbox (id INTEGER PRIMARY KEY, session_id TEXT, message_id TEXT,"
               " payload TEXT, media TEXT NOT NULL DEFAULT '[]', details TEXT NOT NULL DEFAULT '[]',"
               " delivered INTEGER NOT NULL DEFAULT 0, at REAL NOT NULL DEFAULT 0)")
    db.execute("INSERT INTO chat_sessions VALUES (?,?,1)", (SID, f"{SELF}:group:123"))
    # followup.since 夹具：一条段内带图的追问窗消息。at 给个非零值，对齐实机
    # chat_inbox.at（实机由 mcp_chat 写入 time.time()）；since() 现在要 SELECT at。
    db.execute("INSERT INTO chat_inbox (id,session_id,message_id,payload,media,at) VALUES (?,?,?,?,?,?)",
               (EID_MEDIA, SID, "m-media", json.dumps({
                   "sender": "群友丁", "content": "[图片]", "timestamp": NOW_MS,
                   "is_self": False, "segments": []}, ensure_ascii=False), "[]", float(NOW_MS / 1000)))
    db.commit()
    db.close()


def event_of(event_id, *, segments, content="[图片]"):
    return {"id": event_id, "sender": "群友丁", "content": content,
            "timestamp": NOW_MS, "segments": segments}


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def 段内缺_pixels_也带补读命令():
    """实机 6026/5274 的形状：段有 image_index、无 pixels.state、索引里 deferred。"""
    event = event_of(EID_SEG, segments=[{"type": "image", "image_index": 0}])
    text = wg_images.render(event)
    return ("可补读" in text and f"look.py --event {EID_SEG} --index 0" in text
            and text != "[图片]")


@case
def 段缺_image_index_时索引反查发现():
    """段里什么都没有、索引里有 deferred 行——以前这里输出空串，只剩裸 [图片]。"""
    event = event_of(EID_NO_SEG, segments=[])
    labels = wg_images.labels(event)
    text = wg_images.render(event)
    return (len(labels) == 1 and labels[0]["state"] == "deferred"
            and f"look.py --event {EID_NO_SEG} --index 0" in text)


@case
def 已落盘印路径():
    event = event_of(EID_LOCAL, segments=[{"type": "image", "image_index": 0,
                                           "pixels": {"state": "pixels_delivered"}}])
    text = wg_images.render(event)
    return "path=" in text and "可补读" not in text


@case
def 无权限明说():
    event = event_of(EID_DENIED, segments=[{"type": "image", "image_index": 0,
                                            "pixels": {"state": "permission_denied"}}])
    return "无权限" in wg_images.render(event)


@case
def 未知状态失败可见():
    """没见过的 state 不许静默：印出来才能知道该修什么。"""
    event = event_of(EID_WEIRD, segments=[{"type": "image", "image_index": 0,
                                           "pixels": {"state": "budget_exhausted"}}])
    return "未交付:budget_exhausted" in wg_images.render(event)


@case
def 无记录但有占位_也给出命令():
    """段、media、索引全无，但 content 里有占位 = 确有图。不靠不印表达失败。"""
    event = event_of(EID_NONE, segments=[])
    text = wg_images.render(event)
    return "未登记" in text and f"look.py --event {EID_NONE}" in text


@case
def 没有图不长出标记():
    event = event_of(EID_PLAIN, segments=[{"type": "text", "text": "在吗"}], content="在吗")
    return wg_images.render(event) == ""


@case
def media_兜底也带命令():
    """reply.py --context 那条路：段缺失时用 chat_inbox.media 兜底。"""
    event = event_of(EID_MEDIA, segments=[])
    media = [{"kind": "image", "status": "available", "url": "https://example/x.jpg"}]
    text = wg_images.render(event, media)
    return f"look.py --event {EID_MEDIA} --index 0" in text


@case
def 追问行带图片提示():
    """追问窗里收到的图以前零信息（watch 追问分支不渲染图片）。"""
    rows = followup.since(0, SID)
    hit = [r for r in rows if r.get("id") == EID_MEDIA]
    return bool(hit) and f"look.py --event {EID_MEDIA} --index 0" in (hit[0].get("images") or "")


@case
def 唤醒行端到端带图片提示():
    """watch.py 唤醒行：图片后缀必须跟在正文后面印出来。"""
    (STATE / "wg_events.jsonl").write_text(json.dumps({
        "id": EID_WAKE, "sender": "群友丁", "content": "[图片] 看这个",
        "timestamp": NOW_MS, "is_self": False,
        "segments": [{"type": "image", "image_index": 0}, {"type": "at", "qq": SELF}],
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    real = wg_up_wake.config
    wg_up_wake.config = lambda: {"self_account": SELF, "self_names": ["小鲸鱼"],
                         "slash_commands": {"prefix": "#"}}
    argv = sys.argv
    sys.argv = ["watch.py", "--timeout", "2", "--replay"]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = watch.main()
    finally:
        sys.argv = argv
        wg_up_wake.config = real
    text = buf.getvalue()
    return (code == 0 and "WAKE" in text
            and f"look.py --event {EID_WAKE} --index 0" in text)


def main():
    build()
    failed = 0
    for fn in CASES:
        try:
            ok = fn()
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
            continue
        if ok:
            print(f"PASS {fn.__name__}")
        else:
            print(f"FAIL {fn.__name__}")
            failed += 1
    print("OK" if not failed else f"{failed} case(s) failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
