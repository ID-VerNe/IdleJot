"""Single home for SQLite connection policy.

Every database in the project runs in WAL mode, so the pragmas, row factory
and transaction scope live here instead of being copied per store.
"""
import sqlite3
from contextlib import contextmanager


def initialize(db, *, wal=False, foreign_keys=False):
    """Apply the pragmas a freshly opened connection needs."""
    if wal:
        db.execute('PRAGMA journal_mode=WAL')
    if foreign_keys:
        db.execute('PRAGMA foreign_keys=ON')
    return db


@contextmanager
def connect(path, *, timeout=10.0, row_factory=sqlite3.Row, wal=False, foreign_keys=False):
    """Open a transactional connection and always close it.

    The ``with db`` scope commits on success and rolls back on error, matching
    the previous per-store helpers.
    """
    db = sqlite3.connect(path, timeout=timeout)
    if row_factory is not None:
        db.row_factory = row_factory
    try:
        initialize(db, wal=wal, foreign_keys=foreign_keys)
        with db:
            yield db
    finally:
        db.close()
