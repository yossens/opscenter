"""T2 tests: the pure module app/ping.py (template rendering, steps, hide window).

Acceptance criteria come from docs/specs/002-step2-hang-detector.md, task T2
(and the sections "Terms and calculation rules" / "Design decisions" /
"Pure module app/ping.py"). The ``app.ping`` module must consist of pure
functions (stdlib + ``app.workdays`` only, no FastAPI/sqlite), so this file,
following the pattern of ``tests/test_workdays.py``, does not import
``app.ping`` at module level — the import happens inside the test functions via
helper functions, so that ``pytest --collect-only`` collects the file even
before the implementation exists (the correct TDD state: collection passes,
execution fails with ``ModuleNotFoundError``/``ImportError``).

Reference-date calendar (matches tests/test_workdays.py):
- 2024-01-05 — Friday
- 2024-01-08 — Monday (next week)
- 2024-01-09 — Tuesday of the same week
"""

from __future__ import annotations

import re
from datetime import date

import pytest


# ---------------------------------------------------------------------------
# Lazy-import helpers (not at module level — see the file docstring).
# ---------------------------------------------------------------------------


def _render_ping():
    from app.ping import render_ping

    return render_ping


def _escalation_step():
    from app.ping import escalation_step

    return escalation_step


def _is_hidden_after_ping():
    from app.ping import is_hidden_after_ping

    return is_hidden_after_ping


def _prepare_last_note():
    from app.ping import prepare_last_note

    return prepare_last_note


def _default_template():
    from app.ping import DEFAULT_PING_TEMPLATE

    return DEFAULT_PING_TEMPLATE


def _default_hidden_days():
    from app.ping import DEFAULT_PING_HIDDEN_DAYS

    return DEFAULT_PING_HIDDEN_DAYS


# The default template verbatim from app/migrations/003_hang_detector.sql (and
# the spec, section "Migration 003") — used to cross-check the module constant.
MIGRATION_DEFAULT_TEMPLATE = (
    "{waiting_for}, reminder about {counterparty}: waiting on {stage} for {days} "
    "business days. Last status: {last_note}. Any progress?"
)


# ---------------------------------------------------------------------------
# DEFAULT_PING_TEMPLATE / DEFAULT_PING_HIDDEN_DAYS
# ---------------------------------------------------------------------------


def test_default_ping_template_matches_migration_literal():
    """The module constant is byte-for-byte equal to the string seeded by migration 003.

    The full cross-check "constant == value in a fresh DB" is criterion T3;
    here we pin the DB-independent part: the constant literal itself.
    """
    assert _default_template() == MIGRATION_DEFAULT_TEMPLATE


def test_default_ping_hidden_days_is_2():
    assert _default_hidden_days() == 2


# ---------------------------------------------------------------------------
# render_ping — default template, pinned exact strings.
# ---------------------------------------------------------------------------


def test_render_ping_default_template_all_values_filled():
    render_ping = _render_ping()
    template = _default_template()
    result = render_ping(
        template,
        {
            "waiting_for": "Ivan",
            "counterparty": "Daisy Co",
            "stage": "Compliance",
            "days": "7",
            "last_note": "Sent the documents",
        },
    )
    assert result == (
        "Ivan, reminder about Daisy Co: waiting on Compliance for 7 business days. "
        "Last status: Sent the documents. Any progress?"
    )


def test_render_ping_empty_waiting_for_no_leading_comma():
    render_ping = _render_ping()
    template = _default_template()
    result = render_ping(
        template,
        {
            "waiting_for": "",
            "counterparty": "Daisy Co",
            "stage": "Compliance",
            "days": "7",
            "last_note": "Sent the documents",
        },
    )
    assert result == (
        "reminder about Daisy Co: waiting on Compliance for 7 business days. "
        "Last status: Sent the documents. Any progress?"
    )


def test_render_ping_empty_last_note_collapses_whole_phrase():
    render_ping = _render_ping()
    template = _default_template()
    result = render_ping(
        template,
        {
            "waiting_for": "Ivan",
            "counterparty": "Daisy Co",
            "stage": "Compliance",
            "days": "7",
            "last_note": "",
        },
    )
    assert result == (
        "Ivan, reminder about Daisy Co: waiting on Compliance for 7 business days. "
        "Any progress?"
    )
    assert ": ." not in result


def test_render_ping_both_waiting_for_and_last_note_empty_no_artifacts():
    render_ping = _render_ping()
    template = _default_template()
    result = render_ping(
        template,
        {
            "waiting_for": "",
            "counterparty": "Daisy Co",
            "stage": "Compliance",
            "days": "7",
            "last_note": "",
        },
    )
    assert not result.startswith(",")
    assert not result.lstrip().startswith(",")
    assert ": ." not in result
    assert ", ," not in result
    assert "  " not in result
    assert "{" not in result and "}" not in result


