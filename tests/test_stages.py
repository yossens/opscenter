"""T6 tests: stages — create, rename, threshold, reorder.

Acceptance criteria source — docs/specs/001-step1-inbox-pipeline.md, task T6
(and the related "DB schema"/"Business days and card aging" sections, to verify
that changing a stage threshold is reflected in the board's ``aging_level``).
The tests are written against the spec, not the implementation: at the time of
writing ``app/routers/stages.py`` and ``app/repo/stages.py`` did not yet exist
(a correct TDD state — tests collect, but fail).

Only fixtures from ``tests/conftest.py`` are used: ``client``, ``sqlite_conn``.
No new fixtures are added to conftest.py — the helper code (inserting
stages/items directly into the DB, parsing the board response) lives locally in
this file, following the already-accepted ``tests/test_deals.py`` (T5).

Assumptions about the JSON response shapes that the spec does not fix literally
(documented explicitly, as in ``tests/test_deals.py``):

- ``GET /api/stages`` returns a "bare" list of stage objects (no
  ``{"items": [...]}}`` wrapper) — by analogy with ``GET /api/deals``/``GET /api/notes``.
- Each stage object contains at least ``id``, ``name``, ``position``,
  ``threshold_days``, ``is_terminal`` — the same field names as the columns of
  the ``stages`` table in the "DB schema" section.
- ``POST /api/stages`` on success returns 201 and the created stage object
  (with ``id``, updated ``position``, ``threshold_days``); ``PATCH
  /api/stages/{id}`` — 200 and the updated stage object.
- The column shape of ``GET /api/board`` is the same as fixed in
  ``tests/test_deals.py`` (a list of columns, a ``stage_id``/``id`` key, a list
  of cards under one of the keys ``cards``/``deals``/``items``).
- For the criterion "``is_terminal`` cannot be changed via the API (the field is
  ignored, or 422 — fix a single behavior)" the spec itself explicitly allows
  both request outcomes; so the test fixes the invariant the spec requires
  unconditionally ("does not change") and allows either of the two response
  statuses permitted by the criterion text. This is not a weaker check but a
  precise reading of the written criterion (which builds in the variability).

If the implementation chooses a different response shape, these tests point out
exactly where the expectation diverges, instead of just failing with ``KeyError``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from helpers import (
    _deal_row,
    _first_non_terminal_stage,
    _insert_deal,
    _second_non_terminal_stage,
    _stage_row,
    _stages_by_position,
    _terminal_stage,
)

# ---------------------------------------------------------------------------
# Helper functions: direct DB work (the 6-stage T1 seed, inserting items
# bypassing the API where stage_id/dates must be controlled exactly).
# ---------------------------------------------------------------------------


def _stage_ids_by_position(sqlite_conn) -> list[int]:
    return [row["id"] for row in _stages_by_position(sqlite_conn)]


def _entered_iso_at_local_10am(d: date) -> str:
    """A UTC ISO string corresponding to 10:00 local time on date ``d``.

    See the same trick in ``tests/test_deals.py``/``tests/test_workdays.py``.
    """
    local_dt = datetime(d.year, d.month, d.day, 10, 0, 0)
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _entered_at_n_workdays_ago(n: int) -> str:
    """Stage-entry moment (UTC ISO) such that ``workdays_since(ts, today) == n``.

    Uses the already separately tested (T2) pure module
    ``app.workdays.workdays_since`` as an oracle to build the test's input data —
    not to verify the test's own assertion.
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
        "the board column has no card list under any of the expected keys "
        f"{_CARD_LIST_KEYS}: {column}"
    )


def _column_stage_id(column: dict) -> int:
    for key in ("stage_id", "id"):
        if key in column:
            return column[key]
    raise AssertionError(f"the column has neither 'stage_id' nor 'id': {column}")


def _board_column_for_stage(client, stage_id: int) -> dict:
    response = client.get("/api/board")
    assert response.status_code == 200
    columns = response.json()
    return next(c for c in columns if _column_stage_id(c) == stage_id)


def _card_in_column(column: dict, deal_id: int) -> dict:
    return next(c for c in _cards_of(column) if c["id"] == deal_id)


