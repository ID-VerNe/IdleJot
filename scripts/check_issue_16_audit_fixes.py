"""Issue #16 回归：静态审计复核出的 4 处可复现缺陷，逐条对应 issue 验收建议。

  1. store.py 触发 sqlite3 错误路径时，异常类型是 sqlite3.Error 而不是 AttributeError；
  2. grants 乱序插入后，last_scope() 恒返回 created_at 最大那条（不按 uuid id 排序）；
  3. mcp_client.parse()：SSE 通知在前、response 在后时返回 response；非 JSON 502 时
     call_tool 明确报错而不是空结果 {}；
  4. session.alive() 对 {} / 非 JSON 回复返回 False，并触发 ensure() 重建。

全程离线：数据根钉到临时目录、grants 用假库、MCP 走 httpx.MockTransport，不碰真栈。
"""
import ast
import json
import os
import random
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

from check_fixture_support import isolated_env, pin_state_env, scratch_dir  # noqa: E402

# Pin the data root before importing anything that resolves it at import time:
# chatlocal/config.py mkdirs DATA/TMP on import, and the daemons resolve
# STATE_DIR/DB at import. Without this the suite would touch the real
# ~/.water-group (the issue #14 failure mode all over again).
# state 两腿钉合走 pin_state_env（#281）：IDLEJOT 先于 TULPA，单钉一条会被外部穿透。
_SCRATCH = scratch_dir(prefix="tulpa-issue16-")
os.environ.clear()
os.environ.update(isolated_env(_SCRATCH))
pin_state_env(_SCRATCH / "state")

import httpx  # noqa: E402

import chatlocal.store as store  # noqa: E402
import mcp_client  # noqa: E402
import mint_grant  # noqa: E402
import session  # noqa: E402

GRANTS_DDL = """CREATE TABLE grants(
    id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL, epoch TEXT NOT NULL, accounts TEXT NOT NULL,
    created_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)"""


def _seed_grants(db, rows):
    conn = sqlite3.connect(db)
    try:
        conn.execute(GRANTS_DDL)
        conn.executemany("INSERT INTO grants VALUES(?,?,?,?,?,?,?,0)", rows)
        conn.commit()
    finally:
        conn.close()


def test_store_sqlite3_handler_catches():
    """#16-1: store.py 的 except sqlite3.Error 必须接住真实 sqlite3 错误。

    修复前 sqlite3 全文件只出现在 except 类型里，进入该分支时名字解析先炸，
    把原始 sqlite3 故障换成 AttributeError。这里从源码里取出真正发货的那条
    except 子句，在模块自己的命名空间里对真实 sqlite3.OperationalError 执行。
    """
    source = Path(store.__file__).read_text(encoding="utf-8")
    handlers = [node for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.ExceptHandler)
                and isinstance(node.type, ast.Attribute)
                and node.type.attr == "Error"
                and isinstance(node.type.value, ast.Name)
                and node.type.value.id == "sqlite3"]
    assert handlers, "store.py 里应有一条 except sqlite3.Error"
    assert hasattr(store, "sqlite3"), "store.py 必须 import sqlite3，否则 except 子句自身会炸"
    namespace = dict(vars(store))
    for handler in handlers:
        trial = ast.Try(
            body=[ast.Raise(exc=ast.Call(
                func=ast.Attribute(value=ast.Name(id="sqlite3", ctx=ast.Load()),
                                   attr="OperationalError", ctx=ast.Load()),
                args=[ast.Constant(value="attempt to write a readonly database")],
                keywords=[]), cause=None)],
            handlers=[handler], orelse=[], finalbody=[])
        ast.fix_missing_locations(trial)
        code = compile(ast.Module(body=[trial], type_ignores=[]), store.__file__, "exec")
        try:
            exec(code, namespace)
        except AttributeError as exc:
            raise AssertionError(f"except 子句自身炸了: {exc}") from exc
        except sqlite3.Error:
            raise AssertionError("except sqlite3.Error 没有接住 sqlite3.Error") from None
    print("✓ 1. store.py 的 except sqlite3.Error 接住真实 sqlite3 错误")


