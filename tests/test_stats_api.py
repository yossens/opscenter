"""T4 tests: `GET /api/stats` — dashboard statistics.

Acceptance criteria come from docs/specs/006-custom-improvements.md, task T4
(and Design -> `GET /api/stats` response shape, where the exact JSON is given —
that section was deliberately "tuned" for testability during plan-critic). The
tests are written STRICTLY from the spec; the implementation
(`app/routers/dashboard.py`, `app/main.py`) was not read and did not exist at
the time of writing — the correct TDD state: the file collects
(`--collect-only` green) and the tests fail until implementation (ImportError
via `client`/404 and the like).

Only these `tests/conftest.py` fixtures are used: `client`, `sqlite_conn`. The
stages (6, seeded by migration 001) are created by `init_db()` inside the
`client` fixture — the test does not create them, it finds them by name/position,
as already established in `tests/test_stages.py`/`tests/test_deals.py`. Deals
and `llm_calls` rows are inserted directly via `sqlite_conn`, following the same
files and `tests/test_llm_stats.py` (the `llm_calls` schema from migration 004:
`id, created_at, model, input_tokens, output_tokens, duration_ms, status,
purpose`).

The "current age in the current stage" metric is computed via
`app/workdays.py::workdays_since` (Design, T4) — as in `test_deals.py` /
`test_stages.py`, here that separately tested pure module is used ONLY as an
oracle for building the input `stage_entered_at` (finding a calendar date that
yields exactly N business days from "today"), not for checking the test's own
assertion (the assertions below are concrete numbers from criterion T4:
``avg == round((N+M)/2, 1)``, ``max == max(N, M)``).

An assumption documented explicitly (the spec does not fix it literally): for an
empty DB, `deals_total`/`deals_active`/`deals_closed` and every number in the
`llm` block are compared for numeric equality `== 0` (not `is 0` / a strict
`int`-vs-`float` check) — the spec only says "both are 0 for an empty stage"
without specifying the representation of zero.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from helpers import _insert_deal, _stages_by_position

# ---------------------------------------------------------------------------
# 6 stages seeded by migration 001, positions 1..6. The last ("Done") is
# terminal, so 5 non-terminal stages appear in `/api/stats`.
# ---------------------------------------------------------------------------

EXPECTED_NON_TERMINAL_STAGE_NAMES = [
    "Backlog",
    "To Do",
    "In Progress",
    "Review",
    "Blocked",
]

TOP_LEVEL_KEYS = {"deals_total", "deals_active", "deals_closed", "stages", "llm"}
STAGE_ENTRY_KEYS = {
    "stage_id",
    "name",
    "position",
    "deal_count",
    "avg_workdays_in_stage",
    "max_workdays_in_stage",
}
LLM_KEYS = {
    "total_calls",
    "success_calls",
    "error_calls",
    "avg_duration_ms",
    "input_tokens",
    "output_tokens",
}


# ---------------------------------------------------------------------------
# Helper functions: direct DB access (stages are already seeded by migration
# 001, deals/llm_calls inserted bypassing the API, following
# test_deals.py/test_stages.py/test_llm_stats.py).
# ---------------------------------------------------------------------------


def _non_terminal_stages_by_position(sqlite_conn):
    rows = sqlite_conn.execute(
        "SELECT * FROM stages WHERE is_terminal = 0 ORDER BY position"
    ).fetchall()
    assert len(rows) == 5, (
        "expected 5 non-terminal stages out of the 6 seeded by migration 001, "
        f"got {len(rows)}"
    )
    return rows


def _terminal_stage(sqlite_conn):
    row = sqlite_conn.execute(
        "SELECT * FROM stages WHERE is_terminal = 1 LIMIT 1"
    ).fetchone()
    assert row is not None, "expected the terminal stage 'Done'"
    return row


def _stage_by_name(sqlite_conn, name: str):
    row = sqlite_conn.execute(
        "SELECT * FROM stages WHERE name = ?", (name,)
    ).fetchone()
    assert row is not None, f"stage {name!r} not found (expected from the migration 001 seed)"
    return row


def _insert_llm_call(
    sqlite_conn,
    *,
    created_at: str = "2026-01-01T00:00:00",
    model: str = "gemini-3.1-flash-lite",
    input_tokens: int = 0,
    output_tokens: int = 0,
    duration_ms: int = 100,
    status: str = "success",
    purpose: str = "parse_note",
) -> int:
    cur = sqlite_conn.execute(
        """
        INSERT INTO llm_calls
            (created_at, model, input_tokens, output_tokens, duration_ms, status, purpose)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (created_at, model, input_tokens, output_tokens, duration_ms, status, purpose),
    )
    sqlite_conn.commit()
    return cur.lastrowid


