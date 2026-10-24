"""Test suite for Phase 2: bootstrap HTTP POST JSON-RPC health check barrier."""
import json
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from fixture_server import FixtureHandler, fixture_server

from water_group.bootstrap import check_tulpa_mcp_health, wait_tulpa_mcp_ready


class MockMCPHandler(FixtureHandler):
    mode = "401"

    def do_POST(self):
        if self.path != "/mcp":
            self.send_response(404)
            self.end_headers()
            return
        if self.mode == "401":
            self.send_json({"error": "MCP request denied"}, 401)
        elif self.mode == "200":
            self.send_json({"jsonrpc": "2.0", "id": "healthcheck", "result": {}})
        elif self.mode == "500":
            self.send_response(500)
            self.end_headers()


def test_mcp_health_check():
    with fixture_server(MockMCPHandler) as server:
        port = server.server_port
        # 1. Test 401 handshake (standard Starlette MCP guard when unauthenticated)
        MockMCPHandler.mode = "401"
        ok, msg = check_tulpa_mcp_health(port)
        assert ok is True, f"Expected 401 handshake to pass, got {ok}, {msg}"
        assert "401" in msg

        # 2. Test 200 handshake (authenticated MCP response)
        MockMCPHandler.mode = "200"
        ok, msg = check_tulpa_mcp_health(port)
        assert ok is True, f"Expected 200 handshake to pass, got {ok}, {msg}"
        assert "200" in msg

        # 3. Test 500 failure
        MockMCPHandler.mode = "500"
        ok, msg = check_tulpa_mcp_health(port)
        assert ok is False, f"Expected 500 to fail, got {ok}, {msg}"

        # 4. Test wait_tulpa_mcp_ready with dynamic transition
        MockMCPHandler.mode = "500"
        def become_ready_after_delay():
            time.sleep(0.3)
            MockMCPHandler.mode = "200"

        threading.Thread(target=become_ready_after_delay, daemon=True).start()
        ready_ok, ready_msg = wait_tulpa_mcp_ready(port, timeout_seconds=3.0)
        assert ready_ok is True, f"Expected wait barrier to succeed, got {ready_msg}"
        print("✓ wait_tulpa_mcp_ready barrier and health check passed")

        # 5. Test closed port
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            unused_port = s.getsockname()[1]
        ok, msg = check_tulpa_mcp_health(unused_port)
        assert ok is False, f"Expected closed port to fail, got {ok}, {msg}"
        print("✓ Closed port rejection check passed")

        # 6. Test custom host parameter & loopback proxy isolation
        MockMCPHandler.mode = "200"
        ok_host, msg_host = check_tulpa_mcp_health(port, host="127.0.0.1")
        assert ok_host is True and "200" in msg_host
        print("✓ Host parameter & proxy isolation verified")


if __name__ == "__main__":
    test_mcp_health_check()
    print("PASS: bootstrap health check suite passed.")
