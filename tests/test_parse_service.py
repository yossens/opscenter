"""T3 tests: the parsing service ``app/parse_service.py`` + prompts + ``save_suggestion``.

Acceptance criteria source — docs/specs/003-step3-gemini-parsing.md, task T3
(section "Parsing service app/parse_service.py + prompts", criteria 1-9). At the
time of writing ``app/parse_service.py`` and ``app/repo/parsing.py`` do not yet
exist — that is the expected TDD state: the file collects, the tests fail until
the implementation lands.

No test touches the real network: the single egress point
``app.llm_client.call_structured`` is mocked in every test that reaches the
service; additionally the suite is guarded by an autouse network barrier
(``tests/conftest.py::_block_real_network``).

The seam contract this suite imposes on the T3 implementation (MANDATORY for
backend-dev — the tests do not relax it; the spec does not fix these names
literally, so the test-author picks them and documents them here):

- ``app.parse_service`` MUST import the gateway at module level (``from . import
  llm_client``) and call it as ``llm_client.call_structured(...)`` (attribute
  access at call time) — NOT ``from .llm_client import call_structured``.
  Otherwise the tests cannot substitute the call via
  ``monkeypatch.setattr(parse_service.llm_client, "call_structured", fake)``.
  The same pattern is already used in the project for ``config``
  (``app/repo/notes.py``: ``from .. import config`` + ``config.ATTR``).
- ``app.parse_service.ParseResult`` (pydantic v2): ``suggested_deal_id: int |
  None``, ``note_type: Literal['status','task','agreement','reminder']``,
  ``confidence: float``, ``draft_text: str``. The ``confidence`` field must NOT
  be hard-validated by a pydantic constraint (``ge=0, le=1``) at the schema
  level: clamping to [0, 1] is a service post-validation, not a schema refusal.
  If the schema itself rejected out-of-range values, ``call_structured`` could
  never return such an object and the clamping would become dead code
  (see ``test_parse_result_schema_allows_out_of_range_confidence_for_post_validation``).
- ``app.parse_service.build_prompt_payload(active_deals: list[dict], note_text:
  str, images: list[tuple[str, bytes]]) -> tuple[str, list[tuple[str, bytes]],
  int]`` — a pure function (no I/O), returns ``(prompt_text, images,
  skipped_count)``. The ``active_deals`` items are deal dicts ALREADY enriched
  with a ``"stage"`` key (the stage name, computed by the service before calling
  the builder); the dict may (and in the tests will) contain arbitrary other
  deal columns (``rate``, ``jurisdiction``, ``drive_folder_url``, etc.) — the
  builder MUST ignore everything except the five allowed fields.
- ``app.parse_service._deal_prompt_fields(deal: dict) -> dict`` — a MANDATORY
  module-level (not nested) pure helper, used by ``build_prompt_payload`` for
  each deal: returns a dict with EXACTLY the keys ``{"id", "title", "company",
  "partner", "stage"}``. This is the "intermediate per-deal representation of the
  builder" that criterion 2 of the spec refers to — tested directly by a
  structural comparison of the key set (not by substring), so you cannot add an
  extra field and still pass the test.
- ``app.parse_service.parse_note(conn: sqlite3.Connection, note_id: int) ->
  tuple[ParseResult, int]`` — the service entry point. Returns
  ``(result, skipped_images)`` where ``result`` is an ALREADY post-validated
  ``ParseResult`` (``suggested_deal_id`` outside the active id list → ``None``;
  ``confidence`` outside [0,1] → clamped), and ALREADY written to the DB via
  ``app.repo.parsing.save_suggestion``. It implements: (a) ``search_deals(conn,
  q="")`` + mapping ``stage_id -> stages.name`` under the ``"stage"`` key; (b)
  reading the note text truncated to ``config.LLM_NOTE_TEXT_MAX_CHARS`` BEFORE
  calling ``build_prompt_payload`` (the builder receives the already-truncated
  text — designed this way in the spec: "substitutes ... the already-truncated
  note text"); (c) loading the bytes of the note's image attachments from
  ``attachments`` + ``config.ATTACHMENTS_DIR / stored_name``; (d) calling
  ``llm_client.call_structured(prompt_text=..., images=..., response_model=
  ParseResult, purpose="parse_note")``.
- ``app.repo.parsing.save_suggestion(conn, note_id, result: ParseResult) ->
  None`` — writes ONLY ``suggested_deal_id``, ``suggested_note_type``,
  ``llm_confidence``, ``llm_draft``, ``llm_status='suggested'``; it does not
  touch ``deal_id``/``status``/``note_type``.
- The prompt files live at ``config.PROJECT_ROOT / "prompts" / "parse_note.md"``
  and ``config.PROJECT_ROOT / "prompts" / "parse_examples.md"`` (the same
  ``PROJECT_ROOT`` that ``app/config.py`` computes) and are re-read on every
  ``parse_note`` call (design decision 7 of the spec).
  ``prompts/parse_examples.md`` MUST format each of exactly 3 examples with a
  level-two markdown header (a line like ``## ...``) — this is the test-author's
  structural contract for checking "exactly 3 examples" without depending on the
  content.

The test that edits ``prompts/parse_note.md`` (criterion 8, "read on call")
temporarily edits the REAL file in the repository (the only sensible way to
verify reading from a fixed path without mocking the filesystem) and restores
the original content (or deletes the file if it did not exist) in ``finally`` —
regardless of the test outcome. The test is not safe for parallel execution
(``pytest-xdist``); the test project is not run in parallel.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass, field

import pytest

from helpers import _seed_deal

# ---------------------------------------------------------------------------
# Import helpers (deferred — the modules app.parse_service/app.repo.parsing do
# not exist yet; deferred import keeps collect syntactically valid).
# ---------------------------------------------------------------------------


def _parse_service(initialized_db):
    import app.parse_service as parse_service_module

    return parse_service_module


def _parsing_repo(initialized_db):
    from app.repo import parsing as parsing_repo_module

    return parsing_repo_module


# ---------------------------------------------------------------------------
# DB seeding helpers going straight through sqlite3 (no dependency on the repo
# details of other steps — full control over all columns, including the "noise"
# deal fields that must not reach the prompt).
# ---------------------------------------------------------------------------

_NOW = "2026-01-01T00:00:00"


def _seed_note(
    conn,
    *,
    body,
    status="inbox",
    deal_id=None,
    note_type=None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO notes (body, status, deal_id, note_type, created_at)
        VALUES (?,?,?,?,?)
        """,
        (body, status, deal_id, note_type, _NOW),
    )
    conn.commit()
    return cur.lastrowid


