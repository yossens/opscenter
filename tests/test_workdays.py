"""T2 tests: business days and card-aging levels.

Acceptance criteria source — docs/specs/001-step1-inbox-pipeline.md, task T2
(and the design's "Business days and card-aging" section). The ``app.workdays``
module must consist of pure functions (no FastAPI/sqlite), so these tests use no
DB/app fixtures from ``conftest.py`` and import ``app.workdays`` directly inside
the test functions (not at module level), so that ``pytest --collect-only``
collects the file successfully even before the implementation exists.

Reference-date calendar (verified with `datetime.date(...).strftime('%A')`):
- 2024-01-05 — Friday
- 2024-01-06 — Saturday
- 2024-01-07 — Sunday
- 2024-01-08 — Monday (the week after 2024-01-05)
- 2024-01-12 — Friday of the same week as 2024-01-08
- 2024-01-15 — Monday one week after 2024-01-08
- 2026-01-30 — Friday (month boundary: end of January)
- 2026-02-02 — Monday (start of February, first business day after 2026-01-30)
"""

from __future__ import annotations

from datetime import date

import pytest


def _workdays_since():
    from app.workdays import workdays_since

    return workdays_since


def _aging_level():
    from app.workdays import aging_level

    return aging_level


# ---------------------------------------------------------------------------
# workdays_since
# ---------------------------------------------------------------------------

# Fix the stage-entry time at 10:00:00 UTC (not midnight) so that conversion
# to a local date does not "jump" a calendar day for reasonable laptop local
# time zones (within roughly ±10 hours of UTC) — the TZ-change risk is
# explicitly accepted by the design (see the spec's "Risks and edge cases").
WORKDAYS_TABLE = [
    pytest.param(
        "2024-01-05T10:00:00", date(2024, 1, 8), 1, id="friday->monday=1"
    ),
    pytest.param(
        "2024-01-05T10:00:00", date(2024, 1, 5), 0, id="friday->same_friday=0"
    ),
    pytest.param("2024-01-05T10:00:00", date(2024, 1, 6), 0, id="friday->saturday=0"),
    pytest.param(
        "2024-01-05T10:00:00", date(2024, 1, 7), 0, id="friday->sunday=0"
    ),
    pytest.param(
        "2024-01-08T10:00:00",
        date(2024, 1, 12),
        4,
        id="monday->friday_same_week=4",
    ),
    pytest.param(
        "2024-01-08T10:00:00",
        date(2024, 1, 15),
        5,
        id="monday->monday_next_week=5",
    ),
    pytest.param(
        "2024-01-06T10:00:00", date(2024, 1, 8), 1, id="saturday->monday=1"
    ),
]


@pytest.mark.parametrize("entered_at_utc, now_local, expected", WORKDAYS_TABLE)
def test_workdays_since_table(entered_at_utc, now_local, expected):
    workdays_since = _workdays_since()
    assert workdays_since(entered_at_utc, now_local) == expected


def test_workdays_since_same_day_zero_for_monday():
    """Moving into a stage and counting the same day (not only for Friday) = 0."""
    workdays_since = _workdays_since()
    assert workdays_since("2024-01-08T10:00:00", date(2024, 1, 8)) == 0


def test_workdays_since_month_boundary_friday_to_monday():
    """Month boundary: entered Friday 01-30, counted Monday 02-02 = 1."""
    workdays_since = _workdays_since()
    assert workdays_since("2026-01-30T10:00:00", date(2026, 2, 2)) == 1


def test_workdays_since_returns_int():
    workdays_since = _workdays_since()
    result = workdays_since("2024-01-08T10:00:00", date(2024, 1, 15))
    assert isinstance(result, int)


# ---------------------------------------------------------------------------
# aging_level
# ---------------------------------------------------------------------------

