"""Ensure a live chat session exists for the water group and cache its id.

A session belongs to the MCP grant that created it, so refreshing a revoked grant
also drops the session. Re-creating it here means the collector keeps running across
grant rotation without anyone re-issuing anything by hand.
"""
import os
import sys
import uuid
from pathlib import Path

import httpx

import mcp_client

ROOT = Path(__file__).resolve().parents[2]
from wg_paths import state_dir
STATE_DIR = state_dir()
SID_FILE = STATE_DIR / "wg_session.txt"


def _config():
    """读 water-group/config.json（gitignore，私有）。读不到就当空配置。"""
    try:
        import json
        return json.loads((ROOT / "water-group" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _default_target():
    """目标群与人设的默认值从配置里取，不在代码里写死群号。

    优先级：环境变量（多群/测试） > config.json 里第一个 enabled 的群 > 空。
    """
    cfg = _config()
    groups = cfg.get("groups") or {}
    account = str(cfg.get("self_account") or "")
    for gid, item in groups.items():
        if isinstance(item, dict) and item.get("enabled"):
            return f"{account}:group:{gid}", item.get("persona") or cfg.get("default_persona") or ""
    return "", cfg.get("default_persona") or ""


_CONVERSATION, _PERSONA = _default_target()
GROUP = os.environ.get("IDLEJOT_WG_CONVERSATION") or os.environ.get("TULPA_WG_CONVERSATION", _CONVERSATION)
PERSONA = os.environ.get("IDLEJOT_WG_PERSONA") or os.environ.get("TULPA_WG_PERSONA", _PERSONA)
UI = os.environ.get("IDLEJOT_UI_URL") or os.environ.get("TULPA_UI_URL", "http://127.0.0.1:47860")


def current():
    try:
        return SID_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def alive(sid):
    """A stopped session still answers get_chat_session, so check the state too."""
    data = mcp_client.call_tool("get_chat_session",
                                {"session_id": sid, "response_format": "compact"}, 30)
    # Positive confirmation only. A gateway 502, timeout残包 or half-broken
    # connection surfaces as {} or non-JSON text (see mcp_client.parse), and
    # none of those may keep a dead session alive: only an explicitly live
    # session for the queried id counts, everything else goes to rebuild.
    session = data.get("session") or {}
    return (session.get("active") is True
            and session.get("id") == sid
            and session.get("state") != "stopped")


def clear_group_sessions():
    """Stop sessions this group still holds.

    A session is owned by the grant that created it, so a grant refresh leaves an
    orphan the new grant cannot see but also cannot replace: starting a second
    session is rejected. Stopping by conversation id clears exactly that orphan.
    """
    headers = {"x-chatweave-ui": "1"}
    with httpx.Client(timeout=httpx.Timeout(15, connect=5), trust_env=False) as client:
        try:
            listed = client.get(f"{UI}/api/mcp/chats", headers=headers).json()
        except Exception:  # noqa: BLE001
            return False
        stopped = False
        for item in listed.get("sessions") or []:
            if item.get("conversation_id") != GROUP:
                continue
            sid = item.get("id") or item.get("session_id")
            if not sid:
                continue
            try:
                client.post(f"{UI}/api/mcp/chats/{sid}/stop", headers=headers, json={})
                stopped = True
            except Exception:  # noqa: BLE001
                continue
        return stopped


def start():
    data = mcp_client.call_tool(
        "start_chat_session",
        {"conversation_id": GROUP, "persona_preset": PERSONA,
         "response_format": "compact", "idempotency_key": uuid.uuid4().hex},
        90,
    )
    if data.get("error"):
        return ""
    sid = data.get("session_id") or (data.get("session") or {}).get("id")
    if sid:
        SID_FILE.write_text(sid, encoding="utf-8")
    return sid


def ensure():
    sid = current()
    if sid and alive(sid):
        return sid

    listed = mcp_client.call_tool("list_chat_sessions", {}, 30)
    for item in listed.get("sessions") or []:
        if item.get("conversation_id") != GROUP:
            continue
        sid = item.get("id") or item.get("session_id")
        if sid and alive(sid):
            SID_FILE.write_text(sid, encoding="utf-8")
            print(f"resumed {sid}")
            return sid

    sid = start()
    if sid:
        print(f"started {sid}")
        return sid

    if clear_group_sessions():
        print("cleared stale group session")
        sid = start()
        if sid:
            print(f"started {sid}")
            return sid
    print("session bootstrap failed")
    return ""


if __name__ == "__main__":
    sys.exit(0 if ensure() else 1)
