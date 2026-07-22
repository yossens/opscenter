"""T4 tests: "Pinged", "Snooze until…" actions, pings in the feed, board snooze.

Acceptance criteria come from docs/specs/002-step2-hang-detector.md, task T4
(and the related sections "Terms and calculation rules", "Design decisions",
API/"POST /api/deals/{id}/ping", "POST /api/deals/{id}/snooze",
"GET /api/deals/{id}", "GET /api/board", "Risks and edge cases"). The tests are
written against the spec, not the implementation: at writing time
``app/routers/pings.py`` has only ``GET /api/pings`` (T3, already accepted) —
neither ``POST /api/deals/{id}/ping`` nor ``POST /api/deals/{id}/snooze`` exist
(those paths are not registered in any router), so requests to them currently
return 404 — the correct TDD state. ``GET /api/deals/{id}`` and
``GET /api/board`` already exist (Step 1) but do not yet contain
``pings``/``snoozed_until`` — accessing those keys in the tests should raise
``KeyError``/``AssertionError``, not an import error.

Only fixtures from ``tests/conftest.py`` are used: ``client``, ``sqlite_conn``,
``project_root``. Helper code (inserting stages/items/notes/pings directly into
the DB, picking dates "N business days ago") lives locally in this file,
following the already-accepted ``tests/test_deals.py``/``tests/test_pings_block.py``
(T3).

Oracles: ``app.workdays.workdays_since`` (T2 of Step 1) and
``app.ping.escalation_step``/``is_hidden_after_ping`` (T2 of this spec) are used
only to *build* input fixtures/expected values for the test, not as a
re-invocation of the T4 logic under test.

Fixture rule (spec section "Terms"): any backdating of an item's activity in
this file shifts BACK both ``last_activity_at`` AND ``stage_entered_at``
(preserving the invariant ``stage_entered_at <= last_activity_at``). The
step auto-reset test additionally sets the ``pinged_at`` of existing pings
deliberately EARLIER than the new ``last_activity_at`` — otherwise the old pings
would count as "after the activity" and the step would not reset (see the T4
criterion "Auto-reset", checklist 5).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from helpers import (
    _deal_row,
    _first_non_terminal_stage,
    _insert_deal,
    _second_non_terminal_stage,
)

# ---------------------------------------------------------------------------
# Helper functions: stages, items, notes, pings — direct DB work.
# ---------------------------------------------------------------------------


def _set_threshold(sqlite_conn, stage_id: int, threshold_days: int) -> None:
    sqlite_conn.execute(
        "UPDATE stages SET threshold_days = ? WHERE id = ?", (threshold_days, stage_id)
    )
    sqlite_conn.commit()


def _set_ping_hidden_days(sqlite_conn, value: int) -> None:
    sqlite_conn.execute(
        "UPDATE app_meta SET value = ? WHERE key = 'ping_hidden_days'", (str(value),)
    )
    sqlite_conn.commit()


def _insert_ping(
    sqlite_conn,
    deal_id: int,
    pinged_at: str,
    escalation_step: int = 1,
    ping_text: str = "",
) -> int:
    cur = sqlite_conn.execute(
        """
        INSERT INTO deal_pings (deal_id, pinged_at, escalation_step, ping_text)
        VALUES (?, ?, ?, ?)
        """,
        (deal_id, pinged_at, escalation_step, ping_text),
    )
    sqlite_conn.commit()
    return cur.lastrowid


def _entered_iso_at_local_10am(d: date) -> str:
    """A UTC ISO string corresponding to 10:00 local time on the date ``d``."""
    local_dt = datetime(d.year, d.month, d.day, 10, 0, 0)
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _iso_n_workdays_ago(n: int) -> str:
    """A moment in time (UTC ISO) such that ``workdays_since(ts, today) == n``."""
    from app.workdays import workdays_since

    today = date.today()
    for offset in range(1, 40):
        candidate = today - timedelta(days=offset)
        iso = _entered_iso_at_local_10am(candidate)
        if workdays_since(iso, today) == n:
            return iso
    raise AssertionError(f"could not find a date for {n} business days ago")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _ping_rows(sqlite_conn, deal_id: int) -> list:
    return sqlite_conn.execute(
        "SELECT * FROM deal_pings WHERE deal_id = ? ORDER BY id", (deal_id,)
    ).fetchall()


def _get_pings(client) -> dict:
    response = client.get("/api/pings")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == len(body["items"])
    return body


def _ids_in(body: dict) -> list[int]:
    return [i["deal_id"] for i in body["items"]]


def _item_for(body: dict, deal_id: int) -> dict:
    matches = [i for i in body["items"] if i["deal_id"] == deal_id]
    assert matches, f"item {deal_id} not found in items: {body['items']}"
    return matches[0]


def _get_deal(client, deal_id: int) -> dict:
    response = client.get(f"/api/deals/{deal_id}")
    assert response.status_code == 200, response.text
    return response.json()


_CARD_LIST_KEYS = ("cards", "deals", "items")


def _cards_of_or_none(column: dict):
    for key in _CARD_LIST_KEYS:
        if key in column:
            return column[key]
    return None


def _column_stage_id(column: dict) -> int:
    for key in ("stage_id", "id"):
        if key in column:
            return column[key]
    raise AssertionError(f"column has neither 'stage_id' nor 'id': {column}")


def _find_card(board_json: list, deal_id: int) -> dict:
    for column in board_json:
        cards = _cards_of_or_none(column)
        if not cards:
            continue
        for card in cards:
            if card.get("id") == deal_id:
                return card
    raise AssertionError(f"card for item {deal_id} not found on the board: {board_json}")


def _all_card_ids(board_json: list) -> set[int]:
    ids: set[int] = set()
    for column in board_json:
        cards = _cards_of_or_none(column)
        if not cards:
            continue
        for card in cards:
            ids.add(card["id"])
    return ids


def _make_overdue_deal(
    sqlite_conn, title: str, *, threshold: int = 1, workdays_ago: int = 3
):
    """An item in the first non-terminal stage, overdue past the threshold."""
    stage = _first_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage["id"], threshold)
    ts = _iso_n_workdays_ago(workdays_ago)
    deal_id = _insert_deal(
        sqlite_conn, title, stage["id"], stage_entered_at=ts, last_activity_at=ts
    )
    return deal_id, stage


# ===========================================================================
# POST /api/deals/{id}/ping — the key F3 invariant (checklist 3)
# ===========================================================================


def test_ping_creates_deal_pings_row_with_step_1_pinged_at_and_nonempty_text(
    client, sqlite_conn
):
    deal_id, _ = _make_overdue_deal(sqlite_conn, "First ping of the item")

    response = client.post(f"/api/deals/{deal_id}/ping")
    assert response.status_code == 200, response.text

    rows = _ping_rows(sqlite_conn, deal_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["escalation_step"] == 1
    assert row["ping_text"] != ""
    # pinged_at — a valid UTC ISO string like YYYY-MM-DDTHH:MM:SS, close to "now".
    parsed = datetime.strptime(row["pinged_at"], "%Y-%m-%dT%H:%M:%S")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((now - parsed).total_seconds()) < 60


def test_ping_does_not_change_last_activity_at_byte_for_byte(client, sqlite_conn):
    deal_id, _ = _make_overdue_deal(sqlite_conn, "last_activity_at invariant")

    before_db = _deal_row(sqlite_conn, deal_id)["last_activity_at"]
    before_api = _get_deal(client, deal_id)["last_activity_at"]
    assert before_db == before_api

    response = client.post(f"/api/deals/{deal_id}/ping")
    assert response.status_code == 200, response.text

    after_db = _deal_row(sqlite_conn, deal_id)["last_activity_at"]
    after_api = _get_deal(client, deal_id)["last_activity_at"]

    assert after_db == before_db, "ping must not change last_activity_at in the DB"
    assert after_api == before_api, (
        "ping must not change last_activity_at in the API response"
    )


def test_ping_removes_deal_from_block_under_default_hidden_days(client, sqlite_conn):
    deal_id, _ = _make_overdue_deal(sqlite_conn, "Leaves the block after a ping")

    assert deal_id in _ids_in(_get_pings(client))

    response = client.post(f"/api/deals/{deal_id}/ping")
    assert response.status_code == 200, response.text

    assert deal_id not in _ids_in(_get_pings(client))


def test_ping_allowed_for_deal_not_currently_overdue(client, sqlite_conn):
    """Design decision 11: pinging any existing item is allowed."""
    stage = _first_non_terminal_stage(sqlite_conn)
    now = _utc_now_iso()
    deal_id = _insert_deal(
        sqlite_conn,
        "Fresh item, not overdue",
        stage["id"],
        stage_entered_at=now,
        last_activity_at=now,
    )
    assert deal_id not in _ids_in(_get_pings(client))

    response = client.post(f"/api/deals/{deal_id}/ping")
    assert response.status_code == 200, response.text
    rows = _ping_rows(sqlite_conn, deal_id)
    assert len(rows) == 1
    assert rows[0]["escalation_step"] == 1


def test_ping_nonexistent_deal_returns_404_and_deal_pings_stays_empty(
    client, sqlite_conn
):
    response = client.post("/api/deals/999999/ping")
    assert response.status_code == 404

    count = sqlite_conn.execute("SELECT COUNT(*) c FROM deal_pings").fetchone()["c"]
    assert count == 0


# ===========================================================================
# Escalation ladder at M=0 (checklist 4)
# ===========================================================================


def test_ping_ladder_hidden_days_zero_first_ping_visible_at_step_2(client, sqlite_conn):
    deal_id, _ = _make_overdue_deal(sqlite_conn, "Ladder M=0, first ping")
    _set_ping_hidden_days(sqlite_conn, 0)

    response = client.post(f"/api/deals/{deal_id}/ping")
    assert response.status_code == 200, response.text

    body = _get_pings(client)
    item = _item_for(body, deal_id)
    assert item["escalation_step"] == 2
    assert item["escalate"] is False

    rows = _ping_rows(sqlite_conn, deal_id)
    assert rows[-1]["escalation_step"] == 1, (
        "the first ping record is written with the step that was BEFORE this "
        "ping (0 previous pings => escalation_step(0) = 1)"
    )


def test_ping_ladder_hidden_days_zero_second_ping_step_3_escalate_true(
    client, sqlite_conn
):
    deal_id, _ = _make_overdue_deal(sqlite_conn, "Ladder M=0, second ping")
    _set_ping_hidden_days(sqlite_conn, 0)

    assert client.post(f"/api/deals/{deal_id}/ping").status_code == 200
    response = client.post(f"/api/deals/{deal_id}/ping")
    assert response.status_code == 200, response.text

    body = _get_pings(client)
    item = _item_for(body, deal_id)
    assert item["escalation_step"] == 3
    assert item["escalate"] is True


def test_ping_ladder_hidden_days_zero_third_ping_recorded_step_3_stays_in_block(
    client, sqlite_conn
):
    deal_id, _ = _make_overdue_deal(sqlite_conn, "Ladder M=0, third ping")
    _set_ping_hidden_days(sqlite_conn, 0)

    assert client.post(f"/api/deals/{deal_id}/ping").status_code == 200
    assert client.post(f"/api/deals/{deal_id}/ping").status_code == 200
    response = client.post(f"/api/deals/{deal_id}/ping")
    assert response.status_code == 200, response.text

    rows = _ping_rows(sqlite_conn, deal_id)
    assert len(rows) == 3
    assert rows[-1]["escalation_step"] == 3

    body = _get_pings(client)
    item = _item_for(body, deal_id)
    assert item["escalation_step"] == 3, "the step does not grow above 3"
    assert item["escalate"] is True
    assert deal_id in _ids_in(body), "an item at step 3 is not hidden"


# ===========================================================================
# Step auto-reset and removal from the block on real activity (checklist 5)
# ===========================================================================


def test_auto_reset_by_creating_note_then_restored_with_step_reset_to_1(
    client, sqlite_conn
):
    stage = _first_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage["id"], 1)
    activity_ts = _iso_n_workdays_ago(5)
    deal_id = _insert_deal(
        sqlite_conn,
        "Auto-reset by a note",
        stage["id"],
        stage_entered_at=activity_ts,
        last_activity_at=activity_ts,
    )
    # Ping 2 business days ago (default window M=2 has elapsed) => step 2 in the block.
    _insert_ping(sqlite_conn, deal_id, _iso_n_workdays_ago(2), escalation_step=2)

    body = _get_pings(client)
    item = _item_for(body, deal_id)
    assert item["escalation_step"] == 2

    create_resp = client.post(
        "/api/notes", data={"body": "Status updated", "deal_id": str(deal_id)}
    )
    assert create_resp.status_code == 201, create_resp.text

    body_after = _get_pings(client)
    assert deal_id not in _ids_in(body_after), (
        "creating an attached note should immediately remove the item from the block"
    )

    # Return the item to the block: backdate BOTH activity fields past the
    # threshold, and shift the pinged_at of existing pings EVEN EARLIER than the
    # new last_activity_at (otherwise an old ping would be "after activity" and
    # the step would not reset).
    new_activity_ts = _iso_n_workdays_ago(5)
    sqlite_conn.execute(
        "UPDATE deals SET last_activity_at = ?, stage_entered_at = ? WHERE id = ?",
        (new_activity_ts, new_activity_ts, deal_id),
    )
    sqlite_conn.execute(
        "UPDATE deal_pings SET pinged_at = ? WHERE deal_id = ?",
        ("2000-01-01T00:00:00", deal_id),
    )
    sqlite_conn.commit()

    body_restored = _get_pings(client)
    restored_item = _item_for(body_restored, deal_id)
    assert restored_item["escalation_step"] == 1, (
        "pings before the new activity must not count toward pings_since"
    )


def test_auto_reset_by_attaching_existing_note_via_patch(client, sqlite_conn):
    deal_id, _ = _make_overdue_deal(sqlite_conn, "Auto-reset via attach_note")
    assert deal_id in _ids_in(_get_pings(client))

    note_resp = client.post("/api/notes", data={"body": "Note in inbox"})
    assert note_resp.status_code == 201, note_resp.text
    note_id = note_resp.json()["id"]

    patch_resp = client.patch(f"/api/notes/{note_id}", json={"deal_id": deal_id})
    assert patch_resp.status_code == 200, patch_resp.text

    assert deal_id not in _ids_in(_get_pings(client))


def test_auto_reset_by_bulk_attach_note(client, sqlite_conn):
    deal_id, _ = _make_overdue_deal(sqlite_conn, "Auto-reset via bulk_attach")
    assert deal_id in _ids_in(_get_pings(client))

    note_resp = client.post("/api/notes", data={"body": "Note for bulk-attach"})
    assert note_resp.status_code == 201, note_resp.text
    note_id = note_resp.json()["id"]

    bulk_resp = client.post(
        "/api/notes/bulk-attach", json={"note_ids": [note_id], "deal_id": deal_id}
    )
    assert bulk_resp.status_code == 200, bulk_resp.text

    assert deal_id not in _ids_in(_get_pings(client))


def test_auto_reset_by_moving_deal_to_another_stage(client, sqlite_conn):
    deal_id, _ = _make_overdue_deal(sqlite_conn, "Auto-reset by moving stage")
    target_stage = _second_non_terminal_stage(sqlite_conn)
    assert deal_id in _ids_in(_get_pings(client))

    move_resp = client.post(
        f"/api/deals/{deal_id}/move", json={"stage_id": target_stage["id"]}
    )
    assert move_resp.status_code == 200, move_resp.text

    assert deal_id not in _ids_in(_get_pings(client))


def test_auto_reset_by_patching_deal_field(client, sqlite_conn):
    deal_id, _ = _make_overdue_deal(sqlite_conn, "Auto-reset via item PATCH")
    assert deal_id in _ids_in(_get_pings(client))

    patch_resp = client.patch(f"/api/deals/{deal_id}", json={"waiting_on": "Maria"})
    assert patch_resp.status_code == 200, patch_resp.text

    assert deal_id not in _ids_in(_get_pings(client))


# ===========================================================================
# POST /api/deals/{id}/snooze — snooze (checklist 6)
# ===========================================================================


def test_snooze_tomorrow_sets_column_hides_deal_activity_unchanged(client, sqlite_conn):
    deal_id, _ = _make_overdue_deal(sqlite_conn, "Snooze until tomorrow")
    before = _deal_row(sqlite_conn, deal_id)["last_activity_at"]
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    response = client.post(f"/api/deals/{deal_id}/snooze", json={"until": tomorrow})
    assert response.status_code == 200, response.text

    row = _deal_row(sqlite_conn, deal_id)
    assert row["snoozed_until"] == tomorrow
    assert row["last_activity_at"] == before

    assert deal_id not in _ids_in(_get_pings(client))


def test_snooze_until_today_returns_422(client, sqlite_conn):
    deal_id, _ = _make_overdue_deal(sqlite_conn, "Snooze until today — invalid")
    today = date.today().isoformat()

    response = client.post(f"/api/deals/{deal_id}/snooze", json={"until": today})
    assert response.status_code == 422

    row = _deal_row(sqlite_conn, deal_id)
    assert row["snoozed_until"] is None


def test_snooze_until_yesterday_returns_422(client, sqlite_conn):
    deal_id, _ = _make_overdue_deal(sqlite_conn, "Snooze into the past — invalid")
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    response = client.post(f"/api/deals/{deal_id}/snooze", json={"until": yesterday})
    assert response.status_code == 422

    row = _deal_row(sqlite_conn, deal_id)
    assert row["snoozed_until"] is None


def test_snooze_garbage_date_returns_422(client, sqlite_conn):
    for garbage in ("2026-13-45", "tomorrow", "not-a-date", "2026/07/08", ""):
        deal_id, _ = _make_overdue_deal(sqlite_conn, f"Snooze garbage {garbage!r}")
        response = client.post(f"/api/deals/{deal_id}/snooze", json={"until": garbage})
        assert response.status_code == 422, (
            f"until={garbage!r} should be rejected with 422, got "
            f"{response.status_code}: {response.text}"
        )


def test_snooze_null_clears_snooze_and_deal_returns_to_block(client, sqlite_conn):
    deal_id, _ = _make_overdue_deal(sqlite_conn, "Snooze cleared by null")
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    first = client.post(f"/api/deals/{deal_id}/snooze", json={"until": tomorrow})
    assert first.status_code == 200, first.text
    assert deal_id not in _ids_in(_get_pings(client))

    second = client.post(f"/api/deals/{deal_id}/snooze", json={"until": None})
    assert second.status_code == 200, second.text

    row = _deal_row(sqlite_conn, deal_id)
    assert row["snoozed_until"] is None

    assert deal_id in _ids_in(_get_pings(client))


def test_snooze_nonexistent_deal_returns_404(client):
    response = client.post("/api/deals/999999/snooze", json={"until": "2099-01-01"})
    assert response.status_code == 404


# ===========================================================================
# GET /api/deals/{id} — the pings field in the card feed
# ===========================================================================


def test_get_deal_includes_pings_field_in_chronological_order(client, sqlite_conn):
    deal_id, _ = _make_overdue_deal(sqlite_conn, "Feed with pings")
    older = "2026-01-02T10:00:00"
    newer = "2026-01-05T10:00:00"
    # Insert the later-dated ping FIRST in the DB, to make sure the API sorts by
    # pinged_at, not by insertion order/id.
    _insert_ping(
        sqlite_conn, deal_id, newer, escalation_step=2, ping_text="Repeat ping"
    )
    _insert_ping(
        sqlite_conn, deal_id, older, escalation_step=1, ping_text="First ping"
    )

    body = _get_deal(client, deal_id)
    assert "pings" in body
    pings = body["pings"]
    assert len(pings) == 2
    assert [p["pinged_at"] for p in pings] == [older, newer]
    assert [p["escalation_step"] for p in pings] == [1, 2]
    assert [p["ping_text"] for p in pings] == ["First ping", "Repeat ping"]
    for p in pings:
        assert set(p.keys()) >= {"id", "pinged_at", "escalation_step", "ping_text"}

    # The notes shape must not change (regression against the existing Step 1
    # T2/T3 tests).
    assert body["notes"] == []


def test_get_deal_pings_field_empty_list_when_no_pings(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "No pings", stage["id"])

    body = _get_deal(client, deal_id)
    assert body["pings"] == []


# ===========================================================================
# GET /api/board — the snoozed_until field on the card (T7)
# ===========================================================================


def test_board_card_shows_snoozed_until_when_active_tomorrow(client, sqlite_conn):
    deal_id, _ = _make_overdue_deal(sqlite_conn, "Snooze badge for tomorrow")
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    snooze_resp = client.post(f"/api/deals/{deal_id}/snooze", json={"until": tomorrow})
    assert snooze_resp.status_code == 200, snooze_resp.text

    board = client.get("/api/board")
    assert board.status_code == 200
    card = _find_card(board.json(), deal_id)
    assert card["snoozed_until"] == tomorrow


def test_board_card_snoozed_until_null_when_no_snooze(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "No snooze", stage["id"])

    board = client.get("/api/board")
    card = _find_card(board.json(), deal_id)
    assert card["snoozed_until"] is None


def test_board_card_snoozed_until_null_when_snooze_is_today(client, sqlite_conn):
    """Spec: on the board a snooze is "active" only strictly LATER than today
    (> today_local).

    A value = today is NO LONGER active for the board (unlike /api/pings, where
    on that day the item already returns to the block via the membership rule
    ``today_local >= snoozed_until``).
    """
    stage = _first_non_terminal_stage(sqlite_conn)
    today_str = date.today().isoformat()
    deal_id = _insert_deal(
        sqlite_conn,
        "Snooze today — not active on the board",
        stage["id"],
        snoozed_until=today_str,
    )

    board = client.get("/api/board")
    card = _find_card(board.json(), deal_id)
    assert card["snoozed_until"] is None


def test_board_card_snoozed_until_null_when_snooze_expired_yesterday(
    client, sqlite_conn
):
    stage = _first_non_terminal_stage(sqlite_conn)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    deal_id = _insert_deal(
        sqlite_conn, "Snooze expired", stage["id"], snoozed_until=yesterday
    )

    board = client.get("/api/board")
    card = _find_card(board.json(), deal_id)
    assert card["snoozed_until"] is None


def test_board_composition_unchanged_after_ping_and_snooze(client, sqlite_conn):
    """F4: hung/pinged/snoozed items do not disappear from the board."""
    deal_a, _ = _make_overdue_deal(sqlite_conn, "Composition A")
    deal_b, _ = _make_overdue_deal(sqlite_conn, "Composition B")

    before = client.get("/api/board")
    assert before.status_code == 200
    ids_before = _all_card_ids(before.json())
    assert {deal_a, deal_b} <= ids_before

    ping_resp = client.post(f"/api/deals/{deal_a}/ping")
    assert ping_resp.status_code == 200, ping_resp.text
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    snooze_resp = client.post(f"/api/deals/{deal_b}/snooze", json={"until": tomorrow})
    assert snooze_resp.status_code == 200, snooze_resp.text

    # Both items should leave the "Ping Today" block…
    pings_body = _get_pings(client)
    assert deal_a not in _ids_in(pings_body)
    assert deal_b not in _ids_in(pings_body)

    # …but the board's card composition does not change — nothing is hidden.
    after = client.get("/api/board")
    assert after.status_code == 200
    ids_after = _all_card_ids(after.json())
    assert ids_after == ids_before
