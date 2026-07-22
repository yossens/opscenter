"""SQL functions for the hang detector: the "Ping Today" block.

Data-access layer: pure functions ``(conn, ...) -> dict/list``. Routers stay
thin. All business-day and threshold arithmetic goes only through
``app.workdays`` (F5); ping-line rendering, escalation steps, and the hide
window go only through ``app.ping``. There is no date/weekend arithmetic of its
own here.

The block is computed on the fly on each request with a fixed amount of SQL:
one ``deals JOIN stages`` query with correlated subqueries (the last attached
trimmed-non-empty note, ``pings_since``, ``MAX(pinged_at)`` after activity) plus
one settings query from ``app_meta`` — no N+1 over items.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from ..ping import (
    DEFAULT_PING_HIDDEN_DAYS,
    DEFAULT_PING_TEMPLATE,
    escalation_step,
    is_hidden_after_ping,
    prepare_last_note,
    render_ping,
)
from ..workdays import _utc_now, workdays_since

# The set of whitespace characters for TRIM in SQL: space, tab, LF, CR. SQLite's
# default TRIM strips only spaces (0x20) — a body of tabs/newlines would be
# considered non-empty; the explicit character set implements a
# "trimmed-non-empty" body (design decision 12).
_TRIM_CHARS = "' ' || char(9) || char(10) || char(13)"

# One query: cards from tracked non-terminal stages with correlated subqueries.
# Membership filters by age/snooze/window are done in Python (via app.workdays /
# app.ping), since only app.workdays computes business days.
_BLOCK_SQL = f"""
SELECT
    d.id                AS deal_id,
    d.title             AS title,
    d.company           AS company,
    d.waiting_on        AS waiting_on,
    d.stage_id          AS stage_id,
    d.last_activity_at  AS last_activity_at,
    d.snoozed_until     AS snoozed_until,
    s.name              AS stage_name,
    s.threshold_days    AS threshold_days,
    (
        SELECT n.body FROM notes n
        WHERE n.deal_id = d.id
          AND n.status = 'attached'
          AND TRIM(n.body, {_TRIM_CHARS}) != ''
        ORDER BY n.created_at DESC, n.id DESC
        LIMIT 1
    )                   AS last_note_body,
    (
        SELECT COUNT(*) FROM deal_pings p
        WHERE p.deal_id = d.id AND p.pinged_at > d.last_activity_at
    )                   AS pings_since,
    (
        SELECT MAX(p.pinged_at) FROM deal_pings p
        WHERE p.deal_id = d.id AND p.pinged_at > d.last_activity_at
    )                   AS last_ping_at
