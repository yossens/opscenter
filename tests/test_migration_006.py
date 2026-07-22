"""T1 tests: migration 006 — notes rebuild + FTS5 redesign.

Acceptance criteria source — docs/specs/006-custom-improvements.md, task T1
(the "Design → Migration mechanics", "Rebuilt notes table" and "FTS5 redesign"
sections, plus the T1 "Acceptance criteria" block). Migration 006 no longer
consolidates stages; it only (a) FK-safe rebuilds the notes table (adds
is_pinned, ocr_text, and 'info' to the note_type/suggested_note_type CHECKs),
(b) redesigns notes_fts as a two-column external-content FTS5 (body, ocr_text),
and (c) bumps schema_version to '6'.

Only fixtures from ``tests/conftest.py`` are used: ``project_root``,
``data_dir``, ``config``, ``db_module``, ``initialized_db``, ``db_path``,
``sqlite_conn``. For the "FK-toggle guard" criterion (an attachment surviving
the notes rebuild and cascade being re-applied afterward) a genuine pre-006 DB
is built (migrations 001-004 applied directly on a raw sqlite3 connection,
bypassing ``app.db._discover_migrations()``), a note with an attachment is
seeded, and ONLY THEN the real ``db_module.init_db()`` is called — this is the
"real migration path" (executescript+commit without a test-wrapping
transaction) on which the semantics of the leading ``PRAGMA foreign_keys=OFF``
depend (Design, pts 2-3).
"""

from __future__ import annotations

import sqlite3

import pytest

from helpers import _insert_deal_migration as _insert_deal
from helpers import _insert_note_migration as _insert_note

MIGRATION_006_NAME = "006_custom_improvements.sql"

NOTES_TARGET_COLUMNS = {
    "id",
    "body",
    "status",
    "deal_id",
    "note_type",
    "created_at",
    "suggested_deal_id",
    "llm_confidence",
    "llm_status",
    "suggested_note_type",
    "llm_draft",
    "is_pinned",
    "ocr_text",
}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _build_pre006_db(config_module, project_root):
    """Build a genuine pre-006 DB on disk (migrations 001-004), bypassing app.db.

    Reads the migration files directly from app/migrations and runs them on a
    new file at config_module.DB_PATH — the same path db_module.init_db() later
    uses in this same test.
    """
    migrations_dir = project_root / "app" / "migrations"
    conn = sqlite3.connect(str(config_module.DB_PATH))
    conn.row_factory = sqlite3.Row
    for name in (
        "001_init.sql",
        "002_remove_zhdem_raschet.sql",
        "003_hang_detector.sql",
        "004_gemini_parsing.sql",
    ):
        script = (migrations_dir / name).read_text(encoding="utf-8")
        conn.executescript(script)
    conn.commit()
    return conn


def _first_stage_id(conn) -> int:
    row = conn.execute("SELECT id FROM stages ORDER BY position LIMIT 1").fetchone()
    assert row is not None
    return row["id"]


def _insert_attachment(conn, note_id: int) -> int:
    cur = conn.execute(
        """
        INSERT INTO attachments
            (note_id, original_name, stored_name, mime_type, size_bytes, created_at)
        VALUES (?, 'original.txt', 'stored-abc123.txt', 'text/plain', 10, '2026-01-01T00:00:00')
        """,
        (note_id,),
    )
    conn.commit()
    return cur.lastrowid


def _split_sql_statements(sql_text: str) -> list[str]:
    """Strip line comments -- and split the script into statements by ';'."""
    lines = [line.split("--", 1)[0] for line in sql_text.splitlines()]
    cleaned = "\n".join(lines)
    return [stmt.strip() for stmt in cleaned.split(";") if stmt.strip()]


# ---------------------------------------------------------------------------
# Fixtures: a genuine pre-006 DB with a note + attachment, then 006 applied the
# real way.
# ---------------------------------------------------------------------------


@pytest.fixture
def pre006_seed(config, project_root):
    """Build a pre-006 DB and seed the data the migration 006 criteria need:
    a note with an attachment (the FK-toggle criterion)."""
    conn = _build_pre006_db(config, project_root)
    note_id = _insert_note(conn, body="Note with an attachment before migration 006")
    attachment_id = _insert_attachment(conn, note_id)
    conn.close()
    return {"note_id": note_id, "attachment_id": attachment_id}


@pytest.fixture
def migrated(pre006_seed, db_module, config):
    """Apply 006 the REAL migration way: init_db() over the pre-006 DB.

    Critical for the FK-toggle guard criterion: init_db does
    conn.executescript(script) immediately followed by conn.commit(), without a
    test-wrapping transaction — otherwise the leading PRAGMA foreign_keys=OFF in
    the 006 script would be a no-op (Design → Migration mechanics, pts 2-3), and
    DROP TABLE notes would cascade-delete the attachment.
    """
    db_module.init_db()
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    yield conn, pre006_seed
    conn.close()


