"""Minimal IdleJot / Tulpa MCP client for the water-group daemons.

Only the transport lives here: a JSON-RPC call against the local Streamable HTTP
endpoint, with a one-shot grant refresh when the server rejects the token.

The token is never stored in source. Resolution order:
  1. TULPA_MCP_TOKEN environment variable
  2. <state dir>/mcp_token.txt, written by mint_grant.py
"""
import json
import os
import threading
from pathlib import Path

import httpx

URL = os.environ.get("IDLEJOT_MCP_URL") or os.environ.get("TULPA_MCP_URL", "http://127.0.0.1:48777/mcp")
from wg_paths import state_dir
STATE_DIR = state_dir()
TOKEN_FILE = STATE_DIR / "mcp_token.txt"

# The server de-duplicates concurrent calls by (grant, JSON-RPC id), so a shared
# constant id makes any second call look like a duplicate of an in-flight long
# poll and come back as "MCP request denied". Every call needs its own id.
_next_id = 1000
_id_lock = threading.Lock()


def next_id():
    global _next_id
    with _id_lock:
        _next_id += 1
        return _next_id


def current_token():
    token = (os.environ.get("IDLEJOT_MCP_TOKEN") or os.environ.get("TULPA_MCP_TOKEN", "")).strip()
    if token:
        return token
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def headers():
    return {
        "Authorization": f"Bearer {current_token()}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Host": URL.split("//", 1)[-1].split("/", 1)[0],
    }


def _sse_events(text):
    """Yield each SSE event's joined data payload, per the event-stream format."""
    data_lines = []
    for line in text.splitlines():
        if line:
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            continue
        if data_lines:
            yield "\n".join(data_lines)
            data_lines = []
    if data_lines:
        yield "\n".join(data_lines)


def _is_jsonrpc_response(data):
    """A reply to our call: carries the request id plus result or error.

    Notifications (notifications/progress and friends) share the SSE stream
    but have no id/result/error, so this is what tells the real reply apart
    from stream noise.
    """
    return (isinstance(data, dict)
            and "id" in data
            and ("result" in data or "error" in data))


def parse(resp):
    """Extract the JSON-RPC reply from an MCP HTTP response.

    An SSE stream may open with notifications before the reply, so the reply
    is the last event carrying id + result/error -- not merely the first
    parseable data line. A body that is not a JSON-RPC reply at all (gateway
    HTML, truncated stream, empty 200) comes back as an explicit error dict,
    never as something a caller could read as an empty success.
    """
    ctype = resp.headers.get("content-type", "")
    text = resp.text
    if "text/event-stream" in ctype:
        reply = None
        for payload in _sse_events(text):
            try:
                data = json.loads(payload)
            except ValueError:
                continue
            if _is_jsonrpc_response(data):
                reply = data
        if reply is not None:
            return reply
        return {"error": "sse_without_response", "raw": text[:300]}
    try:
        data = json.loads(text)
    except ValueError:
        return {"error": "non_json_response", "raw": text[:300], "status": resp.status_code}
    if not _is_jsonrpc_response(data):
        return {"error": "unexpected_response", "raw": text[:300], "status": resp.status_code}
    return data


def _call_once_raw(name, arguments, timeout):
    """返回 (payload, images)；images 保留 type=image 的像素块。

    content_index 是 data["result"]["content"] 里的下标，服务端
    materials.images[].content_indexes 也用同一套下标（从 1 起跳过文本块）。
    """
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=5), trust_env=False) as client:
        init = {"jsonrpc": "2.0", "id": next_id(), "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "idlejot-water-group", "version": "1.0"}}}
        r = client.post(URL, headers=headers(), json=init)
        if r.status_code >= 400:
            return {"error": "initialize_failed", "status": r.status_code, "body": r.text[:300]}, []
        hdrs = headers()
        sid = r.headers.get("mcp-session-id")
        if sid:
            hdrs["Mcp-Session-Id"] = sid
            client.post(URL, headers=hdrs, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        call = {"jsonrpc": "2.0", "id": next_id(), "method": "tools/call",
                "params": {"name": name, "arguments": arguments}}
        r = client.post(URL, headers=hdrs, json=call)
        data = parse(r)
        if "error" in data:
            # 401 means the grant is gone; 409 means a call with this id is already
            # in flight. Only the first is worth re-minting for.
            data.setdefault("status", r.status_code)
            return data, []
        if "result" not in data:
            # A reply without result/error (gateway HTML, truncated stream,
            # empty 200) is a failed call, not an empty success -- surface it.
            return {"error": "mcp_call_failed", "status": r.status_code, "body": r.text[:300]}, []
        result = data["result"]
        content = result.get("content") or []
        texts = []
        images = []
        for index, item in enumerate(content):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "image":
                images.append(dict(mime=item.get("mimeType", ""),
                                   data_b64=item.get("data", ""),
                                   content_index=index))
            else:
                texts.append(item.get("text", ""))
        body = "\n".join(texts) if texts else json.dumps(result, ensure_ascii=False)
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {"text": body}
        return payload, images


def _refresh_grant():
    """重铸一次授权；失败返回错误字符串，成功返回 None。"""
    try:
        import mint_grant
        mint_grant.mint()
    except Exception as exc:  # noqa: BLE001
        return f"grant refresh failed: {exc}"
    return None


def call_tool_raw(name, arguments, timeout=60.0):
    """同 call_tool，但返回 (payload, images)，保留 type=image 的像素块。

    群聊图片只在 MCP content 里以 base64 交付；只拼 text 会把像素丢掉，
    这正是 issue #27 的根因。调用方按 content_index 与服务端 materials 对齐。
    """
    data, images = _call_once_raw(name, arguments, timeout)
    if data.get("status") == 401:
        error = _refresh_grant()
        if error:
            return {"error": error}, []
        data, images = _call_once_raw(name, arguments, timeout)
    return data, images


def call_tool(name, arguments, timeout=60.0):
    """Call a tool; a revoked grant is re-minted once and the call replayed.

    Grants are bound to the store epoch and the bound-account snapshot, so they die
    whenever local data changes or the QQ account gets bound. For a listener that
    has to survive unattended, recovering here beats waiting for a human.
    """
    data, _ = call_tool_raw(name, arguments, timeout)
    return data
