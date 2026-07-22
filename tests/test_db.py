"""T1 tests: application skeleton, DB, migration 001, run.py.

Acceptance criteria source — docs/specs/001-step1-inbox-pipeline.md, task T1.
Every test runs against an isolated tmp DB (see tests/conftest.py) and never
touches the production data/ directory at the project root.
"""

from __future__ import annotations

import contextlib
import sqlite3

import pytest

from helpers import _insert_deal, _insert_note

# The 6 generic seed stages from migration 001, in position order 1..6.
# Only "Done" is terminal (position 6).
EXPECTED_STAGE_NAMES = [
    "Backlog",
    "To Do",
    "In Progress",
    "Review",
    "Blocked",
    "Done",
]

EXPECTED_TABLES = {
    "stages",
    "deals",
    "notes",
    "attachments",
    "app_meta",
    "notes_fts",
    "deals_fts",
}

EXPECTED_INDEXES = {
    "idx_stages_position",
    "idx_deals_stage",
    "idx_notes_status_created",
    "idx_notes_deal",
    "idx_attachments_note",
}

# Packages whose presence in requirements.txt would indicate outbound network
# calls to external services (Step 1 must be fully offline).
FORBIDDEN_NETWORK_PACKAGES = {
    "requests",
    "aiohttp",
    "boto3",
    "botocore",
    "google-api-python-client",
    "google-auth",
    "google-cloud-storage",
    "gspread",
    "jira",
    "slack-sdk",
    "slack_sdk",
    "paramiko",
    "pysftp",
    "urllib3",
}


def _first_stage_id(sqlite_conn) -> int:
    row = sqlite_conn.execute(
        "SELECT id FROM stages ORDER BY position LIMIT 1"
    ).fetchone()
    assert row is not None, "expected at least one seeded stage"
    return row["id"]


# ---------------------------------------------------------------------------
# WAL, stage seed, app_meta
# ---------------------------------------------------------------------------


def test_init_db_enables_wal_mode(sqlite_conn):
    mode = sqlite_conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_seed_creates_exactly_6_generic_stages(sqlite_conn):
    rows = sqlite_conn.execute(
        "SELECT name, position, threshold_days, is_terminal FROM stages ORDER BY position"
    ).fetchall()
    assert len(rows) == 6
    names = [row["name"] for row in rows]
    assert names == EXPECTED_STAGE_NAMES
    positions = [row["position"] for row in rows]
    assert positions == [1, 2, 3, 4, 5, 6]


def test_seed_stages_all_have_default_threshold_5(sqlite_conn):
    rows = sqlite_conn.execute("SELECT threshold_days FROM stages").fetchall()
    assert all(row["threshold_days"] == 5 for row in rows)


def test_seed_only_done_is_terminal(sqlite_conn):
    rows = sqlite_conn.execute(
        "SELECT name, is_terminal FROM stages ORDER BY position"
    ).fetchall()
    terminal_names = [row["name"] for row in rows if row["is_terminal"] == 1]
    assert terminal_names == ["Done"]


def test_app_meta_has_current_schema_version_6(sqlite_conn):
    row = sqlite_conn.execute(
        "SELECT value FROM app_meta WHERE key = 'schema_version'"
    ).fetchone()
    assert row is not None
    assert row["value"] == "6"
    # 6 = migration 006 (notes rebuild + 2-column FTS) — the last accepted one.


def test_app_meta_has_last_triage_at_and_first_run_at(sqlite_conn):
    for key in ("last_triage_at", "first_run_at"):
        row = sqlite_conn.execute(
            "SELECT value FROM app_meta WHERE key = ?", (key,)
        ).fetchone()
        assert row is not None, f"app_meta.{key} is missing"
        assert row["value"], f"app_meta.{key} is empty"


# ---------------------------------------------------------------------------
# get_conn(): PRAGMA foreign_keys / busy_timeout
# ---------------------------------------------------------------------------