# ---------------------------------------------------------------------------
# Migration file: structure exactly per Design → Migration mechanics, pt 3
# ---------------------------------------------------------------------------


def test_migration_006_file_exists(project_root):
    path = project_root / "app" / "migrations" / MIGRATION_006_NAME
    assert path.exists(), f"app/migrations/{MIGRATION_006_NAME} must exist"


def test_migration_006_script_structure_matches_design(project_root):
    """PRAGMA foreign_keys=OFF (first, in autocommit) -> BEGIN -> ... ->
    PRAGMA foreign_key_check -> COMMIT -> PRAGMA foreign_keys=ON (last, after
    COMMIT, again in autocommit). The order is not an implementation detail but
    a direct Design requirement (otherwise PRAGMA OFF inside a transaction is a
    no-op)."""
    path = project_root / "app" / "migrations" / MIGRATION_006_NAME
    assert path.exists()
    statements = _split_sql_statements(path.read_text(encoding="utf-8"))
    assert statements, "the migration must not be empty"
    normalized = ["".join(s.upper().split()) for s in statements]

    assert normalized[0] == "PRAGMAFOREIGN_KEYS=OFF", (
        "the script's first statement must be PRAGMA foreign_keys=OFF "
        "(before any BEGIN), otherwise it runs inside a transaction and becomes a no-op"
    )
    assert normalized[1] == "BEGIN", "the second statement must be BEGIN"
    assert "PRAGMAFOREIGN_KEY_CHECK" in normalized, (
        "the script must contain PRAGMA foreign_key_check as defense-in-depth"
    )
    assert normalized[-1] == "PRAGMAFOREIGN_KEYS=ON", (
        "the script's last statement must be PRAGMA foreign_keys=ON "
        "(after COMMIT, again in autocommit)"
    )
    commit_positions = [i for i, s in enumerate(normalized) if s == "COMMIT"]
    assert commit_positions, "the script must contain COMMIT"
    assert commit_positions[-1] == len(normalized) - 2, (
        "COMMIT must come immediately before the final PRAGMA foreign_keys=ON"
    )


# ---------------------------------------------------------------------------
# Criterion: schema_version goes from '4' to '6' by applying migration 006 —
# the final "latest" version after init_db() (006 is the last migration).
# ---------------------------------------------------------------------------


def test_fresh_init_db_schema_version_is_6(sqlite_conn):
    row = sqlite_conn.execute(
        "SELECT value FROM app_meta WHERE key='schema_version'"
    ).fetchone()
    assert row is not None
    assert row["value"] == "6"


def test_migration_006_upgrade_path_schema_version_is_6(migrated):
    conn, _ = migrated
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key='schema_version'"
    ).fetchone()
    assert row is not None
    assert row["value"] == "6"


# ---------------------------------------------------------------------------
# Criterion: the notes rebuild keeps all 13 columns + both CHECKs + new columns
# ---------------------------------------------------------------------------


def test_notes_table_has_all_13_target_columns(sqlite_conn):
    columns = {row["name"] for row in sqlite_conn.execute("PRAGMA table_info(notes)")}
    assert columns == NOTES_TARGET_COLUMNS


def test_notes_is_pinned_not_null_with_default_0(sqlite_conn):
    info = {row["name"]: row for row in sqlite_conn.execute("PRAGMA table_info(notes)")}
    assert info["is_pinned"]["notnull"] == 1
    assert str(info["is_pinned"]["dflt_value"]).strip() == "0"


def test_notes_ocr_text_column_is_nullable(sqlite_conn):
    info = {row["name"]: row for row in sqlite_conn.execute("PRAGMA table_info(notes)")}
    assert info["ocr_text"]["notnull"] == 0


def test_notes_is_pinned_defaults_to_0_on_insert(sqlite_conn):
    note_id = _insert_note(sqlite_conn, body="without explicit is_pinned")
    row = sqlite_conn.execute(
        "SELECT is_pinned FROM notes WHERE id=?", (note_id,)
    ).fetchone()
    assert row["is_pinned"] == 0


def test_notes_ocr_text_defaults_to_null_on_insert(sqlite_conn):
    note_id = _insert_note(sqlite_conn, body="without OCR")
    row = sqlite_conn.execute(
        "SELECT ocr_text FROM notes WHERE id=?", (note_id,)
    ).fetchone()
    assert row["ocr_text"] is None


def test_check_rejects_attached_status_without_deal_id_after_006(sqlite_conn):
    with pytest.raises(sqlite3.IntegrityError):
        sqlite_conn.execute(
            """
            INSERT INTO notes (body, status, deal_id, created_at)
            VALUES ('text', 'attached', NULL, '2026-01-01T00:00:00')
            """
        )


