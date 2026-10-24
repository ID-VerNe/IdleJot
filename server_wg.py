"""Water-group slim server entry: HTTP routes + in-process MCP listener.

This is the only backend entry for the water-group-only slim build. It wires
``install_mcp_routes`` (which owns the in-process MCP listener on 48777 via a
lifespan) onto a minimal FastAPI app. It does NOT import ``app.py`` or any
Gradio module.

Run directly: ``python server_wg.py``. Exits 2 if either port is busy so the
supervisor restarts cleanly instead of running half-alive.
"""
from __future__ import annotations

import socket
import sqlite3
import sys

import uvicorn
from fastapi import FastAPI

from chatlocal.config import DATA
from chatlocal.mcp_routes import install_mcp_routes

DB = DATA / "mcp-access.sqlite3"
HTTP_PORT = 47860
MCP_PORT = 48777


def _ensure_settings() -> None:
    """Force settings row to (enabled=1, port=48777).

    mcp_access.CREATE TABLE settings defaults to (0, 18777) on a fresh DB
    (mcp_access.py:42-43), which would leave MCP disabled. We normalize on
    every start. Schema must match mcp_access.py:42.
    """
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB)
    try:
        db.execute(
            "CREATE TABLE IF NOT EXISTS settings("
            "id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,"
            "port INTEGER NOT NULL DEFAULT 48777)"
        )
        db.execute(
            "INSERT OR IGNORE INTO settings(id, enabled, port) VALUES(1, 1, 48777)"
        )
        db.execute("UPDATE settings SET enabled=1, port=? WHERE id=1", (MCP_PORT,))
        db.commit()
    finally:
        db.close()


def _port_busy(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def create_app() -> FastAPI:
    _ensure_settings()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_mcp_routes(app)  # owns lifespan_context; do not reassign afterwards
    @app.get("/")
    def health():
        return {"ok": True, "service": "water-group", "mcp_port": MCP_PORT}
    return app


if __name__ == "__main__":
    busy = [p for p in (HTTP_PORT, MCP_PORT) if _port_busy(p)]
    if busy:
        print(f"port busy: {busy}", file=sys.stderr)
        raise SystemExit(2)
    uvicorn.run(
        create_app(),
        host="127.0.0.1",
        port=HTTP_PORT,
        access_log=False,
        log_level="info",
    )
