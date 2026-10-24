"""Hold keeper connections so SQLite never deletes a WAL sidecar mid-read.

Tulpa opens its databases per request in read-only mode. When the last connection
closes, SQLite removes the -shm/-wal sidecars; a reader that was already mapping
them then fails with "unable to open database file" or a disk I/O error, which the
backend surfaces as 资料读取失败. One long-lived connection per database keeps the
sidecars pinned for as long as this process runs.

Databases that do not exist yet are not an error: the backend creates them on first
write, so this waits instead of exiting.
"""
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
from wg_paths import data_dir, logs_dir, state_dir
STATE_DIR = state_dir()
LOG = logs_dir() / "db_keeper.log"
TARGETS = [data_dir() / "mcp-access.sqlite3", data_dir() / "chats.sqlite3"]
RETRY = 30


def log(line):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


def open_one(path):
    con = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("SELECT count(*) FROM sqlite_master").fetchone()
    return con


def main():
    conns = {}
    log(f"keeper start, pid={os.getpid()}")
    while True:
        for path in TARGETS:
            if path in conns:
                continue
            if not path.exists():
                continue
            try:
                conns[path] = open_one(path)
                log(f"keeper open {path.name}")
            except Exception as exc:  # noqa: BLE001
                log(f"keeper open failed {path.name}: {type(exc).__name__}: {exc}")
        for path, con in list(conns.items()):
            try:
                con.execute("SELECT 1").fetchone()
            except Exception as exc:  # noqa: BLE001
                log(f"keeper dropped {path.name}: {type(exc).__name__}: {exc}")
                try:
                    con.close()
                except Exception:  # noqa: BLE001
                    pass
                conns.pop(path, None)
        time.sleep(RETRY)


if __name__ == "__main__":
    sys.exit(main())