# ===========================================================================
# GET /api/stages
# ===========================================================================


def test_get_stages_returns_seed_ordered_by_position(client, sqlite_conn):
    expected_ids = _stage_ids_by_position(sqlite_conn)
    expected_names = [
        "Backlog",
        "To Do",
        "In Progress",
        "Review",
        "Blocked",
        "Done",
    ]

    response = client.get("/api/stages")

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    # Generic seed from migration 001: exactly 6 stages.
    assert len(body) == 6
    assert [row["name"] for row in body] == expected_names
    assert [row["id"] for row in body] == expected_ids
    for row in body:
        for key in ("id", "name", "position", "threshold_days", "is_terminal"):
            assert key in row, f"a stage must contain the field '{key}': {row}"


# ===========================================================================
# POST /api/stages
# ===========================================================================


def test_post_stage_appends_last_with_default_threshold(client, sqlite_conn):
    max_position_before = max(
        row["position"] for row in _stages_by_position(sqlite_conn)
    )

    response = client.post("/api/stages", json={"name": "New stage"})

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "New stage"
    assert body["position"] > max_position_before
    assert body["threshold_days"] == 5

    row = _stage_row(sqlite_conn, body["id"])
    assert row["position"] > max_position_before
    assert row["threshold_days"] == 5
    # The new stage is really the last by position among all stages.
    all_positions = [r["position"] for r in _stages_by_position(sqlite_conn)]
    assert row["position"] == max(all_positions)


