"""T8 tests: backup.py — DB snapshot via VACUUM INTO, attachments copy,
rotation of 7 generations.

Acceptance criteria source — docs/specs/001-step1-inbox-pipeline.md, task T8
(and related edge cases from the "Risks and edge cases" section: "Running
backup.py twice within one second"). The tests are written against the spec,
not the implementation: at the time of writing ``backup.py`` does not yet exist
in the project root (a correct TDD state — the tests collect but fail when run).

Only fixtures from ``tests/conftest.py`` are used: ``project_root``,
``data_dir``, ``config``, ``initialized_db``, ``sqlite_conn``. No new fixtures
were added to ``conftest.py`` — all helper code (running ``backup.py`` as a
subprocess, listing generation directories) lives locally in this file.

``backup.py`` is a script (not an importable module with a public API, per the
spec), so all tests run it literally as written in the acceptance criteria:
"After running against tmp data..." — via ``subprocess.run([sys.executable,
".../backup.py"])`` with ``OPSCENTER_DATA_DIR`` inherited from the test's
environment (``tests/conftest.py`` sets this variable via ``monkeypatch.setenv``
in the ``data_dir`` fixture, and ``subprocess.run`` without an explicit ``env=``
inherits ``os.environ`` of the current process). This way the tests do not
couple to the script's internals (presence/name of a ``main()`` function, etc.).

Assumptions about the result shape that the spec fixes in the task description
text but does not give as a literal JSON schema (since it is a CLI script, not
an API):

- The generation directory name has the format ``YYYY-MM-DD_HHMMSS`` (literally
  from the T8 task text: "creates `data/backups/<YYYY-MM-DD_HHMMSS>/`"); the
  regex below additionally allows an optional numeric suffix (``_1``/``-1``,
  etc.), since the T8 criterion "repeated run within the same second" explicitly
  leaves the choice between a suffix and skipping up to the implementation.
- The DB snapshot file inside the generation directory is named the same as the
  source DB file — ``config.DB_PATH.name`` (literally in the task text: "with a
  snapshot of `opscenter.db`").
- The copied attachments directory inside the generation directory is named the
  same as the source — ``config.ATTACHMENTS_DIR.name`` (``attachments``).
- The criterion "a repeated run within the same second does not fail (suffix or
  skip — pin down via test behavior)" is itself explicitly left variable by the
  spec; the corresponding test below checks the invariant required under either
  outcome (no process exits with an error, any remaining generation directory
  contains a valid readable snapshot), without picking one of the two allowed
  outcomes for the final directory count (1 or 2) in advance.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
from pathlib import Path

GENERATION_DIR_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}(?:[_-]\d+)?$")


def _run_backup(
    project_root: Path, timeout: float = 60.0
) -> subprocess.CompletedProcess:
    """Runs backup.py as a subprocess with the same interpreter as pytest.

    Does not pass its own ``env=`` explicitly, so it inherits ``os.environ`` of
    the current process — including ``OPSCENTER_DATA_DIR`` set by the ``data_dir``
    fixture via ``monkeypatch.setenv`` (which only takes effect inside the pytest
    process, so environment inheritance by the subprocess is required).
    """
    backup_py = project_root / "backup.py"
    return subprocess.run(
        [sys.executable, str(backup_py)],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _generation_dirs(config_module) -> list[Path]:
    backups_dir = Path(config_module.BACKUPS_DIR)
    if not backups_dir.exists():
        return []
    return sorted(p for p in backups_dir.iterdir() if p.is_dir())


def _make_fake_generation(config_module, name: str) -> Path:
    """Creates a minimally valid generation directory directly on disk
    (bypassing an actual backup.py run) — needed to simulate "9 existing
    generations" in reasonable time without 9 real script runs.
    """
    backups_dir = Path(config_module.BACKUPS_DIR)
    backups_dir.mkdir(parents=True, exist_ok=True)
    gen_dir = backups_dir / name
    gen_dir.mkdir()

    conn = sqlite3.connect(str(gen_dir / config_module.DB_PATH.name))
    conn.execute("CREATE TABLE placeholder (id INTEGER)")
    conn.commit()
    conn.close()

    (gen_dir / config_module.ATTACHMENTS_DIR.name).mkdir()
    return gen_dir


# ===========================================================================
# Basic behavior: generation directory with a date-time name, valid snapshot
# ===========================================================================


def test_backup_script_exists_in_project_root(project_root):
    assert (project_root / "backup.py").exists(), (
        "backup.py must exist in the project root (docs/specs T8)"
    )


def test_backup_creates_generation_directory_with_date_time_name(
    config, initialized_db, project_root
):
    result = _run_backup(project_root)

    assert result.returncode == 0, (
        f"backup.py exited with an error: stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )

    dirs = _generation_dirs(config)
    assert len(dirs) == 1, f"expected exactly one generation directory, got: {dirs}"
    assert GENERATION_DIR_NAME_RE.match(dirs[0].name), (
        "generation directory name does not match the format YYYY-MM-DD_HHMMSS: "
        f"{dirs[0].name!r}"
    )


def test_backup_generation_directory_lives_inside_configured_data_dir(
    config, initialized_db, project_root, data_dir
):
    """Paths come from app/config.py: the generation appears strictly inside the
    test OPSCENTER_DATA_DIR, not in the repository's real data/ directory.
    """
    result = _run_backup(project_root)
    assert result.returncode == 0, result.stderr

    dirs = _generation_dirs(config)
    assert len(dirs) == 1
    assert dirs[0].is_relative_to(data_dir)

    real_data_dir = project_root / "data"
    if real_data_dir.exists():
        real_backups_dir = real_data_dir / "backups"
        if real_backups_dir.exists():
            before_names = {p.name for p in real_backups_dir.iterdir() if p.is_dir()}
            assert dirs[0].name not in before_names or dirs[0] not in list(
                real_backups_dir.iterdir()
            ), "backup must not write into the repository's real data/ directory"


def test_backup_snapshot_is_valid_sqlite_with_same_deal_and_note_rows_as_original(
    config, initialized_db, sqlite_conn, project_root
):
    stage_id = sqlite_conn.execute(
        "SELECT id FROM stages ORDER BY position LIMIT 1"
    ).fetchone()[0]
    sqlite_conn.execute(
        """
        INSERT INTO deals (title, stage_id, stage_entered_at, last_activity_at, created_at)
        VALUES ('Item for backup', ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00', '2026-01-01T00:00:00')
        """,
        (stage_id,),
    )
    deal_id = sqlite_conn.execute(
        "SELECT id FROM deals WHERE title = 'Item for backup'"
    ).fetchone()[0]
    sqlite_conn.execute(
        """
        INSERT INTO notes (body, status, deal_id, created_at)
        VALUES ('Note for backup', 'attached', ?, '2026-01-01T00:00:00')
        """,
        (deal_id,),
    )
    sqlite_conn.commit()

    result = _run_backup(project_root)
    assert result.returncode == 0, result.stderr

    dirs = _generation_dirs(config)
    assert len(dirs) == 1
    generation_dir = dirs[0]

    snapshot_path = generation_dir / config.DB_PATH.name
    assert snapshot_path.exists(), f"DB snapshot not found: {snapshot_path}"

    snapshot_conn = sqlite3.connect(str(snapshot_path))
    try:
        tables = {
            row[0]
            for row in snapshot_conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"deals", "notes", "stages"} <= tables

        deal_row = snapshot_conn.execute(
            "SELECT title FROM deals WHERE id = ?", (deal_id,)
        ).fetchone()
        assert deal_row is not None, "the snapshot must contain the original's deals row"
        assert deal_row[0] == "Item for backup"

        note_row = snapshot_conn.execute(
            "SELECT body, deal_id FROM notes WHERE deal_id = ?", (deal_id,)
        ).fetchone()
        assert note_row is not None, "the snapshot must contain the original's notes row"
        assert note_row[0] == "Note for backup"
        assert note_row[1] == deal_id

        stage_count = snapshot_conn.execute("SELECT COUNT(*) FROM stages").fetchone()[0]
        # 6 = the generic seed (migration 001): Backlog, To Do, In Progress,
        # Review, Blocked, Done.
        assert stage_count == 6, "the snapshot must contain the full set of stages"
    finally:
        snapshot_conn.close()


# ===========================================================================
# VACUUM INTO, not a byte-for-byte copy of the DB file
# ===========================================================================


def test_backup_script_source_uses_vacuum_into_and_no_raw_copy_of_db_file(project_root):
    backup_py = project_root / "backup.py"
    assert backup_py.exists()
    source = backup_py.read_text(encoding="utf-8")

    assert "VACUUM INTO" in source, (
        "backup.py must create the DB snapshot strictly via the literal 'VACUUM INTO'"
    )

    forbidden = re.findall(
        r"shutil\.copy(?:file|2)?\([^)]*\.db[^)]*\)", source, re.IGNORECASE
    )
    assert not forbidden, (
        "backup.py contains a byte-for-byte copy of the .db file via "
        f"shutil.copy*, instead of VACUUM INTO: {forbidden}"
    )


def test_backup_snapshot_includes_writes_committed_but_not_yet_checkpointed_from_wal(
    config, initialized_db, sqlite_conn, project_root
):
    """Indirectly verifies that the snapshot is made via VACUUM INTO, not a dumb
    byte-for-byte copy of ``opscenter.db``: in WAL mode the last committed rows
    may physically live only in the ``-wal`` file until a checkpoint, and a raw
    copy of the main DB file would not see them — VACUUM INTO, which opens a
    full connection, always sees them.
    """
    mode = sqlite_conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"

    stage_id = sqlite_conn.execute(
        "SELECT id FROM stages ORDER BY position LIMIT 1"
    ).fetchone()[0]
    sqlite_conn.execute(
        """
        INSERT INTO deals (title, stage_id, stage_entered_at, last_activity_at, created_at)
        VALUES ('WAL only', ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00', '2026-01-01T00:00:00')
        """,
        (stage_id,),
    )
    sqlite_conn.commit()
    # sqlite_conn stays open (not closed by the fixture until the end of the
    # test) — the data may not yet have been checkpointed into the main DB file.

    result = _run_backup(project_root)
    assert result.returncode == 0, result.stderr

    dirs = _generation_dirs(config)
    assert len(dirs) == 1
    snapshot_path = dirs[0] / config.DB_PATH.name

    snapshot_conn = sqlite3.connect(str(snapshot_path))
    try:
        row = snapshot_conn.execute(
            "SELECT title FROM deals WHERE title = 'WAL only'"
        ).fetchone()
        assert row is not None, (
            "the snapshot does not contain a row committed via WAL without an "
            "explicit checkpoint — looks like a byte-for-byte copy of the DB "
            "file instead of VACUUM INTO"
        )
    finally:
        snapshot_conn.close()


# ===========================================================================
# Attachments directory copy — in full, recursively
# ===========================================================================


def test_backup_copies_attachments_directory_recursively_with_same_bytes(
    config, initialized_db, project_root
):
    attachments_dir = Path(config.ATTACHMENTS_DIR)
    attachments_dir.mkdir(parents=True, exist_ok=True)

    top_file = attachments_dir / "a1b2c3d4.png"
    top_bytes = b"\x89PNG\r\n\x1a\nFAKE-IMAGE-BYTES-FOR-TEST"
    top_file.write_bytes(top_bytes)

    nested_dir = attachments_dir / "nested"
    nested_dir.mkdir()
    nested_file = nested_dir / "doc.txt"
    nested_text = "Nested text file with non-ASCII content"
    nested_file.write_text(nested_text, encoding="utf-8")

    result = _run_backup(project_root)
    assert result.returncode == 0, result.stderr

    dirs = _generation_dirs(config)
    assert len(dirs) == 1
    generation_dir = dirs[0]

    copied_attachments = generation_dir / attachments_dir.name
    assert copied_attachments.exists() and copied_attachments.is_dir(), (
        f"the attachments directory was not copied into the backup generation: {copied_attachments}"
    )

    copied_top_file = copied_attachments / "a1b2c3d4.png"
    assert copied_top_file.exists(), "the top-level attachments file was not copied"
    assert copied_top_file.read_bytes() == top_bytes

    copied_nested_file = copied_attachments / "nested" / "doc.txt"
    assert copied_nested_file.exists(), (
        "nested attachment subdirectories must be copied in full (recursively)"
    )
    assert copied_nested_file.read_text(encoding="utf-8") == nested_text


def test_backup_copies_attachments_directory_even_when_empty(
    config, initialized_db, project_root
):
    """An empty attachments directory (no notes/files) must not crash the script;
    the directory is still copied (either empty or simply absent in the original
    — both outcomes must not raise an error)."""
    result = _run_backup(project_root)

    assert result.returncode == 0, (
        f"backup.py must not crash when the attachments directory is missing/empty: "
        f"stderr={result.stderr!r}"
    )
    dirs = _generation_dirs(config)
    assert len(dirs) == 1


# ===========================================================================
# Rotation: 9 existing generations -> exactly the 7 newest after a run
# ===========================================================================


def test_backup_retention_keeps_exactly_7_newest_when_9_generations_exist(
    config, initialized_db, project_root
):
    fake_names = [f"2020-01-0{i}_000000" for i in range(1, 10)]  # 9 old generations
    for name in fake_names:
        _make_fake_generation(config, name)

    result = _run_backup(project_root)
    assert result.returncode == 0, result.stderr

    dirs_after = _generation_dirs(config)
    names_after = {p.name for p in dirs_after}

    assert len(names_after) == 7, (
        f"expected exactly 7 generations after rotation, got {len(names_after)}: "
        f"{sorted(names_after)}"
    )

    oldest_three = fake_names[:3]
    newest_six_old = fake_names[3:]

    for old_name in oldest_three:
        assert old_name not in names_after, (
            f"the stale generation {old_name!r} must be removed during rotation"
        )
    for kept_name in newest_six_old:
        assert kept_name in names_after, (
            f"the generation {kept_name!r} should have remained among the 7 newest"
        )

    new_generation_names = names_after - set(fake_names)
    assert len(new_generation_names) == 1, (
        "the new generation created by the current backup.py run must be "
        f"present among the remaining 7: {sorted(names_after)}"
    )


def test_backup_retention_does_not_trigger_below_7_generations(
    config, initialized_db, project_root
):
    fake_names = [f"2020-01-0{i}_000000" for i in range(1, 6)]  # 5 old generations
    for name in fake_names:
        _make_fake_generation(config, name)

    result = _run_backup(project_root)
    assert result.returncode == 0, result.stderr

    names_after = {p.name for p in _generation_dirs(config)}
    # 5 old + 1 new = 6, rotation (limit 7) must not remove any.
    assert set(fake_names) <= names_after
    assert len(names_after) == 6


# ===========================================================================
# A repeated run within the same second — does not crash
# ===========================================================================


def test_backup_repeated_run_immediately_after_does_not_crash(
    config, initialized_db, project_root
):
    result1 = _run_backup(project_root)
    result2 = _run_backup(project_root)

    assert result1.returncode == 0, (
        f"the first backup.py run failed: stderr={result1.stderr!r}"
    )
    assert result2.returncode == 0, (
        f"the repeated backup.py run failed (it must either create a generation "
        f"with a suffix or cleanly skip creation — but not crash): "
        f"stderr={result2.stderr!r}"
    )

    dirs = _generation_dirs(config)
    # The spec explicitly leaves the choice between "suffix" (2 directories) and
    # "skip" (1 directory) up to the implementation — we only pin down the fact
    # that both outcomes are allowed and that the result is neither empty nor
    # more than 2.
    assert 1 <= len(dirs) <= 2, (
        f"after two quick consecutive runs, expected 1 or 2 generation "
        f"directories, got {len(dirs)}: {[d.name for d in dirs]}"
    )

    for gen_dir in dirs:
        db_file = gen_dir / config.DB_PATH.name
        assert db_file.exists(), f"the {gen_dir} directory is missing the DB snapshot"
        conn = sqlite3.connect(str(db_file))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            assert "stages" in tables, (
                f"the snapshot in {gen_dir} must be a valid DB with a schema, not "
                "a corrupted/partial file"
            )
        finally:
            conn.close()


# ===========================================================================
# Does not crash or corrupt the live DB with an active WAL connection
# ===========================================================================


def test_backup_does_not_crash_or_corrupt_live_wal_db_with_active_connection(
    config, initialized_db, sqlite_conn, project_root
):
    mode = sqlite_conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal", "the test checks behavior specifically in WAL mode"

    stage_id = sqlite_conn.execute(
        "SELECT id FROM stages ORDER BY position LIMIT 1"
    ).fetchone()[0]
    sqlite_conn.execute(
        """
        INSERT INTO deals (title, stage_id, stage_entered_at, last_activity_at, created_at)
        VALUES ('Live item', ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00', '2026-01-01T00:00:00')
        """,
        (stage_id,),
    )
    sqlite_conn.commit()

    # The connection stays open (like a running application) and holds an active
    # read transaction during the backup.py run — WAL allows this (readers do
    # not block writers and vice versa).
    sqlite_conn.execute("BEGIN")
    sqlite_conn.execute("SELECT COUNT(*) FROM deals").fetchone()

    result = _run_backup(project_root)

    assert result.returncode == 0, (
        f"backup.py failed with an active connection to the live WAL DB: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    sqlite_conn.execute("COMMIT")

    row = sqlite_conn.execute(
        "SELECT title FROM deals WHERE title = 'Live item'"
    ).fetchone()
    assert row is not None, "the live DB must not be harmed by running backup.py"

    integrity = sqlite_conn.execute("PRAGMA integrity_check").fetchone()[0]
    assert integrity == "ok", "the live DB must remain intact after backup.py"

    # The live DB is still writable after the backup.
    sqlite_conn.execute(
        """
        INSERT INTO deals (title, stage_id, stage_entered_at, last_activity_at, created_at)
        VALUES ('After backup', ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00', '2026-01-01T00:00:00')
        """,
        (stage_id,),
    )
    sqlite_conn.commit()
    after_row = sqlite_conn.execute(
        "SELECT id FROM deals WHERE title = 'After backup'"
    ).fetchone()
    assert after_row is not None