def _acquire_get_conn(db_module):
    """Universally obtain a connection from app.db.get_conn().

    get_conn() is described in the spec as a "FastAPI dependency"; we support
    three plausible implementations: a generator with yield, @contextmanager,
    and a plain function returning a connection directly.
    """
    result = db_module.get_conn()
    if hasattr(result, "__next__"):
        conn = next(result)
        return conn, ("generator", result)
    if hasattr(result, "__enter__"):
        conn = result.__enter__()
        return conn, ("contextmanager", result)
    return result, ("plain", None)


def _release_get_conn(conn, handle) -> None:
    kind, obj = handle
    if kind == "generator":
        with contextlib.suppress(StopIteration):
            next(obj)
    elif kind == "contextmanager":
        obj.__exit__(None, None, None)
    else:
        conn.close()


def test_get_conn_sets_foreign_keys_and_busy_timeout(initialized_db):
    conn, handle = _acquire_get_conn(initialized_db)
    try:
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert fk == 1
        assert timeout == 5000
    finally:
        _release_get_conn(conn, handle)


def test_get_conn_row_factory_is_row(initialized_db):
    conn, handle = _acquire_get_conn(initialized_db)
    try:
        assert conn.row_factory is sqlite3.Row
    finally:
        _release_get_conn(conn, handle)


# ---------------------------------------------------------------------------
# Presence of schema tables/indexes
# ---------------------------------------------------------------------------


def test_all_schema_tables_exist(sqlite_conn):
    rows = sqlite_conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    existing = {row["name"] for row in rows}
    missing = EXPECTED_TABLES - existing
    assert not missing, f"missing tables: {missing}"


def test_all_schema_indexes_exist(sqlite_conn):
    rows = sqlite_conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'"
    ).fetchall()
    existing = {row["name"] for row in rows}
    missing = EXPECTED_INDEXES - existing
    assert not missing, f"missing indexes: {missing}"


def test_idx_stages_position_is_unique(sqlite_conn):
    row = sqlite_conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'idx_stages_position'"
    ).fetchone()
    assert row is not None
    assert "UNIQUE" in (row["sql"] or "").upper()


def test_stages_position_uniqueness_enforced(sqlite_conn):
    with pytest.raises(sqlite3.IntegrityError):
        sqlite_conn.execute(
            "INSERT INTO stages (name, position, threshold_days) VALUES ('Duplicate', 1, 5)"
        )


def test_deals_table_has_drive_field(sqlite_conn):
    columns = {row["name"] for row in sqlite_conn.execute("PRAGMA table_info(deals)")}
    assert "drive_folder_url" in columns


def test_notes_table_has_f5_llm_fields(sqlite_conn):
    columns = {row["name"] for row in sqlite_conn.execute("PRAGMA table_info(notes)")}
    assert {"note_type", "suggested_deal_id", "llm_confidence", "llm_status"} <= columns


def test_notes_llm_status_defaults_to_none(sqlite_conn):
    note_id = _insert_note(sqlite_conn, body="unparsed")
    row = sqlite_conn.execute(
        "SELECT llm_status FROM notes WHERE id = ?", (note_id,)
    ).fetchone()
    assert row["llm_status"] == "none"


def test_stages_table_has_threshold_days_column(sqlite_conn):
    columns = {row["name"] for row in sqlite_conn.execute("PRAGMA table_info(stages)")}
    assert "threshold_days" in columns


# ---------------------------------------------------------------------------
# FTS5: notes_fts / deals_fts, INSERT/UPDATE/DELETE via triggers
# ---------------------------------------------------------------------------


def test_notes_fts_and_deals_fts_are_virtual_tables(sqlite_conn):
    for table in ("notes_fts", "deals_fts"):
        row = sqlite_conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (table,)
        ).fetchone()
        assert row is not None
        assert "fts5" in (row["sql"] or "").lower()


def test_at_least_six_fts_sync_triggers_exist(sqlite_conn):
    count = sqlite_conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'"
    ).fetchone()[0]
    assert count >= 6


def test_deals_fts_insert_finds_title(sqlite_conn):
    stage_id = _first_stage_id(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "Daisy", stage_id)
    rows = sqlite_conn.execute(
        "SELECT rowid FROM deals_fts WHERE deals_fts MATCH 'daisy'"
    ).fetchall()
    assert deal_id in {row["rowid"] for row in rows}