AGING_TABLE = [
    # threshold=5: 0-3 ok, 4-5 warn, 6+ overdue (design + criteria).
    pytest.param(0, 5, "ok", id="threshold5_days0_ok"),
    pytest.param(1, 5, "ok", id="threshold5_days1_ok"),
    pytest.param(2, 5, "ok", id="threshold5_days2_ok"),
    pytest.param(3, 5, "ok", id="threshold5_days3_ok"),
    pytest.param(4, 5, "warn", id="threshold5_days4_warn"),
    pytest.param(5, 5, "warn", id="threshold5_days5_warn"),
    pytest.param(6, 5, "overdue", id="threshold5_days6_overdue"),
    pytest.param(100, 5, "overdue", id="threshold5_days100_overdue"),
    # threshold=1: 0.8*1=0.8 -> 0 ok, 1 warn, 2 overdue.
    pytest.param(0, 1, "ok", id="threshold1_days0_ok"),
    pytest.param(1, 1, "warn", id="threshold1_days1_warn"),
    pytest.param(2, 1, "overdue", id="threshold1_days2_overdue"),
    # threshold=10: 0.8*10=8 -> 7 ok, 8 warn.
    pytest.param(7, 10, "ok", id="threshold10_days7_ok"),
    pytest.param(8, 10, "warn", id="threshold10_days8_warn"),
]


@pytest.mark.parametrize("days, threshold, expected", AGING_TABLE)
def test_aging_level_table(days, threshold, expected):
    aging_level = _aging_level()
    assert aging_level(days, threshold) == expected


def test_aging_level_exactly_at_threshold_is_warn_not_overdue():
    """"Exactly at the threshold" (days == threshold) must be warn, not overdue."""
    aging_level = _aging_level()
    for threshold in (1, 5, 10, 15):
        assert aging_level(threshold, threshold) == "warn"


def test_aging_level_exactly_at_80_percent_threshold_is_warn():
    """Exactly at 80% of the threshold (0.8*threshold, whole number of days) is already warn."""
    aging_level = _aging_level()
    for threshold in (5, 10, 15, 20):
        eighty_percent = round(threshold * 0.8)
        assert aging_level(eighty_percent, threshold) == "warn"
        assert aging_level(eighty_percent - 1, threshold) == "ok"


def test_aging_level_non_integer_80_percent_boundary_rounds_up():
    """threshold=3: 0.8*3=2.4 — days=2 ok, days=3 (>=2.4) already warn."""
    aging_level = _aging_level()
    assert aging_level(2, 3) == "ok"
    assert aging_level(3, 3) == "warn"
    assert aging_level(4, 3) == "overdue"


def test_aging_level_threshold_zero():
    """Threshold 0 (unset/degenerate case) — the formula is defined for it too.

    0.8*0 = 0, so days=0 falls into the warn range [0, 0], and any days>0 is
    overdue; days<0 (should never occur) does not fall under ok in this
    degenerate case, but such input is not tested separately, since a negative
    number of days is not a legal input.
    """
    aging_level = _aging_level()
    assert aging_level(0, 0) == "warn"
    assert aging_level(1, 0) == "overdue"


def test_aging_level_return_type_is_str():
    aging_level = _aging_level()
    result = aging_level(3, 5)
    assert isinstance(result, str)
    assert result in {"ok", "warn", "overdue"}


# ---------------------------------------------------------------------------
# The module imports no FastAPI/sqlite (pure functions)
# ---------------------------------------------------------------------------


def test_module_has_no_fastapi_or_sqlite_imports():
    import ast
    import importlib.util

    spec = importlib.util.find_spec("app.workdays")
    assert spec is not None and spec.origin is not None, (
        "app/workdays.py must exist and be importable as a module"
    )

    with open(spec.origin, encoding="utf-8") as fh:
        source = fh.read()

    tree = ast.parse(source, filename=spec.origin)
    imported_root_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_root_names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_root_names.add(node.module.split(".")[0])

    forbidden = {"fastapi", "sqlite3", "starlette"}
    intersection = imported_root_names & forbidden
    assert not intersection, (
        f"app/workdays.py must not import {intersection} "
        "(the module must consist of pure functions)"
    )
