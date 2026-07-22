"""T7 tests: search (GET /api/search) and the "Slice" (GET /api/board/slice).

Acceptance criteria source — docs/specs/001-step1-inbox-pipeline.md, task T7
(and related edge cases from the "Risks and edge cases" section: non-ASCII and
case handling in search go through FTS5 ``unicode61`` + sanitization of the
user's MATCH query in ``app/fts.py``; the business-days computation for the
"Slice" goes through ``app.workdays.workdays_since`` from T2). The tests are
written against the spec, not the implementation: at the time of writing
``app/routers/search.py``, ``app/repo/search.py`` and ``app/fts.py`` do not yet
exist (a correct TDD state — the tests collect but fail).

Only fixtures from ``tests/conftest.py`` are used: ``client``, ``sqlite_conn``.
No new fixtures were added to ``conftest.py`` — the helper code (inserting
stages/items/notes directly into the DB) lives locally in this file, following
the pattern of the already-accepted ``tests/test_deals.py`` (T5) and
``tests/test_stages.py`` (T6).

Assumptions about the JSON response shape (the spec fixes them literally in the
"Search and slice" section, so there is minimal assumption here):

- ``GET /api/search`` returns exactly ``{"deals": [...], "notes": [...]}``; each
  ``deals`` element is at least ``{id, title, snippet}``, each ``notes`` element
  is at least ``{id, deal_id, snippet, status}`` (the spec lists these fields
  literally).
- ``GET /api/board/slice`` returns exactly ``{"text": "..."}`` (fixed literally
  by the spec).
- The "Slice" line format is a literal from the spec:
  ``"<Title> — <stage>, <N business days>, waiting on: <who>"``; when
  ``waiting_on`` is empty, the ``", waiting on: ..."`` fragment is absent
  entirely. Possible decorative line prefixes (e.g. a list marker "- ") are not
  fixed by the spec, so the tests check the format via ``in text`` (presence of
  the exact substring), not via equality of the whole string/whole feed.
- For ``app/fts.py`` the spec fixes a behavioral contract ("each token is quoted,
  the last one with ``*``") but not a literal string format — so the direct unit
  tests of ``sanitize_fts_query`` check observable behavior (the result is a
  valid MATCH query on a real FTS5 table, finding the expected rows by prefix),
  not the exact string form.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from helpers import (
    _first_non_terminal_stage,
    _insert_deal,
    _insert_note,
    _second_non_terminal_stage,
    _stages_by_position,
    _terminal_stage,
)

# ---------------------------------------------------------------------------
# Helper functions: direct DB work (the 6-stage seed from T1, inserting
# items/notes bypassing the API where precise control of fields and timestamps
# is needed). Copied/adapted from the pattern of tests/test_deals.py,
# independently of it (each test file is self-contained).
# ---------------------------------------------------------------------------


def _stage_by_position_index(sqlite_conn, index: int):
    rows = _stages_by_position(sqlite_conn)
    assert len(rows) > index, "expected the generic 6-stage seed (T1)"
    return rows[index]


def _entered_iso_at_local_10am(d: date) -> str:
    """UTC ISO string corresponding to 10:00 local time on date ``d``.

    On the same principle as in ``tests/test_workdays.py``/``tests/test_deals.py``:
    10:00 (not midnight) avoids "jumping" a calendar day when converting to UTC
    for reasonable laptop local TZ offsets.
    """
    local_dt = datetime(d.year, d.month, d.day, 10, 0, 0)
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _entered_at_n_workdays_ago(n: int) -> str:
    """Stage-entry moment (UTC ISO) such that ``workdays_since(ts, today) == n``.

    Uses the already separately tested (T2) pure module
    ``app.workdays.workdays_since`` as an oracle to build the input data — not to
    verify the assertion itself (the test assertion below is a fixed occurrence
    of ``"N business days"`` in the "Slice" text, not a repeated call to
    ``workdays_since``).
    """
    from app.workdays import workdays_since

    today = date.today()
    if n == 0:
        return _entered_iso_at_local_10am(today)
    for offset in range(1, 30):
        candidate = today - timedelta(days=offset)
        iso = _entered_iso_at_local_10am(candidate)
        if workdays_since(iso, today) == n:
            return iso
    raise AssertionError(f"could not find a date for {n} business days ago")


# ===========================================================================
# GET /api/search — basic response shape, empty/garbage input
# ===========================================================================


def test_search_empty_query_returns_empty_groups_not_500(client):
    response = client.get("/api/search", params={"q": ""})

    assert response.status_code == 200
    body = response.json()
    assert body == {"deals": [], "notes": []}


GARBAGE_QUERIES = [
    pytest.param('"', id="single_double_quote"),
    pytest.param("*", id="single_star"),
    pytest.param("AND (", id="AND_with_unclosed_paren"),
    pytest.param('word" OR', id="quote_plus_OR"),
    pytest.param("test-word", id="hyphen_inside_word"),
    pytest.param("(unbalanced", id="unclosed_paren"),
    pytest.param("NEAR/2", id="NEAR_operator_with_slash"),
    pytest.param("OR", id="bare_OR_operator"),
    pytest.param("AND", id="bare_AND_operator"),
    pytest.param('word"quote', id="quote_mid_word"),
    pytest.param("()", id="empty_parens"),
    pytest.param("title:test", id="column_filter_syntax"),
    pytest.param("--", id="double_hyphen"),
    pytest.param('""', id="empty_quoted_phrase"),
    pytest.param("*" * 20, id="many_stars"),
]


@pytest.mark.parametrize("raw_query", GARBAGE_QUERIES)
def test_search_garbage_query_returns_200_valid_json_not_500(client, raw_query):
    response = client.get("/api/search", params={"q": raw_query})

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body.get("deals"), list)
    assert isinstance(body.get("notes"), list)


def test_search_query_containing_boolean_operator_word_treated_as_literal_text(
    client, sqlite_conn
):
    """ "AND"/"OR" as a standalone query — text, not an operator without an operand."""
    note_id = _insert_note(
        sqlite_conn, "Project ANDPARTNERSTOKEN awaits approval", status="inbox"
    )

    response = client.get("/api/search", params={"q": "ANDPARTNERSTOKEN"})

    assert response.status_code == 200
    note_ids = [n["id"] for n in response.json()["notes"]]
    assert note_id in note_ids


# ===========================================================================
# GET /api/search — note search (token, prefix substring, case)
# ===========================================================================


def test_search_finds_note_by_cyrillic_token_in_body(client, sqlite_conn):
    note_id = _insert_note(
        sqlite_conn, "The client sent documents for the item", status="inbox"
    )

    response = client.get("/api/search", params={"q": "documents"})

    assert response.status_code == 200
    note_ids = [n["id"] for n in response.json()["notes"]]
    assert note_id in note_ids


def test_search_finds_note_by_prefix_of_token(client, sqlite_conn):
    note_id = _insert_note(
        sqlite_conn, "We signed a contract with the client", status="inbox"
    )

    response = client.get("/api/search", params={"q": "contra"})

    assert response.status_code == 200
    note_ids = [n["id"] for n in response.json()["notes"]]
    assert note_id in note_ids


def test_search_note_case_insensitive_uppercase_query_finds_lowercase_content(
    client, sqlite_conn
):
    note_id = _insert_note(
        sqlite_conn, "Discussed project funding", status="inbox"
    )

    response = client.get("/api/search", params={"q": "FUND"})

    assert response.status_code == 200
    note_ids = [n["id"] for n in response.json()["notes"]]
    assert note_id in note_ids


def test_search_note_result_deal_id_null_and_status_for_unattached_note(
    client, sqlite_conn
):
    note_id = _insert_note(sqlite_conn, "uniquetokeninbox", status="inbox")

    response = client.get("/api/search", params={"q": "uniquetokeninbox"})

    notes = response.json()["notes"]
    match = next(n for n in notes if n["id"] == note_id)
    assert match["deal_id"] is None
    assert match["status"] == "inbox"


def test_search_note_result_deal_id_and_status_for_attached_note(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "Item for the attached note", stage["id"])
    note_id = _insert_note(
        sqlite_conn, "uniquetokenattached", status="attached", deal_id=deal_id
    )

    response = client.get("/api/search", params={"q": "uniquetokenattached"})

    notes = response.json()["notes"]
    match = next(n for n in notes if n["id"] == note_id)
    assert match["deal_id"] == deal_id
    assert match["status"] == "attached"


def test_search_note_result_contains_snippet(client, sqlite_conn):
    note_id = _insert_note(
        sqlite_conn, "uniquetokensnippet note", status="inbox"
    )

    response = client.get("/api/search", params={"q": "uniquetokensnippet"})

    notes = response.json()["notes"]
    match = next(n for n in notes if n["id"] == note_id)
    assert "snippet" in match
    assert isinstance(match["snippet"], str)


# ===========================================================================
# GET /api/search — item field search (case)
# ===========================================================================


@pytest.mark.parametrize(
    "field, field_value, query",
    [
        ("title", "Camomile Invest", "camomile"),
        ("company", "Vector Finance", "vector"),
        ("waiting_on", "Client lawyer", "lawyer"),
    ],
    ids=["by_title", "by_company", "by_waiting_on"],
)
def test_search_finds_deal_by_field_cyrillic(
    client, sqlite_conn, field, field_value, query
):
    stage = _first_non_terminal_stage(sqlite_conn)
    kwargs = {} if field == "title" else {field: field_value}
    title = field_value if field == "title" else "Other item with no matches"
    deal_id = _insert_deal(sqlite_conn, title, stage["id"], **kwargs)

    response = client.get("/api/search", params={"q": query})

    assert response.status_code == 200
    deal_ids = [d["id"] for d in response.json()["deals"]]
    assert deal_id in deal_ids


def test_search_deal_case_insensitive_uppercase_query(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "Project launch funding", stage["id"])

    response = client.get("/api/search", params={"q": "FUND"})

    assert response.status_code == 200
    deal_ids = [d["id"] for d in response.json()["deals"]]
    assert deal_id in deal_ids


def test_search_deal_result_contains_id_title_snippet(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "uniqueitemtitlefortest", stage["id"])

    response = client.get(
        "/api/search", params={"q": "uniqueitemtitlefortest"}
    )

    deals = response.json()["deals"]
    match = next(d for d in deals if d["id"] == deal_id)
    assert match["title"] == "uniqueitemtitlefortest"
    assert "snippet" in match
    assert isinstance(match["snippet"], str)


# ===========================================================================
# GET /api/search — results grouped separately (deals/notes)
# ===========================================================================


def test_search_results_grouped_separately_deal_only_match(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "uniquetokenonlyitem", stage["id"])
    _insert_note(sqlite_conn, "Note with no match for this word", status="inbox")

    response = client.get("/api/search", params={"q": "uniquetokenonlyitem"})

    body = response.json()
    assert body["notes"] == []
    deal_ids = [d["id"] for d in body["deals"]]
    assert deal_id in deal_ids


def test_search_results_grouped_separately_note_only_match(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    _insert_deal(sqlite_conn, "Item with no match for this word", stage["id"])
    note_id = _insert_note(sqlite_conn, "uniquetokenonlynote", status="inbox")

    response = client.get("/api/search", params={"q": "uniquetokenonlynote"})

    body = response.json()
    assert body["deals"] == []
    note_ids = [n["id"] for n in body["notes"]]
    assert note_id in note_ids


# ===========================================================================
# app/fts.py: sanitize_fts_query — direct contract (an empty query is not fed
# here: the endpoint short-circuits an empty/whitespace ``q`` before calling
# MATCH, see the test above test_search_empty_query_returns_empty_groups_not_500).
# ===========================================================================


def test_sanitize_fts_query_is_a_valid_match_expression_and_finds_by_prefix(
    sqlite_conn,
):
    from app.fts import sanitize_fts_query

    stage_id = sqlite_conn.execute(
        "SELECT id FROM stages ORDER BY position LIMIT 1"
    ).fetchone()[0]
    sqlite_conn.execute(
        """
        INSERT INTO deals (title, stage_id, stage_entered_at, last_activity_at, created_at)
        VALUES ('John Smith', ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00', '2026-01-01T00:00:00')
        """,
        (stage_id,),
    )
    sqlite_conn.commit()

    match_expr = sanitize_fts_query("john smi")

    rows = sqlite_conn.execute(
        "SELECT rowid FROM deals_fts WHERE deals_fts MATCH ?", (match_expr,)
    ).fetchall()
    assert len(rows) == 1


@pytest.mark.parametrize("raw_query", GARBAGE_QUERIES)
def test_sanitize_fts_query_never_produces_invalid_fts5_syntax(sqlite_conn, raw_query):
    from app.fts import sanitize_fts_query

    match_expr = sanitize_fts_query(raw_query)

    # Must not raise sqlite3.OperationalError on a real FTS5 table.
    sqlite_conn.execute(
        "SELECT rowid FROM deals_fts WHERE deals_fts MATCH ?", (match_expr,)
    ).fetchall()


# ===========================================================================
# GET /api/board/slice
# ===========================================================================


def test_slice_response_shape_is_exactly_text_key(client):
    response = client.get("/api/board/slice")

    assert response.status_code == 200
    assert set(response.json().keys()) == {"text"}
    assert isinstance(response.json()["text"], str)


def test_slice_includes_active_deals_grouped_by_stage_in_board_order(
    client, sqlite_conn
):
    stage1 = _first_non_terminal_stage(sqlite_conn)
    stage2 = _second_non_terminal_stage(sqlite_conn)
    empty_stage = _stage_by_position_index(sqlite_conn, 2)
    terminal = _terminal_stage(sqlite_conn)
    assert empty_stage["id"] not in (stage1["id"], stage2["id"], terminal["id"])

    entered_1 = _entered_at_n_workdays_ago(3)
    entered_2 = _entered_at_n_workdays_ago(0)

    deal1_id = _insert_deal(
        sqlite_conn,
        "Item Alpha",
        stage1["id"],
        waiting_on="a lawyer",
        stage_entered_at=entered_1,
    )
    deal2_id = _insert_deal(
        sqlite_conn,
        "Item Beta",
        stage2["id"],
        waiting_on=None,
        stage_entered_at=entered_2,
    )
    closed_deal_id = _insert_deal(
        sqlite_conn,
        "Closed Item",
        terminal["id"],
        closed_at="2026-01-01T00:00:00",
    )

    response = client.get("/api/board/slice")
    assert response.status_code == 200
    text = response.json()["text"]

    # Active stage headers are present, the empty stage is not.
    assert stage1["name"] in text
    assert stage2["name"] in text
    assert empty_stage["name"] not in text
    assert terminal["name"] not in text

    # The closed item is absent entirely.
    assert "Closed Item" not in text
    assert closed_deal_id  # used only for readability of the scenario above

    # Line format: "<Title> — <stage>, <N business days>, waiting on: <who>".
    line1 = f"Item Alpha — {stage1['name']}, 3 business days, waiting on: a lawyer"
    assert line1 in text

    # For the item with no waiting_on the "waiting on:" fragment is absent, and
    # "N business days" is present exactly as is (0 business days — entered the
    # same day).
    fragment2_no_waiting = f"Item Beta — {stage2['name']}, 0 business days"
    assert fragment2_no_waiting in text
    assert f"{fragment2_no_waiting}, waiting on:" not in text

    # Grouping/order: stage 1 header -> its item -> stage 2 header -> its item,
    # in board position order.
    assert (
        text.index(stage1["name"])
        < text.index("Item Alpha")
        < text.index(stage2["name"])
        < text.index("Item Beta")
    )
    assert deal1_id  # used only for readability, the id is not emitted in the text
    assert deal2_id


def test_slice_empty_waiting_on_omits_fragment_entirely(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    _insert_deal(
        sqlite_conn,
        "Item No Waiting",
        stage["id"],
        waiting_on=None,
        stage_entered_at=_entered_at_n_workdays_ago(0),
    )

    response = client.get("/api/board/slice")
    text = response.json()["text"]

    assert "waiting on:" not in text.split("Item No Waiting", 1)[1].split("\n", 1)[0]


def test_slice_excludes_closed_deals_even_when_only_active_deal_present(
    client, sqlite_conn
):
    terminal = _terminal_stage(sqlite_conn)
    _insert_deal(
        sqlite_conn,
        "Only Closed Item",
        terminal["id"],
        closed_at="2026-01-01T00:00:00",
    )

    response = client.get("/api/board/slice")
    assert response.status_code == 200
    text = response.json()["text"]

    assert "Only Closed Item" not in text
    assert terminal["name"] not in text


def test_slice_skips_stages_with_no_active_deals(client, sqlite_conn):
    populated_stage = _first_non_terminal_stage(sqlite_conn)
    empty_stage = _second_non_terminal_stage(sqlite_conn)
    _insert_deal(
        sqlite_conn,
        "The only active item",
        populated_stage["id"],
        stage_entered_at=_entered_at_n_workdays_ago(0),
    )

    response = client.get("/api/board/slice")
    text = response.json()["text"]

    assert populated_stage["name"] in text
    assert empty_stage["name"] not in text


def test_slice_is_flat_plain_text_no_markup(client, sqlite_conn):
    stage = _first_non_terminal_stage(sqlite_conn)
    _insert_deal(
        sqlite_conn,
        "Item for text check",
        stage["id"],
        waiting_on="a manager",
        stage_entered_at=_entered_at_n_workdays_ago(0),
    )

    response = client.get("/api/board/slice")
    text = response.json()["text"]

    assert "<" not in text
    assert ">" not in text
    assert isinstance(text, str)
