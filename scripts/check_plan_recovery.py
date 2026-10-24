"""Test suite for Phase 5: plan_chat_reply rejection handling, KeyError: plan_id prevention, and stale replan."""
from pathlib import Path
import sys
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

from check_fixture_support import temp_dir
from chatlocal.mcp_chat_turns import ChatTurns, TurnRejected
from chatlocal.store import Store
from chatlocal.mcp_access import MCPAccess


def test_rejected_structure():
    with temp_dir(prefix='test-plan-rej-') as tmp:
        store = Store(Path(tmp) / 'chats.sqlite3')
        access = MCPAccess(store)
        mock_chat = MagicMock()
        mock_chat.access = access
        mock_chat.continuation.return_value = {"tool": "wait_chat_messages", "arguments": {}}
        mock_chat.row.return_value = {"prompt_delivered_version": "v1"}

        turns = ChatTurns(mock_chat)

        # 1. Test rejected on stale
        rej = turns.rejected(TurnRejected("stale", "目标相关的新消息尚未纳入决策，请补读并重新规划。"), sid="session-001")
        assert rej["state"] == "REJECTED"
        assert rej["error_code"] == "stale"
        # Crucial check: plan_id must exist as None to prevent KeyError: 'plan_id'
        assert "plan_id" in rej
        assert rej["plan_id"] is None
        assert "recovery_guide" in rej
        assert "严禁在 state!=READY 时调用 send_chat_reply" in rej["recovery_guide"]

        # 2. Test rejected on needs_wait
        rej_wait = turns.rejected(TurnRejected("needs_wait", "仍在收集消息", wait_seconds=5.0), sid="session-001")
        assert rej_wait["state"] == "REJECTED"
        assert rej_wait["plan_id"] is None
        assert rej_wait["recovery"]["action"] == "wait"
        assert rej_wait["recovery"]["retry_after_seconds"] == 5.0
        assert "recovery_guide" in rej_wait

        # 3. Test rejected on target_not_in_batch (with correction_call)
        corr = {"correction_call": {"tool": "plan_chat_reply", "arguments": {"session_id": "session-001"}}}
        rej_corr = turns.rejected(TurnRejected("target_not_in_batch", "目标不在本批", **corr), sid="session-001")
        assert rej_corr["state"] == "REJECTED"
        assert rej_corr["plan_id"] is None
        assert rej_corr["recovery"]["action"] == "correct_request"

        # 4. Test submit with plan_id=None (simulating agent blindly forwarding rejected plan_id)
        mock_grant = {"id": "grant-001", "scope": {"chat": True, "send": True}}
        mock_chat.validate.return_value = (mock_grant, {"id": "session-001", "active": 1, "conversation_id": "111:group:222"})
        import threading
        cancel = threading.Event()
        rej_submit = turns.submit(mock_grant, {"session_id": "session-001", "plan_id": None, "bubbles": []}, cancel)
        assert rej_submit["state"] == "REJECTED"
        assert rej_submit["plan_id"] is None
        assert rej_submit["error_code"] == "plan_required"
        assert "recovery_guide" in rej_submit

        print("✓ All rejected plan_id=None and recovery_guide structure tests passed!")


if __name__ == "__main__":
    test_rejected_structure()
    print("PASS: plan recovery test suite passed.")