def _entered_iso_at_local_10am(d: date) -> str:
    """UTC ISO string corresponding to 10:00 local time on date ``d``.

    See the same technique in tests/test_deals.py / tests/test_stages.py /
    tests/test_workdays.py.
    """
    local_dt = datetime(d.year, d.month, d.day, 10, 0, 0)
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _entered_at_n_workdays_ago(n: int) -> str:
    """Stage-entry moment (UTC ISO) such that ``workdays_since(ts, today) == n``.

    Uses the separately tested (Step 2) pure module
    ``app.workdays.workdays_since`` as an oracle for building the test's input
    data — not for checking the test's own assertion.
    """
    from app.workdays import workdays_since

    today = date.today()
    for offset in range(1, 40):
        candidate = today - timedelta(days=offset)
        iso = _entered_iso_at_local_10am(candidate)
        if workdays_since(iso, today) == n:
            return iso
    raise AssertionError(f"could not find a date for {n} business days ago")


def _get_stats(client) -> dict:
    response = client.get("/api/stats")
    assert response.status_code == 200, response.text
    return response.json()


def _stage_entry_by_name(body: dict, name: str) -> dict:
    for entry in body["stages"]:
        if entry["name"] == name:
            return entry
    raise AssertionError(f"stage {name!r} is absent from body['stages']: {body['stages']!r}")


# ---------------------------------------------------------------------------
# T4 criterion 1: 200 + the exact set of top-level keys
# ---------------------------------------------------------------------------


def test_get_api_stats_returns_200_with_exact_top_level_keys(client):
    body = _get_stats(client)
    assert set(body.keys()) == TOP_LEVEL_KEYS


# ---------------------------------------------------------------------------
# T4 criterion 2: stages — the list of 5 non-terminal stages in position order,
# each entry having exactly the expected set of keys
# ---------------------------------------------------------------------------


def test_stages_list_is_non_terminal_stages_in_position_order_with_exact_keys(
    client, sqlite_conn
):
    expected_rows = _non_terminal_stages_by_position(sqlite_conn)

    body = _get_stats(client)
    stages = body["stages"]

    assert isinstance(stages, list)
    assert len(stages) == 5

    for entry, expected_row in zip(stages, expected_rows):
        assert set(entry.keys()) == STAGE_ENTRY_KEYS
        assert entry["stage_id"] == expected_row["id"]
        assert entry["name"] == expected_row["name"]
        assert entry["position"] == expected_row["position"]

    assert [e["name"] for e in stages] == EXPECTED_NON_TERMINAL_STAGE_NAMES

    # The terminal stage "Done" must not appear in stages.
    assert "Done" not in [e["name"] for e in stages]


# ---------------------------------------------------------------------------
# T4 criterion 3: llm — exactly the expected set of keys
# ---------------------------------------------------------------------------


def test_llm_block_has_exact_keys(client):
    body = _get_stats(client)
    assert set(body["llm"].keys()) == LLM_KEYS


# ---------------------------------------------------------------------------
# T4 criterion 4: deal_count per stage matches the seeded deals
# ---------------------------------------------------------------------------


def test_deal_count_per_stage_matches_seeded_deals(client, sqlite_conn):
    stage_a = _stage_by_name(sqlite_conn, "Backlog")
    stage_b = _stage_by_name(sqlite_conn, "In Progress")

    _insert_deal(sqlite_conn, "Deal A1", stage_a["id"])
    _insert_deal(sqlite_conn, "Deal A2", stage_a["id"])
    _insert_deal(sqlite_conn, "Deal A3", stage_a["id"])
    _insert_deal(sqlite_conn, "Deal B1", stage_b["id"])
    _insert_deal(sqlite_conn, "Deal B2", stage_b["id"])

    body = _get_stats(client)

    assert _stage_entry_by_name(body, "Backlog")["deal_count"] == 3
    assert _stage_entry_by_name(body, "In Progress")["deal_count"] == 2

    # The other non-terminal stages are untouched — 0.
    for name in EXPECTED_NON_TERMINAL_STAGE_NAMES:
        if name in ("Backlog", "In Progress"):
            continue
        assert _stage_entry_by_name(body, name)["deal_count"] == 0


# ---------------------------------------------------------------------------
# T4 criterion 5: deals_active / deals_closed / deals_total
# ---------------------------------------------------------------------------


def test_deals_active_closed_and_total_counts(client, sqlite_conn):
    non_terminal = _stage_by_name(sqlite_conn, "Review")
    terminal = _terminal_stage(sqlite_conn)

    _insert_deal(sqlite_conn, "Active 1", non_terminal["id"])
    _insert_deal(sqlite_conn, "Active 2", non_terminal["id"])
    _insert_deal(sqlite_conn, "Active 3", non_terminal["id"])
    _insert_deal(sqlite_conn, "Closed 1", terminal["id"], closed_at="2026-01-05T00:00:00")

    body = _get_stats(client)

    assert body["deals_active"] == 3
    assert body["deals_closed"] == 1
    assert body["deals_total"] == 4
    assert body["deals_total"] == body["deals_active"] + body["deals_closed"]


# ---------------------------------------------------------------------------
# T4 criterion 6: empty stage -> deal_count==0, avg==0, max==0
# ---------------------------------------------------------------------------


