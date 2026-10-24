"""issue #90 gate: long material must survive the memory path, or say it was cut.

The defect was never the size of the cut, it was the silence. `--dump` handed the
consolidation model 400 of 1524 characters with no field to notice, and
`record_event` capped the summary written back at 500 (then 1000) the same way.
A gate that only asserted "400 is gone" would pass again the day someone picked a
new number, so every check below pins an observable contract instead:

  1. `pending_rows()` returns the body verbatim and marks it not truncated;
  2. `--dump` prints that body whole in its JSON line;
  3. `consolidate_chat_memory(summary=...)` stores a 1600-character summary whole;
  4. a write past `EVENT_TEXT_CHARS` is clipped *and* reports the original
     length, and `query_member_dossier` surfaces both -- detectable, not silent;
  5. an old database gains the two columns without losing its rows;
  6. `clip()` backs up to a sentence boundary, and the chat reader shares it
     instead of keeping a second copy of the limit.

    .venv/Scripts/python.exe scripts/check_text_truncation.py
"""
import atexit
import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

# Pin the data root before anything resolves it. memory_engine's WG_HOME and
# DEFAULT_DB_PATH are evaluated at import time, and `get_memory_engine()` seeds
# on first use -- a later patch would be too late and this gate would write
# fixture profiles into the live library (the #79/#89 class of bug).
_SCRATCH = Path(tempfile.mkdtemp(prefix="tulpa-trunc-"))
atexit.register(shutil.rmtree, _SCRATCH, True)
(_SCRATCH / "home").mkdir(parents=True, exist_ok=True)
os.environ["WATER_GROUP_HOME"] = str(_SCRATCH / "home")
os.environ["IDLEJOT_WG_MEMORY_DB"] = str(_SCRATCH / "mem.sqlite3")

from check_fixture_support import temp_dir  # noqa: E402
from chatlocal import agent_tools  # noqa: E402
from chatlocal.text_limits import (  # noqa: E402
    EVENT_TEXT_CHARS,
    READ_PREVIEW_CHARS,
    Clipped,
    clip,
)
import dossier  # noqa: E402
from water_group.memory_engine import MemoryEngine  # noqa: E402

# Close to the message in the report: a pasted article whose conclusion sits in
# the second half, i.e. exactly the part the old 400-character slice dropped.
BODY = "资料转述：" + "正" * 1500 + "……后半段结论：两位创始人利用周末和假期无偿组织活动。"
SUMMARY = "沉淀摘要：" + "要" * 1595  # 1600 characters, the size that broke in the report


def test_clip_contract():
    """The shared helper: whole text, prefix + loss report, sentence-aware cut."""
    whole = clip("短消息", 100)
    assert whole == Clipped("短消息", False, 3), whole
    assert clip(None, 10) == Clipped("", False, 0)
    assert clip("123456789", 9).truncated is False  # exactly at the limit, no cut

    hard = clip("无标点" * 100, 60)
    assert hard.truncated and len(hard.text) == 60 and hard.original_length == 300, hard

    # A sentence ends 80 characters in; the window is 10% of 100, so the cut
    # backs up to the full stop instead of splitting the next clause.
    sentence = "甲" * 80 + "。" + "乙" * 100
    soft = clip(sentence, 100)
    assert soft.text.endswith("。") and len(soft.text) == 81, soft
    assert soft.original_length == len(sentence)

    # A boundary too far back is not worth the lost budget: hard cut wins.
    far = "丙" * 100 + "。" + "丁" * 100
    assert len(clip(far, 100).text) == 100

    try:
        clip("x", 0)
    except ValueError:
        pass
    else:
        raise AssertionError("clip() must reject a non-positive limit")
    print("✓ 1. clip() 契约：完整/截断标记/原长/句末回落/无标点硬切")


def _seed_inbox(db_path, bodies, session_id="s1"):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE chat_inbox ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, payload TEXT NOT NULL)")
    for body in bodies:
        conn.execute(
            "INSERT INTO chat_inbox (session_id, payload) VALUES (?, ?)",
            (session_id, json.dumps({
                "sender": "群主",
                "sender_id": 10001,
                "content": body,
                "timestamp": 1.0,
                "truncated": False,
            }, ensure_ascii=False)))
    conn.commit()
    conn.close()


def test_pending_rows_keeps_body(tmp):
    """The reader the consolidation model calls must not shorten anything."""
    db_path = tmp / "inbox.sqlite3"
    _seed_inbox(db_path, [BODY])
    with patch.object(dossier, "DB", db_path):
        rows = dossier.pending_rows("s1", 0)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["text"] == BODY, (len(row["text"]), len(BODY))
    assert row["truncated"] is False, row
    print(f"✓ 2. pending_rows() 完整返回 {len(BODY)} 字正文，truncated=False")