def test_check_rejects_inbox_status_with_deal_id_after_006(sqlite_conn):
    stage_id = _first_stage_id(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "Company", stage_id, "2026-01-01T00:00:00")
    with pytest.raises(sqlite3.IntegrityError):
        sqlite_conn.execute(
            """
            INSERT INTO notes (body, status, deal_id, created_at)
            VALUES ('text', 'inbox', ?, '2026-01-01T00:00:00')
            """,
            (deal_id,),
        )


def test_notes_indexes_recreated_after_006(sqlite_conn):
    names = {
        row["name"]
        for row in sqlite_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='notes'"
        )
    }
    assert {"idx_notes_status_created", "idx_notes_deal"} <= names


# ---------------------------------------------------------------------------
# Criterion: note_type/suggested_note_type='info' pass, 'bogus' does not
# ---------------------------------------------------------------------------


def test_note_type_info_inserts_successfully(sqlite_conn):
    cur = sqlite_conn.execute(
        "INSERT INTO notes (body, note_type, created_at) "
        "VALUES ('text', 'info', '2026-01-01T00:00:00')"
    )
    sqlite_conn.commit()
    row = sqlite_conn.execute(
        "SELECT note_type FROM notes WHERE id=?", (cur.lastrowid,)
    ).fetchone()
    assert row["note_type"] == "info"


def test_suggested_note_type_info_inserts_successfully(sqlite_conn):
    cur = sqlite_conn.execute(
        "INSERT INTO notes (body, suggested_note_type, created_at) "
        "VALUES ('text', 'info', '2026-01-01T00:00:00')"
    )
    sqlite_conn.commit()
    row = sqlite_conn.execute(
        "SELECT suggested_note_type FROM notes WHERE id=?", (cur.lastrowid,)
    ).fetchone()
    assert row["suggested_note_type"] == "info"


def test_note_type_bogus_still_rejected_after_006(sqlite_conn):
    with pytest.raises(sqlite3.IntegrityError):
        sqlite_conn.execute(
            "INSERT INTO notes (body, note_type, created_at) "
            "VALUES ('text', 'bogus', '2026-01-01T00:00:00')"
        )


def test_suggested_note_type_bogus_still_rejected_after_006(sqlite_conn):
    with pytest.raises(sqlite3.IntegrityError):
        sqlite_conn.execute(
            "INSERT INTO notes (body, suggested_note_type, created_at) "
            "VALUES ('text', 'bogus', '2026-01-01T00:00:00')"
        )


# ---------------------------------------------------------------------------
# Criterion: two-column FTS5 (body, ocr_text), matches on ocr_text alone
# ---------------------------------------------------------------------------


def test_notes_fts_is_two_column_table_body_and_ocr_text(sqlite_conn):
    cur = sqlite_conn.execute("SELECT * FROM notes_fts LIMIT 0")
    columns = [d[0] for d in cur.description]
    assert columns == ["body", "ocr_text"]


def test_notes_fts_is_virtual_fts5_table(sqlite_conn):
    row = sqlite_conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'notes_fts'"
    ).fetchone()
    assert row is not None
    assert "fts5" in (row["sql"] or "").lower()


def test_notes_fts_matches_on_ocr_text_alone_when_body_empty(sqlite_conn):
    cur = sqlite_conn.execute(
        "INSERT INTO notes (body, ocr_text, created_at) "
        "VALUES ('', 'Signed agreement scan', '2026-01-01T00:00:00')"
    )
    sqlite_conn.commit()
    note_id = cur.lastrowid
    rows = sqlite_conn.execute(
        "SELECT rowid FROM notes_fts WHERE notes_fts MATCH 'signed'"
    ).fetchall()
    assert note_id in {row["rowid"] for row in rows}


# ---------------------------------------------------------------------------
# Criterion: FK-toggle guard — the note and attachment survive the notes
# rebuild, and after the migration cascade deletion works again.
# ---------------------------------------------------------------------------


def test_note_survives_006_rebuild(migrated):
    conn, seed = migrated
    row = conn.execute("SELECT id FROM notes WHERE id=?", (seed["note_id"],)).fetchone()
    assert row is not None, "the note was lost during the notes rebuild by migration 006"


def test_attachment_survives_006_rebuild(migrated):
    conn, seed = migrated
    row = conn.execute(
        "SELECT id FROM attachments WHERE id=?", (seed["attachment_id"],)
    ).fetchone()
    assert row is not None, (
        "the attachment was cascade-deleted during DROP TABLE notes in the rebuild — "
        "the leading PRAGMA foreign_keys=OFF did not take effect (Design, Risks)"
    )


def test_cascade_delete_works_again_after_006_migration(migrated):
    conn, seed = migrated
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM notes WHERE id=?", (seed["note_id"],))
    conn.commit()
    remaining = conn.execute(
        "SELECT COUNT(*) FROM attachments WHERE note_id=?", (seed["note_id"],)
    ).fetchone()[0]
    assert remaining == 0, (
        "after migration 006 (and PRAGMA foreign_keys=ON) the ON DELETE CASCADE "
        "must again delete attachments when a note is deleted"
    )