def test_post_stage_explicit_threshold_days(client, sqlite_conn):
    response = client.post(
        "/api/stages", json={"name": "With threshold", "threshold_days": 12}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["threshold_days"] == 12
    row = _stage_row(sqlite_conn, body["id"])
    assert row["threshold_days"] == 12


@pytest.mark.parametrize("bad_name", ["", "   ", "\n\t"])
def test_post_stage_empty_or_whitespace_name_returns_422(client, sqlite_conn, bad_name):
    ids_before = _stage_ids_by_position(sqlite_conn)

    response = client.post("/api/stages", json={"name": bad_name})

    assert response.status_code == 422
    assert _stage_ids_by_position(sqlite_conn) == ids_before, (
        "an invalid POST must not create a stage"
    )


# ===========================================================================
# PATCH /api/stages/{id}: rename
# ===========================================================================


def test_patch_stage_rename_updates_name_deal_keeps_stage_id(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "Item in a stage", stage["id"])

    response = client.patch(
        f"/api/stages/{stage['id']}", json={"name": "Renamed"}
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"
    assert _stage_row(sqlite_conn, stage["id"])["name"] == "Renamed"
    # The item stays bound to the same stage by id — renaming a stage does not
    # unbind or move items.
    assert _deal_row(sqlite_conn, deal_id)["stage_id"] == stage["id"]


def test_patch_terminal_stage_rename_allowed(client, sqlite_conn):
    terminal = _terminal_stage(sqlite_conn)

    response = client.patch(
        f"/api/stages/{terminal['id']}", json={"name": "Done (renamed)"}
    )

    assert response.status_code == 200
    row = _stage_row(sqlite_conn, terminal["id"])
    assert row["name"] == "Done (renamed)"
    assert row["is_terminal"] == 1, "renaming does not clear the terminal flag"


# ===========================================================================
# PATCH /api/stages/{id}: threshold_days
# ===========================================================================


def test_patch_stage_threshold_days_updates_value(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)

    response = client.patch(f"/api/stages/{stage['id']}", json={"threshold_days": 9})

    assert response.status_code == 200
    assert response.json()["threshold_days"] == 9
    assert _stage_row(sqlite_conn, stage["id"])["threshold_days"] == 9


@pytest.mark.parametrize("bad_threshold", [0, -1, -5])
def test_patch_stage_threshold_days_non_positive_returns_422(
    client, sqlite_conn, bad_threshold
):
    stage = _first_non_terminal_stage(sqlite_conn)
    original_threshold = stage["threshold_days"]

    response = client.patch(
        f"/api/stages/{stage['id']}", json={"threshold_days": bad_threshold}
    )

    assert response.status_code == 422
    assert _stage_row(sqlite_conn, stage["id"])["threshold_days"] == original_threshold


def test_patch_stage_nonexistent_returns_404(client):
    response_name = client.patch("/api/stages/999999", json={"name": "Someone"})
    response_threshold = client.patch("/api/stages/999999", json={"threshold_days": 3})

    assert response_name.status_code == 404
    assert response_threshold.status_code == 404


def test_patch_stage_threshold_days_change_reflected_in_board_aging_level(
    client, sqlite_conn
):
    """Changing a stage threshold affects the future aging_level without
    touching days_in_stage of existing items (the stage-entry date itself is
    not changed by a threshold PATCH).
    """
    stage = _first_non_terminal_stage(sqlite_conn)
    assert stage["threshold_days"] == 5, "T1 seed: every stage has threshold_days=5"
    entered_at = _entered_at_n_workdays_ago(1)
    deal_id = _insert_deal(
        sqlite_conn, "Threshold-sensitive", stage["id"], stage_entered_at=entered_at
    )

    before_column = _board_column_for_stage(client, stage["id"])
    before_card = _card_in_column(before_column, deal_id)
    assert before_card["days_in_stage"] == 1
    assert before_card["aging_level"] == "ok"  # threshold=5: 1 < 0.8*5=4 -> ok

    response = client.patch(f"/api/stages/{stage['id']}", json={"threshold_days": 1})
    assert response.status_code == 200

    after_column = _board_column_for_stage(client, stage["id"])
    after_card = _card_in_column(after_column, deal_id)
    assert after_card["days_in_stage"] == 1, (
        "days_in_stage must not change on a threshold change — only aging_level is recomputed"
    )
    assert after_card["aging_level"] == "warn"  # threshold=1: 0.8*1<=1<=1 -> warn


def test_patch_stage_is_terminal_field_never_changes_is_terminal(client, sqlite_conn):
    """The spec allows both ignoring the field and 422 — but is_terminal must
    not change in either of the two permitted outcomes."""
    stage = _first_non_terminal_stage(sqlite_conn)
    assert stage["is_terminal"] == 0

    response = client.patch(f"/api/stages/{stage['id']}", json={"is_terminal": 1})

    assert response.status_code in (200, 422), (
        "criterion T6 allows either ignoring the field (200) or rejecting it "
        f"(422); got {response.status_code}"
    )
    assert _stage_row(sqlite_conn, stage["id"])["is_terminal"] == 0, (
        "is_terminal must not change via the API in either of the two "
        "permitted request outcomes"
    )


# ===========================================================================
# POST /api/stages/reorder
# ===========================================================================


def test_reorder_full_permutation_updates_order_in_stages_and_board(
    client, sqlite_conn
):
    original_ids = _stage_ids_by_position(sqlite_conn)
    reversed_ids = list(reversed(original_ids))

    response = client.post("/api/stages/reorder", json={"ordered_ids": reversed_ids})

    assert response.status_code == 200

    stages_response = client.get("/api/stages")
    assert [row["id"] for row in stages_response.json()] == reversed_ids

    board_response = client.get("/api/board")
    board_ids = [_column_stage_id(col) for col in board_response.json()]
    assert board_ids == reversed_ids


def test_reorder_does_not_change_deal_stage_id_or_stage_name(client, sqlite_conn):
    original_ids = _stage_ids_by_position(sqlite_conn)
    target_stage = _second_non_terminal_stage(sqlite_conn)
    target_name = target_stage["name"]
    deal_id = _insert_deal(sqlite_conn, "Must not get lost", target_stage["id"])

    reversed_ids = list(reversed(original_ids))
    response = client.post("/api/stages/reorder", json={"ordered_ids": reversed_ids})
    assert response.status_code == 200

    # The item stays bound to the same stage by id...
    row = _deal_row(sqlite_conn, deal_id)
    assert row["stage_id"] == target_stage["id"]
    # ...and the stage with that id still has the same name (not recreated under
    # a different id, not swapped out).
    stage_after = _stage_row(sqlite_conn, target_stage["id"])
    assert stage_after["name"] == target_name

    # The item's card is visible in the board column under the same stage_id as
    # before (at the column's new board position).
    column = _board_column_for_stage(client, target_stage["id"])
    card = _card_in_column(column, deal_id)
    assert card["title"] == "Must not get lost"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda ids: ids[:-1], id="one_id_missing"),
        pytest.param(lambda ids: ids + [999999], id="extra_nonexistent_id"),
        pytest.param(lambda ids: ids[:-1] + [ids[0]], id="duplicate_id"),
    ],
)
def test_reorder_invalid_id_set_returns_422_and_order_unchanged(
    client, sqlite_conn, mutate
):
    original_ids = _stage_ids_by_position(sqlite_conn)
    bad_ids = mutate(list(original_ids))

    response = client.post("/api/stages/reorder", json={"ordered_ids": bad_ids})

    assert response.status_code == 422
    assert _stage_ids_by_position(sqlite_conn) == original_ids, (
        "stage order must not change on a rejected reorder"
    )


