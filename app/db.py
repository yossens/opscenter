"""SQLite connection and migration mechanism for OpsCenter.

- ``init_db()`` — creates the data directories, enables WAL, and sequentially
  applies the migrations from ``app/migrations/`` based on the current
  ``schema_version``.
- ``get_conn()`` — FastAPI dependency: a fresh connection per request with
  ``row_factory=sqlite3.Row``, ``PRAGMA foreign_keys=1`` and
  ``PRAGMA busy_timeout=5000``.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from . import config

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d+)_.*\.sql$")


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _current_version(conn: sqlite3.Connection) -> int:
    """Current schema version (0 if app_meta has not been created yet)."""
    try:
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _discover_migrations() -> list[tuple[int, Path]]:
    """List of (version, path) migrations, sorted by ascending version."""
    found: list[tuple[int, Path]] = []
    for path in MIGRATIONS_DIR.glob("*.sql"):
        match = _MIGRATION_RE.match(path.name)
        if match:
            found.append((int(match.group(1)), path))
    found.sort(key=lambda item: item[0])
    return found


def init_db() -> None:
    """Initializes the DB: directories, WAL, applying pending migrations."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    config.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

    conn = _connect(config.DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        current = _current_version(conn)
        for version, path in _discover_migrations():
            if version > current:
                script = path.read_text(encoding="utf-8")
                conn.executescript(script)
                conn.commit()
    finally:
        conn.close()


def get_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: a connection for the duration of the request.

    ``check_same_thread=False``: FastAPI serves sync dependencies and async
    handlers on different threads (threadpool vs event loop), while the
    connection is created within a single request and is not shared between
    requests — the same-thread restriction is unnecessary and gets in the way
    here.
    """
    conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=1")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
    finally:
        conn.close()
