"""Comprehensive test suite for group member dynamic memory & personalized targeted reply system.

Tests:
1. SQLite initialization, migrations, and schema creation in MemoryEngine.
2. Auto-seeding from persona markdown (占位种子 A/B).
3. Member profile search and alias resolution.
4. Explicit command parser (various patterns and negative cases).
5. Explicit command execution and memory updates.
6. Direct interaction piggyback extraction (preferences and identity claims).
7. Idle digest background extraction.
8. Tier 1 (L1 Context) compact synthesized card generation (~80 tokens).
9. Tier 2 (L2 DB Storage) query_member_dossier with group-scoped boundary enforcement.
10. MCP integration: CHAT_TOOLS, schemas, compact_wait preservation, and tool dispatch.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Import through the bridge package so this suite does not create a second
# top-level copy of the engine under the name `memory_engine`.
from water_group.memory_engine import MEMORY_SLOTS, MemoryEngine
from water_group.memory_format import merge_text_list, render_l1_card
from chatlocal.mcp_chat import CHAT_TOOLS, schemas as chat_schemas
from chatlocal.mcp_chat_output import compact_wait

# The private card is only an existence switch -- `seed_from_persona()` never
# parses it. Production reads `a private persona card (gitignored)`, which is
# gitignored, so a clean checkout (CI runner included) has no card at all.
# Tests therefore write their own stub and pass it explicitly instead of
# depending on whatever the developer's home directory happens to hold.
PERSONA_STUB_TEXT = "# persona stub\n\n种子数据由 seed_from_persona() 内的代码常量提供，卡片内容不参与解析。\n"


def persona_stub(tmp_dir):
    """Returns the path to a stub persona card inside the test sandbox."""
    stub = Path(tmp_dir) / "personas" / "example_persona.md"
    if not stub.exists():
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text(PERSONA_STUB_TEXT, encoding="utf-8")
    return stub


def test_schema_and_init(tmp_dir):
    db_file = tmp_dir / "test_memories.sqlite3"
    engine = MemoryEngine(db_file)
    assert db_file.exists(), "DB file must be created"
    assert engine.get_member_count() == 0, "Initial count should be 0 before seeding"
    with engine._connect() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal", f"Expected WAL journal mode, got {mode}"
    print("✓ 1. SQLite 架构初始化与建表及 WAL 模式测试通过")


def test_seeding(tmp_dir):
    db_file = tmp_dir / "test_memories.sqlite3"
    engine = MemoryEngine(db_file)
    # Explicit stub path: the seeds are code constants, the card only has to exist.
    count = engine.seed_from_persona(persona_path=persona_stub(tmp_dir))
    assert count == 2, f"Expected 2 seeded members, got {count}"
    assert engine.get_member_count() == 2

    # Verify 示例群友A
    a_prof = engine.find_profile("示例群友A")
    assert a_prof is not None, "示例群友A profile must exist"
    assert a_prof["primary_name"] == "示例群友A"
    aliases = json.loads(a_prof["aliases"])
    assert "示例A" in aliases and "示例群友A" in aliases
    interests = json.loads(a_prof["interests"])
    assert "示例游戏" in interests and "示例游戏2" in interests
    memories = json.loads(a_prof["key_memories"])
    assert any("话痨" in m for m in memories)

    # Verify 示例群友B
    b_prof = engine.find_profile("示例群友B")
    assert b_prof is not None, "示例群友B profile must exist"
    b_aliases = json.loads(b_prof["aliases"])
    assert "示例群友B" in b_aliases and "示例B" in b_aliases
    b_interests = json.loads(b_prof["interests"])
    assert any("示例XP" in i for i in b_interests)

    # Re-seeding idempotency
    count2 = engine.seed_from_persona(persona_path=persona_stub(tmp_dir))
    assert count2 == 0, "Idempotent re-seed should not insert new rows"
    assert engine.get_member_count() == 2

    # Explicit path is an existence switch: a missing card seeds nothing and
    # leaves the existing rows alone.
    missing = engine.seed_from_persona(persona_path=tmp_dir / "no-such-persona.md")
    assert missing == 0, "Explicit missing persona path must seed nothing"
    assert engine.get_member_count() == 2
    print("✓ 2. 角色卡预置成员档案自动种子测试通过 (示例群友A / 示例群友B)")


def test_alias_matching(tmp_dir):
    db_file = tmp_dir / "test_memories.sqlite3"
    engine = MemoryEngine(db_file)
    engine.seed_from_persona(persona_path=persona_stub(tmp_dir))

    # Exact name match
    assert engine.find_profile("示例群友A")["user_id"] == "seed:example_a"
    assert engine.find_profile("示例群友B")["user_id"] == "seed:example_b"

    # Alias matches for 示例群友A
    assert engine.find_profile("示例A")["primary_name"] == "示例群友A"

    # Alias matches for 示例群友B
    assert engine.find_profile("示例B")["primary_name"] == "示例群友B"

    # Anti-Hijacking and negative safety tests:
    # 1. Single character queries must NEVER match multi-character profiles
    assert engine.find_profile("示") is None, "Single character '示' must not match '示例群友A'"
    assert engine.find_profile("群") is None, "Single character '群' must not match '示例群友B'"
    # 2. Sentences/phrases containing alias must not match
    assert engine.find_profile("示例群友A今天怎么了") is None, "a phrase must not match a seed profile"
    # 3. Nonexistent
    assert engine.find_profile("路人甲") is None
    print("✓ 3. 成员外号与多别名智能检索及防劫持测试通过")


def test_explicit_commands(tmp_dir):
    db_file = tmp_dir / "test_memories.sqlite3"
    engine = MemoryEngine(db_file)
    engine.seed_from_persona(persona_path=persona_stub(tmp_dir))

    # Pattern 1: @机器人 记在小本本上 [某人] 其实 [喜好/事实]
    c1 = engine.parse_explicit_command("@机器人 记在小本本上 示例群友B 其实最喜欢健身了")
    assert c1["is_command"] is True
    assert c1["target"] == "示例群友B"
    assert "最喜欢健身了" in c1["fact"]

    # Pattern 2: Verb preservation: "示例群友B喜欢示例XP" -> fact must preserve "喜欢" (not just "示例XP"!)
    c2 = engine.parse_explicit_command("@机器人 记在小本本上 示例群友B喜欢示例XP")
    assert c2["is_command"] is True
    assert c2["target"] == "示例群友B"
    assert "喜欢示例XP" in c2["fact"]

    # Pattern 3: @bot invocation
    c3 = engine.parse_explicit_command("@123456 记在小本本上 示例群友B 其实喜欢跑步")
    assert c3["is_command"] is True
    assert c3["target"] == "示例群友B"
    assert "喜欢跑步" in c3["fact"]

    # Pattern 4: Punctuation after bot name
    c4 = engine.parse_explicit_command("@机器人，记在小本本上 示例群友B其实爱女装")
    assert c4["is_command"] is True
    assert c4["target"] == "示例群友B"
    assert "爱女装" in c4["fact"]

    c4b = engine.parse_explicit_command("@机器人: 记在小本本上 示例群友B喜欢健身")
    assert c4b["is_command"] is True
    assert c4b["target"] == "示例群友B"
    assert "喜欢健身" in c4b["fact"]

    # Pattern 5: Conversational prefixes (快记在 / 帮我记在 / 记小本本)
    c5 = engine.parse_explicit_command("快记在小本本上 示例群友B喜欢健身")
    assert c5["is_command"] is True
    assert c5["target"] == "示例群友B"

    c6 = engine.parse_explicit_command("帮我记在小本本上 示例群友A爱玩示例游戏")
    assert c6["is_command"] is True
    assert c6["target"] == "示例群友A"
    assert "爱玩示例游戏" in c6["fact"]

    c7 = engine.parse_explicit_command("记小本本上 示例群友A是个大话痨")
    assert c7["is_command"] is True
    assert c7["target"] == "示例群友A"
    assert "大话痨" in c7["fact"]

    # Pattern 8: Colon delimiter
    c8 = engine.parse_explicit_command("@机器人在小本本上写上 给老王记上一笔：天天摸鱼打卡")
    assert c8["is_command"] is True
    assert c8["target"] == "老王"
    assert "天天摸鱼打卡" in c8["fact"]

    # Negative patterns
    assert engine.parse_explicit_command("今天天气真不错啊")["is_command"] is False
    assert engine.parse_explicit_command("谁有那个小本子看下")["is_command"] is False
    assert engine.parse_explicit_command("记在心上")["is_command"] is False

    # Command Execution: verify predicate preservation in fact, memory, and ack_hint
    res = engine.handle_explicit_command("123456", "测试发言人", "group_100", "@机器人 记在小本本上 示例群友B喜欢猛男")
    assert res is not None
    assert res["target"] == "示例群友B"
    assert res["fact"] == "喜欢猛男"
    assert "记在小本本上了！这就给 示例群友B 记上一笔：喜欢猛男" in res["ack_hint"]

    # Check 示例群友B profile updated (group-scoped overlay)
    b_prof = engine.find_profile("示例群友B", group_id="group_100")
    group_row = b_prof.get("_group") if isinstance(b_prof, dict) else None
    mems = json.loads((group_row["key_memories"] if group_row else b_prof["key_memories"]) or "[]")
    assert any("喜欢猛男" in m for m in mems)
    print("✓ 4. 显式小本本指令解析与执行测试通过 (含真实@提及、谓词完整性保留与标点支持)")


def test_interaction_extraction(tmp_dir):
    db_file = tmp_dir / "test_memories.sqlite3"
    engine = MemoryEngine(db_file)
    engine.seed_from_persona(persona_path=persona_stub(tmp_dir))

    # Direct interaction with first-person declaration
    prof = engine.extract_interaction("999888", "群友阿强", "group_100", "我平时最喜欢玩黑神话悟空了", is_direct=True)
    assert prof is not None
    assert prof["primary_name"] == "群友阿强"
    interests = json.loads(prof["interests"])
    assert any("黑神话" in i for i in interests)

    # ANTI-HIJACKING SAFETY TEST:
    # A random stranger with nickname "月" or "领导发奖金" must NOT hijack seed:example_a!
    impostor_prof = engine.extract_interaction("555444", "月", "group_100", "今天真开心", is_direct=False)
    assert impostor_prof["primary_name"] == "月"
    assert impostor_prof["user_id"] == "555444"
    # Verify seed:example_a is untouched
    a_check = engine.find_profile("示例群友A")
    assert a_check["user_id"] == "seed:example_a", "seed:example_a must NOT be hijacked by user named '月'"

    # Pre-record an event under seed:example_b to test event migration on legitimate binding
    engine.record_event("seed:example_b", "group_100", "示例群友B黑历史", "被群友调侃", "seed_data")

    # Legitimate binding: speaker with exact alias "示例群友B" binds to seed:example_b
    b_bind = engine.extract_interaction("777666", "示例群友B", "group_100", "我才不是搞笑呢", is_direct=True)
    assert b_bind["primary_name"] == "示例群友B"
    assert "777666" in json.loads(b_bind["aliases"])
    # Can now find by QQ number
    found_by_qq = engine.find_profile("777666")
    assert found_by_qq is not None
    assert found_by_qq["primary_name"] == "示例群友B"
    # Verify events were migrated to real QQ
    dossier = engine.query_member_dossier("777666", "group_100")
    assert any("示例群友B黑历史" in e["quote"] for e in dossier["recent_events"])
    print("✓ 5. 直接互动捎带提取、QQ号动态绑定与防冒名劫持测试通过")


def test_idle_digest(tmp_dir):
    db_file = tmp_dir / "test_memories.sqlite3"
    engine = MemoryEngine(db_file)
    engine.seed_from_persona(persona_path=persona_stub(tmp_dir))

    messages = [
        {"sender_id": "111", "sender": "群友小明", "content": "草 乐 难绷 启动！"},
        {"sender_id": "222", "sender": "群友小华", "content": "示例游戏启动！今天蒸了吗？"},
        {"sender_id": "333", "sender": "群友小李", "content": "@机器人 记在小本本上 示例群友A天天在群里装纯"},
    ]

    res = engine.digest_idle_batch("group_100", messages)
    assert res["digested_count"] == 3
    assert "示例群友A" in res["updated_members"]

    # Verify that explicit command in idle batch was processed (group-scoped)
    a_prof = engine.find_profile("示例群友A", group_id="group_100")
    group_row = a_prof.get("_group") if isinstance(a_prof, dict) else None
    mems = json.loads((group_row["key_memories"] if group_row else a_prof["key_memories"]) or "[]")
    assert any("装纯" in m for m in mems)

    # DEDUPLICATION CHECK:
    # Run idle digest a second time on the same window:
    # Events must NOT be duplicated in member_events table!
    with engine._connect() as conn:
        count_before = conn.execute("SELECT COUNT(*) FROM member_events").fetchone()[0]

    engine.digest_idle_batch("group_100", messages)

    with engine._connect() as conn:
        count_after = conn.execute("SELECT COUNT(*) FROM member_events").fetchone()[0]

    assert count_after == count_before, f"Events must not duplicate across idle runs! Before: {count_before}, After: {count_after}"
    print("✓ 6. 空闲后台轻量摘要与多轮重复调用防冗余去重测试通过")


def test_l1_context_generation(tmp_dir):
    db_file = tmp_dir / "test_memories.sqlite3"
    engine = MemoryEngine(db_file)
    engine.seed_from_persona(persona_path=persona_stub(tmp_dir))

    # Generate L1 compact card for 示例群友B and 示例群友A
    cards = engine.build_l1_context("group_100", user_names=["示例群友B", "示例群友A"])
    assert len(cards) == 2, f"Expected 2 cards, got {len(cards)}"

    for card in cards:
        assert card.startswith("【群友档案：")
        assert "外号:" in card
        assert "风格:" in card
        assert "XP喜好:" in card
        assert "核心记忆:" in card
        # Compactness check: must be concise (~50-150 characters, ~80 tokens)
        assert len(card) < 250, f"Card too long ({len(card)} chars): {card}"

    # Issue #60: the card keeps two memories but reads the freshest tail, so a
    # larger storage cap cannot make the card stale.
    synthetic = {
        "user_id": "123450060",
        "primary_name": "测试群友",
        "aliases": "[]",
        "tone_style": "",
        "interests": "[]",
        "traits": "{}",
        "key_memories": json.dumps(["最旧事实", "中间事实", "最新事实"], ensure_ascii=False),
    }
    card = render_l1_card(synthetic)
    assert "中间事实" in card and "最新事实" in card, f"L1 应取最近两条: {card}"
    assert "最旧事实" not in card, f"L1 不应再取最旧条目: {card}"

    print("✓ 7. Tier 1 (L1 Context) 超精简画像卡片生成测试通过 (~80 tokens，取最近两条)")


def test_l2_dossier_and_boundary_rules(tmp_dir):
    db_file = tmp_dir / "test_memories.sqlite3"
    engine = MemoryEngine(db_file)
    engine.seed_from_persona(persona_path=persona_stub(tmp_dir))

    # Record private event in group_A
    b_prof = engine.find_profile("示例群友B")
    assert b_prof is not None
    uid = b_prof["user_id"]

    # Record private event in group_A
    engine.record_event(
        user_id=uid,
        group_id="group_A",
        quote="谁有大胸健身照片私我",
        context="在群A索要健身照",
        source="interaction",
        tags=["健身"],
    )

    # Record private event in group_B
    engine.record_event(
        user_id=uid,
        group_id="group_B",
        quote="今天群B好无聊",
        context="在群B抱怨无聊",
        source="interaction",
        tags=["日常"],
    )

    # Query dossier scoped to group_A
    dossier_a = engine.query_member_dossier("示例群友B", group_id="group_A")
    assert dossier_a["found"] is True
    # Shared personality profile
    assert dossier_a["profile"]["primary_name"] == "示例群友B"
    assert "示例B" in dossier_a["profile"]["aliases"]
    # Group-scoped private event isolation
    events_a = dossier_a["recent_events"]
    assert len(events_a) == 1
    assert events_a[0]["group_id"] == "group_A"
    assert "群A" in events_a[0]["context"]

    # Query dossier scoped to group_B
    dossier_b = engine.query_member_dossier("示例群友B", group_id="group_B")
    events_b = dossier_b["recent_events"]
    assert len(events_b) == 1
    assert events_b[0]["group_id"] == "group_B"
    assert "群B" in events_b[0]["context"]

    # Query without group_id returns all events across groups
    dossier_all = engine.query_member_dossier("示例群友B")
    assert len(dossier_all["recent_events"]) >= 2
    all_groups = {e["group_id"] for e in dossier_all["recent_events"]}
    assert "group_A" in all_groups and "group_B" in all_groups

    print("✓ 8. Tier 2 (L2 DB) query_member_dossier 与群聊私密事件边界隔离测试通过")


def test_native_dossier_updates_and_consolidation(tmp_dir):
    db_path = tmp_dir / "test_native_updates.sqlite3"
    engine = MemoryEngine(db_path)
    engine.seed_from_persona(persona_path=persona_stub(tmp_dir))

    # 1. Test update_member_dossier for existing profile (示例群友A)
    update_res = engine.update_member_dossier(
        identifier="示例群友A",
        key_memories=["经常在深夜偷偷玩示例游戏2"],
        role="至尊别名A2",
        interests=["示例", "反差话痨"],
    )
    assert update_res["success"] is True
    assert update_res["primary_name"] == "示例群友A"
    prof = engine.find_profile("示例群友A")
    memories = json.loads(prof["key_memories"])
    traits = json.loads(prof["traits"])
    interests = json.loads(prof["interests"])
    assert any("经常在深夜偷偷玩示例游戏2" in m for m in memories)
    assert traits.get("role") == "至尊别名A2"
    assert "示例" in interests

    # 2. Test update_member_dossier for a brand new member
    new_res = engine.update_member_dossier(
        identifier="999888",
        primary_name="新群友老王",
        aliases=["老王", "隔壁老王"],
        key_memories=["喜欢研究AI大模型架构"],
        role="技术架构师",
        tone_style="严谨技术宅",
    )
    assert new_res["success"] is True
    new_prof = engine.find_profile("999888")
    assert new_prof is not None
    assert new_prof["primary_name"] == "新群友老王"
    assert "隔壁老王" in json.loads(new_prof["aliases"])
    assert new_prof["tone_style"] == "严谨技术宅"

    # 3. Test consolidate_chat_memory in batch
    con_res = engine.consolidate_chat_memory(
        group_id="group_test_888",
        records=[
            {
                "identifier": "999888",
                "key_memories": ["新加入Tulpa项目讨论"],
                "event": {
                    "quote": "大家晚上好，今天聊聊MCP",
                    "context": "首次在群内技术发言",
                    "tags": ["技术交流"],
                },
            }
        ],
        events=[
            {
                "identifier": "示例群友A",
                "quote": "示例游戏启动！",
                "context": "深夜还在聊天",
                "tags": ["示例游戏", "摸鱼"],
            }
        ],
        summary="群内热烈探讨了MCP协议与记忆归档方案",
    )
    assert con_res["success"] is True
    assert con_res["updated_profiles_count"] >= 1
    assert con_res["recorded_events_count"] == 3  # 1 record event + 1 independent event + 1 summary

    # 4. Edge-case test: string arguments for list fields, overwrite clearing, and per-event group_id
    str_arg_res = engine.update_member_dossier(
        identifier="999888",
        aliases="技术达人",  # String instead of list
        interests="大语言模型",  # String instead of list
        key_memories="单条核心记忆测试",  # String instead of list
    )
    assert str_arg_res["success"] is True
    assert "技术达人" in str_arg_res["profile"]["aliases"]
    assert "大语言模型" in str_arg_res["profile"]["interests"]
    assert "单条核心记忆测试" in str_arg_res["profile"]["key_memories"]
    # Ensure string wasn't split character-by-character into ['单', '条', '核', ...]
    assert "单" not in str_arg_res["profile"]["key_memories"]

    # Test explicit overwrite clearing
    clear_res = engine.update_member_dossier(
        identifier="999888",
        key_memories=[],
        overwrite=True,
    )
    assert clear_res["success"] is True
    assert clear_res["profile"]["key_memories"] == [], "key_memories should be cleared in overwrite mode with empty list"

    # Test consolidate_chat_memory with per-event group_id
    con_scoped_res = engine.consolidate_chat_memory(
        events=[
            {
                "identifier": "999888",
                "group_id": "group_scoped_777",
                "quote": "测试按事件划分群组",
                "context": "独立群事件",
            }
        ]
    )
    assert con_scoped_res["recorded_events_count"] == 1
    dossier_scoped = engine.query_member_dossier("999888", group_id="group_scoped_777")
    assert any("测试按事件划分群组" in e["quote"] for e in dossier_scoped["recent_events"])

    print("✓ 9. MCP 原生档案更新 update_member_dossier 与批量记忆归档 consolidate_chat_memory 测试通过")


def test_sync_onebot_members_primary_name(tmp_dir):
    """Issue #37: a new profile must not freeze a volatile group card as primary_name."""
    engine = MemoryEngine(tmp_dir / "test_sync_primary_name.sqlite3")

    def member(uid, nickname="", card=""):
        return {"user_id": uid, "nickname": nickname, "card": card, "role": "member"}

    # 1. Nickname and card both present and different -> the nickname wins.
    engine.sync_onebot_members([member(10001, nickname="阿橘", card="今晚吃火锅…")], group_id="group_sync")
    prof = engine.find_profile("10001")
    assert prof["primary_name"] == "阿橘", "New profile must take nickname as primary_name, not the card"
    aliases = json.loads(prof["aliases"])
    assert "今晚吃火锅…" in aliases, "Card must still be recorded as an alias"
    traits = json.loads(prof["traits"])
    assert traits["card"] == "今晚吃火锅…" and traits["nickname"] == "阿橘"

    # 2. Card only -> fall back to the card.
    engine.sync_onebot_members([member(10002, card="老王")], group_id="group_sync")
    assert engine.find_profile("10002")["primary_name"] == "老王"

    # 3. Neither -> fall back to the QQ number.
    engine.sync_onebot_members([member(10003)], group_id="group_sync")
    assert engine.find_profile("10003")["primary_name"] == "10003"

    # 4. Re-sync after a card change -> primary_name stays, the new card joins aliases.
    engine.sync_onebot_members([member(10001, nickname="阿橘", card="橘座驾到")], group_id="group_sync")
    prof = engine.find_profile("10001")
    assert prof["primary_name"] == "阿橘", "Card churn must never rewrite an existing primary_name"
    assert "橘座驾到" in json.loads(prof["aliases"])
    assert json.loads(prof["traits"])["card"] == "橘座驾到"

    print("✓ 10. OneBot 成员同步建档主名优先取昵称、名片仅进别名测试通过")