# ---------------------------------------------------------------------------
# render_ping — invariants on arbitrary user templates.
# ---------------------------------------------------------------------------


def test_render_ping_sentinel_never_leaks_into_result():
    """When empty values collapse, the sentinel marker never leaks through."""
    render_ping = _render_ping()
    template = (
        "Status ({waiting_for}): {counterparty} / {stage} / {days} / {last_note}."
    )
    result = render_ping(
        template,
        {
            "waiting_for": "",
            "counterparty": "",
            "stage": "",
            "days": "",
            "last_note": "",
        },
    )
    # No non-printable control character (a potential marker) remains in the
    # final string.
    assert not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", result)
    assert "{" not in result and "}" not in result


def test_render_ping_known_placeholders_substituted_when_non_empty():
    render_ping = _render_ping()
    template = "{counterparty}|{stage}|{days}|{waiting_for}|{last_note}"
    values = {
        "counterparty": "K-Corp",
        "stage": "Discovery",
        "days": "3",
        "waiting_for": "Peter",
        "last_note": "Status OK",
    }
    result = render_ping(template, values)
    for value in values.values():
        assert value in result
    assert "{" not in result and "}" not in result


def test_render_ping_unknown_placeholder_remains_literal():
    render_ping = _render_ping()
    template = "Text {foo} and {counterparty} end"
    result = render_ping(
        template,
        {
            "counterparty": "ACME",
            "stage": "S",
            "days": "1",
            "waiting_for": "W",
            "last_note": "N",
        },
    )
    assert "{foo}" in result
    assert "ACME" in result


def test_render_ping_template_without_placeholders_returned_as_is():
    render_ping = _render_ping()
    template = "Just text without placeholders."
    result = render_ping(
        template,
        {
            "counterparty": "X",
            "stage": "Y",
            "days": "1",
            "waiting_for": "Z",
            "last_note": "W",
        },
    )
    assert result == template


def test_render_ping_kirillica_values_pass_through_unmodified():
    """Non-ASCII placeholder values are not mangled by collapsing/unicode handling."""
    render_ping = _render_ping()
    template = "{counterparty} — {stage} — {last_note}"
    result = render_ping(
        template,
        {
            "counterparty": "ACME «Größe»",
            "stage": "Contract negotiation",
            "days": "4",
            "waiting_for": "Jean-Pierre",
            "last_note": "Awaiting the signed scan",
        },
    )
    assert "ACME «Größe»" in result
    assert "Contract negotiation" in result
    assert "Awaiting the signed scan" in result


# ---------------------------------------------------------------------------
# prepare_last_note
# ---------------------------------------------------------------------------


def test_prepare_last_note_truncates_150_to_120_plus_ellipsis():
    prepare_last_note = _prepare_last_note()
    text = "a" * 150
    result = prepare_last_note(text)
    assert result == "a" * 120 + "…"
    assert len(result) == 121


def test_prepare_last_note_no_truncation_at_exactly_120_chars():
    prepare_last_note = _prepare_last_note()
    text = "b" * 120
    result = prepare_last_note(text)
    assert result == text
    assert "…" not in result


def test_prepare_last_note_truncates_121_chars():
    prepare_last_note = _prepare_last_note()
    text = "c" * 121
    result = prepare_last_note(text)
    assert result == "c" * 120 + "…"
    assert len(result) == 121


def test_prepare_last_note_replaces_newlines_and_tabs_with_space():
    prepare_last_note = _prepare_last_note()
    text = "  Hello\nWorld\tTest  "
    result = prepare_last_note(text)
    assert result == "Hello World Test"
    assert "\n" not in result
    assert "\t" not in result


def test_prepare_last_note_none_returns_empty_string():
    prepare_last_note = _prepare_last_note()
    assert prepare_last_note(None) == ""


def test_prepare_last_note_empty_string_returns_empty_string():
    prepare_last_note = _prepare_last_note()
    assert prepare_last_note("") == ""


def test_prepare_last_note_whitespace_only_after_replace_and_strip_is_empty():
    """A consequence of the documented order of operations (replace -> strip):

    a string of only newlines/tabs/spaces becomes, after replacement, a string
    of spaces -> strip -> an empty string.
    """
    prepare_last_note = _prepare_last_note()
    assert prepare_last_note("  \n\t  \n ") == ""


# ---------------------------------------------------------------------------
# escalation_step
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pings_since, expected",
    [
        pytest.param(0, 1, id="0->1"),
        pytest.param(1, 2, id="1->2"),
        pytest.param(2, 3, id="2->3"),
        pytest.param(5, 3, id="5->3_clamped"),
    ],
)
def test_escalation_step_table(pings_since, expected):
    escalation_step = _escalation_step()
    assert escalation_step(pings_since) == expected


def test_escalation_step_returns_int():
    escalation_step = _escalation_step()
    assert isinstance(escalation_step(0), int)


