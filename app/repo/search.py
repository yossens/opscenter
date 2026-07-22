"""SQL functions for global search and the textual "Slice".

Global search runs over two FTS5 tables (``deals_fts``, ``notes_fts``) with
highlighting via ``snippet()`` and a shared MATCH-query sanitizer
(``app/fts.py``, also used in the T5 search dropdown to guard against FTS
syntax injection). The "Slice" is a flat textual summary of active items,
grouped by stage in board order; business days are computed via
``app/workdays.py``.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from ..fts import sanitize_fts_query
from ..workdays import workdays_since

# snippet() parameters: highlight markers and ellipsis, window of up to 10 tokens.
_SNIP_START = "[b]"
_SNIP_END = "[/b]"
_SNIP_ELLIPSIS = "…"
_SNIP_TOKENS = 10


def search(conn: sqlite3.Connection, q: str) -> dict:
    """Global search over items and notes.

    Returns ``{"deals": [...], "notes": [...]}``. Each ``deals`` element is
    ``{id, title, snippet}``; each ``notes`` element is
    ``{id, deal_id, snippet, status}``. An empty/garbage ``q`` (no valid tokens)
    yields empty groups, without running MATCH.
    """
    match = sanitize_fts_query(q)
    if match is None:
        return {"deals": [], "notes": []}

    deal_rows = conn.execute(
        """
        SELECT
            deals_fts.rowid AS id,
            deals_fts.title AS title,
            snippet(deals_fts, -1, ?, ?, ?, ?) AS snippet
        FROM deals_fts
        WHERE deals_fts MATCH ?
        ORDER BY rank
        """,
        (_SNIP_START, _SNIP_END, _SNIP_ELLIPSIS, _SNIP_TOKENS, match),
    ).fetchall()

    note_rows = conn.execute(
        """
        SELECT
            n.id AS id,
            n.deal_id AS deal_id,
            n.status AS status,
            snippet(notes_fts, 0, ?, ?, ?, ?) AS snippet
        FROM notes_fts
        JOIN notes n ON n.id = notes_fts.rowid
        WHERE notes_fts MATCH ?
        ORDER BY rank
        """,
        (_SNIP_START, _SNIP_END, _SNIP_ELLIPSIS, _SNIP_TOKENS, match),
    ).fetchall()

    deals = [
        {"id": r["id"], "title": r["title"], "snippet": r["snippet"]} for r in deal_rows
    ]
    notes = [
        {
            "id": r["id"],
            "deal_id": r["deal_id"],
            "snippet": r["snippet"],
            "status": r["status"],
        }
        for r in note_rows
    ]
    # WARNING: snippet contains user text from notes/items with the highlight
    # markers [b]/[/b]. The frontend (T15) MUST:
    # 1. HTML-escape the snippet (textContent or innerText)
    # 2. Only then replace [b]/[/b] with the HTML tags <b></b>
    # Otherwise self-XSS via malicious note content is possible.
    return {"deals": deals, "notes": notes}


def board_slice(conn: sqlite3.Connection) -> str:
    """Flat text over active items, grouped by stage.

    Stages are in ``position`` order, non-terminal only, and only those that
    have active (not closed) items. For each stage a stage header is emitted,
    then one line per item:
    ``"<Title> — <stage>, <N business days>[, waiting on: <who>]"``. The
    ``, waiting on: ...`` fragment is omitted when ``waiting_on`` is empty.
    """
    today = date.today()
    stage_rows = conn.execute(
        "SELECT * FROM stages WHERE is_terminal = 0 ORDER BY position"
    ).fetchall()

    blocks: list[str] = []
    for stage in stage_rows:
        deal_rows = conn.execute(
            """
            SELECT * FROM deals
            WHERE stage_id = ? AND closed_at IS NULL
            ORDER BY id
            """,
            (stage["id"],),
        ).fetchall()
        if not deal_rows:
            continue

        lines = [stage["name"]]
        for d in deal_rows:
            days = workdays_since(d["stage_entered_at"], today)
            line = f"{d['title']} — {stage['name']}, {days} business days"
            if d["waiting_on"]:
                line += f", waiting on: {d['waiting_on']}"
            lines.append(line)
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)