def test_memory_cap_consistency(tmp_dir):
    """Issues #43/#60: one cap for every write path, recency-based eviction.

    #43 unified the hardcoded ``[:5]`` ceilings under ``MEMORY_SLOTS``. #60
    raises that ceiling and turns the merged list into a recency queue: a fact
    that is restated moves to the tail, so overflow drops the fact that has
    gone longest without being mentioned. The L1 card keeps its two-entry
    prompt budget and reads the tail, so a larger storage cap cannot make the
    card stale.
    """
    engine = MemoryEngine(tmp_dir / "test_memory_caps.sqlite3")
    uid = "123450001"
    probes = [f"[P] 探针事实{i:02d}" for i in range(1, 9)]

    res = engine.update_member_dossier(identifier=uid, primary_name="探针群友", key_memories=probes)
    assert res["success"] is True
    assert res["profile"]["key_memories"] == probes

    # 1. A save_profile call that passes no memories must leave the column alone.
    engine.save_profile(user_id=uid, primary_name="探针群友")
    assert json.loads(engine.find_profile(uid)["key_memories"]) == probes, (
        "save_profile(key_memories=None) 不得改写记忆列"
    )

    # 2. A save_profile call that passes one memory appends, keeping all eight.
    engine.save_profile(user_id=uid, primary_name="探针群友", key_memories=["[P] 探针事实09"])
    memories = json.loads(engine.find_profile(uid)["key_memories"])
    assert memories == probes + ["[P] 探针事实09"], f"追加后应保留 9 条，实际 {len(memories)} 条"

    # 3. Fill one past the cap; the least recently restated entry goes.
    fill = [f"[P] 探针事实{i:02d}" for i in range(10, MEMORY_SLOTS + 2)]
    engine.save_profile(user_id=uid, primary_name="探针群友", key_memories=fill)
    memories = json.loads(engine.find_profile(uid)["key_memories"])
    assert len(memories) == MEMORY_SLOTS, f"上限应为 {MEMORY_SLOTS} 条，实际 {len(memories)} 条"
    assert memories[-1] == f"[P] 探针事实{MEMORY_SLOTS + 1:02d}"
    assert "[P] 探针事实01" not in memories

    # 4. Restating the oldest fact refreshes it; the next oldest is evicted.
    engine.save_profile(user_id=uid, primary_name="探针群友", key_memories=["[P] 探针事实02"])
    memories = json.loads(engine.find_profile(uid)["key_memories"])
    assert memories[-1] == "[P] 探针事实02", f"复述的条目应移到尾部: {memories[-3:]}"
    engine.save_profile(
        user_id=uid,
        primary_name="探针群友",
        key_memories=[f"[P] 探针事实{MEMORY_SLOTS + 2:02d}"],
    )
    memories = json.loads(engine.find_profile(uid)["key_memories"])
    assert "[P] 探针事实02" in memories, "刚被复述的条目不得先被淘汰"
    assert "[P] 探针事实03" not in memories, f"应淘汰最久未复述的条目: {memories}"

    # 4b. A longer restatement absorbs the shorter wording and moves to the tail.
    merged = merge_text_list(
        ["听八卦", "打示例游戏"], ["爱听八卦并复述"], refresh_restated=True
    )
    assert merged == ["打示例游戏", "爱听八卦并复述"], merged

    # 5. Idle digestion on a keyword hit must not shrink the dossier.
    before_digest = json.loads(engine.find_profile(uid)["key_memories"])
    engine.digest_idle_batch(
        "group_caps",
        [{"sender_id": uid, "sender": "探针群友", "content": "示例游戏启动，今天示例"}],
    )
    assert json.loads(engine.find_profile(uid)["key_memories"]) == before_digest, (
        "空闲消化不得截断档案记忆"
    )

    # 6. `#记在小本本` appends under the same cap instead of rewriting the head five.
    engine.handle_explicit_command("999000111", "路人", "group_caps", "@机器人 记在小本本上 探针群友 喜欢猛男")
    prof = engine.find_profile("探针群友", group_id="group_caps")
    group_row = prof.get("_group") if isinstance(prof, dict) else None
    memories = json.loads((group_row["key_memories"] if group_row else prof["key_memories"]) or "[]")
    assert memories[-1] == "小本本记录: 喜欢猛男", f"小本本指令应追加到尾部: {memories[-3:]}"
    assert len(memories) == MEMORY_SLOTS

    # 7. Member-mapping sync (the card path) obeys the same ceiling.
    engine.sync_member_mappings({uid: {"name": "探针群友", "desc": "名片同步写入的新事实"}})
    memories = json.loads(engine.find_profile(uid)["key_memories"])
    assert "名片同步写入的新事实" in memories, "名片同步写入的记忆必须落库"
    assert len(memories) == MEMORY_SLOTS, f"名片同步应保持 {MEMORY_SLOTS} 条，实际 {len(memories)} 条"

    # 8. Re-seeding an already-populated profile must not collapse it back either.
    engine.seed_from_persona(persona_path=persona_stub(tmp_dir))
    a_id = engine.find_profile("示例群友A")["user_id"]
    engine.save_profile(
        user_id=a_id,
        primary_name="示例群友A",
        key_memories=[f"种子追加{i}" for i in range(1, 7)],
    )
    before_reseed = json.loads(engine.find_profile("示例群友A")["key_memories"])
    assert len(before_reseed) > 5
    engine.seed_from_persona(persona_path=persona_stub(tmp_dir))
    after_reseed = json.loads(engine.find_profile("示例群友A")["key_memories"])
    assert sorted(after_reseed) == sorted(before_reseed), "重复种子不得截断或改写已有记忆"

    # 9. The L2 query returns the whole capped list, not a page of it.
    dossier = engine.query_member_dossier(uid)
    assert len(dossier["profile"]["key_memories"]) == MEMORY_SLOTS

    # 10. Source guard: the hardcoded ceiling must not come back.
    source = (ROOT / "water-group" / "memory_engine.py").read_text(encoding="utf-8")
    assert "[:5]" not in source, "memory_engine.py 不得再出现硬编码 [:5] 截断"

    print("✓ 11. 画像记忆上限统一为 MEMORY_SLOTS、复述续命与溢出淘汰测试通过 (issue #43/#60)")