def test_dump_keeps_body(tmp):
    """`--dump` is the JSON the model actually reads; the whole body must be there."""
    db_path = tmp / "inbox-dump.sqlite3"
    _seed_inbox(db_path, [BODY])
    buffer = io.StringIO()
    with patch.object(dossier, "DB", db_path), \
            patch.object(dossier, "active_session", lambda: ("s1", "qq:10001")), \
            patch.object(dossier, "load_mark", lambda: {}):
        with contextlib.redirect_stdout(buffer):
            code = dossier.cmd_dump({"max_material": 80}, 0)
    assert code == 0, code
    lines = [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1, lines
    assert lines[0]["text"] == BODY and lines[0]["truncated"] is False, lines[0]
    print(f"✓ 3. --dump 单行 JSON 完整输出 {len(BODY)} 字，无静默截断")


def test_summary_is_stored_whole(tmp):
    """The write side: a long conclusion must not land as its first 500 chars."""
    assert len(SUMMARY) == 1600
    engine = MemoryEngine(tmp / "memory-summary.sqlite3")
    result = engine.consolidate_chat_memory(group_id="g1", summary=SUMMARY)
    assert result["success"], result
    with engine._connect() as conn:
        row = conn.execute(
            "SELECT quote, truncated, original_length FROM member_events "
            "WHERE source='summary'").fetchone()
    assert row is not None, "summary event missing"
    assert row["quote"] == SUMMARY, (len(row["quote"]), len(SUMMARY))
    assert row["truncated"] == 0 and row["original_length"] == 0, dict(row)
    print(f"✓ 4. consolidate_chat_memory 的 {len(SUMMARY)} 字 summary 完整落库")


def test_over_limit_event_is_marked(tmp):
    """Beyond the ceiling the row must admit it, and say how much was there."""
    engine = MemoryEngine(tmp / "memory-cap.sqlite3")
    engine.save_profile(user_id="u1", primary_name="某群友", group_id="g1")
    oversized = "文" * (EVENT_TEXT_CHARS + 500)
    event_id = engine.record_event(
        user_id="u1", group_id="g1", quote=oversized, context="超长原文", source="test")
    with engine._connect() as conn:
        row = conn.execute(
            "SELECT length(quote) AS size, truncated, original_length FROM member_events WHERE id=?",
            (event_id,)).fetchone()
    assert row["size"] == EVENT_TEXT_CHARS, dict(row)
    assert row["truncated"] == 1, dict(row)
    assert row["original_length"] == len(oversized), dict(row)

    # The reader has to see the flag, otherwise the agent cannot compensate.
    dossier_out = engine.query_member_dossier("u1", group_id="g1")
    event = dossier_out["recent_events"][0]
    assert event["truncated"] is True and event["original_length"] == len(oversized), event
    print(f"✓ 5. 超限写入截到 {EVENT_TEXT_CHARS} 并标记 truncated/original_length，查询可见")


def test_old_database_migrates(tmp):
    """Real installs predate these columns; adding them must not lose rows."""
    db_path = tmp / "memory-legacy.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE member_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            group_id TEXT NOT NULL,
            timestamp REAL NOT NULL,
            quote TEXT NOT NULL DEFAULT '',
            context TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'interaction',
            tags TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL
        );
        INSERT INTO member_events (user_id, group_id, timestamp, quote, context, source, tags, created_at)
        VALUES ('u9', 'g1', 1.0, '历史事件', '', 'legacy', '[]', 1.0);
        """)
    conn.commit()
    conn.close()

    engine = MemoryEngine(db_path)
    with engine._connect() as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(member_events)").fetchall()}
        row = conn.execute(
            "SELECT quote, truncated, original_length FROM member_events").fetchone()
    assert {"truncated", "original_length"} <= columns, columns
    # History is left unmarked on purpose: the text is already gone, so claiming
    # it was whole (0) is the only honest default.
    assert row["quote"] == "历史事件" and row["truncated"] == 0 and row["original_length"] == 0
    print("✓ 6. 旧库幂等迁移：新增两列，历史行保留且默认未标记")


def test_chat_reader_shares_the_limit():
    """One implementation: the reader must not grow a second copy of the slice."""
    assert agent_tools.clip is clip, agent_tools.clip
    assert READ_PREVIEW_CHARS == 2000
    print("✓ 7. agent_tools 复用 chatlocal.text_limits.clip，无第二份截断实现")


def main():
    print("========================================================================")
    print("        [测试] issue #90 长文本截断：可检出，不静默")
    print("========================================================================")
    with temp_dir("tulpa-trunc-") as tmp:
        test_clip_contract()
        test_pending_rows_keeps_body(tmp)
        test_dump_keeps_body(tmp)
        test_summary_is_stored_whole(tmp)
        test_over_limit_event_is_marked(tmp)
        test_old_database_migrates(tmp)
    test_chat_reader_shares_the_limit()
    print("========================================================================")
    print("✓ 全部 7 项截断契约测试通过！")
    print("========================================================================")


if __name__ == "__main__":
    main()
