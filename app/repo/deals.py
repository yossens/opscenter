"""SQL functions for items, the board, and the archive.

Data-access layer: pure functions ``(conn, ...) -> dict/list``. Routers stay
thin. Times are stored in UTC ISO-8601 (``YYYY-MM-DDTHH:MM:SS``).
``days_in_stage``/``aging_level`` are computed via ``app/workdays.py``.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from ..fts import sanitize_fts_query
from ..workdays import _utc_now, aging_level, workdays_since
from .notes import _attachments_for

# Item columns returned in JSON (in a fixed order).
_DEAL_COLUMNS = (
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
)

# Card fields that may be changed via PATCH /api/deals/{id}.
# Neither title nor stage_id (stage moves happen only via /move) are included.
PATCHABLE_FIELDS = (
    "company",
    "partner",
    "rate",
    "jurisdiction",
    "waiting_on",
    "description",
)


def _deal_dict(row: sqlite3.Row) -> dict:
    return {col: row[col] for col in _DEAL_COLUMNS}


def stage_exists(conn: sqlite3.Connection, stage_id: int) -> bool:
    row = conn.execute("SELECT 1 FROM stages WHERE id = ?", (stage_id,)).fetchone()
    return row is not None


def _stage_row(conn: sqlite3.Connection, stage_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM stages WHERE id = ?", (stage_id,)).fetchone()


def first_stage_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT id FROM stages ORDER BY position LIMIT 1").fetchone()
    return int(row["id"])


def _get_deal_row(conn: sqlite3.Connection, deal_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()


def create_deal(conn: sqlite3.Connection, data: dict, stage_id: int) -> dict:
    """Creates an item in stage ``stage_id``.

    ``created_at`` = ``stage_entered_at`` = ``last_activity_at`` = the moment of
    creation. Optional card fields are taken from ``data``.
    """
    now = _utc_now()
    cur = conn.execute(
        """
        INSERT INTO deals (
            title, company, partner, rate, jurisdiction, waiting_on, description,
            stage_id, stage_entered_at, last_activity_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data["title"],
            data.get("company"),
            data.get("partner"),
            data.get("rate"),
            data.get("jurisdiction"),
            data.get("waiting_on"),
            data.get("description"),
            stage_id,
            now,
            now,
            now,
        ),
    )
    conn.commit()
    row = _get_deal_row(conn, cur.lastrowid)
    return _deal_dict(row)


def get_deal(conn: sqlite3.Connection, deal_id: int) -> dict | None:
    """Item by id with ``days_in_stage``/``aging_level`` and its note feed.

    The feed is attached notes in chronological order (oldest first), each with
    an array of attachments. ``None`` if the item does not exist.
    """
    row = _get_deal_row(conn, deal_id)
    if row is None:
        return None

    stage = _stage_row(conn, row["stage_id"])
    threshold = stage["threshold_days"] if stage is not None else 5
    days = workdays_since(row["stage_entered_at"], date.today())

    result = _deal_dict(row)
    result["days_in_stage"] = days
    result["aging_level"] = aging_level(days, threshold)

    note_rows = conn.execute(
        """
        SELECT * FROM notes
        WHERE deal_id = ? ORDER BY created_at ASC, id ASC
        """,
        (deal_id,),
    ).fetchall()
    result["notes"] = [
        {
            "id": n["id"],
            "body": n["body"],
            "status": n["status"],
            "deal_id": n["deal_id"],
            "note_type": n["note_type"],
            "created_at": n["created_at"],
            "suggested_deal_id": n["suggested_deal_id"],
            "suggested_note_type": n["suggested_note_type"],
            "llm_confidence": n["llm_confidence"],
            "llm_status": n["llm_status"],
            "llm_draft": n["llm_draft"],
            "is_pinned": n["is_pinned"],
            "ocr_text": n["ocr_text"],
            "attachments": _attachments_for(conn, n["id"]),
        }
        for n in note_rows
    ]

    ping_rows = conn.execute(
        """
        SELECT id, pinged_at, escalation_step, ping_text FROM deal_pings
        WHERE deal_id = ? ORDER BY pinged_at ASC, id ASC
        """,
        (deal_id,),
    ).fetchall()
    result["pings"] = [
        {
            "id": p["id"],
            "pinged_at": p["pinged_at"],
            "escalation_step": p["escalation_step"],
            "ping_text": p["ping_text"],
        }
        for p in ping_rows
    ]

    return result