def test_mcp_integration():
    # 1. Check CHAT_TOOLS includes query_member_dossier, update_member_dossier, consolidate_chat_memory
    assert "query_member_dossier" in CHAT_TOOLS, "query_member_dossier must be in CHAT_TOOLS"
    assert "update_member_dossier" in CHAT_TOOLS, "update_member_dossier must be in CHAT_TOOLS"
    assert "consolidate_chat_memory" in CHAT_TOOLS, "consolidate_chat_memory must be in CHAT_TOOLS"

    # 2. Check schemas includes dossiers tools with valid schemas
    from chatlocal.agent_tools import tool
    schemas = chat_schemas(tool)
    dossier_tool = next((s for s in schemas if s["name"] == "query_member_dossier"), None)
    assert dossier_tool is not None, "query_member_dossier schema must exist"
    assert "identifier" in dossier_tool["parameters"]["properties"]
    assert "group_id" in dossier_tool["parameters"]["properties"]

    update_tool = next((s for s in schemas if s["name"] == "update_member_dossier"), None)
    assert update_tool is not None, "update_member_dossier schema must exist"
    assert "identifier" in update_tool["parameters"]["properties"]
    assert "key_memories" in update_tool["parameters"]["properties"]
    assert "traits" in update_tool["parameters"]["properties"]

    consolidate_tool = next((s for s in schemas if s["name"] == "consolidate_chat_memory"), None)
    assert consolidate_tool is not None, "consolidate_chat_memory schema must exist"
    assert "records" in consolidate_tool["parameters"]["properties"]
    assert "events" in consolidate_tool["parameters"]["properties"]
    assert "summary" in consolidate_tool["parameters"]["properties"]

    # 3. Check compact_wait retains member_dossiers AND injects prompt guidance into reminders
    sample_packet = {
        "event": "messages",
        "chat_guidance": {
            "prompt_version": "v1",
            "persona_name": "示例机器人",
            "member_dossiers": ["【群友档案：示例群友B】外号: 示例B | XP: 示例XP"],
            "social_context": {},
        },
    }
    compacted = compact_wait(sample_packet)
    assert "member_dossiers" in compacted["chat_guidance"]
    assert compacted["chat_guidance"]["member_dossiers"] == ["【群友档案：示例群友B】外号: 示例B | XP: 示例XP"]
    assert any("小本本画像" in r for r in compacted["chat_guidance"]["reminders"]), "compact_wait must deliver dynamic memory reminder to Agent"
    print("✓ 12. MCP 协议端工具声明、参数校验与 compact_wait 交付及提示词注入测试通过")