FROM deals d
JOIN stages s ON d.stage_id = s.id
WHERE s.is_terminal = 0 AND s.track_hangs = 1
"""  # noqa: S608 — only the constant _TRIM_CHARS is interpolated, not user input


def get_ping_settings(conn: sqlite3.Connection) -> tuple[str, int]:
    """Reads the ping template and M from ``app_meta`` with fallback to defaults.

    If the ``ping_template``/``ping_hidden_days`` keys are missing (or the value
    for M is non-numeric), the constants from ``app.ping`` are returned (the
    computation does not crash, checklist 8).
    """
    rows = conn.execute(
        "SELECT key, value FROM app_meta "
        "WHERE key IN ('ping_template', 'ping_hidden_days')"
    ).fetchall()
    meta = {r["key"]: r["value"] for r in rows}

    template = meta.get("ping_template")
    if not template:
        template = DEFAULT_PING_TEMPLATE

    try:
        hidden_days = int(meta["ping_hidden_days"])
    except (KeyError, TypeError, ValueError):
        hidden_days = DEFAULT_PING_HIDDEN_DAYS

    return template, hidden_days


def get_ping_settings_view(conn: sqlite3.Connection) -> dict:
    """Detector settings for ``GET /api/settings/ping``.

    Returns the current ``template``/``hidden_days`` (with fallback to defaults
    if the ``app_meta`` keys are missing — the same logic as in
    ``get_ping_settings``) plus ``default_template`` (the constant from
    ``app.ping`` for the "reset to default" button in settings).
    """
    template, hidden_days = get_ping_settings(conn)
    return {
        "template": template,
        "hidden_days": hidden_days,
        "default_template": DEFAULT_PING_TEMPLATE,
    }


def set_ping_settings(
    conn: sqlite3.Connection, template: str, hidden_days: int
) -> None:
    """Writes ``ping_template``/``ping_hidden_days`` into ``app_meta`` (upsert).

    The values are already validated by the router (non-empty template, ``0 <=
    hidden_days <= 365``). The upsert is robust to missing keys (the same
    fallback invariant as the read).
    """
    conn.execute(
        "INSERT INTO app_meta (key, value) VALUES ('ping_template', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (template,),
    )
    conn.execute(
        "INSERT INTO app_meta (key, value) VALUES ('ping_hidden_days', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(hidden_days),),
    )
    conn.commit()


def ping_block(conn: sqlite3.Connection) -> dict:
    """The "Ping Today" block: ``{"count": N, "items": [...]}``.

    Membership: a non-terminal stage with ``track_hangs = 1`` (in SQL), business
    days without activity strictly greater than the stage threshold, snooze not
    active, and the post-ping hide window not in effect. Sorting: ``overdue_by``
    descending, ties broken by ``days_since_activity`` descending, then
    ``deal_id`` ascending.
    """
    template, hidden_days = get_ping_settings(conn)
    today = date.today()
    today_str = today.isoformat()

    rows = conn.execute(_BLOCK_SQL).fetchall()
    items: list[dict] = []
    for r in rows:
        threshold = r["threshold_days"]
        days = workdays_since(r["last_activity_at"], today)
        if days <= threshold:
            continue

        snoozed_until = r["snoozed_until"]
        if snoozed_until is not None and today_str < snoozed_until:
            continue

        pings_since = r["pings_since"] or 0
        last_ping_at = r["last_ping_at"]
        if last_ping_at is not None and is_hidden_after_ping(
            last_ping_at, today, hidden_days, pings_since
        ):
            continue

        step = escalation_step(pings_since)
        counterparty = r["company"] or r["title"]
        ping_text = render_ping(
            template,
            {
                "waiting_for": r["waiting_on"] or "",
                "counterparty": counterparty,
                "stage": r["stage_name"],
                "days": str(days),
                "last_note": prepare_last_note(r["last_note_body"]),
            },
        )

        items.append(
            {
                "deal_id": r["deal_id"],
                "title": r["title"],
                "company": r["company"],
                "stage_id": r["stage_id"],
                "stage_name": r["stage_name"],
                "days_since_activity": days,
                "threshold_days": threshold,
                "overdue_by": days - threshold,
                "waiting_on": r["waiting_on"],
                "last_activity_at": r["last_activity_at"],
                "escalation_step": step,
                "escalate": step == 3,
                "ping_text": ping_text,
            }
        )

    items.sort(
        key=lambda it: (-it["overdue_by"], -it["days_since_activity"], it["deal_id"])
    )
    return {"count": len(items), "items": items}


# Data for a single item to render the ping line — the same logic as in the
# block (last trimmed-non-empty attached note, pings_since after activity).
_PING_DEAL_SQL = f"""
SELECT
    d.id                AS deal_id,
    d.title             AS title,
    d.company           AS company,
    d.waiting_on        AS waiting_on,
    d.last_activity_at  AS last_activity_at,
    s.name              AS stage_name,
    s.threshold_days    AS threshold_days,
    (
        SELECT n.body FROM notes n
        WHERE n.deal_id = d.id
          AND n.status = 'attached'
          AND TRIM(n.body, {_TRIM_CHARS}) != ''
        ORDER BY n.created_at DESC, n.id DESC
        LIMIT 1
    )                   AS last_note_body,
    (
        SELECT COUNT(*) FROM deal_pings p
        WHERE p.deal_id = d.id AND p.pinged_at > d.last_activity_at
    )                   AS pings_since
FROM deals d
JOIN stages s ON d.stage_id = s.id
WHERE d.id = ?
"""  # noqa: S608 — only the constant _TRIM_CHARS is interpolated, not user input


def record_ping(conn: sqlite3.Connection, deal_id: int) -> bool:
    """Writes a row into ``deal_pings`` for item ``deal_id``.

    ``pinged_at`` = the moment of the call (UTC ISO), ``escalation_step`` = the
    current step at ping time (``escalation_step(pings_since)`` from activity),
    ``ping_text`` = a fresh render from the current template. Does NOT touch
    ``last_activity_at`` (key invariant F3). Returns ``False`` if the item does
    not exist (the router returns 404).
    """
    row = conn.execute(_PING_DEAL_SQL, (deal_id,)).fetchone()
    if row is None:
        return False

    template, _ = get_ping_settings(conn)
    days = workdays_since(row["last_activity_at"], date.today())
    pings_since = row["pings_since"] or 0
    step = escalation_step(pings_since)
    counterparty = row["company"] or row["title"]
    ping_text = render_ping(
        template,
        {
            "waiting_for": row["waiting_on"] or "",
            "counterparty": counterparty,
            "stage": row["stage_name"],
            "days": str(days),
            "last_note": prepare_last_note(row["last_note_body"]),
        },
    )

    conn.execute(
        """
        INSERT INTO deal_pings (deal_id, pinged_at, escalation_step, ping_text)
        VALUES (?, ?, ?, ?)
        """,
        (deal_id, _utc_now(), step, ping_text),
    )
    conn.commit()
    return True


def set_snooze(conn: sqlite3.Connection, deal_id: int, until: str | None) -> bool:
    """Writes ``deals.snoozed_until`` (``until`` is already a valid date or None).

    Does NOT touch ``last_activity_at`` (a snooze is not activity). Returns
    ``False`` if the item does not exist (the router returns 404).
    """
    exists = conn.execute("SELECT 1 FROM deals WHERE id = ?", (deal_id,)).fetchone()
    if exists is None:
        return False
    conn.execute("UPDATE deals SET snoozed_until = ? WHERE id = ?", (until, deal_id))
    conn.commit()
    return True
