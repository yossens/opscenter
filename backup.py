"""OpsCenter DB snapshot via ``VACUUM INTO`` + a copy of the attachments directory.

Run directly with the venv interpreter::

    .venv/Scripts/python backup.py

The script creates a new generation directory ``data/backups/<YYYY-MM-DD_HHMMSS>/``
(paths come from ``app/config.py``, overridable via ``OPSCENTER_DATA_DIR``) with a
DB snapshot and a full recursive copy of the attachments directory, then keeps the
7 newest generations and deletes the older ones.

The snapshot is made strictly via ``VACUUM INTO`` — this opens a full connection to
the live DB and also captures committed but not-yet-checkpointed data from the WAL
file. There is no byte-for-byte copy of the DB file here
(``shutil.copy``/``copyfile`` on ``.db``) and there must not be: on a running
application it would produce an incomplete/corrupted snapshot. The ``shutil`` copy
is applied only to the attachments directory.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# The script lives at the project root; on direct run sys.path[0] is this directory,
# so the ``app`` package is imported without additional path setup.
from app import config

RETENTION = 7


def _unique_generation_dir(backups_dir: Path) -> Path:
    """Returns a freshly created generation directory with a unique name.

    The base name is ``YYYY-MM-DD_HHMMSS`` (literally from the spec). On a repeat
    run within the same second a numeric suffix ``_1``, ``_2``, ... is appended so
    that the second run does not fail and does not overwrite the first snapshot.
    """
    base = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    candidate = backups_dir / base
    suffix = 1
    while True:
        try:
            candidate.mkdir(parents=True)
            return candidate
        except FileExistsError:
            candidate = backups_dir / f"{base}_{suffix}"
            suffix += 1


def _snapshot_db(db_path: Path, target: Path) -> None:
    """Creates a DB snapshot via ``VACUUM INTO`` (no file copying).

    ``VACUUM INTO`` requires that the target file not already exist; the
    generation directory was just created empty, so there is no collision. Opening
    a full connection works correctly even with another process's active WAL
    connection: readers and the writer in WAL do not block each other, and
    ``busy_timeout`` guards against brief locks.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        # Parameterizing the file name in VACUUM INTO is not supported by SQLite
        # syntax (it is not a value placeholder but a file name), so the path is
        # escaped as a SQL string literal. The path comes from the config; there
        # is no user input in it.
        literal = str(target).replace("'", "''")
        conn.execute(f"VACUUM INTO '{literal}'")
    finally:
        conn.close()


def _copy_attachments(attachments_dir: Path, target: Path) -> None:
    """Recursively copies the attachments directory (exact bytes, nested folders).

    If the attachments directory does not exist (no note has a file) — silently
    skip: the absence of attachments must not fail the backup.
    """
    if attachments_dir.exists():
        shutil.copytree(attachments_dir, target)


def _rotate(backups_dir: Path, retention: int = RETENTION) -> None:
    """Keeps the ``retention`` newest generations (by name-date), deletes the rest.

    Old directories are determined by the lexicographic order of names (the
    YYYY-MM-DD_HHMMSS format guarantees correct sorting by creation time). Uses
    ``ignore_errors=True`` on deletion so as not to fail if a directory is manually
    removed/moved between runs.
    """
    generations = sorted(p for p in backups_dir.iterdir() if p.is_dir())
    excess = len(generations) - retention
    for old in generations[:excess] if excess > 0 else []:
        shutil.rmtree(old, ignore_errors=True)


def main() -> int:
    """Orchestrates the entire backup process.

    Creates a new generation with a DB snapshot and a copy of the attachments,
    applies rotation. Prints a creation message to stdout (format:
    ASCII-compatible, path in Unicode). Returns 0 on success; otherwise an
    exception takes the process down with a non-zero code.
    """
    backups_dir = Path(config.BACKUPS_DIR)
    backups_dir.mkdir(parents=True, exist_ok=True)

    generation_dir = _unique_generation_dir(backups_dir)

    snapshot_path = generation_dir / Path(config.DB_PATH).name
    _snapshot_db(Path(config.DB_PATH), snapshot_path)

    attachments_copy = generation_dir / Path(config.ATTACHMENTS_DIR).name
    _copy_attachments(Path(config.ATTACHMENTS_DIR), attachments_copy)

    _rotate(backups_dir)

    print(f"Backup created: {generation_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