def _seed_attachment(
    conn, config_module, note_id, data: bytes, mime_type, original_name="file.bin"
) -> int:
    stored_name = uuid.uuid4().hex
    path = config_module.ATTACHMENTS_DIR / stored_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    cur = conn.execute(
        """
        INSERT INTO attachments (note_id, original_name, stored_name, mime_type, size_bytes, created_at)
        VALUES (?,?,?,?,?,?)
        """,
        (note_id, original_name, stored_name, mime_type, len(data), _NOW),
    )
    conn.commit()
    return cur.lastrowid


@dataclass
class _CallStructuredSpy:
    """Fake ``llm_client.call_structured``: records the kwargs of each call and
    always returns a preset ``result`` (a ready ``ParseResult``).
    """

    result: object
    calls: list = field(default_factory=list)

    def __call__(self, *, prompt_text, images, response_model, purpose="parse_note"):
        self.calls.append(
            {
                "prompt_text": prompt_text,
                "images": images,
                "response_model": response_model,
                "purpose": purpose,
            }
        )
        return self.result


def _patch_gateway(monkeypatch, parse_service, result):
    spy = _CallStructuredSpy(result=result)
    monkeypatch.setattr(parse_service.llm_client, "call_structured", spy)
    return spy


# ---------------------------------------------------------------------------
# Criterion 1 (integration): a full pass through the service with a mocked
# gateway, no auto-assignment — "a suggestion != a change".
# ---------------------------------------------------------------------------


