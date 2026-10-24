"""Gate for issue #31: structured group-level notes (black talk, shop names,
venues, in-group conventions) must round-trip.

The defect this pins down: a "remember that" write used to land as a flat
``group_summary`` row inside ``member_events`` -- a timeline entry with no key,
no term lookup, and no read path back. Recording degraded to logging.

Six properties, one per symptom:

1. Storage layer creates ``group_notes`` with a ``(group_id, term)`` primary key.
2. Write: ``consolidate_chat_memory(notes=[...])`` lands a note; it is readable
   by its term -- not by a SQLite session, not by ``query_member_dossier``.
3. Upsert: re-writing the same term overwrites the value in place and does not
   grow the row count (one meaning per term per group, no history pile-up).
4. Cross-group isolation: a term written in group A is invisible from group B.
5. L1 injection: ``build_l1_context`` rides along a capped snippet, so a large
   glossary cannot crowd out the member dossiers (GROUP_NOTE_L1_CAP).
6. MCP wiring: ``query_group_notes`` is in CHAT_TOOLS and has a schema.
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

# Import through the bridge package so this suite does not create a second
# top-level copy of the engine under the name `memory_engine`.
from water_group.memory_engine import GROUP_NOTE_L1_CAP, MemoryEngine
from chatlocal.mcp_chat import CHAT_TOOLS, schemas as chat_schemas


def test_schema_creates_group_notes(tmp_dir):
    """Property 1: the table exists and (group_id, term) is its key."""
    db_file = tmp_dir / "schema.sqlite3"
    engine = MemoryEngine(db_file)
    with engine._connect() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(group_notes)").fetchall()]
        for col in ("group_id", "term", "kind", "value", "updated_at", "source_event_id"):
            assert col in cols, f"group_notes must have column {col}"
        pk = [r[1] for r in conn.execute("PRAGMA table_info(group_notes)").fetchall() if r[5]]
        assert pk == ["group_id", "term"], f"primary key must be (group_id, term), got {pk}"
    print("✓ 1. group_notes 建表与 (group_id, term) 主键测试通过")


def test_write_and_read_by_term(tmp_dir):
    """Property 2: the write is readable back by the term that named it."""
    engine = MemoryEngine(tmp_dir / "w.sqlite3")
    res = engine.consolidate_chat_memory(
        group_id="111222",
        notes=[{"term": "老张", "kind": "shop", "value": "张记烧烤（人民路店）"}],
    )
    assert res["notes_count"] == 1, str(res)

    notes = engine.query_group_notes("111222", query="老张")
    assert len(notes) == 1, f"query by term must return exactly one row, got {notes}"
    assert notes[0]["value"] == "张记烧烤（人民路店）", str(notes[0])
    assert notes[0]["kind"] == "shop", str(notes[0])

    # source_event_id is the provenance column: it must be writable, not a
    # column that only exists in the DDL (issue #6's dead-column lesson).
    engine.upsert_group_notes("111222", [{"term": "溯源", "value": "带事件行"}], source_event_id=4242)
    traced = engine.query_group_notes("111222", query="溯源")
    assert traced[0]["source_event_id"] == 4242, f"source_event_id must round-trip, got {traced}"
    print("✓ 2. consolidate notes 写入与按 term 读回测试通过")


def test_upsert_does_not_grow(tmp_dir):
    """Property 3: one meaning per term per group; a repeat write overwrites."""
    engine = MemoryEngine(tmp_dir / "upsert.sqlite3")
    engine.upsert_group_notes("111222", [{"term": "老张", "value": "第一版"}])
    engine.upsert_group_notes("111222", [{"term": "老张", "value": "第二版"}])
    notes = engine.query_group_notes("111222", query="老张")
    assert len(notes) == 1, f"re-writing a term must not add a row, got {notes}"
    assert notes[0]["value"] == "第二版", f"upsert must replace the value, got {notes}"
    print("✓ 3. group_notes 重复写走 upsert 测试通过")


def test_cross_group_isolation(tmp_dir):
    """Property 4: group A's term must not leak into group B."""
    engine = MemoryEngine(tmp_dir / "isolation.sqlite3")
    engine.upsert_group_notes("group_A", [{"term": "老张", "value": "A群的店"}])
    assert engine.query_group_notes("group_B", query="老张") == [], "term written in A must be invisible in B"
    assert engine.query_group_notes("group_B") == [], "group B must have no notes at all"
    a_notes = engine.query_group_notes("group_A", query="老张")
    assert len(a_notes) == 1 and a_notes[0]["value"] == "A群的店", str(a_notes)
    print("✓ 4. group_notes 跨群隔离测试通过")


def test_l1_injection_is_capped(tmp_dir):
    """Property 5: the snippet rides along, but a wall of notes must not
    displace the member dossiers."""
    engine = MemoryEngine(tmp_dir / "l1cap.sqlite3")
    for i in range(GROUP_NOTE_L1_CAP + 4):
        engine.upsert_group_notes("111222", [{"term": f"术语{i}", "value": f"含义{i}"}])
    # Back-to-back writes can land inside the same timestamp; only the cap is
    # asserted, not which rows survive the ORDER BY.
    cards = engine.build_l1_context("111222")
    note_cards = [c for c in cards if c.startswith("【群级术语】")]
    assert note_cards, f"L1 injection must carry the notes, got {cards}"
    injected = note_cards[0][len("【群级术语】"):].split("；")
    assert len(injected) == GROUP_NOTE_L1_CAP, f"L1 must cap at {GROUP_NOTE_L1_CAP}, got {len(injected)}"
    # The full set stays behind the read tool.
    assert len(engine.query_group_notes("111222", limit=50)) == GROUP_NOTE_L1_CAP + 4
    print("✓ 5. L1 注入封顶测试通过")


def test_mcp_tool_wiring():
    """Property 6: the read path is a declared MCP tool, not a DB session."""
    assert "query_group_notes" in CHAT_TOOLS, "query_group_notes must be in CHAT_TOOLS"
    from chatlocal.agent_tools import tool

    rows = chat_schemas(tool)
    tool_row = next((s for s in rows if s["name"] == "query_group_notes"), None)
    assert tool_row is not None, "query_group_notes schema must exist"
    props = tool_row["parameters"]["properties"]
    for prop in ("group_id", "query", "limit"):
        assert prop in props, f"query_group_notes must accept {prop}"
    # consolidate_chat_memory exposes the structured write path.
    consolidate = next((s for s in rows if s["name"] == "consolidate_chat_memory"), None)
    assert consolidate is not None, "consolidate_chat_memory schema must exist"
    assert "notes" in consolidate["parameters"]["properties"], "consolidate_chat_memory must accept notes"
    print("✓ 6. MCP query_group_notes 工具声明与 consolidate notes 参数测试通过")


def main():
    print("========================================================================")
    print("        [测试] issue #31 群级结构化术语（黑话/店名）读写闭环验证")
    print("========================================================================")
    with tempfile.TemporaryDirectory(dir=str(ROOT / ".tmp"), prefix="group-notes-") as tmp:
        tmp_dir = Path(tmp)
        test_schema_creates_group_notes(tmp_dir)
        test_write_and_read_by_term(tmp_dir)
        test_upsert_does_not_grow(tmp_dir)
        test_cross_group_isolation(tmp_dir)
        test_l1_injection_is_capped(tmp_dir)
    test_mcp_tool_wiring()
    print("========================================================================")
    print("✓ 全部 6 项 group_notes 测试均通过！")
    print("========================================================================")


if __name__ == "__main__":
    main()
