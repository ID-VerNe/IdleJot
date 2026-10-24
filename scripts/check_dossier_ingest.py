"""`dossier.py --ingest` / `--apply` 的门禁（issue #6）。

以**子进程**跑真实 CLI：argparse 分支、退出码、游标文件都是被测契约的一部分，
只 import 函数测不到「写失败时游标有没有被推进」这类顺序错误。

隔离：`WATER_GROUP_HOME` 指向临时目录（`isolated_env`），数据根、状态目录、
Gradio TEMP 全部落在临时树里，退出即删；不碰 `~/.water-group`。
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from check_fixture_support import isolated_env, temp_dir  # noqa: E402

DOSSIER = ROOT / "water-group" / "daemon" / "dossier.py"
GROUP = "100200300"
CEILING = 12000  # chatlocal/text_limits.py: EVENT_TEXT_CHARS


def run(home, *args, stdin=None):
    """Run the real CLI against the throwaway data root."""
    env = isolated_env(home=home)
    env["SYSTEMDRIVE"] = os.environ.get("SYSTEMDRIVE", "C:")
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, str(DOSSIER), *args],
        cwd=str(ROOT), env=env, input=stdin, text=True, encoding="utf-8",
        capture_output=True, timeout=180,
    )


def seed_store(home):
    """Minimal mcp-access store: one active session, no messages."""
    data = home / "data"
    data.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(data / "mcp-access.sqlite3"))
    db.executescript(
        """
        CREATE TABLE chat_sessions (id INTEGER PRIMARY KEY, conversation_id TEXT, active INTEGER);
        CREATE TABLE chat_inbox (id INTEGER PRIMARY KEY, session_id INTEGER, payload TEXT);
        INSERT INTO chat_sessions (id, conversation_id, active) VALUES (1, 'group:100200300', 1);
        """
    )
    db.commit()
    db.close()


def memory(home):
    return sqlite3.connect(str(home / "data" / "group_memories.sqlite3"))


def summary_events(home):
    db = memory(home)
    try:
        return db.execute(
            "SELECT quote, truncated FROM member_events WHERE source='summary'").fetchall()
    finally:
        db.close()


def mark(home):
    path = home / "state" / "wg_dossier.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def test_ingest_writes_one_summary(home):
    payload = home / "chunk.txt"
    payload.write_text("  群里今晚在复盘三国杀  ", encoding="utf-8")
    done = run(home, "--ingest", str(payload), "--group", GROUP)
    assert done.returncode == 0, done.stderr
    rows = summary_events(home)
    assert len(rows) == 1, rows
    quote, truncated = rows[0]
    assert quote == "群里今晚在复盘三国杀", quote  # trimmed
    assert not truncated, "a short summary must not be flagged as a prefix"
    print("✓ 1. --ingest 写入一条 source=summary 事件，正文完整未截断")


def test_ingest_refuses_over_ceiling(home):
    payload = home / "too_long.txt"
    payload.write_text("长" * (CEILING + 1), encoding="utf-8")
    before = len(summary_events(home))
    done = run(home, "--ingest", str(payload), "--group", GROUP)
    assert done.returncode == 2, (done.returncode, done.stdout, done.stderr)
    assert str(CEILING) in done.stderr, done.stderr
    assert len(summary_events(home)) == before, "over-ceiling payload must not be written"
    print(f"✓ 2. 超过 {CEILING} 字直接拒绝（exit 2），库内无新增")


def test_through_moves_only_after_write(home):
    payload = home / "chunk2.txt"
    payload.write_text("第二批沉淀", encoding="utf-8")
    done = run(home, "--ingest", str(payload), "--group", GROUP, "--through", "7")
    assert done.returncode == 0, done.stderr
    assert int(mark(home).get("last_event_id") or 0) == 7, mark(home)

    long_payload = home / "too_long2.txt"
    long_payload.write_text("长" * (CEILING + 1), encoding="utf-8")
    failed = run(home, "--ingest", str(long_payload), "--group", GROUP, "--through", "9")
    assert failed.returncode == 2
    assert int(mark(home).get("last_event_id") or 0) == 7, "失败不得推进游标"
    print("✓ 3. --through 写入成功才推进；失败保持原游标")


def apply_members(home, name, memories):
    payload = home / f"members-{name}.json"
    payload.write_text(json.dumps({
        "group_id": GROUP,
        "members": [{"identifier": name, "aliases": [name], "key_memories": memories}],
    }, ensure_ascii=False), encoding="utf-8")
    done = run(home, "--apply", str(payload))
    assert done.returncode == 0, (done.stdout, done.stderr)
    return done


def test_apply_keeps_group_boundary(home):
    """群内写入不得把群内事实推进跨群主档（issue #5 + #19 写侧边界）。

    两段各钉一处：
    - 新建段：首次带 group_id 写走 INSERT 分支（issue #19 修，memory_dossier.py），
      主档必须为空，群内事实只进覆盖行。
    - 追加段：已存在档案的追加写走 UPDATE 分支（issue #5 保护），主档必须
      留在新建时的样子，群内内容合并进覆盖行。
    """
    # 新建段：INSERT 分支不得把群内事实写进跨群主档
    apply_members(home, "测试甲", ["事实A"])
    db = memory(home)
    try:
        uid = db.execute(
            "SELECT user_id FROM member_profiles WHERE primary_name='测试甲'").fetchone()
        assert uid, "profile row must exist"
        uid = uid[0]
        main = json.loads(db.execute(
            "SELECT key_memories FROM member_profiles WHERE user_id=?", (uid,)).fetchone()[0])
        overlay = json.loads(db.execute(
            "SELECT key_memories FROM member_group_memories WHERE user_id=? AND group_id=?",
            (uid, GROUP)).fetchone()[0])
    finally:
        db.close()
    assert main == [], f"新建档案带 group_id 不得写群内事实进主档（issue #19）：{main}"
    assert "事实A" in overlay, overlay

    # 追加段：UPDATE 分支不得把新事实推进主档，群内内容合并进覆盖行
    apply_members(home, "测试甲", ["事实B"])
    db = memory(home)
    try:
        main = json.loads(db.execute(
            "SELECT key_memories FROM member_profiles WHERE user_id=?", (uid,)).fetchone()[0])
        overlay = json.loads(db.execute(
            "SELECT key_memories FROM member_group_memories WHERE user_id=? AND group_id=?",
            (uid, GROUP)).fetchone()[0])
    finally:
        db.close()
    assert main == [], f"追加写不得推进跨群主档（issue #5）：{main}"
    assert "事实B" in overlay, overlay
    print("✓ 4. 带 group_id 新建主档空、追加主档不变；群内事实只落覆盖行")


def test_consolidate_records_keep_group_boundary(home):
    """consolidate_chat_memory records 路径带 group_id 不把群内事实写进主档（issue #19 分节 PB）。

    records 路径此前调 `update_member_dossier` 不传 group_id，导致群内事实直落跨群主档。
    PB 补了 `group_id=gid or None`；这里钉住：带 group_id 走 records 时主档必须为空，
    群内事实只进覆盖行。
    """
    sys.path.insert(0, str(ROOT))
    from water_group.memory_engine import MemoryEngine  # noqa: E402

    engine = MemoryEngine(str(home / "data" / "group_memories.sqlite3"))
    engine.consolidate_chat_memory(
        group_id=GROUP,
        records=[{"identifier": "测试丁", "key_memories": ["仅在丁群的事实"]}],
    )

    db = memory(home)
    try:
        uid = db.execute(
            "SELECT user_id FROM member_profiles WHERE primary_name='测试丁'").fetchone()
        assert uid, "profile row must exist"
        uid = uid[0]
        main = json.loads(db.execute(
            "SELECT key_memories FROM member_profiles WHERE user_id=?", (uid,)).fetchone()[0])
        overlay = json.loads(db.execute(
            "SELECT key_memories FROM member_group_memories WHERE user_id=? AND group_id=?",
            (uid, GROUP)).fetchone()[0])
    finally:
        db.close()
    assert main == [], f"records 路径带 group_id 不得写群内事实进主档（issue #19 分节 PB）：{main}"
    assert "仅在丁群的事实" in overlay, overlay
    print("✓ 4b. consolidate records 带 group_id 只写覆盖行，主档 key_memories 为空")


def test_apply_rejects_unknown_field(home):
    payload = home / "typo.json"
    payload.write_text(json.dumps({
        "group_id": GROUP,
        "members": [{"identifier": "测试乙", "key_memorys": ["拼错字段"]}],
    }, ensure_ascii=False), encoding="utf-8")
    done = run(home, "--apply", str(payload))
    assert done.returncode == 2, (done.returncode, done.stdout)
    assert "unknown fields" in done.stderr, done.stderr
    db = memory(home)
    try:
        assert not db.execute(
            "SELECT 1 FROM member_profiles WHERE primary_name='测试乙'").fetchone(), \
            "校验失败不得落库"
    finally:
        db.close()
    print("✓ 5. 未知字段 fail-closed（exit 2），不产生半截写入")


def test_dry_run_writes_nothing(home):
    payload = home / "dry.json"
    payload.write_text(json.dumps({
        "group_id": GROUP,
        "members": [{"identifier": "测试丙", "key_memories": ["不该落库"]}],
        "summary": "不该落库的总结",
    }, ensure_ascii=False), encoding="utf-8")
    before = len(summary_events(home))
    done = run(home, "--apply", str(payload), "--dry-run")
    assert done.returncode == 0, done.stderr
    assert "would apply" in done.stdout, done.stdout
    db = memory(home)
    try:
        assert not db.execute(
            "SELECT 1 FROM member_profiles WHERE primary_name='测试丙'").fetchone(), \
            "--dry-run 不得写库"
    finally:
        db.close()
    assert len(summary_events(home)) == before
    print("✓ 6. --dry-run 只打印计划，不写库")


def main():
    with temp_dir(prefix="wg-ingest-") as home:
        seed_store(home)
        test_ingest_writes_one_summary(home)
        test_ingest_refuses_over_ceiling(home)
        test_through_moves_only_after_write(home)
        test_apply_keeps_group_boundary(home)
        test_consolidate_records_keep_group_boundary(home)
        test_apply_rejects_unknown_field(home)
        test_dry_run_writes_nothing(home)
    print("=" * 72)
    print("✓ dossier.py 写入动词 7 项门禁全部通过")
    print("=" * 72)


if __name__ == "__main__":
    main()