def test_parse_note_end_to_end_with_mocked_gateway_succeeds(
    initialized_db, config, sqlite_conn, monkeypatch
):
    parse_service = _parse_service(initialized_db)
    # The stage is looked up by name, not hardcoded by id: "In Progress" is one
    # of the six generic seeded stages and is non-terminal.
    active_stage_id = sqlite_conn.execute(
        "SELECT id FROM stages WHERE name = 'In Progress'"
    ).fetchone()[0]
    deal_id = _seed_deal(
        sqlite_conn,
        title="Acme Corporation",
        company="Acme",
        partner="John Smith",
        stage_id=active_stage_id,
    )
    note_id = _seed_note(sqlite_conn, body="Discussed the contract with the partner, waiting on documents")

    spy = _patch_gateway(
        monkeypatch,
        parse_service,
        parse_service.ParseResult(
            suggested_deal_id=deal_id,
            note_type="status",
            confidence=0.88,
            draft_text="Partner confirmed the contract",
        ),
    )

    result, skipped_images = parse_service.parse_note(sqlite_conn, note_id)

    assert len(spy.calls) == 1
    assert isinstance(result, parse_service.ParseResult)
    assert result.suggested_deal_id == deal_id
    assert result.note_type == "status"
    assert skipped_images == 0

    row = sqlite_conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    assert row["llm_status"] == "suggested"
    assert row["suggested_deal_id"] == deal_id
    assert row["suggested_note_type"] == "status"
    assert row["llm_confidence"] == pytest.approx(0.88)
    assert row["llm_draft"] == "Partner confirmed the contract"
    # Key invariant: parse NEVER attaches or changes the type itself.
    assert row["deal_id"] is None
    assert row["status"] == "inbox"
    assert row["note_type"] is None


# ---------------------------------------------------------------------------
# Criterion 2: data minimization — a structural whitelist.
# ---------------------------------------------------------------------------


def test_deal_prompt_fields_returns_exactly_five_allowed_keys(initialized_db):
    parse_service = _parse_service(initialized_db)
    deal = {
        "id": 42,
        "title": "Item A",
        "company": "Acme Corporation",
        "partner": "John Smith",
        "stage": "Review",
        # Noise fields — must not leak into the helper's result:
        "rate": 12345.0,
        "jurisdiction": "US",
        "waiting_on": "counterparty",
        "description": "secret description",
        "stage_id": 5,
        "drive_folder_url": "https://drive.example/secret",
        "closed_at": None,
        "created_at": _NOW,
        "stage_entered_at": _NOW,
        "last_activity_at": _NOW,
    }

    fields = parse_service._deal_prompt_fields(deal)

    assert set(fields.keys()) == {"id", "title", "company", "partner", "stage"}
    assert fields["id"] == 42
    assert fields["title"] == "Item A"
    assert fields["company"] == "Acme Corporation"
    assert fields["partner"] == "John Smith"
    assert fields["stage"] == "Review"


def test_build_prompt_payload_excludes_forbidden_deal_fields_from_prompt_text(
    initialized_db,
):
    parse_service = _parse_service(initialized_db)
    deal = {
        "id": 7,
        "title": "Test Item",
        "company": "Example Company",
        "partner": "Partner John",
        "stage": "Review",
        "rate": 123456.78,
        "jurisdiction": "MARKER_JURISDICTION_777",
        "waiting_on": "MARKER_WAITING_777",
        "description": "MARKER_DESCRIPTION_SECRET_777",
        "stage_id": 5,
        "drive_folder_url": "https://drive.example/secret-folder-777",
        "closed_at": None,
        "created_at": _NOW,
        "stage_entered_at": _NOW,
        "last_activity_at": _NOW,
    }

    prompt_text, images, skipped = parse_service.build_prompt_payload(
        [deal], "note text to parse", []
    )

    assert images == []
    assert skipped == 0
    for forbidden in (
        "123456.78",
        "MARKER_JURISDICTION_777",
        "MARKER_WAITING_777",
        "MARKER_DESCRIPTION_SECRET_777",
        "OPS-777",
        "OPS-778",
        "secret-folder-777",
    ):
        assert forbidden not in prompt_text, (
            f"{forbidden!r} must not be in prompt_text"
        )

    assert "Test Item" in prompt_text
    assert "Example Company" in prompt_text
    assert "Partner John" in prompt_text
    assert "Review" in prompt_text
    assert "note text to parse" in prompt_text


