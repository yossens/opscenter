"""T5 tests: items — CRUD, moving between stages, board, archive.

Acceptance criteria come from docs/specs/001-step1-inbox-pipeline.md, task T5
(and related edge cases from the "Risks and edge cases" section: Cyrillic and
case in search — all item search/sorting goes through FTS5 + Python
``casefold``, not SQL ``NOCASE``/raw sorting). The tests are written from the
spec, not from the implementation: at the time of writing ``app/routers/deals.py``
and ``app/repo/deals.py`` did not exist yet (the correct TDD state — the tests
collect but fail).

Only the ``tests/conftest.py`` fixtures are used: ``client``, ``sqlite_conn``.
No new fixtures were added to conftest.py — the helper code (inserting
stages/items/notes directly into the DB) lives locally in this file, following
``tests/test_notes.py``/``tests/test_inbox.py``.

Assumptions about the JSON response shape that the spec does not fix literally
(documented explicitly so code review/implementation can cross-check):

- ``GET /api/deals``, ``GET /api/deals/archive`` and ``GET /api/board`` return
  a "bare" list (without a wrapper like ``{"items": [...]}"``) — by analogy
  with the already-accepted ``GET /api/notes`` (T3/T4), which also returns a
  bare list.
- Each ``GET /api/board`` column is an object with a stage identifier key
  (``stage_id`` or, as a fallback, ``id``) and a list of cards under one of the
  keys ``cards``/``deals``/``items``; for the terminal column that list is
  either absent or empty, and somewhere in the column object there is an integer
  counter field (the field name is not fixed by the spec — the test looks for it
  by value, not by a specific key name).
- The item feed in ``GET /api/deals/{id}`` lives under the ``notes`` key (by
  analogy with the ``attachments`` field inside each note, already accepted in
  T3/T4), and each element contains an ``attachments`` key (a list, possibly
  empty).
- "Chronologically" for the item card feed means "oldest first" (unlike the
  Inbox feed, where "newest on top" is explicitly stated).

If the implementation chooses a different response shape, these tests state
exactly where the expectation diverges rather than just failing with a
``KeyError``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from helpers import (
    _deal_row,
    _first_non_terminal_stage,
    _insert_deal,
    _insert_note,
    _stages_by_position,
    _terminal_stage,
)

# ---------------------------------------------------------------------------
# Helper functions: direct DB access (stage seed from T1, inserting
# items/notes bypassing the API where the input timestamps must be controlled
# precisely).
# ---------------------------------------------------------------------------


def _stage_by_position_index(sqlite_conn, index: int):
    rows = _stages_by_position(sqlite_conn)
    assert len(rows) > index, "expected the migration 001 stage seed"
    return rows[index]


def _second_non_terminal_stage(sqlite_conn):
    rows = sqlite_conn.execute(
        "SELECT * FROM stages WHERE is_terminal = 0 ORDER BY position LIMIT 2"
    ).fetchall()
    assert len(rows) >= 2, "need at least 2 non-terminal stages for a move"
    return rows[1]


def _insert_attached_note(sqlite_conn, deal_id: int, body: str, created_at: str) -> int:
    cur = sqlite_conn.execute(
        """
        INSERT INTO notes (body, status, deal_id, created_at)
        VALUES (?, 'attached', ?, ?)
        """,
        (body, deal_id, created_at),
    )
    sqlite_conn.commit()
    return cur.lastrowid


def _insert_attached_note_pinned_with_ocr(
    sqlite_conn, deal_id: int, body: str, created_at: str, ocr_text: str
) -> int:
    cur = sqlite_conn.execute(
        """
        INSERT INTO notes (body, status, deal_id, created_at, is_pinned, ocr_text)
        VALUES (?, 'attached', ?, ?, 1, ?)
        """,
        (body, deal_id, created_at, ocr_text),
    )
    sqlite_conn.commit()
    return cur.lastrowid


def _entered_iso_at_local_10am(d: date) -> str:
    """UTC ISO string corresponding to 10:00 local time on date ``d``.

    10:00 (not midnight) is chosen on the same principle as in
    ``tests/test_workdays.py``: it avoids "jumping" a calendar day when
    converting to UTC for reasonable local-TZ offsets of a laptop.
    """
    local_dt = datetime(d.year, d.month, d.day, 10, 0, 0)
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _entered_at_n_workdays_ago(n: int) -> str:
    """Stage-entry moment (UTC ISO) such that ``workdays_since(ts, today) == n``.

    The test runs on an arbitrary calendar day, so a specific stage-entry date
    for "N business days ago" cannot be hardcoded. Instead we search for the
    calendar date and use the separately tested (T2,
    ``tests/test_workdays.py``) pure module ``app.workdays.workdays_since`` as
    an oracle for building the test's input data — not for checking the
    assertion itself (the test's assertion below is a fixed ``aging_level``
    value from the spec table, not a re-call of ``aging_level``).
    """
    from app.workdays import workdays_since

    today = date.today()
    for offset in range(1, 30):
        candidate = today - timedelta(days=offset)
        iso = _entered_iso_at_local_10am(candidate)
        if workdays_since(iso, today) == n:
            return iso
    raise AssertionError(f"could not find a date for {n} business days ago")


_CARD_LIST_KEYS = ("cards", "deals", "items")


def _cards_of(column: dict) -> list:
    for key in _CARD_LIST_KEYS:
        if key in column:
            return column[key]
    raise AssertionError(
        "the board column contains no card list under any of the "
        f"expected keys {_CARD_LIST_KEYS}: {column}"
    )


def _cards_of_or_none(column: dict):
    for key in _CARD_LIST_KEYS:
        if key in column:
            return column[key]
    return None


def _column_stage_id(column: dict) -> int:
    for key in ("stage_id", "id"):
        if key in column:
            return column[key]
    raise AssertionError(f"the column contains neither 'stage_id' nor 'id': {column}")


# ===========================================================================
# POST /api/deals
# ===========================================================================


def test_post_deal_title_only_created_in_first_stage_with_equal_timestamps(
    client, sqlite_conn
):
    first_stage = _stage_by_position_index(sqlite_conn, 0)

    response = client.post("/api/deals", json={"title": "Rose"})

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Rose"
    assert body["stage_id"] == first_stage["id"]
    assert body["created_at"] == body["stage_entered_at"] == body["last_activity_at"]

    row = _deal_row(sqlite_conn, body["id"])
    assert row["created_at"] == row["stage_entered_at"] == row["last_activity_at"]


@pytest.mark.parametrize("bad_title", ["", "   ", "\n\t"])
def test_post_deal_empty_or_whitespace_title_returns_422(client, bad_title):
    response = client.post("/api/deals", json={"title": bad_title})
    assert response.status_code == 422


def test_post_deal_rate_accepts_number(client):
    response = client.post("/api/deals", json={"title": "With rate", "rate": 12.5})

    assert response.status_code == 201
    assert response.json()["rate"] == 12.5


def test_post_deal_rate_invalid_string_returns_422(client):
    response = client.post("/api/deals", json={"title": "Bad rate", "rate": "abc"})

    assert response.status_code == 422


def test_post_deal_with_explicit_stage_id(client, sqlite_conn):
    target_stage = _second_non_terminal_stage(sqlite_conn)

    response = client.post(
        "/api/deals", json={"title": "Explicit stage", "stage_id": target_stage["id"]}
    )

    assert response.status_code == 201
    assert response.json()["stage_id"] == target_stage["id"]


# ===========================================================================
# GET /api/deals/{id}
# ===========================================================================


def test_get_deal_by_id_includes_aging_fields_and_chronological_feed(
    client, sqlite_conn
):
    stage = _first_non_terminal_stage(sqlite_conn)
    deal_id = _insert_deal(
        sqlite_conn, "Feed", stage["id"], stage_entered_at="2026-01-01T00:00:00"
    )
    older_id = _insert_attached_note(
        sqlite_conn, deal_id, "first chronologically", "2026-01-01T00:00:00"
    )
    newer_id = _insert_attached_note(
        sqlite_conn, deal_id, "second chronologically", "2026-01-02T00:00:00"
    )

    response = client.get(f"/api/deals/{deal_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == deal_id
    assert "days_in_stage" in body
    assert isinstance(body["days_in_stage"], int)
    assert body["aging_level"] in {"ok", "warn", "overdue"}

    feed = body["notes"]
    feed_ids = [n["id"] for n in feed]
    assert feed_ids == [older_id, newer_id]
    for note in feed:
        assert "attachments" in note


def test_get_deal_notes_include_is_pinned_and_ocr_text(client, sqlite_conn):
    """Regression (bugfix, outside spec 006): ``GET /api/deals/{id}`` duplicates

    the note-dict construction bypassing ``app.repo.notes._note_dict`` and did
    not pick up the ``is_pinned``/``ocr_text`` fields (Spec 006, T2), so they
    are missing from the item card feed even though they arrive correctly in
    ``GET /api/notes``.
    """
    stage = _first_non_terminal_stage(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "Note with OCR and pin", stage["id"])
    note_id = _insert_attached_note_pinned_with_ocr(
        sqlite_conn,
        deal_id,
        "document scan",
        "2026-01-01T00:00:00",
        ocr_text="text recognized from the scan",
    )

    response = client.get(f"/api/deals/{deal_id}")

    assert response.status_code == 200
    feed = response.json()["notes"]
    note = next(n for n in feed if n["id"] == note_id)
    assert "is_pinned" in note, f"the 'is_pinned' field is missing from the item feed note: {note}"
    assert "ocr_text" in note, f"the 'ocr_text' field is missing from the item feed note: {note}"
    assert note["is_pinned"] == 1
    assert note["ocr_text"] == "text recognized from the scan"


def test_get_deal_nonexistent_returns_404(client):
    response = client.get("/api/deals/999999")
    assert response.status_code == 404


# ===========================================================================
# PATCH /api/deals/{id}
# ===========================================================================


@pytest.mark.parametrize(
    "field, value",
    [
        ("company", "New Company"),
        ("partner", "Contact person"),
        ("rate", 7.25),
        ("jurisdiction", "US"),
        ("waiting_on", "client"),
        ("description", "card description text"),
    ],
)
def test_patch_deal_field_updates_value_and_last_activity_at(
    client, sqlite_conn, field, value
):
    stage = _first_non_terminal_stage(sqlite_conn)
    deal_id = _insert_deal(
        sqlite_conn,
        "Card edits",
        stage["id"],
        last_activity_at="2020-01-01T00:00:00",
    )

    response = client.patch(f"/api/deals/{deal_id}", json={field: value})

    assert response.status_code == 200
    row = _deal_row(sqlite_conn, deal_id)
    assert row[field] == value
    assert row["last_activity_at"] != "2020-01-01T00:00:00"


def test_patch_deal_stage_id_change_forbidden_returns_422(client, sqlite_conn):
    first = _first_non_terminal_stage(sqlite_conn)
    second = _second_non_terminal_stage(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "No stage change via PATCH", first["id"])

    response = client.patch(f"/api/deals/{deal_id}", json={"stage_id": second["id"]})

    assert response.status_code == 422
    assert _deal_row(sqlite_conn, deal_id)["stage_id"] == first["id"]


def test_patch_deal_empty_body_does_not_bump_last_activity_at(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    deal_id = _insert_deal(
        sqlite_conn,
        "No-op PATCH",
        stage["id"],
        last_activity_at="2020-01-01T00:00:00",
    )
    before = _deal_row(sqlite_conn, deal_id)["last_activity_at"]

    response = client.patch(f"/api/deals/{deal_id}", json={})

    assert response.status_code == 200
    assert _deal_row(sqlite_conn, deal_id)["last_activity_at"] == before


def test_patch_deal_only_unknown_keys_does_not_bump_last_activity_at(
    client, sqlite_conn
):
    stage = _first_non_terminal_stage(sqlite_conn)
    deal_id = _insert_deal(
        sqlite_conn,
        "PATCH with only unknown keys",
        stage["id"],
        last_activity_at="2020-01-01T00:00:00",
    )
    before = _deal_row(sqlite_conn, deal_id)["last_activity_at"]

    response = client.patch(f"/api/deals/{deal_id}", json={"not_a_field": "x"})

    assert response.status_code == 200
    assert _deal_row(sqlite_conn, deal_id)["last_activity_at"] == before


# ===========================================================================
# POST /api/deals/{id}/move
# ===========================================================================


def test_move_to_another_stage_updates_stage_entered_at_and_last_activity_at(
    client, sqlite_conn
):
    first = _first_non_terminal_stage(sqlite_conn)
    second = _second_non_terminal_stage(sqlite_conn)
    deal_id = _insert_deal(
        sqlite_conn,
        "Move between stages",
        first["id"],
        stage_entered_at="2020-01-01T00:00:00",
        last_activity_at="2020-01-01T00:00:00",
    )

    response = client.post(
        f"/api/deals/{deal_id}/move", json={"stage_id": second["id"]}
    )

    assert response.status_code == 200
    row = _deal_row(sqlite_conn, deal_id)
    assert row["stage_id"] == second["id"]
    assert row["stage_entered_at"] != "2020-01-01T00:00:00"
    assert row["last_activity_at"] != "2020-01-01T00:00:00"


def test_move_to_same_stage_is_noop_dates_unchanged(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    deal_id = _insert_deal(
        sqlite_conn,
        "Move to the current stage",
        stage["id"],
        stage_entered_at="2020-01-01T00:00:00",
        last_activity_at="2020-01-01T00:00:00",
    )

    response = client.post(f"/api/deals/{deal_id}/move", json={"stage_id": stage["id"]})

    assert response.status_code == 200
    row = _deal_row(sqlite_conn, deal_id)
    assert row["stage_id"] == stage["id"]
    assert row["stage_entered_at"] == "2020-01-01T00:00:00"
    assert row["last_activity_at"] == "2020-01-01T00:00:00"


def test_move_to_terminal_stage_sets_closed_at(client, sqlite_conn):
    first = _first_non_terminal_stage(sqlite_conn)
    terminal = _terminal_stage(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "To be closed", first["id"])
    assert _deal_row(sqlite_conn, deal_id)["closed_at"] is None

    response = client.post(
        f"/api/deals/{deal_id}/move", json={"stage_id": terminal["id"]}
    )

    assert response.status_code == 200
    row = _deal_row(sqlite_conn, deal_id)
    assert row["stage_id"] == terminal["id"]
    assert row["closed_at"] is not None


def test_move_out_of_terminal_stage_clears_closed_at(client, sqlite_conn):
    first = _first_non_terminal_stage(sqlite_conn)
    terminal = _terminal_stage(sqlite_conn)
    deal_id = _insert_deal(
        sqlite_conn, "Reopen", terminal["id"], closed_at="2026-01-01T00:00:00"
    )

    response = client.post(f"/api/deals/{deal_id}/move", json={"stage_id": first["id"]})

    assert response.status_code == 200
    row = _deal_row(sqlite_conn, deal_id)
    assert row["stage_id"] == first["id"]
    assert row["closed_at"] is None


def test_move_to_nonexistent_stage_returns_404_and_deal_unchanged(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "Invalid move", stage["id"])

    response = client.post(f"/api/deals/{deal_id}/move", json={"stage_id": 999999})

    assert response.status_code == 404
    assert _deal_row(sqlite_conn, deal_id)["stage_id"] == stage["id"]


# ===========================================================================
# GET /api/board
# ===========================================================================


def test_board_columns_ordered_by_position(client, sqlite_conn):
    expected_order = [row["id"] for row in _stages_by_position(sqlite_conn)]

    response = client.get("/api/board")

    assert response.status_code == 200
    columns = response.json()
    assert isinstance(columns, list)
    actual_order = [_column_stage_id(col) for col in columns]
    assert actual_order == expected_order


def test_board_card_contains_required_fields(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    deal_id = _insert_deal(
        sqlite_conn,
        "Board card",
        stage["id"],
        company="Acme LLC",
        partner="John",
        waiting_on="client",
    )

    response = client.get("/api/board")
    assert response.status_code == 200
    columns = response.json()
    column = next(c for c in columns if _column_stage_id(c) == stage["id"])
    cards = _cards_of(column)
    card = next(c for c in cards if c["id"] == deal_id)

    for key in (
        "title",
        "company",
        "partner",
        "waiting_on",
        "days_in_stage",
        "aging_level",
    ):
        assert key in card, f"the board card must contain the field '{key}': {card}"
    assert card["title"] == "Board card"
    assert card["company"] == "Acme LLC"
    assert card["partner"] == "John"
    assert card["waiting_on"] == "client"


def test_board_card_aging_level_overdue_after_6_workdays_at_threshold_5(
    client, sqlite_conn
):
    stage = _first_non_terminal_stage(sqlite_conn)
    assert stage["threshold_days"] == 5, "migration 001 seed: every stage has threshold_days=5"
    entered_at = _entered_at_n_workdays_ago(6)
    deal_id = _insert_deal(
        sqlite_conn, "Overdue item", stage["id"], stage_entered_at=entered_at
    )

    response = client.get("/api/board")
    columns = response.json()
    column = next(c for c in columns if _column_stage_id(c) == stage["id"])
    card = next(c for c in _cards_of(column) if c["id"] == deal_id)

    assert card["days_in_stage"] == 6
    assert card["aging_level"] == "overdue"


def test_board_terminal_column_has_no_card_details_but_has_a_count(client, sqlite_conn):
    terminal = _terminal_stage(sqlite_conn)
    other_stage = _first_non_terminal_stage(sqlite_conn)
    closed_ids = [
        _insert_deal(
            sqlite_conn,
            f"Closed {i}",
            terminal["id"],
            closed_at="2026-01-01T00:00:00",
        )
        for i in range(2)
    ]
    # A control open item in another column, so the terminal column's counter
    # cannot be accidentally confused with the total number of items in the DB.
    _insert_deal(sqlite_conn, "Not closed", other_stage["id"])

    response = client.get("/api/board")
    columns = response.json()
    column = next(c for c in columns if _column_stage_id(c) == terminal["id"])

    assert column.get("is_terminal") is True

    cards = _cards_of_or_none(column)
    assert not cards, (
        f"the terminal column must not return a list of cards with details: {column}"
    )

    count_fields = {
        k: v
        for k, v in column.items()
        if isinstance(v, int)
        and not isinstance(v, bool)
        and k not in ("stage_id", "id", "position", "threshold_days")
    }
    assert len(closed_ids) in count_fields.values(), (
        f"expected the closed-item count ({len(closed_ids)}) among the "
        f"integer fields of the terminal column: {column}"
    )


# ===========================================================================
# GET /api/deals: dropdown search via FTS + alphabetical sorting (casefold)
# ===========================================================================


def test_deals_dropdown_search_finds_by_prefix_case_insensitive_cyrillic(
    client, sqlite_conn
):
    stage = _first_non_terminal_stage(sqlite_conn)
    _insert_deal(sqlite_conn, "Rose", stage["id"])
    _insert_deal(sqlite_conn, "Dandelion", stage["id"])

    lower = client.get("/api/deals", params={"q": "ros"})
    upper = client.get("/api/deals", params={"q": "ROS"})

    assert lower.status_code == 200
    assert upper.status_code == 200
    lower_titles = [d["title"] for d in lower.json()]
    upper_titles = [d["title"] for d in upper.json()]
    assert "Rose" in lower_titles
    assert "Rose" in upper_titles
    assert "Dandelion" not in lower_titles


def test_deals_dropdown_search_excludes_closed_deals(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    terminal = _terminal_stage(sqlite_conn)
    _insert_deal(sqlite_conn, "Rose open", stage["id"])
    _insert_deal(
        sqlite_conn, "Rose closed", terminal["id"], closed_at="2026-01-01T00:00:00"
    )

    response = client.get("/api/deals", params={"q": "rose"})

    assert response.status_code == 200
    titles = [d["title"] for d in response.json()]
    assert "Rose open" in titles
    assert "Rose closed" not in titles


def test_deals_empty_query_returns_active_sorted_by_casefold_alphabet(
    client, sqlite_conn
):
    stage = _first_non_terminal_stage(sqlite_conn)
    _insert_deal(sqlite_conn, "Rose", stage["id"])
    _insert_deal(sqlite_conn, "apple", stage["id"])

    response = client.get("/api/deals", params={"q": ""})

    assert response.status_code == 200
    titles = [d["title"] for d in response.json()]
    assert "apple" in titles and "Rose" in titles
    assert titles.index("apple") < titles.index("Rose"), (
        "alphabetical sorting via casefold must place lowercase 'apple' before "
        "uppercase 'Rose' (not a strict ASCII byte/codepoint order, where "
        "uppercase 'R' < lowercase 'a')"
    )


def test_deals_empty_query_excludes_closed_deals(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    terminal = _terminal_stage(sqlite_conn)
    _insert_deal(sqlite_conn, "Active", stage["id"])
    _insert_deal(
        sqlite_conn, "Closed", terminal["id"], closed_at="2026-01-01T00:00:00"
    )

    response = client.get("/api/deals", params={"q": ""})

    assert response.status_code == 200
    titles = [d["title"] for d in response.json()]
    assert "Active" in titles
    assert "Closed" not in titles


# ===========================================================================
# GET /api/deals/archive
# ===========================================================================


def test_deals_archive_lists_only_closed_sorted_by_closed_at_desc(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    terminal = _terminal_stage(sqlite_conn)
    open_deal_id = _insert_deal(sqlite_conn, "Open", stage["id"])
    older_closed_id = _insert_deal(
        sqlite_conn, "Closed earlier", terminal["id"], closed_at="2025-01-01T00:00:00"
    )
    newer_closed_id = _insert_deal(
        sqlite_conn, "Closed later", terminal["id"], closed_at="2025-06-01T00:00:00"
    )

    response = client.get("/api/deals/archive")

    assert response.status_code == 200
    body = response.json()
    ids = [d["id"] for d in body]
    assert open_deal_id not in ids
    assert ids == [newer_closed_id, older_closed_id]


# ===========================================================================
# DELETE /api/deals/{id}
# ===========================================================================


def test_delete_deal_removes_deal_and_all_dependencies(client, sqlite_conn, config):
    """Deleting an item hard-cleans notes, attachments (the file on disk), pings
    and nulls out suggested_deal_id on other notes."""
    stage = _first_non_terminal_stage(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "For deletion", stage["id"])

    # An attached note with an attachment (a row + a file on disk).
    note_id = _insert_note(sqlite_conn, body="important", status="attached", deal_id=deal_id)
    stored_name = "deadbeef.txt"
    sqlite_conn.execute(
        """
        INSERT INTO attachments
            (note_id, original_name, stored_name, mime_type, size_bytes, created_at)
        VALUES (?, 'file.txt', ?, 'text/plain', 3, '2026-01-01T00:00:00')
        """,
        (note_id, stored_name),
    )
    # A ping.
    sqlite_conn.execute(
        "INSERT INTO deal_pings (deal_id, pinged_at, escalation_step) VALUES (?, '2026-01-01T00:00:00', 1)",
        (deal_id,),
    )
    # An unrelated inbox note that merely suggested this item.
    other_note_id = _insert_note(sqlite_conn, body="candidate", status="inbox")
    sqlite_conn.execute(
        "UPDATE notes SET suggested_deal_id = ? WHERE id = ?", (deal_id, other_note_id)
    )
    sqlite_conn.commit()

    attach_path = config.ATTACHMENTS_DIR / stored_name
    attach_path.write_text("abc", encoding="utf-8")

    response = client.delete(f"/api/deals/{deal_id}")

    assert response.status_code == 204
    q = lambda sql, *p: sqlite_conn.execute(sql, p).fetchone()[0]
    assert q("SELECT COUNT(*) FROM deals WHERE id = ?", deal_id) == 0
    assert q("SELECT COUNT(*) FROM notes WHERE deal_id = ?", deal_id) == 0
    assert q("SELECT COUNT(*) FROM attachments WHERE note_id = ?", note_id) == 0
    assert q("SELECT COUNT(*) FROM deal_pings WHERE deal_id = ?", deal_id) == 0
    assert q("SELECT suggested_deal_id FROM notes WHERE id = ?", other_note_id) is None
    assert not attach_path.exists()


def test_delete_missing_deal_returns_404(client):
    assert client.delete("/api/deals/999999").status_code == 404
