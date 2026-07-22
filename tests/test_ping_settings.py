"""T5 tests: hang-detector settings — template, M, track_hangs.

Acceptance criteria come from docs/specs/002-step2-hang-detector.md, task T5
(and the related sections "Terms and calculation rules", "Design decisions"
(point 4 — terminal stages are excluded unconditionally, an attempt to PATCH
``track_hangs`` on a terminal stage → 422), API/"GET|PUT /api/settings/ping",
"Extensions to existing handlers"/``GET /api/stages``, ``PATCH /api/stages/{id}``,
``POST /api/stages``). The tests are written against the spec, not the
implementation.

TDD state at writing time: ``app/routers/settings.py`` does not exist and is not
registered in ``app/main.py`` — ``GET``/``PUT /api/settings/ping`` currently
return 404. ``app/repo/stages.py._STAGE_COLUMNS`` does not include
``track_hangs`` — accessing ``row["track_hangs"]`` in the responses of ``GET
/api/stages``/``POST /api/stages``/``PATCH /api/stages/{id}`` currently raises a
``KeyError`` (the field is not in the dict), not an import error. ``StagePatch``
in ``app/routers/stages.py`` does not accept the ``track_hangs`` field (it is
currently silently ignored as an extra pydantic field) — the criteria "422 on a
terminal stage" and "changes the flag on a non-terminal stage" currently do not
hold. This is the correct TDD state: the file collects, the tests fail on
missing functionality, not on a build error.

Only fixtures from ``tests/conftest.py`` are used: ``client``, ``sqlite_conn``.
Helper code (inserting stages/items directly into the DB, picking dates "N
business days ago", working with the ``/api/pings`` block) lives locally in this
file, following the already-accepted
``tests/test_pings_block.py``/``tests/test_ping_actions.py`` (T3/T4) and
``tests/test_stages.py`` (T6 of Step 1).

Fixture rule (spec section "Terms"): any backdating of an item's activity in
this file shifts BACK both ``last_activity_at`` AND ``stage_entered_at``
(preserving the invariant ``stage_entered_at <= last_activity_at``).

Assumptions about the response shape that the spec fixes literally (API
section): ``GET /api/settings/ping`` → ``{"template": ..., "hidden_days": ...,
"default_template": ...}``; ``GET /api/stages``/``PATCH /api/stages/{id}``/
``POST /api/stages`` contain the ``track_hangs`` field (by analogy with the
already-accepted ``is_terminal`` field — an integer 0/1, not a JSON bool: the
same convention as the other flag columns of the ``stages`` table, returned
"as is" from SQLite).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from helpers import (
    _first_non_terminal_stage,
    _insert_deal,
    _stage_row,
    _terminal_stage,
)

# ---------------------------------------------------------------------------
# Helper functions: stages, items — direct DB work.
# ---------------------------------------------------------------------------


def _set_threshold(sqlite_conn, stage_id: int, threshold_days: int) -> None:
    sqlite_conn.execute(
        "UPDATE stages SET threshold_days = ? WHERE id = ?", (threshold_days, stage_id)
    )
    sqlite_conn.commit()


def _entered_iso_at_local_10am(d: date) -> str:
    local_dt = datetime(d.year, d.month, d.day, 10, 0, 0)
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _iso_n_workdays_ago(n: int) -> str:
    """A moment in time (UTC ISO) such that ``workdays_since(ts, today) == n``.

    Uses the already independently tested (T2 of Step 1) pure module
    ``app.workdays.workdays_since`` as an oracle to build the test's input data —
    not to verify the test's assertion itself.
    """
    from app.workdays import workdays_since

    today = date.today()
    for offset in range(1, 40):
        candidate = today - timedelta(days=offset)
        iso = _entered_iso_at_local_10am(candidate)
        if workdays_since(iso, today) == n:
            return iso
    raise AssertionError(f"could not find a date for {n} business days ago")


def _make_overdue_deal(
    sqlite_conn, title: str, *, threshold: int = 1, workdays_ago: int = 3, **kwargs
):
    """An item in the first non-terminal stage, overdue past the threshold."""
    stage = _first_non_terminal_stage(sqlite_conn)
    _set_threshold(sqlite_conn, stage["id"], threshold)
    ts = _iso_n_workdays_ago(workdays_ago)
    deal_id = _insert_deal(
        sqlite_conn,
        title,
        stage["id"],
        stage_entered_at=ts,
        last_activity_at=ts,
        **kwargs,
    )
    return deal_id, stage


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


def _get_settings(client) -> dict:
    response = client.get("/api/settings/ping")
    assert response.status_code == 200, response.text
    return response.json()


def _app_meta(sqlite_conn, key: str) -> str | None:
    row = sqlite_conn.execute(
        "SELECT value FROM app_meta WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row is not None else None


# ===========================================================================
# GET /api/settings/ping
# ===========================================================================


def test_get_settings_ping_returns_template_hidden_days_and_default(
    client, sqlite_conn
):
    from app.ping import DEFAULT_PING_HIDDEN_DAYS, DEFAULT_PING_TEMPLATE

    body = _get_settings(client)

    assert body["template"] == DEFAULT_PING_TEMPLATE
    assert body["hidden_days"] == DEFAULT_PING_HIDDEN_DAYS
    assert isinstance(body["hidden_days"], int)
    assert body["default_template"] == DEFAULT_PING_TEMPLATE


def test_get_settings_ping_reflects_app_meta_value(client, sqlite_conn):
    sqlite_conn.execute(
        "UPDATE app_meta SET value = ? WHERE key = 'ping_template'",
        ("Custom template: {counterparty}",),
    )
    sqlite_conn.execute(
        "UPDATE app_meta SET value = '7' WHERE key = 'ping_hidden_days'"
    )
    sqlite_conn.commit()

    body = _get_settings(client)

    assert body["template"] == "Custom template: {counterparty}"
    assert body["hidden_days"] == 7


def test_get_settings_ping_falls_back_to_defaults_when_keys_missing(
    client, sqlite_conn
):
    """Analog of checklist 8 for the new endpoint: missing app_meta keys do not
    break the computation (T5 reuses the same fallback behavior as T3)."""
    from app.ping import DEFAULT_PING_HIDDEN_DAYS, DEFAULT_PING_TEMPLATE

    sqlite_conn.execute(
        "DELETE FROM app_meta WHERE key IN ('ping_template', 'ping_hidden_days')"
    )
    sqlite_conn.commit()

    body = _get_settings(client)

    assert body["template"] == DEFAULT_PING_TEMPLATE
    assert body["hidden_days"] == DEFAULT_PING_HIDDEN_DAYS


# ===========================================================================
# PUT /api/settings/ping — successful save and effect on /api/pings
# ===========================================================================


def test_put_settings_ping_valid_body_updates_app_meta(client, sqlite_conn):
    response = client.put(
        "/api/settings/ping",
        json={"template": "New template {counterparty}", "hidden_days": 4},
    )
    assert response.status_code == 200, response.text

    assert _app_meta(sqlite_conn, "ping_template") == "New template {counterparty}"
    assert _app_meta(sqlite_conn, "ping_hidden_days") == "4"

    body = _get_settings(client)
    assert body["template"] == "New template {counterparty}"
    assert body["hidden_days"] == 4


def test_put_settings_ping_default_template_unaffected_by_custom_template(
    client, sqlite_conn
):
    from app.ping import DEFAULT_PING_TEMPLATE

    response = client.put(
        "/api/settings/ping",
        json={"template": "A completely different template", "hidden_days": 3},
    )
    assert response.status_code == 200, response.text

    body = _get_settings(client)
    assert body["default_template"] == DEFAULT_PING_TEMPLATE
    assert body["template"] == "A completely different template"


def test_put_settings_ping_new_template_reflected_in_pings_block_ping_text(
    client, sqlite_conn
):
    deal_id, _ = _make_overdue_deal(
        sqlite_conn, "Template check", company="Daisy LLC"
    )
    assert deal_id in _ids_in(_get_pings(client))

    response = client.put(
        "/api/settings/ping",
        json={"template": "Ping: {counterparty}", "hidden_days": 2},
    )
    assert response.status_code == 200, response.text

    body = _get_pings(client)
    item = _item_for(body, deal_id)
    assert item["ping_text"].startswith("Ping: "), item["ping_text"]
    assert "Daisy LLC" in item["ping_text"]


def test_put_settings_ping_hidden_days_zero_makes_freshly_pinged_deal_visible(
    client, sqlite_conn
):
    """Checklist 4: M=0 via settings — a pinged item is immediately visible."""
    deal_id, _ = _make_overdue_deal(sqlite_conn, "M=0 via settings")

    ping_resp = client.post(f"/api/deals/{deal_id}/ping")
    assert ping_resp.status_code == 200, ping_resp.text
    # The default M=2 should hide the freshly pinged item.
    assert deal_id not in _ids_in(_get_pings(client))

    current_template = _get_settings(client)["template"]
    put_resp = client.put(
        "/api/settings/ping",
        json={"template": current_template, "hidden_days": 0},
    )
    assert put_resp.status_code == 200, put_resp.text

    assert deal_id in _ids_in(_get_pings(client)), (
        "hidden_days=0 should immediately show the freshly pinged item"
    )


# ===========================================================================
# PUT /api/settings/ping — validation (422), app_meta state unchanged
# ===========================================================================


@pytest.mark.parametrize("bad_template", ["", "   ", "\n\t"])
def test_put_settings_ping_blank_template_returns_422_and_keeps_old_value(
    client, sqlite_conn, bad_template
):
    before = _app_meta(sqlite_conn, "ping_template")

    response = client.put(
        "/api/settings/ping", json={"template": bad_template, "hidden_days": 2}
    )

    assert response.status_code == 422
    assert _app_meta(sqlite_conn, "ping_template") == before


@pytest.mark.parametrize("bad_hidden_days", [-1, -5])
def test_put_settings_ping_negative_hidden_days_returns_422(
    client, sqlite_conn, bad_hidden_days
):
    before = _app_meta(sqlite_conn, "ping_hidden_days")

    response = client.put(
        "/api/settings/ping",
        json={"template": "Valid template", "hidden_days": bad_hidden_days},
    )

    assert response.status_code == 422
    assert _app_meta(sqlite_conn, "ping_hidden_days") == before


def test_put_settings_ping_non_numeric_hidden_days_returns_422(client, sqlite_conn):
    before = _app_meta(sqlite_conn, "ping_hidden_days")

    response = client.put(
        "/api/settings/ping",
        json={"template": "Valid template", "hidden_days": "abc"},
    )

    assert response.status_code == 422
    assert _app_meta(sqlite_conn, "ping_hidden_days") == before


def test_put_settings_ping_hidden_days_zero_is_valid_boundary(client, sqlite_conn):
    response = client.put(
        "/api/settings/ping", json={"template": "Valid template", "hidden_days": 0}
    )

    assert response.status_code == 200, response.text
    assert _app_meta(sqlite_conn, "ping_hidden_days") == "0"


def test_put_settings_ping_hidden_days_365_is_valid_boundary(client, sqlite_conn):
    response = client.put(
        "/api/settings/ping",
        json={"template": "Valid template", "hidden_days": 365},
    )

    assert response.status_code == 200, response.text
    assert _app_meta(sqlite_conn, "ping_hidden_days") == "365"


def test_put_settings_ping_hidden_days_366_returns_422(client, sqlite_conn):
    before = _app_meta(sqlite_conn, "ping_hidden_days")

    response = client.put(
        "/api/settings/ping",
        json={"template": "Valid template", "hidden_days": 366},
    )

    assert response.status_code == 422
    assert _app_meta(sqlite_conn, "ping_hidden_days") == before


# ===========================================================================
# GET /api/stages — the new track_hangs field
# ===========================================================================


def test_get_stages_includes_track_hangs_matching_db_and_terminal_default(
    client, sqlite_conn
):
    response = client.get("/api/stages")
    assert response.status_code == 200
    body = response.json()
    assert body, "a non-empty stage seed is expected"

    for row in body:
        assert "track_hangs" in row, f"stage without track_hangs: {row}"
        db_row = _stage_row(sqlite_conn, row["id"])
        assert row["track_hangs"] == db_row["track_hangs"]
        if db_row["is_terminal"]:
            assert row["track_hangs"] == 0
        else:
            assert row["track_hangs"] == 1


# ===========================================================================
# PATCH /api/stages/{id}: track_hangs on a non-terminal stage
# ===========================================================================


def test_patch_stage_track_hangs_false_removes_overdue_deal_from_pings_block(
    client, sqlite_conn
):
    deal_id, stage = _make_overdue_deal(sqlite_conn, "Tracking turns off")
    assert deal_id in _ids_in(_get_pings(client))

    response = client.patch(f"/api/stages/{stage['id']}", json={"track_hangs": False})
    assert response.status_code == 200, response.text

    assert not _stage_row(sqlite_conn, stage["id"])["track_hangs"]
    assert deal_id not in _ids_in(_get_pings(client))


def test_patch_stage_track_hangs_true_restores_deal_in_pings_block(client, sqlite_conn):
    deal_id, stage = _make_overdue_deal(sqlite_conn, "Tracking turns on again")

    off_resp = client.patch(f"/api/stages/{stage['id']}", json={"track_hangs": False})
    assert off_resp.status_code == 200, off_resp.text
    assert deal_id not in _ids_in(_get_pings(client))

    on_resp = client.patch(f"/api/stages/{stage['id']}", json={"track_hangs": True})
    assert on_resp.status_code == 200, on_resp.text

    assert _stage_row(sqlite_conn, stage["id"])["track_hangs"] == 1
    assert deal_id in _ids_in(_get_pings(client))


def test_patch_stage_track_hangs_on_terminal_stage_returns_422_and_unchanged(
    client, sqlite_conn
):
    terminal = _terminal_stage(sqlite_conn)
    before = _stage_row(sqlite_conn, terminal["id"])["track_hangs"]
    assert before == 0, "migration 003 turns off track_hangs for terminal stages"

    response_true = client.patch(
        f"/api/stages/{terminal['id']}", json={"track_hangs": True}
    )
    assert response_true.status_code == 422
    assert _stage_row(sqlite_conn, terminal["id"])["track_hangs"] == before

    response_false = client.patch(
        f"/api/stages/{terminal['id']}", json={"track_hangs": False}
    )
    assert response_false.status_code == 422
    assert _stage_row(sqlite_conn, terminal["id"])["track_hangs"] == before


def test_patch_stage_track_hangs_nonexistent_stage_returns_404(client):
    response = client.patch("/api/stages/999999", json={"track_hangs": False})
    assert response.status_code == 404


def test_patch_stage_name_and_threshold_days_still_work_after_track_hangs_addition(
    client, sqlite_conn
):
    """Regression: adding track_hangs must not break the existing PATCH fields."""
    stage = _first_non_terminal_stage(sqlite_conn)

    response = client.patch(
        f"/api/stages/{stage['id']}",
        json={"name": "Renamed T5", "threshold_days": 8},
    )

    assert response.status_code == 200, response.text
    row = _stage_row(sqlite_conn, stage["id"])
    assert row["name"] == "Renamed T5"
    assert row["threshold_days"] == 8
    # track_hangs was not touched, since it was not passed in the PATCH.
    assert row["track_hangs"] == 1


# ===========================================================================
# POST /api/stages — new stages are created with track_hangs = 1
# ===========================================================================


def test_post_stage_creates_with_track_hangs_one_by_default(client, sqlite_conn):
    response = client.post("/api/stages", json={"name": "New stage T5"})
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["track_hangs"] == 1
    assert _stage_row(sqlite_conn, body["id"])["track_hangs"] == 1


def test_post_stage_ignores_track_hangs_field_in_request_body(client, sqlite_conn):
    """Spec: "the field in the body is not accepted" — POST always creates with
    track_hangs=1, even if the client explicitly tried to pass 0."""
    response = client.post(
        "/api/stages", json={"name": "Attempt to disable immediately", "track_hangs": 0}
    )
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["track_hangs"] == 1
    assert _stage_row(sqlite_conn, body["id"])["track_hangs"] == 1
