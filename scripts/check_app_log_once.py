"""issue #97 gate: one backend start writes one startup banner, not two.

The backend runs two uvicorn servers in one process: the HTTP one from
``server_wg.py`` and the MCP one from ``MCPService.start()``. uvicorn logs through
a module-level ``uvicorn.error`` logger (uvicorn/server.py:52) and the MCP server
is built with ``log_config=None``, so it installs no handler and every record
bubbles up to the handler the HTTP server installed -- "Started server process
[pid]" printed twice with the same pid. ``app.log`` is the first place anyone
looks when the backend seems to have been started twice, so the duplicate read as
a real double start.

The gate reproduces exactly that pair (no real stack, no fixed ports, no
``~/.water-group``) and pins four contracts instead of one number:

  1. a start writes each banner line exactly once, and so does a stop;
  2. the MCP server really was running -- otherwise "no duplicate" is vacuous;
  3. a second start/stop cycle is still exactly once (the filter is scoped, it
     does not pile up on the shared logger);
  4. the fix does not silence MCP failures and does not work by raising the
     shared logger's level: a WARNING from the MCP thread is still printed, now
     tagged ``[mcp]``, while INFO from that thread is dropped.

    .venv/Scripts/python.exe scripts/check_app_log_once.py
"""
import io
import logging
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

# Pins WATER_GROUP_HOME before chatlocal.config resolves DATA (issue #14/#79).
from check_fixture_support import temp_dir  # noqa: E402

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from chatlocal.mcp_routes import install_mcp_routes  # noqa: E402
from chatlocal.mcp_server import MCP_THREAD_NAME, _MCPThreadLogFilter  # noqa: E402
from chatlocal.store import Store  # noqa: E402

# What app.log gets: uvicorn's default handler writes to stderr, and the
# supervisor points that at app.log. Both lists are the exact wording uvicorn
# 0.54 emits; a silent rename shows up here as a zero count.
STARTUP = ("Started server process", "Waiting for application startup.",
           "Application startup complete.", "Uvicorn running")
SHUTDOWN = ("Shutting down", "Waiting for application shutdown.",
            "Application shutdown complete.", "Finished server process")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def once(keys, output):
    """Every key exactly once -- not "at most", so a lost banner also fails."""
    found = {key: output.count(key) for key in keys}
    assert found == {key: 1 for key in keys}, (found, output)
    return found


class Backend:
    """The HTTP server + in-process MCP server pair, wired as server_wg.py does."""

    def __init__(self, folder):
        self.app = FastAPI()
        self.service = install_mcp_routes(self.app, Store(folder / "chats.sqlite3"))
        self.output = io.StringIO()
        self.server = self.thread = None

    def start(self):
        # Fresh ports every cycle: the MCP listener uses SO_EXCLUSIVEADDRUSE and
        # a just-closed HTTP port can still be in TIME_WAIT on Windows.
        self.output = io.StringIO()
        self.service.access.configure(True, free_port())
        port = free_port()
        # uvicorn.Config.__init__ calls configure_logging(), which binds its
        # stderr handler to whatever sys.stderr is at that moment -- before the
        # server thread even exists. Redirect first, restore after startup: the
        # handler keeps writing to the buffer for the whole cycle, shutdown
        # included.
        real, sys.stderr = sys.stderr, self.output
        try:
            config = uvicorn.Config(self.app, host="127.0.0.1", port=port,
                                    access_log=False, log_level="info")
            self.server = uvicorn.Server(config)
            self.thread = threading.Thread(target=self.server.run, daemon=True, name="app-log-gate")
            self.thread.start()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if getattr(self.server, "started", False) and self.service.status()["running"]:
                    return self
                time.sleep(0.02)
        finally:
            sys.stderr = real
        raise AssertionError("backend did not start:\n" + self.output.getvalue())

    def stop(self):
        self.server.should_exit = True
        self.thread.join(10)
        assert not self.thread.is_alive(), "backend thread survived shutdown"
        return self.output.getvalue()


def assert_running(backend):
    """Non-vacuous: without this, "no duplicate" also passes when MCP never ran."""
    assert backend.service.status()["running"], backend.output.getvalue()
    assert backend.service.thread.name == MCP_THREAD_NAME, backend.service.thread.name
    assert logging.getLogger("uvicorn.error").getEffectiveLevel() == logging.INFO, \
        "the fix must not raise the shared logger's level"


def test_one_banner_per_cycle(folder):
    backend = Backend(folder)

    backend.start()
    assert_running(backend)
    once(STARTUP, backend.output.getvalue())
    once(SHUTDOWN, backend.stop())
    print("✓ 1. 首轮启停：4 条启动行 + 4 条停止行各恰好 1 条")

    # Second cycle on the same service: stop() must take the filter with it.
    backend.start()
    assert_running(backend)
    once(STARTUP, backend.output.getvalue())
    test_mcp_errors_stay_visible(backend)
    once(SHUTDOWN, backend.stop())
    print("✓ 3. 第二轮启停：过滤器不累积，仍各恰好 1 条")


def test_mcp_errors_stay_visible(backend):
    """A filtered thread must still be able to report a failure."""
    def emit():
        logging.getLogger("uvicorn.error").info("mcp fixture info")
        logging.getLogger("uvicorn.error").warning("mcp fixture warning")

    worker = threading.Thread(target=emit, name=MCP_THREAD_NAME)
    worker.start()
    worker.join()
    output = backend.output.getvalue()
    assert "mcp fixture info" not in output, output
    assert "[mcp] mcp fixture warning" in output, output
    print("✓ 2. MCP 线程 INFO 被丢弃、WARNING 仍可见且带 [mcp] 来源前缀")


def test_filter_does_not_linger():
    leftover = [f for f in logging.getLogger("uvicorn.error").filters
                if isinstance(f, _MCPThreadLogFilter)]
    assert not leftover, "the log filter outlived the MCP server"
    print("✓ 4. 过滤器随 MCP 服务器退出移除，共享 logger 无残留")


def main():
    print("========================================================================")
    print("        [测试] issue #97 一次启动只写一份启动日志")
    print("========================================================================")
    with temp_dir("tulpa-app-log-") as tmp:
        test_one_banner_per_cycle(tmp)
    test_filter_does_not_linger()
    print("========================================================================")
    print("✓ 全部 4 项 app.log 唯一性测试通过！")
    print("========================================================================")


if __name__ == "__main__":
    main()
