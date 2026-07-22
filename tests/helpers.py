"""Shared test helper functions (plain module, not pytest fixtures)."""

_NOW = "2026-01-01T00:00:00"


def _seed_deal(
    conn,
    *,
    title,
    stage_id: int = 1,
    last_activity_at: str = _NOW,
    company=None,
    partner=None,
    rate=None,
    jurisdiction=None,
    waiting_on=None,
    description=None,
    drive_folder_url=None,
    closed_at=None,
) -> int:
    """Insert an item directly into the DB and return its ID."""
    cur = conn.execute(
        """
        INSERT INTO deals (
            title, company, partner, rate, jurisdiction, waiting_on, description,
            stage_id, stage_entered_at, last_activity_at, created_at, closed_at,
            drive_folder_url
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            title,
            company,
            partner,
            rate,
            jurisdiction,
            waiting_on,
            description,
            stage_id,
            _NOW,
            last_activity_at,
            _NOW,
            closed_at,
            drive_folder_url,
        ),
    )
    conn.commit()
    return cur.lastrowid


def _insert_deal(
    conn,
    title: str,
    stage_id: int,
    *,
    company: str | None = None,
    partner: str | None = None,
    waiting_on: str | None = None,
    jurisdiction: str | None = None,
    description: str | None = None,
    stage_entered_at: str = _NOW,
    last_activity_at: str = _NOW,
    created_at: str = _NOW,
    closed_at: str | None = None,
    snoozed_until: str | None = None,
) -> int:
    """Insert an item into the current (full) schema; every field except
    title/stage_id has a default, so both 2-arg and kwargs calls work unchanged."""
    cur = conn.execute(
        """
        INSERT INTO deals (
            title, company, partner, waiting_on, jurisdiction, description,
            stage_id, stage_entered_at, last_activity_at, created_at,
            closed_at, snoozed_until
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title,
            company,
            partner,
            waiting_on,
            jurisdiction,
            description,
            stage_id,
            stage_entered_at,
            last_activity_at,
            created_at,
            closed_at,
            snoozed_until,
        ),
    )
    conn.commit()
    return cur.lastrowid


def _insert_deal_migration(conn, title: str, stage_id: int, ts: str = _NOW) -> int:
    """Variant for upgrade-path tests: minimal schema, a single ts for
    stage_entered_at/last_activity_at/created_at."""
    cur = conn.execute(
        """
        INSERT INTO deals (title, stage_id, stage_entered_at, last_activity_at, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (title, stage_id, ts, ts, ts),
    )
    conn.commit()
    return cur.lastrowid


def _first_non_terminal_stage(conn):
    row = conn.execute(
        "SELECT * FROM stages WHERE is_terminal = 0 ORDER BY position LIMIT 1"
    ).fetchone()
    assert row is not None
    return row


def _second_non_terminal_stage(conn):
    rows = conn.execute(
        "SELECT * FROM stages WHERE is_terminal = 0 ORDER BY position LIMIT 2"
    ).fetchall()
    assert len(rows) >= 2, "at least 2 non-terminal stages are required"
    return rows[1]


def _terminal_stage(conn):
    row = conn.execute(
        "SELECT * FROM stages WHERE is_terminal = 1 LIMIT 1"
    ).fetchone()
    assert row is not None, "expected the terminal 'Done' stage from the seed"
    return row


def _stages_by_position(conn):
    return conn.execute("SELECT * FROM stages ORDER BY position").fetchall()


def _stage_row(conn, stage_id: int):
    row = conn.execute(
        "SELECT * FROM stages WHERE id = ?", (stage_id,)
    ).fetchone()
    assert row is not None
    return row


def _deal_row(conn, deal_id: int):
    row = conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    assert row is not None
    return row


def _insert_note(
    conn,
    body: str = "",
    status: str = "inbox",
    deal_id: int | None = None,
    created_at: str = "2026-01-01T00:00:00",
    is_pinned: int = 0,
    note_type: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO notes (body, status, deal_id, created_at, is_pinned, note_type)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (body, status, deal_id, created_at, is_pinned, note_type),
    )
    conn.commit()
    return cur.lastrowid


def _insert_note_migration(
    conn,
    body: str = "",
    status: str = "inbox",
    deal_id: int | None = None,
) -> int:
    """Variant for upgrade-path tests: pre-006 schema without is_pinned/note_type."""
    cur = conn.execute(
        """
        INSERT INTO notes (body, status, deal_id, created_at)
        VALUES (?, ?, ?, '2026-01-01T00:00:00')
        """,
        (body, status, deal_id),
    )
    conn.commit()
    return cur.lastrowid
