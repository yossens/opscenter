"""T3 tests: GET /api/pings — computing the "Ping Today" block.

Acceptance criteria come from docs/specs/002-step2-hang-detector.md, task T3
(and the related sections "Terms and calculation rules", "Design decisions",
API/"GET /api/pings", "Risks and edge cases"). The tests are written against the
spec, not the implementation: at writing time ``app/repo/pings.py`` and
``app/routers/pings.py`` do not exist yet, the router is not registered in
``app/main.py`` — ``GET /api/pings`` currently returns 404 (the correct TDD
state: the file collects, tests fail on 404 rather than on an import error).

Only fixtures from ``tests/conftest.py`` are used: ``client``, ``sqlite_conn``,
``project_root``. Helper code (inserting stages/items/notes/pings directly into
the DB, picking dates "N business days ago") lives locally in this file,
following the already-accepted ``tests/test_deals.py``/``tests/test_stages.py``
(T5/T6 of Step 1).

Oracles: ``app.workdays.workdays_since`` (already accepted in T2 of Step 1) and
``app.ping.render_ping``/``prepare_last_note``/``DEFAULT_PING_TEMPLATE``/
``DEFAULT_PING_HIDDEN_DAYS`` (already accepted in T2 of this spec) are used only
to *build* the test's expected inputs/reference strings (the same way
``workdays_since`` is used as an oracle in ``tests/test_deals.py``), not as a
re-invocation of the logic under test.

Fixture rule (spec section "Terms"): any backdating of an item's activity in
this file shifts BACK both ``last_activity_at`` AND ``stage_entered_at`` (by the
same value — the simplest way to preserve the invariant
``stage_entered_at <= last_activity_at``, which always holds in real Step 1 data).

Assumptions about the JSON response shape that the spec fixes literally (API
section): ``{"count": N, "items": [{"deal_id", "title", "company", "stage_id",
"stage_name", "days_since_activity", "threshold_days", "overdue_by",
"waiting_on", "last_activity_at", "escalation_step", "escalate",
"ping_text"}]}``. These field names are used literally in the tests (not via
fallback key variants, as in the Step 1 board tests) — the spec gives the exact
JSON example specifically for this endpoint.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

from helpers import (
    _first_non_terminal_stage,
    _insert_deal,
    _second_non_terminal_stage,
    _stages_by_position,
    _terminal_stage,
)

# ---------------------------------------------------------------------------
# Helper functions: stages, items, notes, pings — direct DB work.
# ---------------------------------------------------------------------------


def _set_threshold(sqlite_conn, stage_id: int, threshold_days: int) -> None:
    sqlite_conn.execute(
        "UPDATE stages SET threshold_days = ? WHERE id = ?", (threshold_days, stage_id)
    )
    sqlite_conn.commit()


def _set_track_hangs(sqlite_conn, stage_id: int, value: int) -> None:
    sqlite_conn.execute(
        "UPDATE stages SET track_hangs = ? WHERE id = ?", (value, stage_id)
    )
    sqlite_conn.commit()


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
    """A UTC ISO string corresponding to 10:00 local time on the date ``d``.

    The same trick as in ``tests/test_deals.py``/``tests/test_workdays.py``:
    10:00 (not midnight) avoids "jumping" the calendar day when converting to
    UTC for reasonable laptop local-TZ offsets.
    """
    local_dt = datetime(d.year, d.month, d.day, 10, 0, 0)
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _iso_n_workdays_ago(n: int) -> str:
    """A moment in time (UTC ISO) such that ``workdays_since(ts, today) == n``.

    A single generator of fixture dates for both "activity N business days ago"
    and "ping N business days ago" — the business-day count in both cases goes
    through the same ``app.workdays.workdays_since`` (F5), so both scenarios are
    built by one function. Uses the already independently tested (T2 of Step 1)
    pure module ``app.workdays.workdays_since`` as an oracle to build the test's
    input data — not to verify the assertion itself.
    """
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


def _get_pings(client) -> dict:
    response = client.get("/api/pings")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == len(body["items"])
    return body


def _item_for(body: dict, deal_id: int) -> dict:
    matches = [i for i in body["items"] if i["deal_id"] == deal_id]
    assert matches, f"item {deal_id} not found in items: {body['items']}"
    return matches[0]


def _ids_in(body: dict) -> list[int]:
    return [i["deal_id"] for i in body["items"]]


_CARD_LIST_KEYS = ("cards", "deals", "items")


def _board_cards_of(column: dict) -> list:
    for key in _CARD_LIST_KEYS:
        if key in column:
            return column[key]
    raise AssertionError(f"board column without a card list: {column}")


def _board_column_stage_id(column: dict) -> int:
    for key in ("stage_id", "id"):
        if key in column:
            return column[key]
    raise AssertionError(f"column without 'stage_id'/'id': {column}")


# ===========================================================================
# Empty DB (checklist 8)
# ===========================================================================


def test_pings_zero_stages_zero_deals_returns_200_empty_block(client, sqlite_conn):
    # 0 items initially in a fresh DB; remove the stages too (0 stages).
    sqlite_conn.execute("DELETE FROM deal_pings")
    sqlite_conn.execute("DELETE FROM notes")
    sqlite_conn.execute("DELETE FROM deals")
    sqlite_conn.execute("DELETE FROM stages")
    sqlite_conn.commit()
    assert sqlite_conn.execute("SELECT COUNT(*) c FROM stages").fetchone()["c"] == 0
    assert sqlite_conn.execute("SELECT COUNT(*) c FROM deals").fetchone()["c"] == 0

    response = client.get("/api/pings")
    assert response.status_code == 200
    assert response.json() == {"count": 0, "items": []}


def test_pings_zero_stages_zero_deals_index_and_board_pages_still_200(
    client, sqlite_conn
):
    sqlite_conn.execute("DELETE FROM deal_pings")
    sqlite_conn.execute("DELETE FROM notes")
    sqlite_conn.execute("DELETE FROM deals")
    sqlite_conn.execute("DELETE FROM stages")
    sqlite_conn.commit()

    assert client.get("/").status_code == 200
    assert client.get("/board").status_code == 200


def test_pings_no_deals_at_all_returns_empty_block_on_seeded_stages(client):
    """A fresh DB with the Step 1 stage seed but not a single item."""
    body = _get_pings(client)
    assert body == {"count": 0, "items": []}


# ===========================================================================
# Membership: threshold, strict "greater than", N business days (checklist 1)
# ===========================================================================


def test_membership_backdated_two_workdays_past_threshold_one_appears(
    client, sqlite_conn
):
    stage = _first_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage["id"], 1)
    ts = _iso_n_workdays_ago(2)
    deal_id = _insert_deal(
        sqlite_conn,
        "Overdue by 2 business days",
        stage["id"],
        stage_entered_at=ts,
        last_activity_at=ts,
    )

    body = _get_pings(client)
    item = _item_for(body, deal_id)

    assert item["days_since_activity"] == 2
    assert item["threshold_days"] == 1
    assert item["overdue_by"] == 1
    assert item["stage_id"] == stage["id"]
    assert item["stage_name"] == stage["name"]
    assert item["last_activity_at"] == ts
    assert isinstance(item["escalation_step"], int)
    assert isinstance(item["escalate"], bool)


def test_membership_days_equal_threshold_excluded_strictly_greater_required(
    client, sqlite_conn
):
    stage = _second_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage["id"], 2)
    ts = _iso_n_workdays_ago(2)
    deal_id = _insert_deal(
        sqlite_conn,
        "Exactly at the threshold",
        stage["id"],
        stage_entered_at=ts,
        last_activity_at=ts,
    )

    body = _get_pings(client)
    assert deal_id not in _ids_in(body)


def test_membership_below_threshold_excluded(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage["id"], 5)
    ts = _iso_n_workdays_ago(1)
    deal_id = _insert_deal(
        sqlite_conn,
        "Fresh activity",
        stage["id"],
        stage_entered_at=ts,
        last_activity_at=ts,
    )

    body = _get_pings(client)
    assert deal_id not in _ids_in(body)


# ===========================================================================
# Terminal stages and track_hangs=0 (checklist 7, F4)
# ===========================================================================


def test_terminal_stage_deal_excluded_regardless_of_age(client, sqlite_conn):
    terminal = _terminal_stage(sqlite_conn)
    ancient = "2020-01-01T00:00:00"
    deal_id = _insert_deal(
        sqlite_conn,
        "Item closed a year ago",
        terminal["id"],
        stage_entered_at=ancient,
        last_activity_at=ancient,
        closed_at=ancient,
    )

    body = _get_pings(client)
    assert deal_id not in _ids_in(body)


def test_track_hangs_zero_excludes_overdue_deal(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage["id"], 1)
    _set_track_hangs(sqlite_conn, stage["id"], 0)
    ts = _iso_n_workdays_ago(5)
    deal_id = _insert_deal(
        sqlite_conn,
        "Stage without tracking",
        stage["id"],
        stage_entered_at=ts,
        last_activity_at=ts,
    )

    body = _get_pings(client)
    assert deal_id not in _ids_in(body)


def test_track_hangs_one_non_terminal_stage_included_when_overdue(client, sqlite_conn):
    """Positive control for the previous test: track_hangs=1 (default) — in the block."""
    stage = _first_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage["id"], 1)
    assert stage["track_hangs"] == 1
    ts = _iso_n_workdays_ago(5)
    deal_id = _insert_deal(
        sqlite_conn,
        "Stage with tracking",
        stage["id"],
        stage_entered_at=ts,
        last_activity_at=ts,
    )

    body = _get_pings(client)
    assert deal_id in _ids_in(body)


# ===========================================================================
# Snooze (checklist 6)
# ===========================================================================


def test_snooze_tomorrow_excludes_deal(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage["id"], 1)
    ts = _iso_n_workdays_ago(3)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    deal_id = _insert_deal(
        sqlite_conn,
        "Snoozed until tomorrow",
        stage["id"],
        stage_entered_at=ts,
        last_activity_at=ts,
        snoozed_until=tomorrow,
    )

    body = _get_pings(client)
    assert deal_id not in _ids_in(body)


def test_snooze_today_includes_deal(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage["id"], 1)
    ts = _iso_n_workdays_ago(3)
    today_str = date.today().isoformat()
    deal_id = _insert_deal(
        sqlite_conn,
        "Snooze returns today",
        stage["id"],
        stage_entered_at=ts,
        last_activity_at=ts,
        snoozed_until=today_str,
    )

    body = _get_pings(client)
    assert deal_id in _ids_in(body)


def test_snooze_yesterday_includes_deal_as_inert_leftover(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage["id"], 1)
    ts = _iso_n_workdays_ago(3)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    deal_id = _insert_deal(
        sqlite_conn,
        "Snooze in the past — leftover in the column",
        stage["id"],
        stage_entered_at=ts,
        last_activity_at=ts,
        snoozed_until=yesterday,
    )

    body = _get_pings(client)
    assert deal_id in _ids_in(body)


# ===========================================================================
# Hide window after a ping / escalation ladder (F3, checklist 4)
# ===========================================================================


def test_ping_just_now_hidden_under_default_hidden_days(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage["id"], 1)
    activity_ts = _iso_n_workdays_ago(5)
    deal_id = _insert_deal(
        sqlite_conn,
        "Just pinged",
        stage["id"],
        stage_entered_at=activity_ts,
        last_activity_at=activity_ts,
    )
    _insert_ping(sqlite_conn, deal_id, _utc_now_iso(), escalation_step=1)

    body = _get_pings(client)
    assert deal_id not in _ids_in(body)


def test_ping_two_workdays_ago_visible_with_escalation_step_2(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage["id"], 1)
    activity_ts = _iso_n_workdays_ago(5)
    deal_id = _insert_deal(
        sqlite_conn,
        "Pinged 2 business days ago",
        stage["id"],
        stage_entered_at=activity_ts,
        last_activity_at=activity_ts,
    )
    ping_ts = _iso_n_workdays_ago(2)
    _insert_ping(sqlite_conn, deal_id, ping_ts, escalation_step=2)

    body = _get_pings(client)
    item = _item_for(body, deal_id)
    assert item["escalation_step"] == 2
    assert item["escalate"] is False


def test_two_pings_after_activity_elapsed_window_step_3_escalate_true(
    client, sqlite_conn
):
    stage = _first_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage["id"], 1)
    activity_ts = _iso_n_workdays_ago(6)
    deal_id = _insert_deal(
        sqlite_conn,
        "Two pings, window elapsed",
        stage["id"],
        stage_entered_at=activity_ts,
        last_activity_at=activity_ts,
    )
    _insert_ping(sqlite_conn, deal_id, _iso_n_workdays_ago(3), escalation_step=2)
    _insert_ping(sqlite_conn, deal_id, _iso_n_workdays_ago(2), escalation_step=3)

    body = _get_pings(client)
    item = _item_for(body, deal_id)
    assert item["escalation_step"] == 3
    assert item["escalate"] is True


def test_three_pings_after_activity_still_in_block_step_3_no_further_hiding(
    client, sqlite_conn
):
    stage = _first_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage["id"], 1)
    activity_ts = _iso_n_workdays_ago(8)
    deal_id = _insert_deal(
        sqlite_conn,
        "Three pings — step does not grow, does not hide",
        stage["id"],
        stage_entered_at=activity_ts,
        last_activity_at=activity_ts,
    )
    _insert_ping(sqlite_conn, deal_id, _iso_n_workdays_ago(4), escalation_step=2)
    _insert_ping(sqlite_conn, deal_id, _iso_n_workdays_ago(2), escalation_step=3)
    # Third ping — brand new; at pings_since>=3 the window does not apply at all.
    _insert_ping(sqlite_conn, deal_id, _utc_now_iso(), escalation_step=3)

    body = _get_pings(client)
    item = _item_for(body, deal_id)
    assert item["escalation_step"] == 3
    assert item["escalate"] is True


# ===========================================================================
# Sorting (overdue_by desc, tie-break by days_since_activity desc)
# ===========================================================================


def test_sorting_by_overdue_by_descending(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage["id"], 1)
    deal_low = _insert_deal(
        sqlite_conn,
        "overdue_by=2",
        stage["id"],
        stage_entered_at=_iso_n_workdays_ago(3),
        last_activity_at=_iso_n_workdays_ago(3),
    )
    deal_mid = _insert_deal(
        sqlite_conn,
        "overdue_by=3",
        stage["id"],
        stage_entered_at=_iso_n_workdays_ago(4),
        last_activity_at=_iso_n_workdays_ago(4),
    )
    deal_high = _insert_deal(
        sqlite_conn,
        "overdue_by=4",
        stage["id"],
        stage_entered_at=_iso_n_workdays_ago(5),
        last_activity_at=_iso_n_workdays_ago(5),
    )

    body = _get_pings(client)
    ids = _ids_in(body)
    ours = [d for d in ids if d in (deal_low, deal_mid, deal_high)]
    assert ours == [deal_high, deal_mid, deal_low]


def test_sorting_tie_on_overdue_by_breaks_by_days_since_activity_descending(
    client, sqlite_conn
):
    stage_a = _first_non_terminal_stage(sqlite_conn)
    stage_b = _second_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage_a["id"], 1)
    _set_threshold(sqlite_conn, stage_b["id"], 3)

    # Both overdue_by = 2, but deal_more_days has a larger days_since_activity.
    deal_fewer_days = _insert_deal(
        sqlite_conn,
        "days=3, threshold=1, overdue_by=2",
        stage_a["id"],
        stage_entered_at=_iso_n_workdays_ago(3),
        last_activity_at=_iso_n_workdays_ago(3),
    )
    deal_more_days = _insert_deal(
        sqlite_conn,
        "days=5, threshold=3, overdue_by=2",
        stage_b["id"],
        stage_entered_at=_iso_n_workdays_ago(5),
        last_activity_at=_iso_n_workdays_ago(5),
    )

    body = _get_pings(client)
    item_fewer = _item_for(body, deal_fewer_days)
    item_more = _item_for(body, deal_more_days)
    assert item_fewer["overdue_by"] == item_more["overdue_by"] == 2

    ids = _ids_in(body)
    assert ids.index(deal_more_days) < ids.index(deal_fewer_days)


# ===========================================================================
# ping_text: template render, {counterparty} fallback, {last_note} trimmed
# ===========================================================================


def test_ping_text_uses_company_as_counterparty_when_present(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage["id"], 1)
    ts = _iso_n_workdays_ago(3)
    deal_id = _insert_deal(
        sqlite_conn,
        "Item title, must not appear in the text",
        stage["id"],
        stage_entered_at=ts,
        last_activity_at=ts,
        company="Daisy Counterparty LLC",
    )

    body = _get_pings(client)
    item = _item_for(body, deal_id)
    assert "Daisy Counterparty LLC" in item["ping_text"]


def test_ping_text_falls_back_to_title_when_company_empty(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage["id"], 1)
    ts = _iso_n_workdays_ago(3)
    deal_id = _insert_deal(
        sqlite_conn,
        "UniqueTitleWithoutCompany42",
        stage["id"],
        stage_entered_at=ts,
        last_activity_at=ts,
        company=None,
    )

    body = _get_pings(client)
    item = _item_for(body, deal_id)
    assert "UniqueTitleWithoutCompany42" in item["ping_text"]


def test_ping_text_last_note_is_latest_by_created_at_trimmed_nonempty(
    client, sqlite_conn
):
    stage = _first_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage["id"], 1)
    ts = _iso_n_workdays_ago(3)
    deal_id = _insert_deal(
        sqlite_conn,
        "Item with notes",
        stage["id"],
        stage_entered_at=ts,
        last_activity_at=ts,
    )
    _insert_attached_note(
        sqlite_conn, deal_id, "First status OK", "2026-01-01T00:00:00"
    )
    # A note that is later by created_at — only whitespace/newlines, NOT counted
    # as a status (design decision 12): ping_text should contain the text of the
    # previous trimmed-nonempty note.
    _insert_attached_note(sqlite_conn, deal_id, "   \n \t ", "2026-01-02T00:00:00")

    body = _get_pings(client)
    item = _item_for(body, deal_id)
    assert "First status OK" in item["ping_text"]


def test_ping_text_no_notes_and_no_waiting_on_has_no_dangling_artifacts(
    client, sqlite_conn
):
    stage = _first_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage["id"], 1)
    ts = _iso_n_workdays_ago(3)
    deal_id = _insert_deal(
        sqlite_conn,
        "No notes and no waiting_on",
        stage["id"],
        stage_entered_at=ts,
        last_activity_at=ts,
        waiting_on=None,
    )

    body = _get_pings(client)
    item = _item_for(body, deal_id)
    text = item["ping_text"]
    assert ": ." not in text
    assert ", ," not in text
    assert "  " not in text
    assert "{" not in text and "}" not in text
    assert not text.lstrip().startswith(",")


def test_ping_text_after_deleting_settings_keys_returns_200_with_defaults(
    client, sqlite_conn
):
    from app.ping import DEFAULT_PING_TEMPLATE, prepare_last_note, render_ping

    stage = _first_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage["id"], 1)
    ts = _iso_n_workdays_ago(2)
    deal_id = _insert_deal(
        sqlite_conn,
        "After deleting the settings",
        stage["id"],
        stage_entered_at=ts,
        last_activity_at=ts,
        company="Daisy Settings",
        waiting_on="Ivan",
    )
    _insert_attached_note(sqlite_conn, deal_id, "Status OK", "2026-01-01T00:00:00")

    sqlite_conn.execute(
        "DELETE FROM app_meta WHERE key IN ('ping_template', 'ping_hidden_days')"
    )
    sqlite_conn.commit()

    response = client.get("/api/pings")
    assert response.status_code == 200
    body = response.json()
    item = _item_for(body, deal_id)

    expected = render_ping(
        DEFAULT_PING_TEMPLATE,
        {
            "waiting_for": "Ivan",
            "counterparty": "Daisy Settings",
            "stage": stage["name"],
            "days": "2",
            "last_note": prepare_last_note("Status OK"),
        },
    )
    assert item["ping_text"] == expected


def test_ping_hidden_days_default_of_2_applies_after_deleting_setting_key(
    client, sqlite_conn
):
    stage = _first_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage["id"], 1)
    ts = _iso_n_workdays_ago(5)
    deal_id = _insert_deal(
        sqlite_conn,
        "Default M after deleting the key",
        stage["id"],
        stage_entered_at=ts,
        last_activity_at=ts,
    )
    _insert_ping(sqlite_conn, deal_id, _utc_now_iso(), escalation_step=1)

    sqlite_conn.execute("DELETE FROM app_meta WHERE key = 'ping_hidden_days'")
    sqlite_conn.commit()

    body = _get_pings(client)
    assert deal_id not in _ids_in(body), (
        "without the ping_hidden_days key the default M=2 "
        "(DEFAULT_PING_HIDDEN_DAYS) should apply, hiding the freshly pinged item"
    )


# ===========================================================================
# Cross-check of the duplicated default (design decision 8)
# ===========================================================================


def test_default_ping_template_constant_matches_seeded_app_meta_value(sqlite_conn):
    from app.ping import DEFAULT_PING_TEMPLATE

    row = sqlite_conn.execute(
        "SELECT value FROM app_meta WHERE key = 'ping_template'"
    ).fetchone()
    assert row is not None
    assert row["value"] == DEFAULT_PING_TEMPLATE


def test_default_ping_hidden_days_constant_matches_seeded_app_meta_value(sqlite_conn):
    from app.ping import DEFAULT_PING_HIDDEN_DAYS

    row = sqlite_conn.execute(
        "SELECT value FROM app_meta WHERE key = 'ping_hidden_days'"
    ).fetchone()
    assert row is not None
    assert row["value"] == str(DEFAULT_PING_HIDDEN_DAYS)


# ===========================================================================
# F5 consistency: items from /api/pings are always overdue on the board
# ===========================================================================


def test_block_items_are_always_overdue_and_consistent_with_board_aging(
    client, sqlite_conn
):
    stage_a = _first_non_terminal_stage(sqlite_conn)
    stage_b = _second_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage_a["id"], 1)
    _set_threshold(sqlite_conn, stage_b["id"], 2)

    deal_1 = _insert_deal(
        sqlite_conn,
        "F5-1",
        stage_a["id"],
        stage_entered_at=_iso_n_workdays_ago(3),
        last_activity_at=_iso_n_workdays_ago(3),
    )
    deal_2 = _insert_deal(
        sqlite_conn,
        "F5-2",
        stage_a["id"],
        stage_entered_at=_iso_n_workdays_ago(4),
        last_activity_at=_iso_n_workdays_ago(4),
    )
    deal_3 = _insert_deal(
        sqlite_conn,
        "F5-3",
        stage_b["id"],
        stage_entered_at=_iso_n_workdays_ago(5),
        last_activity_at=_iso_n_workdays_ago(5),
    )

    pings_body = _get_pings(client)
    board_response = client.get("/api/board")
    assert board_response.status_code == 200
    columns = board_response.json()

    for deal_id in (deal_1, deal_2, deal_3):
        item = _item_for(pings_body, deal_id)
        column = next(
            c for c in columns if _board_column_stage_id(c) == item["stage_id"]
        )
        card = next(c for c in _board_cards_of(column) if c["id"] == deal_id)
        assert card["days_in_stage"] >= item["days_since_activity"]
        assert card["aging_level"] == "overdue"


# ===========================================================================
# Diff review: app/repo/pings.py does not implement its own business-day math.
# ===========================================================================


def test_repo_pings_module_exists(project_root):
    path = project_root / "app" / "repo" / "pings.py"
    assert path.exists(), "app/repo/pings.py must exist"


def test_repo_pings_module_has_no_manual_weekday_arithmetic(project_root):
    path = project_root / "app" / "repo" / "pings.py"
    assert path.exists(), "app/repo/pings.py must exist"
    text = path.read_text(encoding="utf-8")

    assert ".weekday(" not in text, (
        "app/repo/pings.py must not count business days manually via "
        "date.weekday() — only through app.workdays (F5)"
    )
    assert "% 7" not in text, (
        "app/repo/pings.py must not reinvent week arithmetic "
        "(a sign of copy-pasting app/workdays.py._count_weekdays)"
    )
    assert "workdays_since" in text, (
        "app/repo/pings.py must use app.workdays.workdays_since "
        "to compute days without activity (F5)"
    )


# ===========================================================================
# Perf: block computation < 100 ms for 200 items (best-of-3 after warmup)
# ===========================================================================


def test_get_pings_under_100ms_for_200_deals(client, sqlite_conn):
    stage_ids = [
        row["id"]
        for row in sqlite_conn.execute(
            "SELECT id FROM stages WHERE is_terminal = 0 ORDER BY position"
        ).fetchall()
    ]
    assert stage_ids, "at least one non-terminal stage from the T1 seed is needed"

    activity_ts = _iso_n_workdays_ago(
        10
    )  # definitely overdue at the default threshold=5

    deal_rows = [
        (
            f"Perf item {i}",
            stage_ids[i % len(stage_ids)],
            activity_ts,
            activity_ts,
            activity_ts,
        )
        for i in range(200)
    ]
    sqlite_conn.executemany(
        """
        INSERT INTO deals (title, stage_id, stage_entered_at, last_activity_at, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        deal_rows,
    )
    sqlite_conn.commit()

    deal_ids = [
        row["id"]
        for row in sqlite_conn.execute("SELECT id FROM deals ORDER BY id").fetchall()
    ]
    assert len(deal_ids) == 200

    note_rows = [
        (f"Item status {i}", "attached", deal_ids[i], activity_ts) for i in range(200)
    ]
    sqlite_conn.executemany(
        "INSERT INTO notes (body, status, deal_id, created_at) VALUES (?, ?, ?, ?)",
        note_rows,
    )

    ping_ts = _iso_n_workdays_ago(3)
    ping_rows = [(deal_ids[i], ping_ts, 1, "") for i in range(50)]
    sqlite_conn.executemany(
        "INSERT INTO deal_pings (deal_id, pinged_at, escalation_step, ping_text) VALUES (?, ?, ?, ?)",
        ping_rows,
    )
    sqlite_conn.commit()

    # Warm up with one request (outside the measurement).
    warmup = client.get("/api/pings")
    assert warmup.status_code == 200
    assert warmup.json()["count"] >= 150, (
        "expected most of the 200 overdue items in the block "
        "(some of the 50 pinged ones may be temporarily hidden by the M window)"
    )

    timings = []
    for _ in range(3):
        start = time.perf_counter()
        response = client.get("/api/pings")
        elapsed = time.perf_counter() - start
        assert response.status_code == 200
        timings.append(elapsed)

    assert min(timings) < 0.1, (
        f"GET /api/pings should fit within 100 ms for 200 items "
        f"(best-of-3), got: {timings}"
    )