# ===========================================================================
# DELETE /api/stages/{id}
# ===========================================================================
# User requirement (after Step 1 acceptance): stages can be deleted. Rules:
# only an empty (no items, including archived) non-terminal stage can be
# deleted; the terminal "Done" stage and the last remaining working stage are
# protected (409).


def test_delete_empty_stage_removes_from_list_and_board(client, sqlite_conn):
    stage = _second_non_terminal_stage(sqlite_conn)

    response = client.delete(f"/api/stages/{stage['id']}")

    assert response.status_code == 200
    ids = [row["id"] for row in client.get("/api/stages").json()]
    assert stage["id"] not in ids

    board = client.get("/api/board")
    assert board.status_code == 200
    assert all(_column_stage_id(col) != stage["id"] for col in board.json()), (
        "the deleted stage's column must not be returned by the board"
    )


def test_delete_stage_with_deal_returns_409_and_keeps_both(client, sqlite_conn):
    stage = _second_non_terminal_stage(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "Item in a stage", stage["id"])

    response = client.delete(f"/api/stages/{stage['id']}")

    assert response.status_code == 409
    ids = [row["id"] for row in client.get("/api/stages").json()]
    assert stage["id"] in ids, "a stage with items must remain"
    deal_row = _deal_row(sqlite_conn, deal_id)
    assert deal_row["stage_id"] == stage["id"], "the item must not be affected"


def test_delete_terminal_stage_returns_409(client, sqlite_conn):
    terminal = _terminal_stage(sqlite_conn)

    response = client.delete(f"/api/stages/{terminal['id']}")

    assert response.status_code == 409
    ids = [row["id"] for row in client.get("/api/stages").json()]
    assert terminal["id"] in ids


def test_delete_nonexistent_stage_returns_404(client):
    response = client.delete("/api/stages/999999")

    assert response.status_code == 404


def test_delete_last_non_terminal_stage_returns_409(client, sqlite_conn):
    non_terminal_ids = [
        row["id"]
        for row in sqlite_conn.execute(
            "SELECT id FROM stages WHERE is_terminal = 0 ORDER BY position"
        ).fetchall()
    ]
    # Empty seed: delete all working stages except one via the API.
    for stage_id in non_terminal_ids[:-1]:
        assert client.delete(f"/api/stages/{stage_id}").status_code == 200

    response = client.delete(f"/api/stages/{non_terminal_ids[-1]}")

    assert response.status_code == 409, (
        "the last working stage must be protected from deletion"
    )
    remaining = [row["id"] for row in client.get("/api/stages").json()]
    assert non_terminal_ids[-1] in remaining


def test_create_stage_after_delete_appends_last(client, sqlite_conn):
    stage = _second_non_terminal_stage(sqlite_conn)
    assert client.delete(f"/api/stages/{stage['id']}").status_code == 200

    response = client.post("/api/stages", json={"name": "New after deletion"})

    assert response.status_code == 201
    created = response.json()
    positions = [row["position"] for row in _stages_by_position(sqlite_conn)]
    assert created["position"] == max(positions)