def test_last_scope_orders_by_created_at():
    """#16-2: last_scope() 恒返回 created_at 最大的授权，不按 uuid4 id 排序。"""
    base = time.time() - 60

    # Deterministic case: ids sort exactly opposite to created_at, so the old
    # `ORDER BY id DESC` provably returned the OLDEST grant.
    db_rev = _SCRATCH / "grants-reversed.sqlite3"
    rows = [(f"{9 - seq:032x}", "water-group" if seq % 2 else "elsewhere",
             f"token-{seq}", json.dumps({"seq": seq}), "epoch", "[]", base + seq)
            for seq in range(6)]
    _seed_grants(db_rev, rows)
    with patch.object(mint_grant, "DB", db_rev):
        scope = mint_grant.last_scope()
    assert scope == {"seq": 5}, f"应返回 created_at 最新的 seq=5，实际 {scope}"

    # Realistic case: uuid4 hex ids in shuffled insertion order.
    db_uuid = _SCRATCH / "grants-uuid.sqlite3"
    rows = [(uuid.uuid4().hex, "water-group", f"token-{seq}",
             json.dumps({"seq": seq}), "epoch", "[]", base + seq)
            for seq in range(6)]
    random.shuffle(rows)
    _seed_grants(db_uuid, rows)
    with patch.object(mint_grant, "DB", db_uuid):
        scope = mint_grant.last_scope()
    assert scope == {"seq": 5}, f"uuid 乱序插入后仍应返回 seq=5，实际 {scope}"

    # Fallback branch: no grant under NAME -> newest grant overall.
    db_fallback = _SCRATCH / "grants-fallback.sqlite3"
    rows = [(uuid.uuid4().hex, "elsewhere", f"token-{seq}",
             json.dumps({"seq": seq}), "epoch", "[]", base + seq)
            for seq in range(3)]
    random.shuffle(rows)
    _seed_grants(db_fallback, rows)
    with patch.object(mint_grant, "DB", db_fallback):
        scope = mint_grant.last_scope()
    assert scope == {"seq": 2}, f"兜底分支应返回最新的 seq=2，实际 {scope}"

    # Cold start: empty grants table -> DEFAULT_SCOPE.
    db_empty = _SCRATCH / "grants-empty.sqlite3"
    _seed_grants(db_empty, [])
    with patch.object(mint_grant, "DB", db_empty):
        scope = mint_grant.last_scope()
    assert scope == mint_grant.DEFAULT_SCOPE, f"空表应回落 DEFAULT_SCOPE，实际 {scope}"
    print("✓ 2. last_scope() 恒返回 created_at 最新的授权（含兜底与空表）")


def _response(status=200, ctype="application/json", text="", headers=None):
    hdrs = {"content-type": ctype} if ctype else {}
    hdrs.update(headers or {})
    return httpx.Response(status, headers=hdrs, text=text)


def test_parse_extracts_jsonrpc_response():
    """#16-3a: SSE 里通知在前时返回真 response，坏体显式报错。"""
    reply = {"jsonrpc": "2.0", "id": 7, "result": {"content": [{"type": "text", "text": "[1,2,3]"}]}}
    notification = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}}

    def sse(*payloads):
        return "".join(f"data: {json.dumps(p)}\n\n" for p in payloads)

    # Notification first, response second: the old code returned the notification.
    data = mcp_client.parse(_response(ctype="text/event-stream", text=sse(notification, reply)))
    assert data == reply, f"应返回真 response，实际 {data}"
    # Response first, trailing notification after: still the response.
    data = mcp_client.parse(_response(ctype="text/event-stream", text=sse(reply, notification)))
    assert data == reply, f"response 后被通知污染，实际 {data}"
    # Two responses on one stream: the last one wins.
    later = dict(reply, id=8)
    data = mcp_client.parse(_response(ctype="text/event-stream", text=sse(notification, reply, later)))
    assert data == later, f"应取最后一条 response，实际 {data}"
    # Multi-line data event: lines join per the event-stream format.
    split = 'data: {"jsonrpc":"2.0","id":9,\ndata:  "result":{"content":[]}}\n\n'
    data = mcp_client.parse(_response(ctype="text/event-stream", text=split))
    assert data.get("id") == 9 and "result" in data, f"多行 data 事件应拼接解析，实际 {data}"
    # Notification-only stream: explicit error, never an empty success.
    data = mcp_client.parse(_response(ctype="text/event-stream", text=sse(notification)))
    assert data.get("error") == "sse_without_response", f"只有通知应报错，实际 {data}"

    # Plain JSON-RPC reply passes through.
    data = mcp_client.parse(_response(text=json.dumps(reply)))
    assert data == reply, f"普通 JSON-RPC 响应应原样返回，实际 {data}"
    # Non-JSON body (gateway 502 HTML): error carries the HTTP status.
    data = mcp_client.parse(_response(status=502, ctype="text/html", text="<html>502 Bad Gateway</html>"))
    assert data.get("error") == "non_json_response" and data.get("status") == 502, f"502 应显式报错，实际 {data}"
    # JSON that is not a reply shape is an error too.
    data = mcp_client.parse(_response(text='{"detail":"nope"}'))
    assert data.get("error") == "unexpected_response", f"非 response 形状应报错，实际 {data}"
    print("✓ 3a. parse() 按 JSON-RPC response 提取，坏体显式报错")


def _mock_transport(handler):
    """Patch httpx.Client so _call_once talks to a MockTransport, no real network."""
    real_client = httpx.Client  # capture before patching, or the factory recurses

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)
    return patch.object(httpx, "Client", factory)


def _mcp_handler(tools_call):
    def handle(request):
        body = json.loads(request.content)
        method = body.get("method")
        if method == "initialize":
            return _response(text=json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {}}),
                             headers={"mcp-session-id": "fixture-session"})
        if method == "notifications/initialized":
            return _response(status=202, ctype="", text="")
        if method == "tools/call":
            return tools_call(body)
        raise AssertionError(f"unexpected method {method}")
    return handle


