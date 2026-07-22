"""T1 tests: migration 003 — deal_pings, snoozed_until, track_hangs, settings.

Acceptance criteria source — docs/specs/002-step2-hang-detector.md, task T1
(the "Migration 003" section, plus "DB schema"/"Terms" for context). The tests
are written against the spec, not the implementation: at the time of writing
``app/migrations/003_hang_detector.sql`` did not yet exist (a correct TDD state
— tests collect, but fail).

Only fixtures from ``tests/conftest.py`` are used: ``project_root``,
``data_dir``, ``config``, ``db_module``, ``initialized_db``, ``db_path``,
``sqlite_conn``. For the "upgrade path" criterion a version-2 DB with data is
built by hand — we read and run ``001_init.sql``/``002_remove_zhdem_raschet.sql``
directly on a raw sqlite3 connection, bypassing ``app.db._discover_migrations()``,
so the test does not depend on whether the (not-yet-written) 003 file is found —
then the real ``db_module.init_db()`` is called, which must detect the current
version 2 and apply only migration 003.
"""

from __future__ import annotations

import sqlite3

import pytest
from helpers import _insert_deal_migration as _insert_deal

# Exact default ping-template string from the migration 003 seed.
DEFAULT_PING_TEMPLATE = (
    "{waiting_for}, reminder about {counterparty}: waiting on {stage} for {days} "
    "business days. Last status: {last_note}. Any progress?"
)
DEFAULT_PING_HIDDEN_DAYS = "2"

DEAL_PINGS_COLUMNS = {"id", "deal_id", "pinged_at", "escalation_step", "ping_text"}

