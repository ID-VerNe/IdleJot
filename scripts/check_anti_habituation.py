"""Test suite for Phase 4: Persona expression diversity & anti-habituation detection."""
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from chatlocal import mcp_chat_prompts as prompts
from chatlocal.mcp_chat_output import compact_wait


def test_habit_detection():
    row = dict(conversation_id="111:group:222", participation="natural", persona="test persona", persona_name="测试")
    now = time.time()

    # Case 1: Diverse messages, no habituation
    recent_diverse = [
        dict(id=1, onebot_message_id=101, is_self=True, content="今天天气不错", received_at=now - 50),
        dict(id=2, onebot_message_id=102, is_self=False, sender_id=222, content="确实挺好", received_at=now - 40),
        dict(id=3, onebot_message_id=103, is_self=True, content="要不要出去转转", received_at=now - 20),
    ]
    batch = [dict(id=4, onebot_message_id=104, sender_id=222, content="走起", segments=[])]
    pkt_diverse = prompts.packet(row, recent_diverse, batch)
    assert not pkt_diverse["social_context"].get("repeated_phrases")
    assert any("【表达去模板化】" in r for r in pkt_diverse["reminders"])

    # Case 2: Repetitive suffix ("草")
    recent_habituated = [
        dict(id=1, onebot_message_id=101, is_self=True, content="这也太搞笑了草", received_at=now - 50),
        dict(id=2, onebot_message_id=102, is_self=False, sender_id=222, content="哈哈哈", received_at=now - 40),
        dict(id=3, onebot_message_id=103, is_self=True, content="绝了草", received_at=now - 20),
    ]
    pkt_habituated = prompts.packet(row, recent_habituated, batch)
    repeated = pkt_habituated["social_context"].get("repeated_phrases", [])
    assert "草" in repeated, f"Expected '草' in repeated_phrases, got {repeated}"
    assert any("【防语癖固化】" in r and "“草”" in r for r in pkt_habituated["reminders"])

    # Case 4: Repetitive suffix with punctuation and repeated characters
    recent_punctuated = [
        dict(id=1, onebot_message_id=101, is_self=True, content="这也太搞笑了草！", received_at=now - 50),
        dict(id=2, onebot_message_id=102, is_self=False, sender_id=222, content="确实", received_at=now - 40),
        dict(id=3, onebot_message_id=103, is_self=True, content="绝了草草草~", received_at=now - 20),
    ]
    pkt_punctuated = prompts.packet(row, recent_punctuated, batch)
    repeated_punc = pkt_punctuated["social_context"].get("repeated_phrases", [])
    assert "草" in repeated_punc, f"Expected '草' with punctuation/repetition, got {repeated_punc}"
    assert any("【防语癖固化】" in r and "“草”" in r for r in pkt_punctuated["reminders"])

    print("✓ Anti-habituation detection and diversity reminder test passed")


def test_compact_wait_habit_hints():
    """compact_wait keeps its own brief wording for both habit states.

    The full packet and the compact wire packet carry the same decision in
    two phrasings; this locks the compact side so a wording change on one
    side cannot silently drift from the other.
    """
    row = dict(conversation_id="111:group:222", participation="natural", persona="test persona", persona_name="测试")
    now = time.time()
    batch = [dict(id=4, onebot_message_id=104, sender_id=222, content="走起", segments=[])]

    def compact_reminders(recent):
        packet = prompts.packet(row, recent, batch)
        compact = compact_wait(dict(event='messages', decision_required=True, chat_guidance=packet))
        return compact['chat_guidance']['reminders']

    habituated = [
        dict(id=1, onebot_message_id=101, is_self=True, content="这也太搞笑了草", received_at=now - 50),
        dict(id=2, onebot_message_id=102, is_self=False, sender_id=222, content="哈哈哈", received_at=now - 40),
        dict(id=3, onebot_message_id=103, is_self=True, content="绝了草", received_at=now - 20),
    ]
    reminders = compact_reminders(habituated)
    assert any('【防语癖固化】' in r and '“草”' in r for r in reminders)
    assert any('最近发言连续出现重复尾缀' in r for r in reminders)
    assert not any('多次出现重复尾缀/单字口癖' in r for r in reminders), 'compact must keep its brief wording'

    diverse = [
        dict(id=1, onebot_message_id=101, is_self=True, content="今天天气不错", received_at=now - 50),
        dict(id=2, onebot_message_id=102, is_self=False, sender_id=222, content="确实挺好", received_at=now - 40),
        dict(id=3, onebot_message_id=103, is_self=True, content="要不要出去转转", received_at=now - 20),
    ]
    reminders = compact_reminders(diverse)
    assert any('【表达去模板化】' in r for r in reminders)
    assert any('根据语境灵活变换句式与语气。' in r for r in reminders)
    assert not any('单字或结尾' in r for r in reminders), 'compact must keep its brief wording'

    print("✓ compact_wait habit hint wording test passed")


if __name__ == "__main__":
    test_habit_detection()
    test_compact_wait_habit_hints()
    print("PASS: anti-habituation test suite passed.")
