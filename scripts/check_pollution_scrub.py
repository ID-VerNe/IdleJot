"""`dossier.py --audit-pollution` / `--scrub-pollution` / `--restore-pollution` 门禁（issue #34）。

以**子进程**跑真实 CLI：argparse 分支、退出码、`--dry-run` 与 `--json` 都是被测契约，
只 import 函数测不到「预演有没有写库」这类顺序错误。

隔离：`WATER_GROUP_HOME` 指向临时目录（`isolated_env`），引擎库落在临时树里，
退出即删；不碰 `~/.water-group`。

夹具形状按 issue #34 的四种群友各钉一种：
- 主档 == 覆盖行（生产库最常见形状，全量污染）
- 主档部分在覆盖行、部分仅主档持有（保守边界：仅主档那部分必须留下）
- 无覆盖行（无证据，一律不动）
- 有覆盖行但在别的群也有事件且该群无覆盖行（越 scope，跳过不清理）
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from check_fixture_support import isolated_env, temp_dir  # noqa: E402

DOSSIER = ROOT / "water-group" / "daemon" / "dossier.py"
GROUP_A = "900000001"
GROUP_B = "900000002"


def run(home, *args):
    env = isolated_env(home=home)
    env["SYSTEMDRIVE"] = os.environ.get("SYSTEMDRIVE", "C:")
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, str(DOSSIER), *args],
        cwd=str(ROOT), env=env, text=True, encoding="utf-8",
        capture_output=True, timeout=180,
    )


def memory_db(home):
    return sqlite3.connect(str(home / "data" / "group_memories.sqlite3"))


def snapshot(home):
    """(main, overlay, backup) columns verbatim, for byte-identity assertions."""
    db = memory_db(home)
    try:
        main = db.execute(
            "SELECT user_id, key_memories, key_memories_backup FROM member_profiles "
            "ORDER BY user_id").fetchall()
        overlay = db.execute(
            "SELECT user_id, group_id, key_memories, key_memories_backup "
            "FROM member_group_memories ORDER BY user_id, group_id").fetchall()
        events = db.execute("SELECT COUNT(*) FROM member_events").fetchone()[0]
        return main, overlay, events
    finally:
        db.close()


def build_fixture(home):
    """Writes the four member shapes directly: the pre-#19 bug is what put the
    group facts in the cross-group row, and no supported write path reproduces
    that any more, so the fixture has to be built the way the bug left it."""
    from water_group.memory_engine import MemoryEngine

    engine = MemoryEngine(str(home / "data" / "group_memories.sqlite3"))
    # `get_memory_engine()` seeds the two placeholder cards on first load, so the
    # CLI's first call adds them no matter which verb runs. Seeding here too keeps
    # the fixture stable and makes "audit writes nothing" assertable.
    engine.seed_from_persona()
    members = [
        # 全量污染：主档 == 覆盖行
        ("100000001", "污染甲", ["群内事实A1", "群内事实A2", "群内事实A3"],
         ["群内事实A1", "群内事实A2", "群内事实A3"], []),
        # 部分污染 + 仅主档持有
        ("100000002", "污染乙", ["群内事实B1", "群内事实B2", "仅主档事实B3", "仅主档事实B4"],
         ["群内事实B1", "群内事实B2"], []),
        # 无覆盖行
        ("100000003", "无覆盖丙", ["无法验证的事实C1"], None, []),
        # 越 scope：A 群有覆盖行，B 群有事件但无覆盖行
        ("100000004", "越scope丁", ["群内事实D1", "仅主档事实D2"],
         ["群内事实D1"], [(GROUP_B, "丁在B群的发言")]),
    ]
    now = 1000.0
    with engine.lock, engine._connect() as conn:
        for uid, name, global_mem, overlay_mem, extra_events in members:
            conn.execute(
                "INSERT INTO member_profiles (user_id, primary_name, aliases, tone_style,"
                " interests, traits, key_memories, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (uid, name, json.dumps([name], ensure_ascii=False), "普通群友", "[]", "{}",
                 json.dumps(global_mem, ensure_ascii=False), now, now),
            )
            if overlay_mem is not None:
                conn.execute(
                    "INSERT INTO member_group_memories (user_id, group_id, key_memories,"
                    " tone_style, interests, aliases, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (uid, GROUP_A, json.dumps(overlay_mem, ensure_ascii=False), "", "[]", "[]",
                     now, now),
                )
            conn.execute(
                "INSERT INTO member_events (user_id, group_id, timestamp, quote, context,"
                " source, tags, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (uid, GROUP_A, now, "甲在A群的发言", "夹具", "interaction", "[]", now),
            )
            for gid, quote in extra_events:
                conn.execute(
                    "INSERT INTO member_events (user_id, group_id, timestamp, quote, context,"
                    " source, tags, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (uid, gid, now, quote, "夹具", "interaction", "[]", now),
                )
    return engine


def group_view(engine, uid, group_id):
    """What a reader in `group_id` sees -- the `_memory_view` union."""
    prof = engine.find_profile(uid, group_id=group_id)
    return engine._memory_view(prof)[0]


def cross_group_view(engine, uid):
    """What a reader that asks for no group sees: the raw cross-group row."""
    prof = engine.find_profile(uid)
    return json.loads(prof["key_memories"] or "[]")


def test_audit_is_read_only(home):
    before = snapshot(home)
    done = run(home, "--audit-pollution")
    assert done.returncode == 0, done.stderr
    after = snapshot(home)
    assert before == after, "只读审计不得改动任何列"
    print("✓ 1. --audit-pollution 纯只读：主档/覆盖行/备份/事件四块前后一致")


def test_audit_numbers_match_sql(home):
    done = run(home, "--audit-pollution", "--json")
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    # 两位 seed 占位档案（无覆盖行）也在库里，一并计入
    assert report["profiles_total"] == 6, report
    assert report["members_with_overlay"] == 3, report
    assert report["no_overlay_members"] == 3, report  # 无覆盖丙 + 两个 seed
    assert report["groups"] == [GROUP_A], report["groups"]
    # 主档总数 3+4+1+2+1+1=12；覆盖行已持有 3+2+1(丁越scope仍计入可证明)=6
    assert report["global_memories_total"] == 12, report
    assert report["provable_pollution_total"] == 6, report
    # 越 scope 的丁不可安全剔除，故 scrubbable = 3+2 = 5
    assert report["scrubbable_total"] == 5, report
    assert report["out_of_scope_members"] == 1, report
    # 仅主档持有：B3/B4 + D2 + C1 + 两个 seed = 6；其中有覆盖行群友的 3 条
    assert report["global_only_total"] == 6, report
    assert report["global_only_with_overlay_total"] == 3, report
    by_id = {m["user_id"]: m for m in report["members"]}
    assert by_id["100000004"]["in_scope"] is False, by_id["100000004"]
    assert by_id["100000002"]["in_scope"] is True, by_id["100000002"]
    print("✓ 2. --audit-pollution 报告数值与手工 SQL 一致（含越 scope 单列）")


def test_scrub_removes_only_overlap(home):
    done = run(home, "--scrub-pollution", "--json")
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    assert report["dry_run"] is False, report
    assert report["scrubbed_members"] == 2, report
    assert report["removed_total"] == 5, report
    assert report["kept_total"] == 2, report
    assert [s["user_id"] for s in report["skipped"]] == ["100000004"], report["skipped"]
    db = memory_db(home)
    try:
        main = {uid: json.loads(km or "[]") for uid, km in db.execute(
            "SELECT user_id, key_memories FROM member_profiles").fetchall()}
    finally:
        db.close()
    assert main["100000001"] == [], main["100000001"]
    assert main["100000002"] == ["仅主档事实B3", "仅主档事实B4"], main["100000002"]
    assert main["100000003"] == ["无法验证的事实C1"], main["100000003"]
    assert main["100000004"] == ["群内事实D1", "仅主档事实D2"], main["100000004"]
    print("✓ 3. --scrub-pollution 只剔主档中覆盖行已持有的条目（5 条），其余一律不动")


def test_group_view_unchanged_after_scrub(home, engine):
    """核心不变式：清理后同一群的 _memory_view 并集必须逐条相同。

    overlay 是「完整视图快照」，剔掉的条目覆盖行里都有；并集因此不变，
    群友在本群看到的记忆一条不少。
    """
    views_before = {uid: group_view(engine, uid, GROUP_A)
                    for uid in ("100000001", "100000002", "100000004")}
    done = run(home, "--scrub-pollution")
    assert done.returncode == 0, done.stderr
    for uid, before in views_before.items():
        after = group_view(engine, uid, GROUP_A)
        assert after == before, f"{uid} 群内视图被清理改变: {before} -> {after}"
    print("✓ 4. 清理后本群 _memory_view 并集逐条不变（群友视角零损失）")


def test_cross_group_leak_closed(home, engine):
    before = cross_group_view(engine, "100000002")
    assert "群内事实B1" in before, before
    done = run(home, "--scrub-pollution")
    assert done.returncode == 0, done.stderr
    after = cross_group_view(engine, "100000002")
    assert "群内事实B1" not in after and "群内事实B2" not in after, after
    assert "仅主档事实B3" in after and "仅主档事实B4" in after, after
    print("✓ 5. 无 group_id 的跨群查询不再看到群内专属事实，仅主档条目仍保留")


def test_backup_and_restore(home, engine):
    original = {uid: cross_group_view(engine, uid) for uid in
                ("100000001", "100000002")}
    done = run(home, "--scrub-pollution")
    assert done.returncode == 0, done.stderr
    db = memory_db(home)
    try:
        backups = {uid: json.loads(bk or "[]") for uid, bk in db.execute(
            "SELECT user_id, key_memories_backup FROM member_profiles").fetchall()}
    finally:
        db.close()
    # 备份存的是清理前的完整主档列表，顺序也保住了
    assert backups["100000001"] == original["100000001"], backups["100000001"]
    assert backups["100000002"] == original["100000002"], backups["100000002"]

    restored = run(home, "--restore-pollution", "--json")
    assert restored.returncode == 0, restored.stderr
    report = json.loads(restored.stdout)
    assert report["restored_members"] == 2, report
    for uid, want in original.items():
        assert cross_group_view(engine, uid) == want, uid
    db = memory_db(home)
    try:
        left = db.execute(
            "SELECT COUNT(*) FROM member_profiles WHERE key_memories_backup IS NOT NULL "
            "AND key_memories_backup != '' AND key_memories_backup != '[]'").fetchone()[0]
    finally:
        db.close()
    assert left == 0, "还原后备份槽应清空，否则会向后来的查询报告假的回滚提示"
    print("✓ 6. 被剔列表入 key_memories_backup；--restore-pollution 原样还原并清槽")


def test_dry_run_writes_nothing(home):
    before = snapshot(home)
    done = run(home, "--scrub-pollution", "--dry-run", "--json")
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    assert report["dry_run"] is True, report
    assert report["removed_total"] == 5, report
    after = snapshot(home)
    assert before == after, "--dry-run 不得写库（含备份列）"
    print("✓ 7. --scrub-pollution --dry-run 只报计划，主档/覆盖行/备份均未动")


def test_restore_dry_run_and_idempotence(home):
    assert run(home, "--scrub-pollution").returncode == 0
    before = snapshot(home)
    dry = run(home, "--restore-pollution", "--dry-run", "--json")
    assert dry.returncode == 0, dry.stderr
    assert json.loads(dry.stdout)["dry_run"] is True
    assert snapshot(home) == before, "--restore-pollution --dry-run 不得写库"
    # 还原一次后再还原：没有可还原内容，且不得把已还原的状态改坏
    assert run(home, "--restore-pollution").returncode == 0
    second = run(home, "--restore-pollution", "--json")
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["restored_members"] == 0, second.stdout
    print("✓ 8. --restore-pollution 有 --dry-run，且重复还原是幂等空操作")


def main():
    print("=" * 72)
    print("        [门禁] issue #34 跨群主档污染审计 / 清理 / 还原")
    print("=" * 72)
    # 每项一个干净夹具：审计/清理是有顺序的写入，共用一份会互相污染断言
    cases = [
        ("wg-pollution-", test_audit_is_read_only, False),
        ("wg-pollution-", test_audit_numbers_match_sql, False),
        ("wg-pollution-", test_scrub_removes_only_overlap, False),
        ("wg-pollution-", test_group_view_unchanged_after_scrub, True),
        ("wg-pollution-", test_cross_group_leak_closed, True),
        ("wg-pollution-", test_backup_and_restore, True),
        ("wg-pollution-", test_dry_run_writes_nothing, False),
        ("wg-pollution-", test_restore_dry_run_and_idempotence, False),
    ]
    for prefix, test, needs_engine in cases:
        with temp_dir(prefix=prefix) as home:
            engine = build_fixture(home)
            test(home, engine) if needs_engine else test(home)
    print("=" * 72)
    print("✓ issue #34 污染审计 / 清理 / 还原 8 项门禁全部通过")
    print("=" * 72)


if __name__ == "__main__":
    main()
