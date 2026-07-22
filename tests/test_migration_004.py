"""T1 tests: dependencies, config, .env, migration 004 (Gemini parsing).

Acceptance criteria source — docs/specs/003-step3-gemini-parsing.md, task T1
(the "Migration 004" and "Other decisions" pts 1-4,8-9 sections). The tests are
written against the spec, not the implementation: at the time of writing
``app/migrations/004_gemini_parsing.sql``, the LLM constants in ``app/config.py``
and ``.env.example`` did not yet exist (a correct TDD state — tests collect, but
fail).

Only fixtures from ``tests/conftest.py`` are used: ``project_root``,
``data_dir``, ``config``, ``db_module``, ``initialized_db``, ``db_path``,
``sqlite_conn``. For the "upgrade path" criterion a genuine version-3 DB is built
(001+002+003 applied directly on a raw sqlite3 connection, bypassing
``app.db._discover_migrations()`` so the test does not depend on whether the
not-yet-written 004 file is found) — real Step 1-2 data is seeded, and ONLY THEN
the real ``db_module.init_db()`` is called, which must detect version 3 and apply
only migration 004. This proves a v3->v4 transition, not a no-op comparison on an
already-v4 DB.
"""

from __future__ import annotations

import sqlite3
import sys

import pytest
from helpers import _insert_deal_migration as _insert_deal

MIGRATION_004_NAME = "004_gemini_parsing.sql"

# Exact default confidence-threshold string seeded by migration 004.
DEFAULT_CONFIDENCE_THRESHOLD_STR = "0.7"

LLM_CALLS_COLUMNS = {
    "id",
    "created_at",
    "model",
    "input_tokens",
    "output_tokens",
    "duration_ms",
    "status",
    "purpose",
}

# Columns of notes/deals/stages/attachments/deal_pings that already exist at
# version 3 (Steps 1-2) — used for a byte-for-byte before/after comparison in
# the v3->v4 upgrade-path test. Migration 004 must not change them.
NOTE_COLUMNS_V3 = [
    "id",
    "body",
    "status",
    "deal_id",
    "note_type",
    "created_at",
    "suggested_deal_id",
    "llm_confidence",
    "llm_status",
]
DEAL_COLUMNS_V3 = [
    "id",
    "title",
    "company",
    "partner",
    "rate",
    "jurisdiction",
    "waiting_on",
    "description",
    "stage_id",
    "stage_entered_at",
    "last_activity_at",
    "created_at",
    "closed_at",
    "drive_folder_url",
    "snoozed_until",
]
STAGE_COLUMNS_V3 = [
    "id",
    "name",
    "position",
    "threshold_days",
    "is_terminal",
    "track_hangs",
]
ATTACHMENT_COLUMNS_V3 = [
    "id",
    "note_id",
    "original_name",
    "stored_name",
    "mime_type",
    "size_bytes",
    "created_at",
]
DEAL_PINGS_COLUMNS_V3 = ["id", "deal_id", "pinged_at", "escalation_step", "ping_text"]

# Operations allowed for the "additive-only" migration 004 (the "Diff review" section, cr. 5).
ALLOWED_STATEMENT_PREFIXES = (
    "ALTER TABLE",
    "CREATE TABLE",
    "CREATE INDEX",
    "CREATE UNIQUE INDEX",
    "INSERT INTO",
    "UPDATE",
)

# Tables existing at v3 that migration 004 must not recreate.
EXISTING_TABLES_V3 = [
    "deals",
    "stages",
    "notes",
    "attachments",
    "app_meta",
    "deal_pings",
]


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _purge_app_modules() -> None:
    """Local copy of the conftest helper — needed to re-import app.config with
    extra env variables inside individual tests (env override)."""
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


def _first_stage_id(conn, terminal: bool = False) -> int:
    row = conn.execute(
        "SELECT id FROM stages WHERE is_terminal = ? ORDER BY position LIMIT 1",
        (1 if terminal else 0,),
    ).fetchone()
    assert row is not None, "expected at least one stage with the requested is_terminal flag"
    return row["id"]


def _insert_attached_note(conn, deal_id: int, body: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO notes (body, status, deal_id, created_at)
        VALUES (?, 'attached', ?, '2026-01-01T00:00:00')
        """,
        (body, deal_id),
    )
    conn.commit()
    return cur.lastrowid


def _insert_inbox_note(conn, body: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO notes (body, status, created_at)
        VALUES (?, 'inbox', '2026-01-01T00:00:00')
        """,
        (body,),
    )
    conn.commit()
    return cur.lastrowid


