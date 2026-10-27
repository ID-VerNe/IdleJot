"""Single home for SQLite connection policy.

Every database in the project runs in WAL mode, so the pragmas, row factory
and transaction scope live here instead of being copied per store.

issue #169 PC: read-only connections also live here. The daemons that read
``mcp-access.sqlite3`` (dossier / followup / wg_context / wg_gap / wg_text /
mint_grant) all want ``?mode=ro`` so a reader can never mutate the backend's
database, and they all need the same Windows-safe URI form (``Path.as_uri()``
+ ``?mode=ro``; a bare ``file:{path}`` with backslashes is parsed as a
filename). That shape was copied 11 times across 7 files before this helper.
``connect_ro`` is the one place that decides it.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path


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


def connect_ro(path, *, timeout=5.0):
    """Open a read-only connection via the SQLite URI ``?mode=ro`` form.

    ``path`` is a :class:`~pathlib.Path` (or a value ``Path(path)`` accepts).
    The URI is built with ``Path(path).resolve().as_uri() + "?mode=ro"`` and
    opened with ``uri=True``: the database is opened read-only at the SQLite
    level, so a reader cannot mutate it even if a bug tries to write. The
    ``as_uri()`` form is required on Windows, where a bare ``file:{path}``
    with backslashes is parsed as a filename and ``?mode=ro`` is then treated
    as part of the name (``unable to open database file``).

    Returns a plain :class:`sqlite3.Connection`; the caller owns its lifetime
    (the daemon read sites open, query, and ``close()`` in a ``try/finally``).
    ``timeout`` defaults to 5.0 to match the daemon read sites' existing
    budget; ``investigation_sql`` used 0.5 because it runs untrusted SQL with
    a progress handler, and keeps its own tighter timeout by calling
    ``connect_ro(path, timeout=0.5)``.
    """
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=timeout)