def test_per_group_memory_isolation(tmp_dir):
    engine = MemoryEngine(tmp_dir / "per_group.sqlite3")
    engine.seed_from_persona(persona_path=persona_stub(tmp_dir))
    uid = "777888999"
    engine.update_member_dossier(identifier=uid, primary_name="测试员", key_memories=["全局事实"])
    engine.update_member_dossier(identifier=uid, primary_name="测试员", key_memories=["A群事实"], group_id="group_A")
    engine.update_member_dossier(identifier=uid, primary_name="测试员", key_memories=["B群事实"], group_id="group_B")

    da = engine.query_member_dossier(uid, group_id="group_A")
    db = engine.query_member_dossier(uid, group_id="group_B")
    dg = engine.query_member_dossier(uid)
    assert "A群事实" in da["profile"]["key_memories"] and "B群事实" not in da["profile"]["key_memories"], str(da["profile"]["key_memories"])
    assert "B群事实" in db["profile"]["key_memories"] and "A群事实" not in db["profile"]["key_memories"], str(db["profile"]["key_memories"])
    assert da["memory_scope"] == "group" and db["memory_scope"] == "group"
    assert "全局事实" in dg["profile"]["key_memories"]

    cards_a = engine.build_l1_context("group_A", user_ids=[uid])
    cards_b = engine.build_l1_context("group_B", user_ids=[uid])
    assert any("A群事实" in c for c in cards_a), str(cards_a)
    assert any("B群事实" in c for c in cards_b), str(cards_b)
    print("ok  13. 群友画像记忆按群隔离测试通过")



def main():
    print("========================================================================")
    print("        [测试] Tulpa 群友动态小本本记忆 & 专属调侃系统验证")
    print("========================================================================")
    with tempfile.TemporaryDirectory(dir=str(ROOT / ".tmp"), prefix="mem-check-") as tmp:
        tmp_dir = Path(tmp)
        test_schema_and_init(tmp_dir)
        test_seeding(tmp_dir)
        test_alias_matching(tmp_dir)
        test_explicit_commands(tmp_dir)
        test_interaction_extraction(tmp_dir)
        test_idle_digest(tmp_dir)
        test_l1_context_generation(tmp_dir)
        test_l2_dossier_and_boundary_rules(tmp_dir)
        test_native_dossier_updates_and_consolidation(tmp_dir)
        test_sync_onebot_members_primary_name(tmp_dir)
        test_memory_cap_consistency(tmp_dir)
        test_per_group_memory_isolation(tmp_dir)
        test_mcp_integration()
    print("========================================================================")
    print("✓ 全部 12 项核心测试均通过！")
    print("========================================================================")


if __name__ == "__main__":
    main()
