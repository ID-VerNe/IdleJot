"""SQLite storage layer for the group-memory engine (issue #11, section PA).

Owns the connection strategy, the schema (including the idempotent migrations
for issue #5's ``key_memories_backup`` and issue #90's truncation columns) and
the data-root constants the whole engine shares. The rest of the engine reads
``self.db_path`` / ``self.lock`` / ``self._connect()`` from here.
"""
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

# Data root follows the same rule as water-group/daemon/wg_paths.py (the single
# source of truth). Restated instead of imported: wg_paths is a flat module for
# `python water-group/daemon/xxx.py` launches, and reaching it from here as
# `water_group.daemon.wg_paths` would load one file under two module names --
# the double-load trap the bridge shim documents. Keep the two in sync.
WG_HOME = Path(os.environ.get("WATER_GROUP_HOME") or (Path.home() / ".water-group"))

# Pinned by tests and by anyone running a second instance against a scratch DB.
DEFAULT_DB_PATH = Path(
    os.environ.get("IDLEJOT_WG_MEMORY_DB") or WG_HOME / "data" / "group_memories.sqlite3"
)

# Dossiers are appended to on every consolidation pass, so both lists need a
# ceiling or they turn into a wall of restatements. The memory cap is a storage
# budget, not a prompt budget: the L1 card renders only the two most recently
# restated entries. Twenty-four covers five facets (identity, verbal habits,
# games, mishaps, group memes) at four entries each, plus headroom.
MEMORY_SLOTS = 24
INTEREST_SLOTS = 16

# Cap on how many high-frequency group notes get injected into an L1 context
# batch. Group glossaries must not crowd out member dossiers: injection is a
# budgeted snippet, not a full dump (issue #31). The read tool stays the
# authoritative interface for the full set.
GROUP_NOTE_L1_CAP = 5

# 泛称不是名字。让「领导」「群友」参与子串匹配，等于任何两个字都能撞上档案：
# 一个包含别名但不等于别名的短语，不得命中该别名的档案。
GENERIC_ALIASES = ("领导", "群友")


class MemoryStoreMixin:
    def __init__(self, db_path=None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()


    def _init_db(self):
        with self.lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS member_profiles (
                    user_id TEXT PRIMARY KEY,
                    primary_name TEXT NOT NULL,
                    aliases TEXT NOT NULL DEFAULT '[]',
                    tone_style TEXT NOT NULL DEFAULT '',
                    interests TEXT NOT NULL DEFAULT '[]',
                    traits TEXT NOT NULL DEFAULT '{}',
                    key_memories TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_profiles_name ON member_profiles(primary_name);

                CREATE TABLE IF NOT EXISTS member_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    quote TEXT NOT NULL DEFAULT '',
                    context TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'interaction',
                    tags TEXT NOT NULL DEFAULT '[]',
                    truncated INTEGER NOT NULL DEFAULT 0,
                    original_length INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_user_group ON member_events(user_id, group_id, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_events_group ON member_events(group_id, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_events_user ON member_events(user_id, timestamp DESC);

                CREATE TABLE IF NOT EXISTS member_group_memories (
                    user_id      TEXT NOT NULL,
                    group_id     TEXT NOT NULL,
                    key_memories TEXT NOT NULL DEFAULT '[]',
                    tone_style   TEXT NOT NULL DEFAULT '',
                    interests    TEXT NOT NULL DEFAULT '[]',
                    aliases      TEXT NOT NULL DEFAULT '[]',
                    created_at   REAL NOT NULL,
                    updated_at   REAL NOT NULL,
                    PRIMARY KEY (user_id, group_id),
                    FOREIGN KEY (user_id) REFERENCES member_profiles(user_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_group_memories_group
                    ON member_group_memories(group_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_group_memories_user
                    ON member_group_memories(user_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS group_notes (
                    group_id        TEXT NOT NULL,
                    term            TEXT NOT NULL,
                    kind            TEXT NOT NULL DEFAULT 'glossary',
                    value           TEXT NOT NULL DEFAULT '',
                    updated_at      REAL NOT NULL,
                    source_event_id INTEGER,
                    PRIMARY KEY (group_id, term)
                );
                CREATE INDEX IF NOT EXISTS idx_group_notes_group
                    ON group_notes(group_id, updated_at DESC);
                """
            )
            # Idempotent column add for rollback backup (SQLite has no IF NOT EXISTS for ALTER).
            # Both rows get one: a shrinking overwrite is destructive in either
            # scope, and issue #5 showed the profile column alone was never
            # written, so "restore from backup" was a promise with no data.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(member_profiles)").fetchall()}
            if "key_memories_backup" not in cols:
                conn.execute("ALTER TABLE member_profiles ADD COLUMN key_memories_backup TEXT")
            group_cols = {r[1] for r in conn.execute("PRAGMA table_info(member_group_memories)").fetchall()}
            if "key_memories_backup" not in group_cols:
                conn.execute("ALTER TABLE member_group_memories ADD COLUMN key_memories_backup TEXT")

            # issue #90: an event that had to be shortened says so, instead of
            # reading like a complete quote. Existing rows keep 0/0 -- the text
            # they lost is already gone, and inventing a marker for it would
            # claim knowledge the database does not have.
            event_cols = {r[1] for r in conn.execute("PRAGMA table_info(member_events)").fetchall()}
            if "truncated" not in event_cols:
                conn.execute("ALTER TABLE member_events ADD COLUMN truncated INTEGER NOT NULL DEFAULT 0")
            if "original_length" not in event_cols:
                conn.execute("ALTER TABLE member_events ADD COLUMN original_length INTEGER NOT NULL DEFAULT 0")