def test_parse_note_prompt_excludes_other_notes_and_terminal_deals(
    initialized_db, config, sqlite_conn, monkeypatch
):
    """Service level: the active deal is present (by stage name, not by numeric
    stage_id), the terminal one ("Done") is absent; notes other than the one
    being parsed are also absent from prompt_text."""
    parse_service = _parse_service(initialized_db)

    active_deal_id = _seed_deal(
        sqlite_conn,
        title="Active Item Alpha",
        company="Alpha LLC",
        partner="Partner Alpha",
        stage_id=1,  # "Backlog", non-terminal
    )
    _seed_deal(
        sqlite_conn,
        title="Closed Item Omega",
        company="Omega LLC",
        partner="Partner Omega",
        stage_id=6,  # "Done", terminal
    )

    target_note_id = _seed_note(
        sqlite_conn, body="Text of the note being parsed MARKER_CURRENT"
    )
    _seed_note(
        sqlite_conn,
        body="OTHER_NOTE_MUST_BE_HIDDEN",
        deal_id=active_deal_id,
        status="attached",
    )

    spy = _patch_gateway(
        monkeypatch,
        parse_service,
        parse_service.ParseResult(
            suggested_deal_id=None, note_type="task", confidence=0.5, draft_text="d"
        ),
    )

    parse_service.parse_note(sqlite_conn, target_note_id)

    prompt_text = spy.calls[0]["prompt_text"]
    assert "MARKER_CURRENT" in prompt_text
    assert "Active Item Alpha" in prompt_text
    assert str(active_deal_id) in prompt_text
    assert "Backlog" in prompt_text  # stage name, not the numeric stage_id

    assert "Closed Item Omega" not in prompt_text
    assert "Omega LLC" not in prompt_text
    assert "OTHER_NOTE_MUST_BE_HIDDEN" not in prompt_text


# ---------------------------------------------------------------------------
# Criterion 3: images (builder + service loading of attachment bytes).
# ---------------------------------------------------------------------------


def test_build_prompt_payload_filters_invalid_and_oversized_images(
    initialized_db, config, monkeypatch
):
    parse_service = _parse_service(initialized_db)
    monkeypatch.setattr(config, "LLM_IMAGE_MAX_BYTES", 500)

    valid_png = b"P" * 100
    oversized_png = b"O" * 600
    pdf_bytes = b"D" * 10
    svg_bytes = b"S" * 10

    images_in = [
        ("image/png", valid_png),
        ("image/png", oversized_png),
        ("application/pdf", pdf_bytes),
        ("image/svg+xml", svg_bytes),
    ]

    prompt_text, images_out, skipped = parse_service.build_prompt_payload(
        [], "note text", images_in
    )

    assert images_out == [("image/png", valid_png)]
    assert skipped == 3


def test_build_prompt_payload_keeps_first_n_images_when_over_count_limit(
    initialized_db, config, monkeypatch
):
    parse_service = _parse_service(initialized_db)
    monkeypatch.setattr(config, "LLM_IMAGE_MAX_COUNT", 2)
    imgs = [("image/png", f"img{i}".encode()) for i in range(5)]

    prompt_text, images_out, skipped = parse_service.build_prompt_payload(
        [], "text", imgs
    )

    assert images_out == imgs[:2]
    assert skipped == 3


def test_parse_note_loads_real_attachment_bytes_and_filters_per_design(
    initialized_db, config, sqlite_conn, monkeypatch
):
    """The service reads the real attachment bytes from disk (config.ATTACHMENTS_DIR
    / stored_name), not fields of the note dict; the size is measured from the
    actual byte length."""
    parse_service = _parse_service(initialized_db)
    monkeypatch.setattr(config, "LLM_IMAGE_MAX_BYTES", 1000)
    monkeypatch.setattr(config, "LLM_IMAGE_MAX_COUNT", 4)

    deal_id = _seed_deal(sqlite_conn, title="Item", stage_id=1)
    note_id = _seed_note(sqlite_conn, body="note with a screenshot")

    valid_png = b"\x89PNG_VALID" + b"0" * 100  # << 1000 bytes
    oversized_png = b"\x89PNG_OVERSIZED" + b"0" * 2000  # > 1000 bytes
    pdf_bytes = b"%PDF-1.4 not an image"
    svg_bytes = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"

    _seed_attachment(sqlite_conn, config, note_id, valid_png, "image/png", "shot1.png")
    _seed_attachment(
        sqlite_conn, config, note_id, oversized_png, "image/png", "shot2.png"
    )
    _seed_attachment(
        sqlite_conn, config, note_id, pdf_bytes, "application/pdf", "doc.pdf"
    )
    _seed_attachment(
        sqlite_conn, config, note_id, svg_bytes, "image/svg+xml", "vector.svg"
    )

    spy = _patch_gateway(
        monkeypatch,
        parse_service,
        parse_service.ParseResult(
            suggested_deal_id=deal_id, note_type="task", confidence=0.8, draft_text="d"
        ),
    )

    result, skipped_images = parse_service.parse_note(sqlite_conn, note_id)

    assert skipped_images == 3
    sent_images = spy.calls[0]["images"]
    assert len(sent_images) == 1
    mime_type, data = sent_images[0]
    assert mime_type == "image/png"
    assert data == valid_png  # bytes not corrupted