def patch_deal(conn: sqlite3.Connection, deal_id: int, updates: dict) -> dict | None:
    """Updates card fields and ``last_activity_at``.

    ``updates`` contains only whitelisted card fields (see ``PATCHABLE_FIELDS``);
    keys not in the whitelist are ignored. ``None`` if the item does not exist.
    """
    if _get_deal_row(conn, deal_id) is None:
        return None

    columns = [k for k in updates if k in PATCHABLE_FIELDS]
    if not columns:
        # No-op PATCH (empty body or only unrecognized keys): change nothing and
        # do NOT bump last_activity_at — otherwise the hang detector's
        # "days without activity" counter would reset with no real edit.
        return _deal_dict(_get_deal_row(conn, deal_id))
    now = _utc_now()
    set_parts = [f"{col} = ?" for col in columns]
    set_parts.append("last_activity_at = ?")
    params: list = [updates[col] for col in columns]
    params.append(now)
    params.append(deal_id)
    conn.execute(
        f"UPDATE deals SET {', '.join(set_parts)} WHERE id = ?",  # noqa: S608
        params,
    )
    conn.commit()
    return _deal_dict(_get_deal_row(conn, deal_id))


def move_deal(conn: sqlite3.Connection, deal_id: int, stage_id: int) -> dict | None:
    """Moves the item to stage ``stage_id``.

    Updates ``stage_entered_at`` and ``last_activity_at``; entering a terminal
    stage sets ``closed_at``, leaving one clears it. Moving to the current stage
    is a no-op (dates untouched). ``None`` if the item does not exist; the
    sentinel dict ``{"__stage_not_found__": True}`` indicates a non-existent
    stage while the item exists (the caller must handle it separately for 404).
    """
    deal = _get_deal_row(conn, deal_id)
    if deal is None:
        return None

    target = _stage_row(conn, stage_id)
    if target is None:
        # Signal to the caller: stage not found (the item exists).
        return {"__stage_not_found__": True}

    if deal["stage_id"] == stage_id:
        # No-op: dates do not change.
        return _deal_dict(deal)

    now = _utc_now()
    closed_at = now if target["is_terminal"] else None
    conn.execute(
        """
        UPDATE deals
        SET stage_id = ?, stage_entered_at = ?, last_activity_at = ?, closed_at = ?
        WHERE id = ?
        """,
        (stage_id, now, now, closed_at, deal_id),
    )
    conn.commit()
    return _deal_dict(_get_deal_row(conn, deal_id))


def delete_deal(conn: sqlite3.Connection, deal_id: int) -> list[str] | None:
    """Hard-deletes the item with all its dependencies (single transaction).

    Deletes attached notes (and, cascading, their attachment rows), pings, then
    the item itself. For notes in other statuses that merely suggested this item
    (``suggested_deal_id``), the reference is nulled out so as not to violate the
    FK. Returns the list of ``stored_name`` values for the deleted notes'
    attachments (the router removes the files from disk) or ``None`` if the item
    does not exist.
    """
    if _get_deal_row(conn, deal_id) is None:
        return None
    stored_names = [
        r["stored_name"]
        for r in conn.execute(
            """
            SELECT a.stored_name FROM attachments a
            JOIN notes n ON a.note_id = n.id
            WHERE n.deal_id = ?
            """,
            (deal_id,),
        ).fetchall()
    ]
    conn.execute(
        "UPDATE notes SET suggested_deal_id = NULL WHERE suggested_deal_id = ?",
        (deal_id,),
    )
    conn.execute("DELETE FROM notes WHERE deal_id = ?", (deal_id,))
    conn.execute("DELETE FROM deal_pings WHERE deal_id = ?", (deal_id,))
    conn.execute("DELETE FROM deals WHERE id = ?", (deal_id,))
    conn.commit()
    return stored_names


