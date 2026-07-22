"""SQL functions for stages: list, create, rename/threshold, reorder, delete.

Data-access layer: pure functions ``(conn, ...) -> dict/list``. Routers stay
thin. ``is_terminal`` is not changed via the API. The waiting threshold is
stored in the ``stages.threshold_days`` column. Only an empty non-terminal
stage (and not the last working one) can be deleted — see ``delete_stage``.
"""

from __future__ import annotations

import sqlite3

# Stage columns returned in JSON (in a fixed order). ``track_hangs`` and
# ``is_terminal`` are plain int 0/1 (the same convention as the stages flag columns).
_STAGE_COLUMNS = (
    "id",
    "name",
    "position",
    "threshold_days",
    "is_terminal",
    "track_hangs",
)


def _stage_dict(row: sqlite3.Row) -> dict:
    """Converts a stage DB row into a dict with the fixed set of columns."""
    return {col: row[col] for col in _STAGE_COLUMNS}


def _get_stage_row(conn: sqlite3.Connection, stage_id: int) -> sqlite3.Row | None:
    """Gets the stage row by id, or ``None`` if the stage does not exist."""
    return conn.execute("SELECT * FROM stages WHERE id = ?", (stage_id,)).fetchone()


def list_stages(conn: sqlite3.Connection) -> list[dict]:
    """All stages, sorted by ``position``."""
    rows = conn.execute("SELECT * FROM stages ORDER BY position").fetchall()
    return [_stage_dict(r) for r in rows]


def create_stage(conn: sqlite3.Connection, name: str, threshold_days: int = 5) -> dict:
    """Creates a stage as the last one by ``position`` (max(position) + 1).

    ``is_terminal`` is always 0 — terminality is not set via the API.
    """
    row = conn.execute("SELECT MAX(position) AS m FROM stages").fetchone()
    next_position = (row["m"] if row["m"] is not None else -1) + 1
    cur = conn.execute(
        """
        INSERT INTO stages (name, position, threshold_days, is_terminal)
        VALUES (?, ?, ?, 0)
        """,
        (name, next_position, threshold_days),
    )
    conn.commit()
    return _stage_dict(_get_stage_row(conn, cur.lastrowid))


def patch_stage(
    conn: sqlite3.Connection, stage_id: int, updates: dict
) -> dict | str | None:
    """Changes ``name``, ``threshold_days`` and/or ``track_hangs``.

    ``updates`` contains only validated keys (``name``, ``threshold_days``,
    ``track_hangs``); ``is_terminal`` and ``position`` are not changed by this
    endpoint. Returns:

    - ``None`` — the stage does not exist (router returns 404);
    - ``"terminal_track_hangs"`` — an attempt to change ``track_hangs`` on a
      terminal stage (router returns 422, DB is not changed — design decision 4:
      terminal stages are unconditionally excluded from the detector);
    - ``dict`` — the updated stage.
    """
    row = _get_stage_row(conn, stage_id)
    if row is None:
        return None

    if "track_hangs" in updates and row["is_terminal"]:
        return "terminal_track_hangs"

    allowed = ("name", "threshold_days", "track_hangs")
    columns = [k for k in updates if k in allowed]
    if columns:
        set_parts = [f"{col} = ?" for col in columns]
        params: list = [updates[col] for col in columns]
        params.append(stage_id)
        conn.execute(
            f"UPDATE stages SET {', '.join(set_parts)} WHERE id = ?",  # noqa: S608
            params,
        )
        conn.commit()
    return _stage_dict(_get_stage_row(conn, stage_id))


def delete_stage(conn: sqlite3.Connection, stage_id: int) -> str:
    """Deletes an empty non-terminal stage.

    Returns a result status code:

    - ``"ok"`` — the stage was deleted;
    - ``"not_found"`` — the stage does not exist;
    - ``"terminal"`` — the terminal stage ("Done") is protected from deletion;
      the archive of closed items rests on it;
    - ``"has_deals"`` — items reference the stage (including those historically
      closed in it) — they must be moved first;
    - ``"last"`` — the last remaining non-terminal stage is protected,
      otherwise new items would have nowhere to land.

    Gaps in ``position`` after deletion are acceptable: sorting by ``position``
    and ``create_stage`` (max+1) are correct without renumbering.
    """
    row = _get_stage_row(conn, stage_id)
    if row is None:
        return "not_found"
    if row["is_terminal"]:
        return "terminal"
    deals = conn.execute(
        "SELECT COUNT(*) AS c FROM deals WHERE stage_id = ?", (stage_id,)
    ).fetchone()
    if deals["c"]:
        return "has_deals"
    non_terminal = conn.execute(
        "SELECT COUNT(*) AS c FROM stages WHERE is_terminal = 0"
    ).fetchone()
    if non_terminal["c"] <= 1:
        return "last"
    conn.execute("DELETE FROM stages WHERE id = ?", (stage_id,))
    conn.commit()
    return "ok"


def reorder_stages(conn: sqlite3.Connection, ordered_ids: list[int]) -> bool:
    """Reorders stages according to the full ``ordered_ids`` list.

    ``ordered_ids`` must be a permutation of all existing stage ids (no gaps,
    duplicates, or extra non-existent ids) — otherwise the order is not changed
    and ``False`` is returned. The items' ``stage_id`` is not affected: only the
    stages' ``position`` changes.
    """
    existing_ids = [
        row["id"]
        for row in conn.execute("SELECT id FROM stages ORDER BY position").fetchall()
    ]
    if sorted(ordered_ids) != sorted(existing_ids):
        return False

    # Two-phase update: first into a temporary position range (to avoid
    # violating the UNIQUE index on position during the shuffle), then into the
    # final values 0..N-1.
    offset = len(existing_ids) + 1000
    for temp_pos, stage_id in enumerate(ordered_ids):
        conn.execute(
            "UPDATE stages SET position = ? WHERE id = ?",
            (temp_pos + offset, stage_id),
        )
    for final_pos, stage_id in enumerate(ordered_ids):
        conn.execute(
            "UPDATE stages SET position = ? WHERE id = ?",
            (final_pos, stage_id),
        )
    conn.commit()
    return True
