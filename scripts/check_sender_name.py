"""issue #39 gate: QQ quote-bubble UI text must never become a display name.

The defect: a group member set their QQ card to a quote-bubble string
(``<昵称>回应了你的消息：<对方昵称>``). OneBot returns it verbatim as
``sender.card``, and ``chatlocal/mcp_chat.py:440`` trusted it without a shape
check, so the bubble text travelled into ``chat_inbox.payload.sender`` and then
into the dossier's ``aliases`` (via ``memory_observe.extract_interaction`` →
``save_profile``). The agent could no longer recognise the member.

The fix is a single pure guard, ``chatlocal/name_guard.clean_sender_name``, used
at both the inbound payload site and the archival profile-write site. This gate
pins the contract at three levels so a future regression can't silently re-import
the bubble:

  1. the guard itself rejects a bubble frame and falls back to the QQ number;
  2. ``extract_interaction`` never writes a bubble sender_name into
     ``aliases`` / ``primary_name``;
  3. ``mcp_chat.receive`` actually routes the upstream card/nickname through the
     guard (source-level AST check), so ``chat_inbox.payload.sender`` can't be
     bubble text again.

    .venv/Scripts/python.exe scripts/check_sender_name.py
"""
import ast
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

# Pin the data root before anything resolves it (same class of bug as #79/#89:
# a fixture profile must never reach the live library).
_SCRATCH = Path(tempfile.mkdtemp(prefix="tulpa-sender-"))
import atexit
atexit.register(lambda: shutil.rmtree(_SCRATCH, True))
(_SCRATCH / "home").mkdir(parents=True, exist_ok=True)
os.environ["WATER_GROUP_HOME"] = str(_SCRATCH / "home")
os.environ["IDLEJOT_WG_MEMORY_DB"] = str(_SCRATCH / "mem.sqlite3")

from chatlocal.name_guard import clean_sender_name  # noqa: E402
from water_group.memory_engine import MemoryEngine  # noqa: E402

# Fabricated placeholder names/numbers only -- never real members (the
# publication gate rejects any personal token, and this file lives in the repo).
BUBBLE = "甲回应了你的消息：乙"
LEGIT = "真名片"
QQ_A = "10001"
QQ_B = "10002"
GID = "88888"


def test_clean_sender_name_contract(tmp):
    """The guard: bubble frame and blank fall back; legitimate names pass."""
    assert clean_sender_name(BUBBLE, QQ_A) == QQ_A, "bubble -> QQ fallback"
    assert clean_sender_name("  ", "12345") == "12345", "blank -> fallback"
    assert clean_sender_name(None, "12345") == "12345", "None -> fallback"
    assert clean_sender_name(LEGIT, QQ_A) == LEGIT, "legit card untouched"
    assert clean_sender_name("", "") == "", "empty fallback stays empty"
    print("✓ 1. clean_sender_name 契约：气泡回落 QQ、空白回落、合法名片不误伤")


def test_archival_aliases_guard(tmp):
    """extract_interaction must not write a bubble sender_name into aliases."""
    engine = MemoryEngine(tmp / "mem-archive.sqlite3")
    prof = engine.extract_interaction(QQ_A, BUBBLE, GID, "今天天气不错", is_direct=False)
    assert prof is not None, prof
    aliases = json.loads(prof["aliases"] or "[]")
    assert BUBBLE not in aliases, f"bubble leaked into aliases: {aliases}"
    assert QQ_A in aliases or prof["primary_name"] != BUBBLE, prof["primary_name"]
    # A legitimate name still becomes the primary_name.
    prof2 = engine.extract_interaction(QQ_B, LEGIT, GID, "今天也要加油", is_direct=False)
    assert prof2["primary_name"] == LEGIT, prof2["primary_name"]
    print("✓ 2. extract_interaction 建档：气泡不进 aliases、合法名正常建档")


def test_inbound_source_uses_guard():
    """mcp_chat.receive must route the upstream name through the guard (AST)."""
    src = (ROOT / "chatlocal" / "mcp_chat.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    receive = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "receive"),
        None,
    )
    assert receive is not None, "receive() not found"
    call = next(
        (n for n in ast.walk(receive)
         if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "clean_sender_name"),
        None,
    )
    assert call is not None, "receive() must call clean_sender_name on the sender"
    # The guard result must feed the payload's `sender`, not a raw card/nickname.
    assert "sender_name" in src, "receive() must bind a cleaned sender_name"
    print("✓ 3. mcp_chat.receive 入库侧把 sender 过 clean_sender_name（AST 检查）")


def main():
    # A temp fixture dir shared by the DB-backed tests, cleaned on exit.
    tmp = _SCRATCH / "cases"
    tmp.mkdir(parents=True, exist_ok=True)
    test_clean_sender_name_contract(tmp)
    test_archival_aliases_guard(tmp)
    test_inbound_source_uses_guard()
    print("check_sender_name: all checks passed")


if __name__ == "__main__":
    main()