def test_empty_stage_reports_zero_count_and_zero_avg_max(client, sqlite_conn):
    # No deals are seeded at all — every non-terminal stage is empty.
    body = _get_stats(client)

    for name in EXPECTED_NON_TERMINAL_STAGE_NAMES:
        entry = _stage_entry_by_name(body, name)
        assert entry["deal_count"] == 0
        assert entry["avg_workdays_in_stage"] == 0
        assert entry["max_workdays_in_stage"] == 0


# ---------------------------------------------------------------------------
# T4 criterion 7: avg/max_workdays_in_stage — the exact formula from criterion T4
# ---------------------------------------------------------------------------


def test_avg_and_max_workdays_in_stage_match_hand_computed_formula(client, sqlite_conn):
    stage = _stage_by_name(sqlite_conn, "To Do")

    n_workdays = 3
    m_workdays = 8
    _insert_deal(
        sqlite_conn,
        "Aging 1",
        stage["id"],
        stage_entered_at=_entered_at_n_workdays_ago(n_workdays),
    )
    _insert_deal(
        sqlite_conn,
        "Aging 2",
        stage["id"],
        stage_entered_at=_entered_at_n_workdays_ago(m_workdays),
    )

    body = _get_stats(client)
    entry = _stage_entry_by_name(body, "To Do")

    assert entry["deal_count"] == 2
    assert entry["avg_workdays_in_stage"] == pytest.approx(
        round((n_workdays + m_workdays) / 2, 1)
    )
    assert entry["max_workdays_in_stage"] == max(n_workdays, m_workdays)


def test_avg_workdays_in_stage_rounds_to_one_decimal_on_non_integer_mean(
    client, sqlite_conn
):
    """Three deals (1, 2, 2 business days ago) give a mean of 5/3 = 1.6(6) ->
    rounded to 1 decimal = 1.7 — pins the rounding behavior specifically, not
    just the coincidentally-integer means from the previous test."""
    stage = _stage_by_name(sqlite_conn, "Blocked")

    for n in (1, 2, 2):
        _insert_deal(
            sqlite_conn,
            f"Aging {n}",
            stage["id"],
            stage_entered_at=_entered_at_n_workdays_ago(n),
        )

    body = _get_stats(client)
    entry = _stage_entry_by_name(body, "Blocked")

    assert entry["deal_count"] == 3
    assert entry["avg_workdays_in_stage"] == pytest.approx(1.7)
    assert entry["max_workdays_in_stage"] == 2


# ---------------------------------------------------------------------------
# T4 criterion 8: the llm block matches the seeded llm_calls
# ---------------------------------------------------------------------------


def test_llm_block_matches_seeded_llm_calls(client, sqlite_conn):
    _insert_llm_call(
        sqlite_conn, status="success", duration_ms=100, input_tokens=1000, output_tokens=200
    )
    _insert_llm_call(
        sqlite_conn, status="success", duration_ms=300, input_tokens=2000, output_tokens=300
    )
    _insert_llm_call(
        sqlite_conn, status="error", duration_ms=50, input_tokens=0, output_tokens=0
    )

    body = _get_stats(client)
    llm = body["llm"]

    assert llm["total_calls"] == 3
    assert llm["success_calls"] == 2
    assert llm["error_calls"] == 1
    assert llm["total_calls"] == llm["success_calls"] + llm["error_calls"]
    # (100 + 300 + 50) / 3 == 150 exactly — chosen with no remainder so it does
    # not depend on int rounding/truncation the spec leaves unspecified.
    assert llm["avg_duration_ms"] == 150
    assert llm["input_tokens"] == 3000
    assert llm["output_tokens"] == 500


# ---------------------------------------------------------------------------
# T4 criterion 9: empty llm_calls -> zeros, no division by 0
# ---------------------------------------------------------------------------


def test_empty_llm_calls_table_returns_all_zero_llm_block(client, sqlite_conn):
    count = sqlite_conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
    assert count == 0, "test precondition: the llm_calls table must be empty"

    body = _get_stats(client)
    llm = body["llm"]

    assert llm["total_calls"] == 0
    assert llm["success_calls"] == 0
    assert llm["error_calls"] == 0
    assert llm["avg_duration_ms"] == 0
    assert llm["input_tokens"] == 0
    assert llm["output_tokens"] == 0


# ---------------------------------------------------------------------------
# T4 criterion 10 (edge case from Risks): a fully empty DB -> a valid zeroed
# structure, not a 500
# ---------------------------------------------------------------------------


def test_empty_database_returns_valid_zeroed_structure_not_500(client, sqlite_conn):
    deal_count = sqlite_conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
    llm_count = sqlite_conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
    assert deal_count == 0 and llm_count == 0, "precondition: a fully empty DB"

    body = _get_stats(client)

    assert body["deals_total"] == 0
    assert body["deals_active"] == 0
    assert body["deals_closed"] == 0
    assert len(body["stages"]) == 5
    for entry in body["stages"]:
        assert entry["deal_count"] == 0
        assert entry["avg_workdays_in_stage"] == 0
        assert entry["max_workdays_in_stage"] == 0
    for key in LLM_KEYS:
        assert body["llm"][key] == 0