def _insert_attachment(
    conn, note_id: int, stored_name: str = "stored-abc123.txt"
) -> int:
    cur = conn.execute(
        """
        INSERT INTO attachments
            (note_id, original_name, stored_name, mime_type, size_bytes, created_at)
        VALUES (?, 'original.txt', ?, 'text/plain', 10, '2026-01-01T00:00:00')
        """,
        (note_id, stored_name),
    )
    conn.commit()
    return cur.lastrowid


def _insert_ping(conn, deal_id: int) -> int:
    cur = conn.execute(
        """
        INSERT INTO deal_pings (deal_id, pinged_at, escalation_step, ping_text)
        VALUES (?, '2026-01-02T00:00:00', 1, 'Test ping')
        """,
        (deal_id,),
    )
    conn.commit()
    return cur.lastrowid


def _dump_table(conn, table: str, columns: list[str]) -> list[dict]:
    cols = ", ".join(columns)
    rows = conn.execute(f"SELECT {cols} FROM {table} ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def _build_v3_db_with_data(config_module, project_root):
    """Build a genuine version-3 DB on disk (Step 1 + migrations 002+003), bypassing app.db.

    Reads 001_init.sql/002_remove_zhdem_raschet.sql/003_hang_detector.sql
    directly from app/migrations and runs them on a new file at
    config_module.DB_PATH — the same path db_module.init_db() later uses in this
    same test.
    """
    migrations_dir = project_root / "app" / "migrations"
    conn = sqlite3.connect(str(config_module.DB_PATH))
    conn.row_factory = sqlite3.Row
    for name in (
        "001_init.sql",
        "002_remove_zhdem_raschet.sql",
        "003_hang_detector.sql",
    ):
        script = (migrations_dir / name).read_text(encoding="utf-8")
        conn.executescript(script)
    conn.commit()
    return conn


def _split_sql_statements(sql_text: str) -> list[str]:
    """Strip line comments -- and split the script into statements."""
    lines = [line.split("--", 1)[0] for line in sql_text.splitlines()]
    cleaned = "\n".join(lines)
    return [stmt.strip() for stmt in cleaned.split(";") if stmt.strip()]


# ---------------------------------------------------------------------------
# Criterion 2: fresh DB after init_db() — schema_version, columns, table,
# index, app_meta.
# ---------------------------------------------------------------------------


def test_fresh_db_schema_version_is_current(sqlite_conn):
    # init_db() applies ALL migrations, so the current schema version is the
    # last accepted one (6 after 006).
    row = sqlite_conn.execute(
        "SELECT value FROM app_meta WHERE key = 'schema_version'"
    ).fetchone()
    assert row is not None
    assert row["value"] == "6"


def test_notes_has_suggested_note_type_column_nullable(sqlite_conn):
    info = {row["name"]: row for row in sqlite_conn.execute("PRAGMA table_info(notes)")}
    assert "suggested_note_type" in info
    assert info["suggested_note_type"]["notnull"] == 0


def test_notes_suggested_note_type_defaults_to_null_on_insert(sqlite_conn):
    note_id = _insert_inbox_note(sqlite_conn, "Note without a suggestion")
    row = sqlite_conn.execute(
        "SELECT suggested_note_type FROM notes WHERE id = ?", (note_id,)
    ).fetchone()
    assert row["suggested_note_type"] is None


@pytest.mark.parametrize("valid_type", ["status", "task", "agreement", "reminder"])
def test_notes_suggested_note_type_check_accepts_enum_values(sqlite_conn, valid_type):
    cur = sqlite_conn.execute(
        "INSERT INTO notes (body, suggested_note_type, created_at) VALUES (?, ?, '2026-01-01T00:00:00')",
        ("Note", valid_type),
    )
    sqlite_conn.commit()
    row = sqlite_conn.execute(
        "SELECT suggested_note_type FROM notes WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    assert row["suggested_note_type"] == valid_type


def test_notes_suggested_note_type_check_rejects_invalid_value(sqlite_conn):
    with pytest.raises(sqlite3.IntegrityError):
        sqlite_conn.execute(
            "INSERT INTO notes (body, suggested_note_type, created_at) VALUES (?, 'bogus', '2026-01-01T00:00:00')",
            ("Note",),
        )


def test_notes_has_llm_draft_column_nullable(sqlite_conn):
    info = {row["name"]: row for row in sqlite_conn.execute("PRAGMA table_info(notes)")}
    assert "llm_draft" in info
    assert info["llm_draft"]["notnull"] == 0


def test_notes_llm_draft_defaults_to_null_on_insert(sqlite_conn):
    note_id = _insert_inbox_note(sqlite_conn, "Note without a draft")
    row = sqlite_conn.execute(
        "SELECT llm_draft FROM notes WHERE id = ?", (note_id,)
    ).fetchone()
    assert row["llm_draft"] is None


def test_llm_calls_table_has_exact_metadata_columns(sqlite_conn):
    """Covers both criterion 2 (table/columns present) and criterion 4 (NO
    columns for prompt/response content) — the column set is checked for exact
    equality, so you cannot add an extra content column and still pass."""
    columns = {
        row["name"] for row in sqlite_conn.execute("PRAGMA table_info(llm_calls)")
    }
    assert columns == LLM_CALLS_COLUMNS


def test_llm_calls_notnull_and_default_columns(sqlite_conn):
    info = {
        row["name"]: row for row in sqlite_conn.execute("PRAGMA table_info(llm_calls)")
    }
    assert info["created_at"]["notnull"] == 1
    assert info["model"]["notnull"] == 1
    assert info["status"]["notnull"] == 1
    assert info["purpose"]["notnull"] == 1


def test_llm_calls_status_check_rejects_invalid_value(sqlite_conn):
    with pytest.raises(sqlite3.IntegrityError):
        sqlite_conn.execute(
            "INSERT INTO llm_calls (created_at, model, status) VALUES ('2026-01-01T00:00:00', 'm', 'bogus')"
        )


def test_llm_calls_insert_success_row_with_defaults(sqlite_conn):
    cur = sqlite_conn.execute(
        "INSERT INTO llm_calls (created_at, model, status) VALUES ('2026-01-01T00:00:00', 'gemini-3.1-flash-lite', 'success')"
    )
    sqlite_conn.commit()
    row = sqlite_conn.execute(
        "SELECT input_tokens, output_tokens, duration_ms, purpose FROM llm_calls WHERE id = ?",
        (cur.lastrowid,),
    ).fetchone()
    assert row["input_tokens"] == 0
    assert row["output_tokens"] == 0
    assert row["duration_ms"] == 0
    assert row["purpose"] == "parse_note"


def test_idx_llm_calls_created_exists_on_llm_calls(sqlite_conn):
    row = sqlite_conn.execute(
        "SELECT tbl_name FROM sqlite_master WHERE type = 'index' AND name = 'idx_llm_calls_created'"
    ).fetchone()
    assert row is not None
    assert row["tbl_name"] == "llm_calls"


def test_app_meta_seeds_llm_confidence_threshold_as_0_7(sqlite_conn):
    row = sqlite_conn.execute(
        "SELECT value FROM app_meta WHERE key = 'llm_confidence_threshold'"
    ).fetchone()
    assert row is not None
    assert row["value"] == DEFAULT_CONFIDENCE_THRESHOLD_STR


# ---------------------------------------------------------------------------
# Criterion 6: cross-check the threshold default — constant vs seeded value.
# ---------------------------------------------------------------------------


def test_default_confidence_threshold_constant_matches_seeded_value(
    config, sqlite_conn
):
    row = sqlite_conn.execute(
        "SELECT value FROM app_meta WHERE key = 'llm_confidence_threshold'"
    ).fetchone()
    assert row is not None
    assert str(config.DEFAULT_CONFIDENCE_THRESHOLD) == row["value"]


# ---------------------------------------------------------------------------
# Criterion 3: v3->v4 upgrade path on a genuine version-3 DB with data.
# ---------------------------------------------------------------------------


def test_migration_004_upgrade_preserves_step1_2_data(config, db_module, project_root):
    conn = _build_v3_db_with_data(config, project_root)

    non_terminal_stage_id = _first_stage_id(conn, terminal=False)
    deal_id = _insert_deal(conn, "Daisy Holding", non_terminal_stage_id)
    note_id = _insert_attached_note(conn, deal_id, "Agreement signed by the partner")
    _insert_inbox_note(conn, "A free inbox note")
    _insert_attachment(conn, note_id)
    _insert_ping(conn, deal_id)

    stages_before = _dump_table(conn, "stages", STAGE_COLUMNS_V3)
    deals_before = _dump_table(conn, "deals", DEAL_COLUMNS_V3)
    notes_before = _dump_table(conn, "notes", NOTE_COLUMNS_V3)
    attachments_before = _dump_table(conn, "attachments", ATTACHMENT_COLUMNS_V3)
    pings_before = _dump_table(conn, "deal_pings", DEAL_PINGS_COLUMNS_V3)

    version_before = conn.execute(
        "SELECT value FROM app_meta WHERE key = 'schema_version'"
    ).fetchone()["value"]
    assert version_before == "3", "test precondition: we build exactly a version-3 DB"
    conn.close()

    # The single call into the migration machinery (app/db.py is unchanged in
    # T1) — must detect version 3 and apply only 004.
    db_module.init_db()

    conn2 = sqlite3.connect(str(config.DB_PATH))
    conn2.row_factory = sqlite3.Row
    try:
        stages_after = _dump_table(conn2, "stages", STAGE_COLUMNS_V3)
        deals_after = _dump_table(conn2, "deals", DEAL_COLUMNS_V3)
        notes_after = _dump_table(conn2, "notes", NOTE_COLUMNS_V3)
        attachments_after = _dump_table(conn2, "attachments", ATTACHMENT_COLUMNS_V3)
        pings_after = _dump_table(conn2, "deal_pings", DEAL_PINGS_COLUMNS_V3)

        # init_db() on a v3 DB applies ALL pending migrations in a row, but none
        # of them (004-006) touches the seeded stages: migration 006 rebuilds
        # notes + FTS and no longer consolidates stages, so the stages dump is
        # byte-for-byte identical before and after. The invariant this test
        # protects is that migrations 004-006 do not corrupt Step 1-2 seed data.
        assert stages_after == stages_before, "stages rows changed by migrations 004-006"
        assert deals_after == deals_before, "old deals rows changed by migration 004"
        assert notes_after == notes_before, (
            "old notes rows changed by migration 004 (across v3 columns)"
        )
        assert attachments_after == attachments_before, (
            "old attachments rows changed by migration 004"
        )
        assert pings_after == pings_before, (
            "old deal_pings rows changed by migration 004"
        )

        version_after = conn2.execute(
            "SELECT value FROM app_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        # init_db() carries a v2/v3/v4 DB up to the last accepted version (6
        # after migration 006), not only to 004.
        assert version_after == "6"

        # All notes (including those created before the migration) — the LLM
        # suggestion has not run yet: llm_status='none', new columns NULL.
        note_rows = conn2.execute(
            "SELECT llm_status, suggested_note_type, llm_draft, suggested_deal_id FROM notes"
        ).fetchall()
        assert note_rows, "expected notes after the migration"
        for row in note_rows:
            assert row["llm_status"] == "none"
            assert row["suggested_note_type"] is None
            assert row["llm_draft"] is None
            assert row["suggested_deal_id"] is None

        # FTS search over an existing (pre-migration) note still works.
        matches = conn2.execute(
            "SELECT rowid FROM notes_fts WHERE notes_fts MATCH 'agreement'"
        ).fetchall()
        assert note_id in {row["rowid"] for row in matches}
    finally:
        conn2.close()


# ---------------------------------------------------------------------------
# Criterion 5: migration diff review — additive operations only, no existing
# table is recreated.
# ---------------------------------------------------------------------------


def test_migration_004_file_exists(project_root):
    path = project_root / "app" / "migrations" / MIGRATION_004_NAME
    assert path.exists(), f"app/migrations/{MIGRATION_004_NAME} must exist"


def test_migration_004_contains_no_drop_or_delete(project_root):
    path = project_root / "app" / "migrations" / MIGRATION_004_NAME
    text = path.read_text(encoding="utf-8")
    assert "DROP" not in text.upper()
    assert "DELETE" not in text.upper()


def test_migration_004_statements_are_only_additive_operations(project_root):
    path = project_root / "app" / "migrations" / MIGRATION_004_NAME
    text = path.read_text(encoding="utf-8")
    statements = _split_sql_statements(text)
    assert statements, "the migration must not be empty"
    for stmt in statements:
        upper = stmt.upper()
        assert upper.startswith(ALLOWED_STATEMENT_PREFIXES), (
            f"unexpected operation type in migration 004: {stmt[:60]!r}"
        )


@pytest.mark.parametrize("existing_table", EXISTING_TABLES_V3)
def test_migration_004_does_not_recreate_existing_tables(project_root, existing_table):
    path = project_root / "app" / "migrations" / MIGRATION_004_NAME
    text = path.read_text(encoding="utf-8").upper()
    assert f"CREATE TABLE {existing_table.upper()}" not in text
    assert f"CREATE TABLE IF NOT EXISTS {existing_table.upper()}" not in text


def test_migration_004_creates_llm_calls_table(project_root):
    path = project_root / "app" / "migrations" / MIGRATION_004_NAME
    text = path.read_text(encoding="utf-8").upper()
    assert "CREATE TABLE LLM_CALLS" in text


# ---------------------------------------------------------------------------
# Config constants (the "Other decisions" pts 1-4 section) + override via
# OPSCENTER_*.
# ---------------------------------------------------------------------------


def test_llm_config_defaults(config):
    assert config.LLM_MODEL == "gemini-3.1-flash-lite"
    assert config.LLM_TIMEOUT_S == 30
    assert config.LLM_NOTE_TEXT_MAX_CHARS == 8000
    assert config.LLM_IMAGE_MAX_BYTES == 7 * 1024 * 1024
    assert config.LLM_IMAGE_MAX_COUNT == 4
    assert config.LLM_PRICE_INPUT_PER_1M == pytest.approx(1.50)
    assert config.LLM_PRICE_OUTPUT_PER_1M == pytest.approx(9.00)
    assert config.DEFAULT_CONFIDENCE_THRESHOLD == pytest.approx(0.7)


def test_llm_model_overridable_via_env(data_dir, monkeypatch):
    monkeypatch.setenv("OPSCENTER_LLM_MODEL", "gemini-test-override")
    _purge_app_modules()
    import app.config as config_module

    assert config_module.LLM_MODEL == "gemini-test-override"


def test_llm_timeout_s_overridable_via_env(data_dir, monkeypatch):
    monkeypatch.setenv("OPSCENTER_LLM_TIMEOUT_S", "99")
    _purge_app_modules()
    import app.config as config_module

    assert config_module.LLM_TIMEOUT_S == 99


def test_default_confidence_threshold_overridable_via_env(data_dir, monkeypatch):
    monkeypatch.setenv("OPSCENTER_DEFAULT_CONFIDENCE_THRESHOLD", "0.42")
    _purge_app_modules()
    import app.config as config_module

    assert config_module.DEFAULT_CONFIDENCE_THRESHOLD == pytest.approx(0.42)


def test_llm_image_max_bytes_overridable_via_env(data_dir, monkeypatch):
    monkeypatch.setenv("OPSCENTER_LLM_IMAGE_MAX_BYTES", "12345")
    _purge_app_modules()
    import app.config as config_module

    assert config_module.LLM_IMAGE_MAX_BYTES == 12345


# ---------------------------------------------------------------------------
# Criterion 7: the secret comes strictly from the environment.
# ---------------------------------------------------------------------------


def test_gitignore_contains_env(project_root):
    text = (project_root / ".gitignore").read_text(encoding="utf-8")
    lines = {line.strip() for line in text.splitlines()}
    assert ".env" in lines


def test_env_example_exists_with_empty_gemini_key(project_root):
    path = project_root / ".env.example"
    assert path.exists(), ".env.example must exist"
    text = path.read_text(encoding="utf-8")
    assert "GEMINI_API_KEY" in text
    key_lines = [
        line for line in text.splitlines() if line.strip().startswith("GEMINI_API_KEY")
    ]
    assert key_lines, ".env.example must contain a GEMINI_API_KEY= line"
    for line in key_lines:
        assert line.strip() == "GEMINI_API_KEY=", (
            f"the GEMINI_API_KEY value in .env.example must be empty, got: {line!r}"
        )


def test_config_module_does_not_reference_secret_key(project_root):
    """Design decision 8: the secret does not appear in app/config.py — only
    non-secret LLM constants. The key is read by the SDK directly from the environment."""
    text = (project_root / "app" / "config.py").read_text(encoding="utf-8")
    assert "GEMINI_API_KEY" not in text


def test_main_calls_load_dotenv(project_root):
    text = (project_root / "app" / "main.py").read_text(encoding="utf-8")
    assert "load_dotenv" in text


def test_run_calls_load_dotenv(project_root):
    text = (project_root / "run.py").read_text(encoding="utf-8")
    assert "load_dotenv" in text


def test_requirements_lists_new_dependencies(project_root):
    text = (project_root / "requirements.txt").read_text(encoding="utf-8")
    names = {
        line.strip().split("=")[0].split(">")[0].split("<")[0].split("[")[0].lower()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert "httpx" in names
    assert "python-dotenv" in names