def test_parse_note_returns_skipped_images_count_for_endpoint(
    initialized_db, config, sqlite_conn, monkeypatch
):
    """`skipped_images` is available to the caller (passed through to T4-2) — a
    separate check for the scenario of exceeding LLM_IMAGE_MAX_COUNT (default=4)
    with real files."""
    parse_service = _parse_service(initialized_db)
    note_id = _seed_note(sqlite_conn, body="five screenshots")
    for i in range(5):
        _seed_attachment(
            sqlite_conn, config, note_id, f"img{i}".encode(), "image/png", f"s{i}.png"
        )

    spy = _patch_gateway(
        monkeypatch,
        parse_service,
        parse_service.ParseResult(
            suggested_deal_id=None, note_type="task", confidence=0.5, draft_text="d"
        ),
    )

    _, skipped_images = parse_service.parse_note(sqlite_conn, note_id)

    assert skipped_images == 1  # LLM_IMAGE_MAX_COUNT defaults to 4 out of 5
    assert len(spy.calls[0]["images"]) == 4


# ---------------------------------------------------------------------------
# Criterion 4: truncating the note text before sending.
# ---------------------------------------------------------------------------


def test_parse_note_truncates_long_note_body_before_prompt(
    initialized_db, config, sqlite_conn, monkeypatch
):
    parse_service = _parse_service(initialized_db)
    monkeypatch.setattr(config, "LLM_NOTE_TEXT_MAX_CHARS", 50)

    long_body = ("F" * 50) + "TAIL_THAT_SHOULD_BE_TRUNCATED"
    note_id = _seed_note(sqlite_conn, body=long_body)

    spy = _patch_gateway(
        monkeypatch,
        parse_service,
        parse_service.ParseResult(
            suggested_deal_id=None, note_type="task", confidence=0.5, draft_text="d"
        ),
    )

    parse_service.parse_note(sqlite_conn, note_id)

    prompt_text = spy.calls[0]["prompt_text"]
    assert "TAIL_THAT_SHOULD_BE_TRUNCATED" not in prompt_text
    assert "F" * 50 in prompt_text


# ---------------------------------------------------------------------------
# Criterion 5: post-validation (id outside the active list -> None; confidence
# clamped to [0, 1]).
# ---------------------------------------------------------------------------


def test_parse_note_nulls_suggested_deal_id_not_in_active_list(
    initialized_db, sqlite_conn, monkeypatch
):
    parse_service = _parse_service(initialized_db)
    _seed_deal(sqlite_conn, title="The only active one", stage_id=1)
    note_id = _seed_note(sqlite_conn, body="text")

    bogus_id = 999999
    _patch_gateway(
        monkeypatch,
        parse_service,
        parse_service.ParseResult(
            suggested_deal_id=bogus_id, note_type="task", confidence=0.9, draft_text="d"
        ),
    )

    result, _ = parse_service.parse_note(sqlite_conn, note_id)

    assert result.suggested_deal_id is None
    row = sqlite_conn.execute(
        "SELECT suggested_deal_id, llm_status FROM notes WHERE id = ?", (note_id,)
    ).fetchone()
    assert row["suggested_deal_id"] is None
    assert row["llm_status"] == "suggested"  # not an error, a normal outcome


def test_parse_note_nulls_suggested_deal_id_when_deal_is_terminal(
    initialized_db, sqlite_conn, monkeypatch
):
    """The deal exists in the DB but in a terminal stage — so it is not in the
    active list passed to the model, and is treated as an invalid id."""
    parse_service = _parse_service(initialized_db)
    terminal_id = _seed_deal(sqlite_conn, title="Closed", stage_id=6)
    note_id = _seed_note(sqlite_conn, body="text")

    _patch_gateway(
        monkeypatch,
        parse_service,
        parse_service.ParseResult(
            suggested_deal_id=terminal_id,
            note_type="task",
            confidence=0.9,
            draft_text="d",
        ),
    )

    result, _ = parse_service.parse_note(sqlite_conn, note_id)

    assert result.suggested_deal_id is None