def test_call_once_surfaces_failures():
    """#16-3b: 通知在前的 SSE 与 502 残包经过 _call_once_raw/call_tool 的归宿。"""
    reply = {"jsonrpc": "2.0", "id": 0, "result": {"content": [{"type": "text", "text": "[1,2,3]"}]}}
    notification = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}

    def sse(body):
        rid = body["id"]
        text = (f'data: {json.dumps(notification)}\n\n'
                f'data: {json.dumps(dict(reply, id=rid))}\n\n')
        return _response(ctype="text/event-stream", text=text)

    with _mock_transport(_mcp_handler(sse)):
        data, images = mcp_client._call_once_raw("wait_chat_messages", {}, 5)
    assert data == [1, 2, 3], f"通知在前的 SSE 应拿到真 response，实际 {data}"
    assert images == [], f"纯文本响应不应带图片块，实际 {images}"

    def gateway_502(body):
        return _response(status=502, ctype="text/html", text="<html>502 Bad Gateway</html>")

    with _mock_transport(_mcp_handler(gateway_502)):
        data, images = mcp_client._call_once_raw("wait_chat_messages", {}, 5)
    assert data.get("error") and data.get("status") == 502, f"502 应显式报错，实际 {data}"
    assert images == [], f"错误路径不应带图片块，实际 {images}"
    assert "result" not in data, f"502 不得落成空结果，实际 {data}"

    # call_tool 对非 401 的错误直接上交，不再伪装成成功空结果。
    with _mock_transport(_mcp_handler(gateway_502)):
        data = mcp_client.call_tool("wait_chat_messages", {}, 5)
    assert data.get("error") and data.get("status") == 502, f"call_tool 应上交 502 错误，实际 {data}"

    # 401 仍走换发 grants 重放一次：错误字典带 status，call_tool 的 401 特判重新生效。
    attempts = []

    def unauthorized_then_ok(body):
        attempts.append(body["method"])
        if len(attempts) == 1:
            return _response(status=401, ctype="text/plain", text="unauthorized")
        return sse(body)

    with _mock_transport(_mcp_handler(unauthorized_then_ok)), \
            patch.object(mint_grant, "mint", lambda: "fixture-token"):
        data = mcp_client.call_tool("wait_chat_messages", {}, 5)
    assert data == [1, 2, 3], f"401 换发后应重放成功，实际 {data}"
    assert attempts.count("tools/call") == 2, f"应重放一次 tools/call，实际 {attempts}"
    print("✓ 3b. _call_once_raw/call_tool 对 502 显式报错，401 换发重放照旧")


def test_alive_requires_positive_confirmation():
    """#16-4: alive() 只有 session.active is True 且 id 一致才判活。"""
    sid = "sid-abc"
    cases = [
        ({}, False),
        ({"text": "not json"}, False),
        ({"raw": "x"}, False),
        ({"error": "boom"}, False),
        ({"session": {"active": False}}, False),
        ({"session": {"active": True, "id": "other"}}, False),
        ({"session": {"active": True, "id": sid, "state": "stopped"}}, False),
        ({"session": {"active": True, "id": sid}}, True),
        ({"session": {"active": True, "id": sid, "state": "waiting_messages"}}, True),
    ]
    for reply, expected in cases:
        with patch.object(mcp_client, "call_tool", lambda *a, **k: reply):
            assert session.alive(sid) is expected, f"{reply} 应判 {'活' if expected else '死'}"
    print("✓ 4a. alive() 对 {} / 非 JSON / 错 id 一律判死，正向确认才判活")


def test_ensure_rebuilds_dead_session():
    """#16-4: 死会话（get_chat_session 返回 {}）必须触发 ensure() 重建。"""
    sid_file = _SCRATCH / "wg_session.txt"
    sid_file.write_text("stale-sid", encoding="utf-8")
    seen = []

    def fake_call(name, arguments, timeout=60.0):
        seen.append(name)
        if name == "get_chat_session":
            return {}  # 网关 502 / 残包经修复后的形状：不是成功
        if name == "list_chat_sessions":
            return {"sessions": []}
        if name == "start_chat_session":
            return {"session_id": "fresh-sid"}
        raise AssertionError(name)

    with patch.object(session, "SID_FILE", sid_file), \
            patch.object(mcp_client, "call_tool", fake_call):
        assert session.ensure() == "fresh-sid"
    assert sid_file.read_text(encoding="utf-8") == "fresh-sid", "重建后应写回新 sid"
    assert seen[:3] == ["get_chat_session", "list_chat_sessions", "start_chat_session"], seen
    print("✓ 4b. ensure() 对死会话走重建并写回新 sid")


def main():
    test_store_sqlite3_handler_catches()
    test_last_scope_orders_by_created_at()
    test_parse_extracts_jsonrpc_response()
    test_call_once_surfaces_failures()
    test_alive_requires_positive_confirmation()
    test_ensure_rebuilds_dead_session()
    print("PASS: issue #16 回归检查全部通过。")


if __name__ == "__main__":
    main()