def test_deals_fts_update_reflects_new_title(sqlite_conn):
    stage_id = _first_stage_id(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "Daisy", stage_id)
    sqlite_conn.execute("UPDATE deals SET title = 'Tulip' WHERE id = ?", (deal_id,))
    sqlite_conn.commit()

    old_match = sqlite_conn.execute(
        "SELECT rowid FROM deals_fts WHERE deals_fts MATCH 'daisy'"
    ).fetchall()
    new_match = sqlite_conn.execute(
        "SELECT rowid FROM deals_fts WHERE deals_fts MATCH 'tulip'"
    ).fetchall()

    assert deal_id not in {row["rowid"] for row in old_match}
    assert deal_id in {row["rowid"] for row in new_match}


def test_deals_fts_delete_removes_entry(sqlite_conn):
    stage_id = _first_stage_id(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "Daisy", stage_id)
    sqlite_conn.execute("DELETE FROM deals WHERE id = ?", (deal_id,))
    sqlite_conn.commit()

    rows = sqlite_conn.execute(
        "SELECT rowid FROM deals_fts WHERE deals_fts MATCH 'daisy'"
    ).fetchall()
    assert deal_id not in {row["rowid"] for row in rows}


def test_notes_fts_insert_finds_body(sqlite_conn):
    note_id = _insert_note(sqlite_conn, body="Agreement with the partner is ready")
    rows = sqlite_conn.execute(
        "SELECT rowid FROM notes_fts WHERE notes_fts MATCH 'agreement'"
    ).fetchall()
    assert note_id in {row["rowid"] for row in rows}


def test_notes_fts_update_reflects_new_body(sqlite_conn):
    note_id = _insert_note(sqlite_conn, body="Original text")
    sqlite_conn.execute(
        "UPDATE notes SET body = 'Modified text' WHERE id = ?", (note_id,)
    )
    sqlite_conn.commit()

    old_match = sqlite_conn.execute(
        "SELECT rowid FROM notes_fts WHERE notes_fts MATCH 'original'"
    ).fetchall()
    new_match = sqlite_conn.execute(
        "SELECT rowid FROM notes_fts WHERE notes_fts MATCH 'modified'"
    ).fetchall()

    assert note_id not in {row["rowid"] for row in old_match}
    assert note_id in {row["rowid"] for row in new_match}


def test_notes_fts_delete_removes_entry(sqlite_conn):
    note_id = _insert_note(sqlite_conn, body="Temporary entry")
    sqlite_conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    sqlite_conn.commit()

    rows = sqlite_conn.execute(
        "SELECT rowid FROM notes_fts WHERE notes_fts MATCH 'temporary'"
    ).fetchall()
    assert note_id not in {row["rowid"] for row in rows}


def test_deals_fts_case_insensitive_search(sqlite_conn):
    stage_id = _first_stage_id(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "Financing LLC", stage_id)
    rows = sqlite_conn.execute(
        "SELECT rowid FROM deals_fts WHERE deals_fts MATCH 'FINANCING'"
    ).fetchall()
    assert deal_id in {row["rowid"] for row in rows}


# ---------------------------------------------------------------------------
# Note-attachment CHECK invariant: (deal_id IS NOT NULL) = (status='attached')
# ---------------------------------------------------------------------------


def test_check_rejects_attached_status_without_deal_id(sqlite_conn):
    with pytest.raises(sqlite3.IntegrityError):
        sqlite_conn.execute(
            """
            INSERT INTO notes (body, status, deal_id, created_at)
            VALUES ('text', 'attached', NULL, '2026-01-01T00:00:00')
            """
        )


@pytest.mark.parametrize("status", ["inbox", "deferred", "archived"])
def test_check_rejects_non_attached_status_with_deal_id(sqlite_conn, status):
    stage_id = _first_stage_id(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "Company", stage_id)
    with pytest.raises(sqlite3.IntegrityError):
        sqlite_conn.execute(
            """
            INSERT INTO notes (body, status, deal_id, created_at)
            VALUES ('text', ?, ?, '2026-01-01T00:00:00')
            """,
            (status, deal_id),
        )