# ---------------------------------------------------------------------------
# is_hidden_after_ping
# ---------------------------------------------------------------------------


def test_is_hidden_after_ping_m2_friday_ping_hidden_on_monday():
    """M=2: ping on Friday, Monday — 1 business day < 2 -> hidden."""
    is_hidden_after_ping = _is_hidden_after_ping()
    result = is_hidden_after_ping("2024-01-05T10:00:00", date(2024, 1, 8), 2, 1)
    assert result is True


def test_is_hidden_after_ping_m2_friday_ping_visible_on_tuesday():
    """M=2: ping on Friday, Tuesday next week — 2 business days >= 2 -> visible."""
    is_hidden_after_ping = _is_hidden_after_ping()
    result = is_hidden_after_ping("2024-01-05T10:00:00", date(2024, 1, 9), 2, 1)
    assert result is False


def test_is_hidden_after_ping_m0_visible_same_day_as_ping():
    is_hidden_after_ping = _is_hidden_after_ping()
    result = is_hidden_after_ping("2024-01-08T10:00:00", date(2024, 1, 8), 0, 1)
    assert result is False


def test_is_hidden_after_ping_pings_since_zero_always_false():
    """pings_since=0 -> the window does not apply, always visible, even a ping "today"."""
    is_hidden_after_ping = _is_hidden_after_ping()
    result = is_hidden_after_ping("2024-01-08T10:00:00", date(2024, 1, 8), 10, 0)
    assert result is False


def test_is_hidden_after_ping_pings_since_three_always_false():
    """pings_since>=3 -> the window does not apply (step 3 "does not grow further")."""
    is_hidden_after_ping = _is_hidden_after_ping()
    assert is_hidden_after_ping("2024-01-08T10:00:00", date(2024, 1, 8), 10, 3) is False


def test_is_hidden_after_ping_pings_since_more_than_three_always_false():
    is_hidden_after_ping = _is_hidden_after_ping()
    assert is_hidden_after_ping("2024-01-08T10:00:00", date(2024, 1, 8), 10, 5) is False


def test_is_hidden_after_ping_pings_since_two_window_behaves_like_one():
    is_hidden_after_ping = _is_hidden_after_ping()
    assert is_hidden_after_ping("2024-01-05T10:00:00", date(2024, 1, 8), 2, 2) is True
    assert is_hidden_after_ping("2024-01-05T10:00:00", date(2024, 1, 9), 2, 2) is False


def test_is_hidden_after_ping_returns_bool():
    is_hidden_after_ping = _is_hidden_after_ping()
    result = is_hidden_after_ping("2024-01-08T10:00:00", date(2024, 1, 8), 2, 1)
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# The module does not import FastAPI/sqlite/starlette (only stdlib + app.workdays).
# ---------------------------------------------------------------------------


def test_module_has_no_fastapi_or_sqlite_imports():
    import ast
    import importlib.util

    spec = importlib.util.find_spec("app.ping")
    assert spec is not None and spec.origin is not None, (
        "app/ping.py must exist and be importable as a module"
    )

    with open(spec.origin, encoding="utf-8") as fh:
        source = fh.read()

    tree = ast.parse(source, filename=spec.origin)
    imported_root_names: set[str] = set()
    imported_full_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_root_names.add(alias.name.split(".")[0])
                imported_full_names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_root_names.add(node.module.split(".")[0])
                imported_full_names.add(node.module)

    forbidden = {"fastapi", "sqlite3", "starlette"}
    intersection = imported_root_names & forbidden
    assert not intersection, (
        f"app/ping.py must not import {intersection} "
        "(the module must consist of pure functions)"
    )


def test_module_only_imports_stdlib_and_app_workdays():
    """Stricter than the FastAPI/sqlite ban: only stdlib + app.workdays are allowed.

    From the design section "Pure module app/ping.py": "Only stdlib + an import
    from app.workdays; no FastAPI/sqlite".
    """
    import ast
    import importlib.util
    import sys

    spec = importlib.util.find_spec("app.ping")
    assert spec is not None and spec.origin is not None

    with open(spec.origin, encoding="utf-8") as fh:
        source = fh.read()

    tree = ast.parse(source, filename=spec.origin)
    stdlib_names = set(sys.stdlib_module_names)

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                full_name = alias.name
                root_name = full_name.split(".")[0]
                if root_name == "app":
                    if full_name != "app.workdays":
                        violations.append(full_name)
                elif root_name not in stdlib_names:
                    violations.append(full_name)
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            root_name = node.module.split(".")[0]
            if root_name == "app":
                if node.module != "app.workdays":
                    violations.append(node.module)
            elif root_name not in stdlib_names:
                violations.append(node.module)

    assert not violations, (
        f"app/ping.py imports disallowed modules: {violations} "
        "(only stdlib and app.workdays are allowed)"
    )
