"""T3 tests: OCR via the Gemini parse path (``app/parse_service.py`` +
``app/repo/parsing.py``).

Acceptance criteria come from docs/specs/006-custom-improvements.md, task T3
("OCR backend via the Gemini parse path"). The tests are written against the
spec, not the implementation: at the time of writing, ``ParseResult.extracted_text``
and writing ``ocr_text`` in ``save_suggestion`` do not yet exist — this is the
expected TDD state (the file collects, the tests fail until T3 is implemented).

No test touches the real network: the single egress point
``app.llm_client.call_structured`` is mocked in every test that reaches the
service (the same pattern as ``tests/test_parse_service.py``:
``_CallStructuredSpy`` + ``monkeypatch.setattr(parse_service.llm_client,
"call_structured", spy)``), plus the autouse network barrier
(``tests/conftest.py::_block_real_network``).

The project lesson ("mocked gateway tests miss real bugs" —
``feedback_gateway_mocked_tests_miss_real_bugs``) is applied like this:
assertions on ``save_suggestion``/the end-to-end ``parse_note`` go through a REAL
sqlite3 connection (the ``sqlite_conn`` fixture) and a real 2-column
``notes_fts`` (created by migration 006, T1, already landed) — a broken column
name or wrong SQL in ``save_suggestion`` will actually fail the test rather than
silently pass around the mock. Only the network to Gemini itself (the single
egress point) is mocked, as everywhere else in the suite.

The fixture ``tests/fixtures/ocr_ru.png`` is a valid, decodable PNG (header +
IHDR + IDAT/zlib + IEND verified at generation time), but WITHOUT rendered
glyphs: the project has no text/font rendering library (Pillow etc.), and adding
a new dependency is forbidden by the spec's Non-goals ("No new third-party
dependencies"). The target string is embedded as ``iTXt`` metadata (UTF-8) of the
file — enough for the automated test below (the gateway is mocked, no real OCR
happens), but NOT enough for the manual check described in the spec's Risks under
``LLM_SMOKE=1`` (a real Gemini must read the text off the image) — for that, a
human will need to replace this placeholder with a real photo/screenshot of
readable text before the manual run. See the test-author's final report for the
same caveat.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

_NOW = "2026-01-01T00:00:00"


# ---------------------------------------------------------------------------
# Import helpers (deferred — as in tests/test_parse_service.py).
# ---------------------------------------------------------------------------


def _parse_service(initialized_db):
    import app.parse_service as parse_service_module

    return parse_service_module


def _parsing_repo(initialized_db):
    from app.repo import parsing as parsing_repo_module

    return parsing_repo_module


# ---------------------------------------------------------------------------
# Helpers that seed the DB directly through sqlite3 (an independent copy modeled
# on tests/test_parse_service.py — every test file is self-contained).
# ---------------------------------------------------------------------------


def _seed_note(
    conn,
    *,
    body="",
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
    conn, config_module, note_id, data: bytes, mime_type, original_name="file.png"
) -> int:
    import uuid

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
    """Fake ``llm_client.call_structured`` — the same pattern as in
    ``tests/test_parse_service.py``: it records the call's kwargs and always
    returns a preset ``result`` (a ready ``ParseResult``)."""

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


# ===========================================================================
# Criterion 1: ParseResult accepts extracted_text (None/""/non-empty string)
# and note_type="info".
# ===========================================================================


def test_parse_result_accepts_extracted_text_none(initialized_db):
    parse_service = _parse_service(initialized_db)

    result = parse_service.ParseResult(
        suggested_deal_id=None,
        note_type="task",
        confidence=0.5,
        draft_text="d",
        extracted_text=None,
    )

    assert result.extracted_text is None


def test_parse_result_extracted_text_defaults_to_none_when_omitted(initialized_db):
    """``extracted_text: str | None = None`` — the field is optional and defaults
    to None if the constructor is called without it (e.g. by old code / old
    mocks)."""
    parse_service = _parse_service(initialized_db)

    result = parse_service.ParseResult(
        suggested_deal_id=None, note_type="task", confidence=0.5, draft_text="d"
    )

    assert result.extracted_text is None


def test_parse_result_accepts_extracted_text_empty_string(initialized_db):
    parse_service = _parse_service(initialized_db)

    result = parse_service.ParseResult(
        suggested_deal_id=None,
        note_type="task",
        confidence=0.5,
        draft_text="d",
        extracted_text="",
    )

    assert result.extracted_text == ""


def test_parse_result_accepts_extracted_text_nonempty_string(initialized_db):
    parse_service = _parse_service(initialized_db)

    result = parse_service.ParseResult(
        suggested_deal_id=None,
        note_type="task",
        confidence=0.5,
        draft_text="d",
        extracted_text="Contract signed 2026",
    )

    assert result.extracted_text == "Contract signed 2026"


def test_parse_result_accepts_note_type_info(initialized_db):
    parse_service = _parse_service(initialized_db)

    result = parse_service.ParseResult(
        suggested_deal_id=None,
        note_type="info",
        confidence=0.5,
        draft_text="d",
        extracted_text=None,
    )

    assert result.note_type == "info"


@pytest.mark.parametrize(
    "note_type", ["status", "task", "agreement", "reminder", "info"]
)
def test_parse_result_accepts_all_note_types_including_info(initialized_db, note_type):
    """Extending the Literal with the new value 'info' must not break the
    previously existing values (status/task/agreement/reminder) — T3 does not
    narrow the schema contract."""
    parse_service = _parse_service(initialized_db)

    result = parse_service.ParseResult(
        suggested_deal_id=None,
        note_type=note_type,
        confidence=0.5,
        draft_text="d",
    )

    assert result.note_type == note_type


def test_parse_result_rejects_unknown_note_type(initialized_db):
    """The Literal stays a closed set — 'bogus' is still rejected by the schema
    (extending it with 'info' does not turn the Literal into an arbitrary
    string)."""
    import pydantic

    parse_service = _parse_service(initialized_db)

    with pytest.raises(pydantic.ValidationError):
        parse_service.ParseResult(
            suggested_deal_id=None,
            note_type="bogus",
            confidence=0.5,
            draft_text="d",
        )


# ===========================================================================
# Criterion 2: save_suggestion writes result.extracted_text into notes.ocr_text,
# without touching body/deal_id/status/note_type. A real sqlite3 connection —
# a broken column name/SQL will actually fail the test.
# ===========================================================================


def test_save_suggestion_writes_extracted_text_into_ocr_text(
    initialized_db, sqlite_conn
):
    parsing_repo = _parsing_repo(initialized_db)
    parse_service = _parse_service(initialized_db)
    note_id = _seed_note(
        sqlite_conn,
        body="original note text",
        status="inbox",
        deal_id=None,
        note_type=None,
    )
    result = parse_service.ParseResult(
        suggested_deal_id=None,
        note_type="status",
        confidence=0.7,
        draft_text="draft",
        extracted_text="Contract signed 2026",
    )

    parsing_repo.save_suggestion(sqlite_conn, note_id, result)

    row = sqlite_conn.execute(
        "SELECT ocr_text, body, deal_id, status, note_type FROM notes WHERE id = ?",
        (note_id,),
    ).fetchone()
    assert row["ocr_text"] == "Contract signed 2026"
    # Text extraction does not change the user-supplied/confirmed fields.
    assert row["body"] == "original note text"
    assert row["deal_id"] is None
    assert row["status"] == "inbox"
    assert row["note_type"] is None


def test_save_suggestion_with_extracted_text_none_leaves_ocr_text_null(
    initialized_db, sqlite_conn
):
    """extracted_text=None must not become an empty string '' in the DB and must
    not cause the write to fail."""
    parsing_repo = _parsing_repo(initialized_db)
    parse_service = _parse_service(initialized_db)
    note_id = _seed_note(sqlite_conn, body="note without attachments")
    result = parse_service.ParseResult(
        suggested_deal_id=None,
        note_type="task",
        confidence=0.4,
        draft_text="d",
        extracted_text=None,
    )

    parsing_repo.save_suggestion(sqlite_conn, note_id, result)

    row = sqlite_conn.execute(
        "SELECT ocr_text FROM notes WHERE id = ?", (note_id,)
    ).fetchone()
    assert row["ocr_text"] is None


def test_save_suggestion_extracted_text_reindexes_notes_fts_via_au_trigger(
    initialized_db, sqlite_conn
):
    """Unit level (without going through parse_note/the mocked gateway):
    ``save_suggestion`` itself does an ``UPDATE notes`` -> the ``notes_au``
    trigger (Spec 006, T1, already landed) must reindex the ocr_text column in
    the real 2-column notes_fts."""
    parsing_repo = _parsing_repo(initialized_db)
    parse_service = _parse_service(initialized_db)
    note_id = _seed_note(sqlite_conn, body="")
    result = parse_service.ParseResult(
        suggested_deal_id=None,
        note_type="info",
        confidence=0.6,
        draft_text="d",
        extracted_text="Contract signed document scan",
    )

    parsing_repo.save_suggestion(sqlite_conn, note_id, result)

    rows = sqlite_conn.execute(
        "SELECT rowid FROM notes_fts WHERE notes_fts MATCH 'signed'"
    ).fetchall()
    assert note_id in {row["rowid"] for row in rows}


# ===========================================================================
# Criterion 3 (the concrete end-to-end test from the spec): image fixture +
# mocked gateway -> parse_note -> ocr_text contains the substring AND an
# FTS search for 'signed' finds this note.
# ===========================================================================


def test_ocr_fixture_image_end_to_end_populates_ocr_text_and_is_fts_searchable(
    initialized_db, config, sqlite_conn, monkeypatch, project_root
):
    parse_service = _parse_service(initialized_db)
    fixture_path = project_root / "tests" / "fixtures" / "ocr_ru.png"
    assert fixture_path.exists(), (
        "tests/fixtures/ocr_ru.png must exist (T3 fixture, see the module "
        "docstring about the stdlib-only constraint)"
    )
    image_bytes = fixture_path.read_bytes()

    note_id = _seed_note(sqlite_conn, body="document scan")
    _seed_attachment(
        sqlite_conn,
        config,
        note_id,
        image_bytes,
        "image/png",
        original_name="ocr_ru.png",
    )

    spy = _patch_gateway(
        monkeypatch,
        parse_service,
        parse_service.ParseResult(
            suggested_deal_id=None,
            note_type="info",
            confidence=0.9,
            draft_text="d",
            extracted_text="Contract signed",
        ),
    )

    result, skipped_images = parse_service.parse_note(sqlite_conn, note_id)

    assert len(spy.calls) == 1
    assert skipped_images == 0
    assert result.extracted_text == "Contract signed"

    row = sqlite_conn.execute(
        "SELECT ocr_text FROM notes WHERE id = ?", (note_id,)
    ).fetchone()
    assert row["ocr_text"] is not None
    assert "Contract signed" in row["ocr_text"]

    rows = sqlite_conn.execute(
        "SELECT rowid FROM notes_fts WHERE notes_fts MATCH 'signed'"
    ).fetchall()
    assert note_id in {r["rowid"] for r in rows}, (
        "an FTS search for 'signed' must find the note after save_suggestion "
        "wrote ocr_text and the notes_au trigger reindexed notes_fts "
        "(Spec 006, T1 landed + T3)"
    )