def test_parse_note_clamps_confidence_above_one(
    initialized_db, sqlite_conn, monkeypatch
):
    parse_service = _parse_service(initialized_db)
    note_id = _seed_note(sqlite_conn, body="text")

    _patch_gateway(
        monkeypatch,
        parse_service,
        parse_service.ParseResult(
            suggested_deal_id=None, note_type="task", confidence=5.0, draft_text="d"
        ),
    )

    result, _ = parse_service.parse_note(sqlite_conn, note_id)

    assert result.confidence == 1.0
    row = sqlite_conn.execute(
        "SELECT llm_confidence FROM notes WHERE id = ?", (note_id,)
    ).fetchone()
    assert row["llm_confidence"] == pytest.approx(1.0)


def test_parse_note_clamps_confidence_below_zero(
    initialized_db, sqlite_conn, monkeypatch
):
    parse_service = _parse_service(initialized_db)
    note_id = _seed_note(sqlite_conn, body="text")

    _patch_gateway(
        monkeypatch,
        parse_service,
        parse_service.ParseResult(
            suggested_deal_id=None, note_type="task", confidence=-3.0, draft_text="d"
        ),
    )

    result, _ = parse_service.parse_note(sqlite_conn, note_id)

    assert result.confidence == 0.0
    row = sqlite_conn.execute(
        "SELECT llm_confidence FROM notes WHERE id = ?", (note_id,)
    ).fetchone()
    assert row["llm_confidence"] == pytest.approx(0.0)


def test_parse_result_schema_allows_out_of_range_confidence_for_post_validation(
    initialized_db,
):
    """Schema contract: ``ParseResult`` does NOT reject confidence outside [0,1]
    at the pydantic level — otherwise the clamping in the service would be
    unreachable code (the model could never return such an object). Clamping is
    the responsibility of ``parse_note`` post-validation, not of the schema."""
    parse_service = _parse_service(initialized_db)

    over = parse_service.ParseResult(
        suggested_deal_id=None, note_type="task", confidence=1.5, draft_text="d"
    )
    assert over.confidence == 1.5

    under = parse_service.ParseResult(
        suggested_deal_id=None, note_type="reminder", confidence=-0.5, draft_text="d"
    )
    assert under.confidence == -0.5


# ---------------------------------------------------------------------------
# Criterion 6: a null suggestion is valid (not an error).
# ---------------------------------------------------------------------------


def test_save_suggestion_writes_null_suggested_deal_id_as_valid(
    initialized_db, sqlite_conn
):
    parsing_repo = _parsing_repo(initialized_db)
    parse_service = _parse_service(initialized_db)
    note_id = _seed_note(sqlite_conn, body="junk note")
    result = parse_service.ParseResult(
        suggested_deal_id=None, note_type="task", confidence=0.2, draft_text="draft"
    )

    parsing_repo.save_suggestion(sqlite_conn, note_id, result)

    row = sqlite_conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    assert row["suggested_deal_id"] is None
    assert row["llm_status"] == "suggested"
    assert row["suggested_note_type"] == "task"
    assert row["llm_confidence"] == pytest.approx(0.2)
    assert row["llm_draft"] == "draft"


def test_parse_note_accepts_null_suggestion_from_model_as_valid_outcome(
    initialized_db, sqlite_conn, monkeypatch
):
    """The model itself, normally, returned suggested_deal_id=null (not as a
    result of post-validation, but as a direct answer) — this must not raise."""
    parse_service = _parse_service(initialized_db)
    note_id = _seed_note(sqlite_conn, body="unclear note")

    _patch_gateway(
        monkeypatch,
        parse_service,
        parse_service.ParseResult(
            suggested_deal_id=None, note_type="reminder", confidence=0.3, draft_text="d"
        ),
    )

    result, skipped_images = parse_service.parse_note(sqlite_conn, note_id)

    assert result.suggested_deal_id is None
    row = sqlite_conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    assert row["llm_status"] == "suggested"
    assert row["suggested_deal_id"] is None


# ---------------------------------------------------------------------------
# Criterion 7: save_suggestion does not touch confirmed fields.
# ---------------------------------------------------------------------------


def test_save_suggestion_does_not_touch_deal_id_or_status_on_inbox_note(
    initialized_db, sqlite_conn
):
    parsing_repo = _parsing_repo(initialized_db)
    parse_service = _parse_service(initialized_db)
    note_id = _seed_note(
        sqlite_conn, body="inbox note", status="inbox", deal_id=None, note_type=None
    )
    deal_id = _seed_deal(sqlite_conn, title="Candidate", stage_id=1)
    result = parse_service.ParseResult(
        suggested_deal_id=deal_id,
        note_type="agreement",
        confidence=0.95,
        draft_text="draft text",
    )

    parsing_repo.save_suggestion(sqlite_conn, note_id, result)

    row = sqlite_conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    assert row["deal_id"] is None
    assert row["status"] == "inbox"
    assert row["note_type"] is None
    assert row["suggested_deal_id"] == deal_id
    assert row["suggested_note_type"] == "agreement"
    assert row["llm_confidence"] == pytest.approx(0.95)
    assert row["llm_draft"] == "draft text"
    assert row["llm_status"] == "suggested"


