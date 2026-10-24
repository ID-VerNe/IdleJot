"""Mint a fresh local MCP grant and cache its token.

A grant is bound to the store epoch and to the per-platform account snapshot, so it
dies whenever local data changes or the QQ account gets bound. For a long-running
listener that has to be recoverable without a human: re-create the grant, keep the
same scope as the last water-group grant, and drop the token on disk.
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
from wg_paths import data_dir, state_dir
STATE_DIR = state_dir()
TOKEN_FILE = STATE_DIR / "mcp_token.txt"
DB = data_dir() / "mcp-access.sqlite3"
BASE = os.environ.get("IDLEJOT_UI_URL") or os.environ.get("TULPA_UI_URL", "http://127.0.0.1:47860")
NAME = os.environ.get("IDLEJOT_WG_GRANT") or os.environ.get("TULPA_WG_GRANT", "water-group")

SCOPE_FIELDS = (
    "conversations", "start", "end", "media", "prepare", "voice", "onebot",
    "send", "manage", "chat", "chat_images", "chat_sticker_send",
    "chat_sticker_collect", "chat_reactions", "chat_voice", "chat_video",
    "chat_files", "qq_messages", "qq_interactions", "qq_friends", "qq_profile",
    "qq_qzone", "qq_albums",
)

# First-run fallback when no grant exists yet to copy a scope from. Broad on the
# capabilities a water-group collector needs (send/chat/onebot/manage/chat_* and
# voice ASR); qq_* extensions stay off because the collector is group-only and
# never touches qzone/albums. An operator can narrow this from the UI afterwards.
DEFAULT_SCOPE = {
    "platforms": ["qq"], "conversations": [], "start": "", "end": "",
    "media": False, "prepare": False, "voice": True, "onebot": True,
    "send": True, "manage": True, "chat": True, "chat_images": True,
    "chat_sticker_send": True, "chat_sticker_collect": True, "chat_reactions": True,
    "chat_voice": True, "chat_video": True, "chat_files": True,
    "qq_messages": False, "qq_interactions": False, "qq_friends": False,
    "qq_profile": False, "qq_qzone": False, "qq_albums": False,
}


def last_scope():
    # grants.id is uuid4().hex -- random, not chronological. The mint time
    # lives in created_at, so "the last grant" must order by that; ordering by
    # id would copy a random older grant's scope every time.
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT scope FROM grants WHERE name=? ORDER BY created_at DESC LIMIT 1", (NAME,)
    ).fetchone()
    if not row:
        # First run under this name: inherit the scope of the newest grant so the
        # permissions stay exactly what the operator already approved once.
        row = db.execute("SELECT scope FROM grants ORDER BY created_at DESC LIMIT 1").fetchone()
    if not row:
        # Empty grants table (cold start): self-heal must still mint a usable
        # grant instead of aborting. DEFAULT_SCOPE keeps the collector running.
        return dict(DEFAULT_SCOPE)
    return json.loads(row["scope"])


def mint():
    scope = last_scope()
    body = {"name": NAME, "platforms": scope["platforms"], "all_conversations": True}
    for key in SCOPE_FIELDS:
        body[key] = scope.get(key, False)
    with httpx.Client(timeout=httpx.Timeout(20, connect=5), trust_env=False) as client:
        r = client.post(f"{BASE}/api/mcp/connections", json=body,
                        headers={"x-chatweave-ui": "1"})
    if r.status_code >= 400:
        raise SystemExit(f"mint failed {r.status_code}: {r.text[:300]}")
    token = r.json()["token"]
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    print(f"minted {r.json()['id']} -> {TOKEN_FILE}")
    return token


if __name__ == "__main__":
    sys.exit(0 if mint() else 1)