def test_check_allows_attached_status_with_deal_id(sqlite_conn):
    stage_id = _first_stage_id(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "Company", stage_id)
    # must not raise
    sqlite_conn.execute(
        """
        INSERT INTO notes (body, status, deal_id, created_at)
        VALUES ('text', 'attached', ?, '2026-01-01T00:00:00')
        """,
        (deal_id,),
    )
    sqlite_conn.commit()


def test_check_allows_inbox_status_with_null_deal_id(sqlite_conn):
    # must not raise
    _insert_note(sqlite_conn, body="a regular note", status="inbox", deal_id=None)


def test_status_enum_check_rejects_unknown_value(sqlite_conn):
    with pytest.raises(sqlite3.IntegrityError):
        sqlite_conn.execute(
            """
            INSERT INTO notes (body, status, deal_id, created_at)
            VALUES ('text', 'bogus_status', NULL, '2026-01-01T00:00:00')
            """
        )


# ---------------------------------------------------------------------------
# Cascade deletion of attachments when a note is deleted
# ---------------------------------------------------------------------------


def test_delete_note_cascades_to_attachments(sqlite_conn):
    sqlite_conn.execute("PRAGMA foreign_keys = ON")
    note_id = _insert_note(sqlite_conn, body="with attachment")
    sqlite_conn.execute(
        """
        INSERT INTO attachments
            (note_id, original_name, stored_name, mime_type, size_bytes, created_at)
        VALUES (?, 'file.txt', 'abc123.txt', 'text/plain', 10, '2026-01-01T00:00:00')
        """,
        (note_id,),
    )
    sqlite_conn.commit()

    before = sqlite_conn.execute(
        "SELECT COUNT(*) FROM attachments WHERE note_id = ?", (note_id,)
    ).fetchone()[0]
    assert before == 1

    sqlite_conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    sqlite_conn.commit()

    after = sqlite_conn.execute(
        "SELECT COUNT(*) FROM attachments WHERE note_id = ?", (note_id,)
    ).fetchone()[0]
    assert after == 0


# ---------------------------------------------------------------------------
# Test isolation from the production data/ directory
# ---------------------------------------------------------------------------


def test_db_path_lives_inside_tmp_data_dir(config, data_dir):
    db_path_str = str(config.DB_PATH)
    assert str(data_dir) in db_path_str


def test_attachments_and_backups_dirs_live_inside_tmp_data_dir(config, data_dir):
    assert str(data_dir) in str(config.ATTACHMENTS_DIR)
    assert str(data_dir) in str(config.BACKUPS_DIR)


# ---------------------------------------------------------------------------
# HTML page '/'
# ---------------------------------------------------------------------------


def test_root_page_returns_200_and_html(client):
    response = client.get("/")
    assert response.status_code == 200
    content_type = response.headers.get("content-type", "")
    assert "text/html" in content_type
    assert "<html" in response.text.lower()


# ---------------------------------------------------------------------------
# run.py: host 127.0.0.1, no network dependencies
# ---------------------------------------------------------------------------


def test_run_py_binds_to_localhost_only(project_root):
    run_py = project_root / "run.py"
    assert run_py.exists(), "run.py must exist at the project root"
    source = run_py.read_text(encoding="utf-8")
    assert "127.0.0.1" in source
    assert "0.0.0.0" not in source


def test_requirements_txt_has_no_external_network_packages(project_root):
    req_file = project_root / "requirements.txt"
    assert req_file.exists(), "requirements.txt must exist at the project root"
    lines = [
        line.strip().lower()
        for line in req_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines, "requirements.txt must not be empty"

    def package_name(line: str) -> str:
        for sep in ("==", ">=", "<=", "~=", ">", "<", "[", ";"):
            if sep in line:
                line = line.split(sep, 1)[0]
        return line.strip()

    package_names = {package_name(line) for line in lines}
    forbidden_found = package_names & FORBIDDEN_NETWORK_PACKAGES
    assert not forbidden_found, f"network dependencies found: {forbidden_found}"