def test_save_suggestion_preserves_preexisting_note_type(initialized_db, sqlite_conn):
    """A note_type already set (e.g. by the keyboard triage of Step 1) must NOT
    be overwritten by suggested_note_type."""
    parsing_repo = _parsing_repo(initialized_db)
    parse_service = _parse_service(initialized_db)
    note_id = _seed_note(
        sqlite_conn, body="already labeled note", note_type="reminder"
    )
    result = parse_service.ParseResult(
        suggested_deal_id=None, note_type="task", confidence=0.4, draft_text="d"
    )

    parsing_repo.save_suggestion(sqlite_conn, note_id, result)

    row = sqlite_conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    assert row["note_type"] == "reminder"  # untouched
    assert row["suggested_note_type"] == "task"  # suggestion recorded separately


# ---------------------------------------------------------------------------
# Criterion 8: the prompt files exist, are in English, and are read on call;
# parse_examples.md — exactly 3 neutral examples.
# ---------------------------------------------------------------------------


def test_prompt_files_exist(config):
    assert (config.PROJECT_ROOT / "prompts" / "parse_note.md").exists(), (
        "prompts/parse_note.md must exist (T3)"
    )
    assert (config.PROJECT_ROOT / "prompts" / "parse_examples.md").exists(), (
        "prompts/parse_examples.md must exist (T3)"
    )


def test_parse_note_prompt_file_is_in_english(config):
    path = config.PROJECT_ROOT / "prompts" / "parse_note.md"
    text = path.read_text(encoding="utf-8")
    assert "System instructions" in text
    assert "suggested_deal_id" in text


def test_parse_examples_file_has_exactly_three_examples(config):
    """Structural contract: each example is formatted as a level-two markdown
    header ('## ...'). Exactly 3 such headers."""
    path = config.PROJECT_ROOT / "prompts" / "parse_examples.md"
    text = path.read_text(encoding="utf-8")
    headers = re.findall(r"^##\s+.+$", text, flags=re.MULTILINE)
    assert len(headers) == 3, (
        "prompts/parse_examples.md must contain EXACTLY 3 examples, each "
        "formatted as a level-two markdown header ('## ...') "
        f"found: {len(headers)}"
    )


def test_prompt_files_are_read_fresh_on_each_call_not_cached(
    initialized_db, config, sqlite_conn, monkeypatch
):
    """Design decision 7: editing prompts/parse_note.md between two parse_note
    calls changes the prompt_text of the second call — the file is not cached in
    process memory. The test edits the REAL repository file and restores the
    original state (or deletes it if the file did not exist) in finally."""
    parse_service = _parse_service(initialized_db)
    prompt_path = config.PROJECT_ROOT / "prompts" / "parse_note.md"
    existed_before = prompt_path.exists()
    original_bytes = prompt_path.read_bytes() if existed_before else None

    try:
        marker_a = "MARKER_PROMPT_VERSION_A_11111"
        marker_b = "MARKER_PROMPT_VERSION_B_22222"

        deal_id = _seed_deal(sqlite_conn, title="Item", stage_id=1)
        note_id_1 = _seed_note(sqlite_conn, body="first note")
        note_id_2 = _seed_note(sqlite_conn, body="second note")

        prompt_path.parent.mkdir(parents=True, exist_ok=True)

        spy = _patch_gateway(
            monkeypatch,
            parse_service,
            parse_service.ParseResult(
                suggested_deal_id=deal_id,
                note_type="task",
                confidence=0.8,
                draft_text="d",
            ),
        )

        prompt_path.write_text(
            f"# System instructions\n{marker_a}\n", encoding="utf-8"
        )
        parse_service.parse_note(sqlite_conn, note_id_1)
        assert marker_a in spy.calls[0]["prompt_text"]

        prompt_path.write_text(
            f"# System instructions\n{marker_b}\n", encoding="utf-8"
        )
        parse_service.parse_note(sqlite_conn, note_id_2)
        assert len(spy.calls) == 2
        assert marker_b in spy.calls[1]["prompt_text"]
        assert marker_a not in spy.calls[1]["prompt_text"]
    finally:
        if existed_before:
            prompt_path.write_bytes(original_bytes)
        else:
            prompt_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Criterion 9 (light check; full review is the reviewer-security task):
