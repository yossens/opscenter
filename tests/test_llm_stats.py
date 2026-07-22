"""T5 tests: LLM cost statistics (`GET /api/llm/stats`).

Acceptance criteria come from docs/specs/003-step3-gemini-parsing.md, task T5
(section "API", subsection `GET /api/llm/stats`; criteria T5 1-5). At the time
of writing neither `app/routers/llm.py` nor the aggregator in
`app/repo/parsing.py` (or `app/repo/llm_calls.py`) existed yet — the expected
TDD state: the file collects (`--collect-only` green) and the tests fail until
implementation (ImportError/404/AttributeError and the like).

The tests are written STRICTLY from the spec; the T5 implementation was not read.

Only these `tests/conftest.py` fixtures are used: `client`, `sqlite_conn`,
`config`, `data_dir`, `monkeypatch` (pytest built-in). `llm_calls` rows are
seeded directly via `sqlite_conn` (see the migration 004 schema in spec T1:
`id, created_at, model, input_tokens, output_tokens, duration_ms, status,
purpose`; `created_at` is UTC ISO `YYYY-MM-DDTHH:MM:SS`).

--------------------------------------------------------------------------
SEAM FOR BACKEND-DEV: how this suite deterministically builds "today" and the
UTC->local conversion (MUST read before implementing T5)
--------------------------------------------------------------------------

1. The tests' "local date today" is `datetime.date.today()` (the calendar date
   of the machine's system clock, in the OS's local TZ). This is EXACTLY the
   same value that `datetime.now(timezone.utc).astimezone().date()` would give
   — just shorter. No calendar date is hardcoded: the test is deterministic
   relative to the moment it runs, not relative to a specific calendar day, so
   it passes on any day.

2. To build a UTC ISO `llm_calls.created_at` timestamp that corresponds to a
   given LOCAL time `local_naive` (a naive `datetime`), the tests use the
   technique already established in `tests/test_pings_block.py`
   (`_entered_iso_at_local_10am`) and `tests/test_deals.py` /
   `tests/test_search.py` / `tests/test_stages.py`:
   `local_naive.astimezone(timezone.utc)`. A naive `datetime.astimezone()`
   treats self as a time IN THE SYSTEM'S LOCAL timezone (documented in the
   stdlib) — so the resulting UTC string correctly corresponds to the given
   local time on THIS machine, regardless of its TZ.

3. For the main scenarios (today / 10 days ago / 40 days ago) local noon
   (12:00) is used — by analogy with "10:00" in `_entered_iso_at_local_10am`:
   noon is guaranteed to be far from midnight, so the conversion to UTC cannot
   "jump" to a neighboring calendar day for any reasonable TZ offset (even ±12h).

4. A separate `created_at` row is deliberately placed WITHIN A FEW MINUTES OF
   LOCAL MIDNIGHT (00:02 or 23:58 local time — see
   `_local_midnight_edge_case_iso`), so that its UTC CALENDAR date DIFFERS from
   the local calendar date (if the machine's local TZ offset is nonzero — the
   typical case for a user's laptop, not UTC). This pins the spec's mandatory
   requirement: bucketing MUST convert `created_at` from UTC to a local date
   BEFORE comparing against the "today" / "last 30 days" window boundaries; you
   MUST NOT compare `date(created_at)` (the naive UTC date) directly against the
   local date — that is the exact source of the off-by-one this test catches.

5. The existing pure module `app/workdays.py::_to_local_date` already solves
   exactly this (UTC ISO/`datetime`/`date` -> local `date` via
   `.astimezone().date()`) for card aging (Step 2) — the T5 implementation may
   reuse the same conversion technique (not necessarily this private helper
   literally, but the same approach: convert `created_at` to the local timezone
   BEFORE taking the date, not the other way around).

If the local TZ of the machine running the tests happens to be exactly UTC+0,
scenario 4 does not apply (midnight ± a couple of minutes gives no date shift)
— the test `pytest.skip`s with an explanation rather than passing falsely.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# Helper functions for building fixture data
# ---------------------------------------------------------------------------


def _local_naive_to_utc_iso(local_naive: datetime) -> str:
    """UTC ISO string (the `llm_calls.created_at` format) for a naive local
    `datetime`. A naive `datetime.astimezone()` treats self as the system's
    local time (see the module docstring, item 2)."""
    return local_naive.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _local_noon_iso(local_date: date) -> str:
    return _local_naive_to_utc_iso(datetime.combine(local_date, time(12, 0, 0)))


def _local_midnight_edge_case_iso(local_date: date) -> str:
    """UTC ISO timestamp within a few minutes of local midnight on
    `local_date`, whose UTC CALENDAR date DIFFERS from `local_date`.

    Tries 00:02 (catches positive TZ offsets, east of UTC) and 23:58 (catches
    negative offsets, west of UTC). See the module docstring, item 4.
    """
    for minute_of_day in (2, 23 * 60 + 58):
        local_naive = datetime.combine(local_date, time(0, 0, 0)) + timedelta(
            minutes=minute_of_day
        )
        if local_naive.date() != local_date:
            continue
        utc_dt = local_naive.astimezone(timezone.utc)
        if utc_dt.date() != local_date:
            return utc_dt.strftime("%Y-%m-%dT%H:%M:%S")
    pytest.skip(
        "This machine's local TZ has a zero offset from UTC — the scenario "
        "'different UTC/local calendar dates for the same midnight' does not "
        "apply (no date shift to verify the conversion)."
    )


def _insert_llm_call(
    sqlite_conn,
    *,
    created_at: str,
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


def _get_stats(client) -> dict:
    response = client.get("/api/llm/stats")
    assert response.status_code == 200, response.text
    body = response.json()
    for window in ("today", "last_30_days"):
        assert window in body, f"the response is missing the key '{window}': {body!r}"
        for key in ("calls", "input_tokens", "output_tokens", "cost_usd"):
            assert key in body[window], (
                f"window '{window}' is missing the key '{key}': {body[window]!r}"
            )
    return body


def _client_with_price_overrides(monkeypatch, input_price: float, output_price: float):
    """Builds a NEW FastAPI app with overridden `app.config` price constants,
    patching them BEFORE the first `create_app()` call in this test (the same
    principle as `tests/conftest.py::small_upload_client` for
    `MAX_UPLOAD_BYTES`) — works regardless of whether the router reads the
    constant as a module attribute at request time or binds it once at import,
    since the patch is applied before the router's first import within the
    test."""
    import app.config as config_module

    monkeypatch.setattr(config_module, "LLM_PRICE_INPUT_PER_1M", input_price)
    monkeypatch.setattr(config_module, "LLM_PRICE_OUTPUT_PER_1M", output_price)

    from app.main import create_app
    from fastapi.testclient import TestClient

    return TestClient(create_app())


# ---------------------------------------------------------------------------
# Criterion T5-2: bucketing "today" / "last 30 days" + UTC->local
# ---------------------------------------------------------------------------


def test_stats_windowing_separates_today_10_days_ago_and_40_days_ago(
    client, sqlite_conn
):
    today_local = date.today()
    ten_days_ago = today_local - timedelta(days=10)
    forty_days_ago = today_local - timedelta(days=40)

    _insert_llm_call(
        sqlite_conn,
        created_at=_local_noon_iso(today_local),
        input_tokens=100,
        output_tokens=50,
    )
    _insert_llm_call(
        sqlite_conn,
        created_at=_local_naive_to_utc_iso(
            datetime.combine(today_local, time(15, 30, 0))
        ),
        input_tokens=200,
        output_tokens=80,
    )
    _insert_llm_call(
        sqlite_conn,
        created_at=_local_noon_iso(ten_days_ago),
        input_tokens=300,
        output_tokens=120,
    )
    _insert_llm_call(
        sqlite_conn,
        created_at=_local_noon_iso(forty_days_ago),
        input_tokens=999,
        output_tokens=999,
    )

    body = _get_stats(client)

    # "Today": only the 2 rows from today.
    assert body["today"]["calls"] == 2
    assert body["today"]["input_tokens"] == 300
    assert body["today"]["output_tokens"] == 130

    # "Last 30 days": today's rows + the 10-day-old row, WITHOUT the 40-day-old one.
    assert body["last_30_days"]["calls"] == 3
    assert body["last_30_days"]["input_tokens"] == 600
    assert body["last_30_days"]["output_tokens"] == 250


def test_stats_local_midnight_boundary_row_uses_local_date_not_utc_date(
    client, sqlite_conn
):
    """A row whose `created_at` falls on local midnight ± a couple of minutes
    (a UTC timestamp on the NEIGHBORING UTC calendar day relative to the local
    date) must be classified by its LOCAL date — pins the UTC->local conversion
    and catches the off-by-one near midnight (criterion T5-2)."""
    today_local = date.today()
    boundary_iso = _local_midnight_edge_case_iso(today_local)

    _insert_llm_call(
        sqlite_conn, created_at=boundary_iso, input_tokens=10, output_tokens=5
    )

    body = _get_stats(client)

    assert body["today"]["calls"] == 1
    assert body["today"]["input_tokens"] == 10
    assert body["today"]["output_tokens"] == 5
    assert body["last_30_days"]["calls"] == 1
    assert body["last_30_days"]["input_tokens"] == 10
    assert body["last_30_days"]["output_tokens"] == 5


# ---------------------------------------------------------------------------
# Criterion T5-3: cost_usd = input/1e6*price_in + output/1e6*price_out
# ---------------------------------------------------------------------------


def test_cost_usd_matches_formula_with_default_price_constants(
    client, sqlite_conn, config
):
    today_local = date.today()
    _insert_llm_call(
        sqlite_conn,
        created_at=_local_noon_iso(today_local),
        input_tokens=123_456,
        output_tokens=7_890,
    )

    body = _get_stats(client)

    expected_cost = (
        123_456 / 1e6 * config.LLM_PRICE_INPUT_PER_1M
        + 7_890 / 1e6 * config.LLM_PRICE_OUTPUT_PER_1M
    )
    assert body["today"]["cost_usd"] == pytest.approx(expected_cost)
    assert body["last_30_days"]["cost_usd"] == pytest.approx(expected_cost)


def test_cost_usd_changes_when_price_constants_are_overridden_via_config(
    data_dir, monkeypatch, sqlite_conn
):
    import app.config as config_module

    default_input_price = config_module.LLM_PRICE_INPUT_PER_1M
    default_output_price = config_module.LLM_PRICE_OUTPUT_PER_1M
    override_input_price = default_input_price + 5.0
    override_output_price = default_output_price + 20.0

    today_local = date.today()
    _insert_llm_call(
        sqlite_conn,
        created_at=_local_noon_iso(today_local),
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )

    with _client_with_price_overrides(
        monkeypatch, override_input_price, override_output_price
    ) as overridden_client:
        body = _get_stats(overridden_client)

    expected_overridden_cost = (
        1_000_000 / 1e6 * override_input_price + 1_000_000 / 1e6 * override_output_price
    )
    expected_default_cost = (
        1_000_000 / 1e6 * default_input_price + 1_000_000 / 1e6 * default_output_price
    )

    assert body["today"]["cost_usd"] == pytest.approx(expected_overridden_cost)
    # Pins "changes accordingly": with overridden prices the cost is NOT equal
    # to what the default prices would give for the same tokens.
    assert body["today"]["cost_usd"] != pytest.approx(expected_default_cost)


# ---------------------------------------------------------------------------
# Criterion T5-4: empty llm_calls -> 200 with zeros, not 500
# ---------------------------------------------------------------------------


def test_empty_llm_calls_table_returns_200_with_all_zero_fields(client, sqlite_conn):
    count = sqlite_conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
    assert count == 0, "test precondition: the llm_calls table must be empty"

    body = _get_stats(client)

    for window in ("today", "last_30_days"):
        assert body[window]["calls"] == 0
        assert body[window]["input_tokens"] == 0
        assert body[window]["output_tokens"] == 0
        assert body[window]["cost_usd"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Criterion T5-5: status='error' rows count toward calls but do not distort the
# token/cost sums (their tokens are 0 per the migration 004 schema)
# ---------------------------------------------------------------------------


def test_error_status_rows_counted_in_calls_but_excluded_from_token_and_cost_sums(
    client, sqlite_conn, config
):
    today_local = date.today()
    ts = _local_noon_iso(today_local)

    _insert_llm_call(
        sqlite_conn,
        created_at=ts,
        status="success",
        input_tokens=500,
        output_tokens=200,
    )
    _insert_llm_call(
        sqlite_conn, created_at=ts, status="error", input_tokens=0, output_tokens=0
    )
    _insert_llm_call(
        sqlite_conn, created_at=ts, status="error", input_tokens=0, output_tokens=0
    )

    body = _get_stats(client)

    # All 3 calls (success + 2 errors) happened and are counted in the call counter.
    assert body["today"]["calls"] == 3
    assert body["last_30_days"]["calls"] == 3

    # Tokens/cost come only from the successful row (errors have 0 tokens).
    assert body["today"]["input_tokens"] == 500
    assert body["today"]["output_tokens"] == 200
    expected_cost = (
        500 / 1e6 * config.LLM_PRICE_INPUT_PER_1M
        + 200 / 1e6 * config.LLM_PRICE_OUTPUT_PER_1M
    )
    assert body["today"]["cost_usd"] == pytest.approx(expected_cost)
