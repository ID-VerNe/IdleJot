"""Ensure a live chat session exists for the water group and cache its id.

A session belongs to the MCP grant that created it, so refreshing a revoked grant
also drops the session. Re-creating it here means the collector keeps running across
grant rotation without anyone re-issuing anything by hand.

issue #164：本文件以前自己 `json.loads(ROOT/water-group/config.json)`，绕过了
`wg_config`——`IDLEJOT_WG_CONFIG` 指夹具时这里读的还是开发机实机配置，于是
「测试里 `session.GROUP` 是哪个群」由实机配置决定。现在配置只从 `wg_config` 来，
群号只从 `wg_isolation` 来（env > `<state>/wg_conversation.txt` > config 里唯一的群），
多群且未钉死时**拒绝启动**——不再静默锁到「第一个 enabled 的群」。
"""
import os
import sys
import uuid
from pathlib import Path

import httpx

import mcp_client
import wg_config  # noqa: E402  (config.json 的唯一读取口，issue #4 PA0)
import wg_event_ref  # noqa: E402  (group_of 的唯一定义，issue #255 收口)
import wg_isolation  # noqa: E402  (群身份的唯一解析口，issue #164 PA)

ROOT = Path(__file__).resolve().parents[2]
from wg_paths import state_dir
STATE_DIR = state_dir()
SID_FILE = STATE_DIR / "wg_session.txt"


def _config():
    """读配置。委托 ``wg_config``：`IDLEJOT_WG_CONFIG` 在这里同样生效。"""
    return wg_config.config()


def _persona(conversation):
    """本群的人设：`config.groups.<gid>.persona`，回退 `default_persona`。

    群号从 conversation id 反解（`<账号>:group:<群号>`），不是从「第一个 enabled」猜。
    """
    cfg = _config()
    gid = wg_event_ref.group_of(conversation)
    item = (cfg.get("groups") or {}).get(gid) if gid else None
    if isinstance(item, dict) and item.get("persona"):
        return str(item["persona"])
    return str(cfg.get("default_persona") or "")


_CONVERSATION = wg_isolation.resolve()[0]
GROUP = _CONVERSATION
PERSONA = (os.environ.get("IDLEJOT_WG_PERSONA") or os.environ.get("TULPA_WG_PERSONA")
           or _persona(_CONVERSATION))
UI = os.environ.get("IDLEJOT_UI_URL") or os.environ.get("TULPA_UI_URL", "http://127.0.0.1:47860")

# 拒绝信息只打一次：`collector.bootstrap_session()` 每 10 秒重试一轮，同一句
# 说明重复进日志等于噪音，而噪音会让人以为「它在忙」。
_REFUSED = False


def identity_problem():
    """**多群未钉死**时返回拒绝原因，否则空串（issue #164 PC）。

    改前这里会「取第一个 enabled 的群」——多群时那是**按配置书写顺序**替用户选群，
    B 群的会话静默锁到 A 群，读侧再把两个群都给它看，回复也不校验归属。所以多群
    未钉死时宁可拒绝：一个说清原因的拒绝，比一个发错群的会话好。

    **只管歧义，不管「一个群都没有」**：零配置（`groups` 为空、或全都没 enabled）
    时改前也是空群号、也照样起，把它算成「没身份」会改掉既有行为——`check_issue_16_
    audit_fixes.py` 的 `ensure()` 重建用例就是零配置夹具，它会整条红。那种情况已经
    由 `wg_config.problems()`（issue #51）单独报出来，`up.py --status` 也印，不该由
    这里二次裁决。这里只拦「有多个群、所以选谁都是替用户选」这一种。
    """
    if GROUP or not wg_isolation.ambiguous():
        return ""
    groups = "、".join(wg_isolation.enabled_groups())
    return (f"本会话的群身份未钉死，而 config 里有 {len(wg_isolation.enabled_groups())} "
            f"个启用群（{groups}）。多群下「取第一个 enabled」等于按书写顺序替你"
            f"选群，正是 issue #164 的现场，所以这里拒绝启动。\n"
            f"       钉死方式（二选一）：\n"
            f"         - 环境变量 IDLEJOT_WG_CONVERSATION=<账号>:group:<群号>\n"
            f"         - 或写 {STATE_DIR / wg_isolation.CONVERSATION_FILE}"
            f"（内容同上，一行）")


def refuse_if_unidentified():
    """多群歧义时打印原因并返回 True（调用方据此退出）。零配置不拦。"""
    global _REFUSED
    problem = identity_problem()
    if not problem:
        return False
    if not _REFUSED:
        _REFUSED = True
        print(f"[拒绝] {problem}")
    return True


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
    # 多群歧义是前提：那种状态下 `start()` 拿的是空 conversation_id，而
    # `clear_group_sessions()` 会按空串比对——两个群都匹配不上、也都不该动
    # （issue #164 PC）。零配置不拦（见 `identity_problem()`）。
    if refuse_if_unidentified():
        return ""
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