# the service must not import HTTP transport libraries directly — all outgoing
# traffic must go through app/llm_client.py.
# ---------------------------------------------------------------------------


def test_parse_service_source_has_no_direct_http_transport_imports(config):
    path = config.PROJECT_ROOT / "app" / "parse_service.py"
    assert path.exists(), "app/parse_service.py must exist"
    source = path.read_text(encoding="utf-8")

    forbidden_imports = (
        "import requests",
        "import httpx",
        "import urllib.request",
        "import http.client",
        "import aiohttp",
        "import socket",
    )
    for forbidden in forbidden_imports:
        assert forbidden not in source, (
            f"app/parse_service.py must not contain {forbidden!r} — all outgoing "
            "traffic must go through app/llm_client.py (full diff review is the "
            "reviewer-security task)"
        )


# ---------------------------------------------------------------------------
# T11 regression: atomicity of confirm_suggestion / change_suggestion.
#
# Invariant: attaching to a deal (deal_id + status='attached') and updating
# llm_status must be committed in ONE transaction — either both or neither.
# Before the fix attach_note did its own conn.commit(), so a failure on the
# second UPDATE (llm_status) left an inconsistent state on disk: the note was
# already attached (deal_id set, status='attached') but llm_status was still
# 'suggested' (looking both attached and awaiting confirmation).
# ---------------------------------------------------------------------------


class _FailOnSql:
    """A sqlite3 connection proxy: delegates everything, but crashes execute on
    SQL containing ``marker`` — simulating a crash between two writes."""

    def __init__(self, real, marker):
        self._real = real
        self._marker = marker

    def execute(self, sql, *args, **kwargs):
        if self._marker in sql:
            raise sqlite3.OperationalError("simulated crash before second write")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _seed_suggested_note(conn, deal_id):
    note_id = _seed_note(conn, body="awaiting confirmation", status="inbox")
    conn.execute(
        "UPDATE notes SET suggested_deal_id = ?, suggested_note_type = 'task', "
        "llm_status = 'suggested' WHERE id = ?",
        (deal_id, note_id),
    )
    conn.commit()
    return note_id


def test_confirm_suggestion_is_atomic_with_llm_status(
    initialized_db, sqlite_conn, db_path
):
    parsing_repo = _parsing_repo(initialized_db)
    deal_id = _seed_deal(sqlite_conn, title="Candidate", stage_id=1)
    note_id = _seed_suggested_note(sqlite_conn, deal_id)

    proxy = _FailOnSql(sqlite_conn, "llm_status = 'confirmed'")
    with pytest.raises(sqlite3.OperationalError):
        parsing_repo.confirm_suggestion(proxy, note_id)

    # Read through a FRESH connection: we see only what was committed to disk.
    verify = sqlite3.connect(str(db_path))
    verify.row_factory = sqlite3.Row
    try:
        row = verify.execute(
            "SELECT deal_id, status, llm_status FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
    finally:
        verify.close()

    # The attach must NOT stay half-applied: since the llm_status write failed,
    # deal_id/status were rolled back too (attach_note no longer commits itself).
    assert row["deal_id"] is None
    assert row["status"] == "inbox"
    assert row["llm_status"] == "suggested"


def test_change_suggestion_is_atomic_with_llm_status(
    initialized_db, sqlite_conn, db_path
):
    parsing_repo = _parsing_repo(initialized_db)
    suggested_deal_id = _seed_deal(sqlite_conn, title="Suggested", stage_id=1)
    chosen_deal_id = _seed_deal(sqlite_conn, title="Chosen", stage_id=1)
    note_id = _seed_suggested_note(sqlite_conn, suggested_deal_id)

    proxy = _FailOnSql(sqlite_conn, "llm_status = 'rejected'")
    with pytest.raises(sqlite3.OperationalError):
        parsing_repo.change_suggestion(proxy, note_id, chosen_deal_id)

    verify = sqlite3.connect(str(db_path))
    verify.row_factory = sqlite3.Row
    try:
        row = verify.execute(
            "SELECT deal_id, status, llm_status FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
    finally:
        verify.close()

    assert row["deal_id"] is None
    assert row["status"] == "inbox"
    assert row["llm_status"] == "suggested"