def board(conn: sqlite3.Connection) -> list[dict]:
    """Board columns by ``position``.

    Non-terminal columns contain a ``cards`` list (cards with board fields and
    ``days_in_stage``/``aging_level``) and ``count``. The terminal column
    returns only ``count`` and the ``is_terminal`` flag (no card details).
    """
    today = date.today()
    today_str = today.isoformat()
    stage_rows = conn.execute("SELECT * FROM stages ORDER BY position").fetchall()
    columns: list[dict] = []
    for stage in stage_rows:
        col: dict = {
            "stage_id": stage["id"],
            "name": stage["name"],
            "position": stage["position"],
            "threshold_days": stage["threshold_days"],
            "is_terminal": bool(stage["is_terminal"]),
        }
        if stage["is_terminal"]:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM deals WHERE stage_id = ?", (stage["id"],)
            ).fetchone()["c"]
            col["count"] = count
        else:
            deal_rows = conn.execute(
                "SELECT * FROM deals WHERE stage_id = ? ORDER BY id", (stage["id"],)
            ).fetchall()
            cards = []
            for d in deal_rows:
                days = workdays_since(d["stage_entered_at"], today)
                snoozed = d["snoozed_until"]
                # On the board a snooze is "active" only strictly later than
                # today (> today_local); today/past/empty -> null.
                snoozed_active = (
                    snoozed if snoozed is not None and snoozed > today_str else None
                )
                cards.append(
                    {
                        "id": d["id"],
                        "title": d["title"],
                        "company": d["company"],
                        "partner": d["partner"],
                        "waiting_on": d["waiting_on"],
                        "days_in_stage": days,
                        "aging_level": aging_level(days, stage["threshold_days"]),
                        "snoozed_until": snoozed_active,
                    }
                )
            col["cards"] = cards
            col["count"] = len(cards)
        columns.append(col)
    return columns


def search_deals(conn: sqlite3.Connection, q: str) -> list[dict]:
    """Search dropdown of active (non-terminal) items.

    Empty ``q`` -> all active items, sorted by ``title.casefold()`` in Python
    (case-insensitive, correct for non-ASCII text; not SQL NOCASE). Non-empty
    ``q`` -> FTS5 prefix search over ``deals_fts`` (case-insensitive),
    excluding closed (terminal) items.

    Sorting uses Python ``casefold()`` rather than SQL ``COLLATE NOCASE`` to
    ensure correct case-insensitive comparison of non-ASCII text: SQLite's
    NOCASE does not understand non-ASCII letters, whereas ``str.casefold()``
    works correctly.
    """
    if q and q.strip():
        match = sanitize_fts_query(q)
        if match is None:
            return []
        rows = conn.execute(
            """
            SELECT d.* FROM deals d
            JOIN deals_fts ON d.id = deals_fts.rowid
            JOIN stages s ON d.stage_id = s.id
            WHERE deals_fts MATCH ? AND s.is_terminal = 0
            """,
            (match,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT d.* FROM deals d
            JOIN stages s ON d.stage_id = s.id
            WHERE s.is_terminal = 0
            """
        ).fetchall()

    deals = [_deal_dict(r) for r in rows]
    deals.sort(key=lambda d: (d["title"] or "").casefold())
    return deals


def archive(conn: sqlite3.Connection) -> list[dict]:
    """Closed items (``closed_at IS NOT NULL``), newest first by ``closed_at``."""
    rows = conn.execute(
        """
        SELECT * FROM deals
        WHERE closed_at IS NOT NULL
        ORDER BY closed_at DESC, id DESC
        """
    ).fetchall()
    return [_deal_dict(r) for r in rows]