# Column sets of Step 1 tables NOT touched by migration 003 (used for a
# byte-for-byte before/after comparison in the upgrade-path test).
STAGE_COLUMNS = ["id", "name", "position", "threshold_days", "is_terminal"]
DEAL_COLUMNS = [
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
]
NOTE_COLUMNS = [
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
ATTACHMENT_COLUMNS = [
    "id",
    "note_id",
    "original_name",
    "stored_name",
    "mime_type",
    "size_bytes",
    "created_at",
]

# Operations allowed for the "additive-only" migration 003 (the "Diff review" section).
ALLOWED_STATEMENT_PREFIXES = (
    "ALTER TABLE",
    "CREATE TABLE",
    "CREATE INDEX",
    "CREATE UNIQUE INDEX",
    "INSERT INTO",
    "UPDATE",
)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


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


def _dump_table(conn, table: str, columns: list[str]) -> list[dict]:
    cols = ", ".join(columns)
    rows = conn.execute(f"SELECT {cols} FROM {table} ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def _build_v2_db_with_data(config_module, project_root):
    """Build a version-2 DB on disk (Step 1 + migration 002), bypassing app.db.

    Reads ``001_init.sql`` and ``002_remove_zhdem_raschet.sql`` directly from
    ``app/migrations`` and runs them on a new file at ``config_module.DB_PATH`` —
    the same path ``db_module.init_db()`` later uses in this same test.
    """
    migrations_dir = project_root / "app" / "migrations"
    conn = sqlite3.connect(str(config_module.DB_PATH))
    conn.row_factory = sqlite3.Row
    for name in ("001_init.sql", "002_remove_zhdem_raschet.sql"):
        script = (migrations_dir / name).read_text(encoding="utf-8")
        conn.executescript(script)
    conn.commit()
    return conn


def _split_sql_statements(sql_text: str) -> list[str]:
    """Strip line comments ``--`` and split the script into statements."""
    lines = [line.split("--", 1)[0] for line in sql_text.splitlines()]
    cleaned = "\n".join(lines)
    return [stmt.strip() for stmt in cleaned.split(";") if stmt.strip()]


# ---------------------------------------------------------------------------
# Fresh DB after init_db(): schema_version, presence of columns/tables/index
# ---------------------------------------------------------------------------


def test_fresh_db_schema_version_is_current(sqlite_conn):
    # sqlite_conn is initialized via initialized_db -> init_db(), which applies
    # ALL discovered migrations (including the later 004 and 006) — the test
    # checks the current schema version, not the version "right after 003".
    row = sqlite_conn.execute(
        "SELECT value FROM app_meta WHERE key = 'schema_version'"
    ).fetchone()
    assert row is not None
    assert row["value"] == "6"


def test_deals_has_snoozed_until_column(sqlite_conn):
    columns = {row["name"] for row in sqlite_conn.execute("PRAGMA table_info(deals)")}
    assert "snoozed_until" in columns


def test_deals_snoozed_until_is_nullable(sqlite_conn):
    info = {row["name"]: row for row in sqlite_conn.execute("PRAGMA table_info(deals)")}
    assert info["snoozed_until"]["notnull"] == 0


def test_deals_snoozed_until_defaults_to_null_on_insert(sqlite_conn):
    stage_id = _first_stage_id(sqlite_conn, terminal=False)
    deal_id = _insert_deal(sqlite_conn, "Test item", stage_id)
    row = sqlite_conn.execute(
        "SELECT snoozed_until FROM deals WHERE id = ?", (deal_id,)
    ).fetchone()
    assert row["snoozed_until"] is None


def test_stages_has_track_hangs_column(sqlite_conn):
    columns = {row["name"] for row in sqlite_conn.execute("PRAGMA table_info(stages)")}
    assert "track_hangs" in columns


def test_stages_track_hangs_defaults_to_1_on_insert(sqlite_conn):
    cur = sqlite_conn.execute(
        "INSERT INTO stages (name, position, threshold_days) VALUES ('New stage', 999, 5)"
    )
    sqlite_conn.commit()
    row = sqlite_conn.execute(
        "SELECT track_hangs FROM stages WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    assert row["track_hangs"] == 1


def test_stages_track_hangs_is_not_null(sqlite_conn):
    with pytest.raises(sqlite3.IntegrityError):
        sqlite_conn.execute(
            """
            INSERT INTO stages (name, position, threshold_days, track_hangs)
            VALUES ('Stage', 998, 5, NULL)
            """
        )


def test_fresh_db_track_hangs_matches_is_terminal_flag_generically(sqlite_conn):
    """Checked by the is_terminal flag, not tied to stage names (T1)."""
    rows = sqlite_conn.execute("SELECT is_terminal, track_hangs FROM stages").fetchall()
    assert rows, "expected at least one seeded stage"
    terminal_rows = [row for row in rows if row["is_terminal"] == 1]
    non_terminal_rows = [row for row in rows if row["is_terminal"] == 0]
    assert terminal_rows, "expected at least one terminal stage in the seed"
    assert non_terminal_rows, "expected at least one non-terminal stage in the seed"
    assert all(row["track_hangs"] == 0 for row in terminal_rows)
    assert all(row["track_hangs"] == 1 for row in non_terminal_rows)


def test_deal_pings_table_exists_with_expected_columns(sqlite_conn):
    info = {
        row["name"]: row for row in sqlite_conn.execute("PRAGMA table_info(deal_pings)")
    }
    assert DEAL_PINGS_COLUMNS <= set(info)


def test_deal_pings_notnull_columns(sqlite_conn):
    info = {
        row["name"]: row for row in sqlite_conn.execute("PRAGMA table_info(deal_pings)")
    }
    assert info["deal_id"]["notnull"] == 1
    assert info["pinged_at"]["notnull"] == 1
    assert info["escalation_step"]["notnull"] == 1
    assert info["ping_text"]["notnull"] == 1


def test_deal_pings_ping_text_defaults_to_empty_string(sqlite_conn):
    stage_id = _first_stage_id(sqlite_conn, terminal=False)
    deal_id = _insert_deal(sqlite_conn, "Item", stage_id)
    cur = sqlite_conn.execute(
        """
        INSERT INTO deal_pings (deal_id, pinged_at, escalation_step)
        VALUES (?, '2026-01-01T00:00:00', 1)
        """,
        (deal_id,),
    )
    sqlite_conn.commit()
    row = sqlite_conn.execute(
        "SELECT ping_text FROM deal_pings WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    assert row["ping_text"] == ""


def test_deal_pings_deal_id_foreign_key_enforced(sqlite_conn):
    sqlite_conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        sqlite_conn.execute(
            """
            INSERT INTO deal_pings (deal_id, pinged_at, escalation_step)
            VALUES (999999, '2026-01-01T00:00:00', 1)
            """
        )


def test_idx_deal_pings_deal_exists_on_deal_pings(sqlite_conn):
    row = sqlite_conn.execute(
        "SELECT tbl_name FROM sqlite_master WHERE type = 'index' AND name = 'idx_deal_pings_deal'"
    ).fetchone()
    assert row is not None
    assert row["tbl_name"] == "deal_pings"


def test_app_meta_seeds_ping_template_with_exact_default_string(sqlite_conn):
    row = sqlite_conn.execute(
        "SELECT value FROM app_meta WHERE key = 'ping_template'"
    ).fetchone()
    assert row is not None
    assert row["value"] == DEFAULT_PING_TEMPLATE


def test_app_meta_seeds_ping_hidden_days_as_2(sqlite_conn):
    row = sqlite_conn.execute(
        "SELECT value FROM app_meta WHERE key = 'ping_hidden_days'"
    ).fetchone()
    assert row is not None
    assert row["value"] == DEFAULT_PING_HIDDEN_DAYS


# ---------------------------------------------------------------------------
# Upgrade path: a version-2 DB with Step 1 data is not broken by migration 003
# ---------------------------------------------------------------------------


def test_migration_003_upgrade_preserves_step1_data_and_seeds_defaults(
    config, db_module, project_root
):
    conn = _build_v2_db_with_data(config, project_root)

    # Extra terminal stage with a non-standard name — verifies that the
    # UPDATE track_hangs works by the is_terminal flag, not by the name "Done".
    conn.execute(
        "INSERT INTO stages (name, position, threshold_days, is_terminal) VALUES (?, ?, ?, ?)",
        ("Archive (custom terminal)", 200, 5, 1),
    )
    conn.commit()

    non_terminal_stage_id = _first_stage_id(conn, terminal=False)
    deal_id = _insert_deal(conn, "Daisy Holding", non_terminal_stage_id)
    note_id = _insert_attached_note(conn, deal_id, "Agreement signed by the partner")
    _insert_attachment(conn, note_id)

    stages_before = _dump_table(conn, "stages", STAGE_COLUMNS)
    deals_before = _dump_table(conn, "deals", DEAL_COLUMNS)
    notes_before = _dump_table(conn, "notes", NOTE_COLUMNS)
    attachments_before = _dump_table(conn, "attachments", ATTACHMENT_COLUMNS)

    version_before = conn.execute(
        "SELECT value FROM app_meta WHERE key = 'schema_version'"
    ).fetchone()["value"]
    assert version_before == "2", "test precondition: we build exactly a version-2 DB"
    conn.close()

    # The single call into the migration machinery (app/db.py is unchanged in
    # T1) — must detect version 2 and apply only 003.
    db_module.init_db()

    conn2 = sqlite3.connect(str(config.DB_PATH))
    conn2.row_factory = sqlite3.Row
    try:
        stages_after = _dump_table(conn2, "stages", STAGE_COLUMNS)
        deals_after = _dump_table(conn2, "deals", DEAL_COLUMNS)
        notes_after = _dump_table(conn2, "notes", NOTE_COLUMNS)
        attachments_after = _dump_table(conn2, "attachments", ATTACHMENT_COLUMNS)

        # init_db() on a v2 DB applies ALL pending migrations in a row, but none
        # of them (003-006) touches the seeded stages: migration 006 rebuilds
        # notes + FTS and no longer consolidates stages, so the stages dump is
        # byte-for-byte identical before and after. The invariant this test
        # protects is that migrations 003-006 do not corrupt Step 1 seed data.
        assert stages_after == stages_before, "stages rows changed by migrations 003-006"
        assert deals_after == deals_before, "old deals rows changed by migration 003"
        assert notes_after == notes_before, "old notes rows changed by migration 003"
        assert attachments_after == attachments_before, (
            "old attachments rows changed by migration 003"
        )

        version_after = conn2.execute(
            "SELECT value FROM app_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        # db_module.init_db() applies ALL pending migrations, not just 003 —
        # with 004 and 006 present, the final version of the v2 DB after
        # init_db() is "6", not "3".
        assert version_after == "6"

        snooze_rows = conn2.execute("SELECT snoozed_until FROM deals").fetchall()
        assert snooze_rows, "expected items after the migration"
        assert all(row["snoozed_until"] is None for row in snooze_rows)

        for row in conn2.execute("SELECT is_terminal, track_hangs FROM stages"):
            if row["is_terminal"] == 1:
                assert row["track_hangs"] == 0
            else:
                assert row["track_hangs"] == 1

        # FTS search over an existing (pre-migration) note still works.
        matches = conn2.execute(
            "SELECT rowid FROM notes_fts WHERE notes_fts MATCH 'agreement'"
        ).fetchall()
        assert note_id in {row["rowid"] for row in matches}

        # The migration must also seed the detector settings on upgrade.
        template_row = conn2.execute(
            "SELECT value FROM app_meta WHERE key = 'ping_template'"
        ).fetchone()
        hidden_days_row = conn2.execute(
            "SELECT value FROM app_meta WHERE key = 'ping_hidden_days'"
        ).fetchone()
        assert template_row is not None
        assert template_row["value"] == DEFAULT_PING_TEMPLATE
        assert hidden_days_row is not None
        assert hidden_days_row["value"] == DEFAULT_PING_HIDDEN_DAYS
    finally:
        conn2.close()


# ---------------------------------------------------------------------------
# Diff review: additive-only migration (ALTER/CREATE/INSERT/UPDATE), no
# DROP/DELETE/recreation of existing tables.
# ---------------------------------------------------------------------------


def test_migration_003_file_exists(project_root):
    path = project_root / "app" / "migrations" / "003_hang_detector.sql"
    assert path.exists(), "app/migrations/003_hang_detector.sql must exist"


def test_migration_003_contains_no_drop_or_delete(project_root):
    path = project_root / "app" / "migrations" / "003_hang_detector.sql"
    text = path.read_text(encoding="utf-8")
    assert "DROP" not in text.upper()
    assert "DELETE" not in text.upper()


def test_migration_003_statements_are_only_additive_operations(project_root):
    path = project_root / "app" / "migrations" / "003_hang_detector.sql"
    text = path.read_text(encoding="utf-8")
    statements = _split_sql_statements(text)
    assert statements, "the migration must not be empty"
    for stmt in statements:
        upper = stmt.upper()
        assert upper.startswith(ALLOWED_STATEMENT_PREFIXES), (
            f"unexpected operation type in migration 003: {stmt[:60]!r}"
        )


@pytest.mark.parametrize(
    "existing_table", ["deals", "stages", "notes", "attachments", "app_meta"]
)
def test_migration_003_does_not_recreate_existing_tables(project_root, existing_table):
    path = project_root / "app" / "migrations" / "003_hang_detector.sql"
    text = path.read_text(encoding="utf-8").upper()
    assert f"CREATE TABLE {existing_table.upper()}" not in text
    assert f"CREATE TABLE IF NOT EXISTS {existing_table.upper()}" not in text
