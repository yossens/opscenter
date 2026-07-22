"""T4 tests: the parsing API (parse/confirm/change/reject), serializers, threshold.

Acceptance criteria source — docs/specs/003-step3-gemini-parsing.md, task T4
(section "API", criteria 1-9, and the related "Transitions" table in the
"Design" section). The tests are written to the spec, not to the implementation:
at the time of writing ``app/routers/parsing.py`` does not exist and is not
registered in ``app/main.py`` — ``POST /api/notes/{id}/parse|confirm|change|
reject`` and ``GET|PUT /api/settings/parse`` currently return 404. This is the
correct TDD state: the file collects (``--collect-only`` green), the tests fail
until the implementation lands.

``app/parse_service.py``, ``app/repo/parsing.py`` and ``app/llm_client.py``
(tasks T2/T3) are already implemented and covered by their own tests
(``tests/test_parse_service.py``, ``tests/test_llm_client.py``) — this file does
not redefine or re-check them, but uses them as ready seams.

The seam contract (mocking/seeding) that backend-dev MUST honor when
implementing T4 (the tests do not relax it):

1. **Mocking the gateway for the ``parse`` endpoint tests**: ``app.parse_service``
   imports the gateway at module level (``from . import llm_client``) and calls
   it as ``llm_client.call_structured(...)`` — attribute access at call time
   (the contract is fixed by ``tests/test_parse_service.py``). These tests
   substitute ``app.parse_service.llm_client.call_structured`` via
   ``monkeypatch.setattr`` — the whole real service (``parse_note``,
   ``build_prompt_payload``, ``save_suggestion``) runs as is, and the network is
   never opened (plus the autouse network barrier from ``tests/conftest.py``).
   The T4 router must call ``parse_service.parse_note(conn, note_id)`` (and not
   go into ``llm_client`` itself), otherwise this substitution has no effect.
2. **Seeding an "already parsed" note for confirm/change/reject**: these three
   endpoints are tested WITHOUT running the real service — the suggestion columns
   (``suggested_deal_id``, ``suggested_note_type``, ``llm_confidence``,
   ``llm_draft``, ``llm_status='suggested'``) are written directly by a SQL
   update (``_seed_suggestion`` below), bypassing
   ``app.repo.parsing.save_suggestion``/``llm_client``. This is a deliberate
   simplification: confirm/change/reject do not need to know where the
   suggestion came from, and direct SQL fully decouples these tests from the T3
   service and requires no gateway mocking for them at all.
3. **Mapping service exceptions to HTTP statuses (mandatory for the
   implementation, not just for the tests)**: ``app.parse_service.parse_note``
   raises a bare ``ValueError`` for a missing note and
   ``app.llm_client.LLMError`` on a gateway/network failure (confirmed by
   reading the ``parse_note`` signature — this does not mean the router is read;
   it is a statement of the contract the router must NOT break). The router must
   catch these TWO exception types separately: ``ValueError`` → 404, ``LLMError``
   → 502 or 503 with a ``{"detail": ...}`` body. The tests below check both
   paths.

Notes on the shape of the ``POST /api/notes/{id}/parse`` response: the spec says
"returns the serialized note with llm fields... and ``skipped_images: N``" —
interpreted literally as ONE flat JSON object: the serialized note fields
(including llm fields) at the top level PLUS a ``skipped_images`` key at the same
top level (not a nested envelope like ``{"note": ..., ...}``).

Only fixtures from ``tests/conftest.py`` are used: ``client``, ``sqlite_conn``,
``config``. The helper code (seeding deals/notes/attachments/suggestions
directly into the DB) lives locally in this file, following the pattern of
``tests/test_notes.py``/``tests/test_inbox.py``/``tests/test_parse_service.py``.

Stages are seeded per the real migration 001 seed: ``stage_id`` 1..5 are
non-terminal ("active" for the T3 data-minimization purpose), ``stage_id=6``
("Done") is terminal (see ``app/migrations/001_init.sql``).
"""

from __future__ import annotations

import uuid

import pytest

from helpers import _seed_deal

_NOW = "2026-01-01T00:00:00"
_TERMINAL_STAGE_ID = 6


# ---------------------------------------------------------------------------
# Seeding straight through sqlite3 (full control over the columns).
# ---------------------------------------------------------------------------


def _seed_note(
    conn,
    *,
    body: str = "note",
    status: str = "inbox",
    deal_id: int | None = None,
    note_type: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO notes (body, status, deal_id, note_type, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (body, status, deal_id, note_type, _NOW),
    )
    conn.commit()
    return cur.lastrowid


def _seed_attachment(
    conn,
    config_module,
    note_id: int,
    data: bytes,
    mime_type: str,
    original_name="s.png",
) -> int:
    stored_name = uuid.uuid4().hex
    path = config_module.ATTACHMENTS_DIR / stored_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    cur = conn.execute(
        """
        INSERT INTO attachments (note_id, original_name, stored_name, mime_type, size_bytes, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (note_id, original_name, stored_name, mime_type, len(data), _NOW),
    )
    conn.commit()
    return cur.lastrowid


def _seed_suggestion(
    conn,
    note_id: int,
    *,
    suggested_deal_id: int | None,
    suggested_note_type: str = "task",
    llm_confidence: float = 0.8,
    llm_draft: str = "draft",
) -> None:
    """Writes the suggestion columns directly, bypassing the service/gateway
    (see item 2 of the module docstring)."""
    conn.execute(
        """
        UPDATE notes
        SET suggested_deal_id = ?,
            suggested_note_type = ?,
            llm_confidence = ?,
            llm_draft = ?,
            llm_status = 'suggested'
        WHERE id = ?
        """,
        (suggested_deal_id, suggested_note_type, llm_confidence, llm_draft, note_id),
    )
    conn.commit()


def _note_row(conn, note_id: int):
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    assert row is not None, f"note {note_id} must exist"
    return row


def _deal_row(conn, deal_id: int):
    row = conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    assert row is not None, f"deal {deal_id} must exist"
    return row


def _app_meta(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else None


def _inbox_ids(client) -> list[int]:
    feed = client.get("/api/notes", params={"status": "inbox"}).json()
    return [n["id"] for n in feed]


def _import_parse_service():
    import app.parse_service as parse_service_module

    return parse_service_module


def _mock_gateway_success(monkeypatch, result):
    parse_service = _import_parse_service()
    monkeypatch.setattr(
        parse_service.llm_client, "call_structured", lambda **kw: result
    )
    return parse_service


def _mock_gateway_error(monkeypatch, exc: Exception):
    parse_service = _import_parse_service()

    def _raise(**kwargs):
        raise exc

    monkeypatch.setattr(parse_service.llm_client, "call_structured", _raise)
    return parse_service


# ===========================================================================
# Criterion 2: POST /api/notes/{id}/parse — success, "no auto-assignment" invariant.
# ===========================================================================


def test_parse_success_writes_suggestion_columns_and_returns_them_in_response(
    client, sqlite_conn, monkeypatch
):
    parse_service = _import_parse_service()
    deal_id = _seed_deal(sqlite_conn, title="Acme Corporation", stage_id=2)
    note_id = _seed_note(sqlite_conn, body="Discussed the contract with the partner")

    fake_result = parse_service.ParseResult(
        suggested_deal_id=deal_id,
        note_type="status",
        confidence=0.87,
        draft_text="Partner confirmed the contract",
    )
    _mock_gateway_success(monkeypatch, fake_result)

    response = client.post(f"/api/notes/{note_id}/parse")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["llm_status"] == "suggested"
    assert body["suggested_deal_id"] == deal_id
    assert body["suggested_note_type"] == "status"
    assert body["llm_confidence"] == pytest.approx(0.87)
    assert body["llm_draft"] == "Partner confirmed the contract"
    assert "skipped_images" in body
    assert body["skipped_images"] == 0

    row = _note_row(sqlite_conn, note_id)
    assert row["llm_status"] == "suggested"
    assert row["suggested_deal_id"] == deal_id
    assert row["suggested_note_type"] == "status"
    assert row["llm_confidence"] == pytest.approx(0.87)
    assert row["llm_draft"] == "Partner confirmed the contract"


def test_parse_success_does_not_auto_assign_deal_status_or_note_type(
    client, sqlite_conn, monkeypatch
):
    """Critical invariant "a suggestion != a change": after a successful parse
    the note stays unattached, and a note_type already set by a human (the
    keyboard triage of Step 1) is not overwritten by the LLM suggestion."""
    parse_service = _import_parse_service()
    deal_id = _seed_deal(sqlite_conn, title="Candidate Item", stage_id=3)
    note_id = _seed_note(sqlite_conn, body="already marked as a task", note_type="task")

    fake_result = parse_service.ParseResult(
        suggested_deal_id=deal_id,
        note_type="agreement",
        confidence=0.95,
        draft_text="draft",
    )
    _mock_gateway_success(monkeypatch, fake_result)

    response = client.post(f"/api/notes/{note_id}/parse")

    assert response.status_code == 200, response.text
    row = _note_row(sqlite_conn, note_id)
    assert row["deal_id"] is None
    assert row["status"] == "inbox"
    assert row["note_type"] == "task"  # NOT overwritten by suggested_note_type='agreement'
    assert row["suggested_note_type"] == "agreement"  # suggestion recorded separately


def test_parse_success_with_null_suggested_deal_id_is_a_valid_outcome(
    client, sqlite_conn, monkeypatch
):
    """"Junk" note: the model normally returned suggested_deal_id=null — not an
    error, the note gets a suggestion with an empty candidate deal."""
    parse_service = _import_parse_service()
    note_id = _seed_note(sqlite_conn, body="unclear note")

    fake_result = parse_service.ParseResult(
        suggested_deal_id=None, note_type="task", confidence=0.3, draft_text="d"
    )
    _mock_gateway_success(monkeypatch, fake_result)

    response = client.post(f"/api/notes/{note_id}/parse")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["suggested_deal_id"] is None
    assert body["llm_status"] == "suggested"

    row = _note_row(sqlite_conn, note_id)
    assert row["suggested_deal_id"] is None
    assert row["deal_id"] is None
    assert row["status"] == "inbox"


def test_parse_response_skipped_images_reflects_real_service_count(
    client, sqlite_conn, config, monkeypatch
):
    """`skipped_images` in the response is not a stubbed 0 but the value actually
    passed through from the service (we deliberately exceed the image size
    limit)."""
    parse_service = _import_parse_service()
    monkeypatch.setattr(config, "LLM_IMAGE_MAX_BYTES", 10)
    note_id = _seed_note(sqlite_conn, body="note with a screenshot")
    _seed_attachment(sqlite_conn, config, note_id, b"0" * 100, "image/png")

    fake_result = parse_service.ParseResult(
        suggested_deal_id=None, note_type="task", confidence=0.4, draft_text="d"
    )
    _mock_gateway_success(monkeypatch, fake_result)

    response = client.post(f"/api/notes/{note_id}/parse")

    assert response.status_code == 200, response.text
    assert response.json()["skipped_images"] == 1


# ===========================================================================
# Criterion 3: POST /api/notes/{id}/parse — gateway/network error (LLMError).
# ===========================================================================


def test_parse_gateway_error_returns_502_or_503_with_detail_and_note_unchanged(
    client, sqlite_conn, monkeypatch
):
    from app.llm_client import LLMError

    note_id = _seed_note(sqlite_conn, body="note without network")
    _mock_gateway_error(monkeypatch, LLMError("gateway unavailable"))

    response = client.post(f"/api/notes/{note_id}/parse")

    assert response.status_code in (502, 503), response.text
    assert "detail" in response.json()

    row = _note_row(sqlite_conn, note_id)
    assert row["llm_status"] == "none"
    assert row["suggested_deal_id"] is None
    assert row["suggested_note_type"] is None
    assert row["llm_confidence"] is None
    assert row["llm_draft"] is None
    # "The note is not lost": it stays usable for ordinary manual processing.
    assert row["deal_id"] is None
    assert row["status"] == "inbox"


def test_parse_gateway_error_note_still_visible_in_inbox_feed_for_retry(
    client, sqlite_conn, monkeypatch
):
    from app.llm_client import LLMError

    note_id = _seed_note(sqlite_conn, body="retry later")
    _mock_gateway_error(monkeypatch, LLMError("timeout"))

    response = client.post(f"/api/notes/{note_id}/parse")
    assert response.status_code in (502, 503)

    assert note_id in _inbox_ids(client)


# ===========================================================================
# Criterion 9 (partial): 404 on parse of a nonexistent note.
# ===========================================================================


def test_parse_nonexistent_note_returns_404(client):
    response = client.post("/api/notes/999999/parse")
    assert response.status_code == 404


# ===========================================================================
# Criterion 4: POST /api/notes/{id}/confirm.
# ===========================================================================


def test_confirm_attaches_to_suggested_deal_copies_type_and_marks_confirmed(
    client, sqlite_conn
):
    deal_id = _seed_deal(sqlite_conn, title="Item to confirm", stage_id=2)
    activity_before = _deal_row(sqlite_conn, deal_id)["last_activity_at"]
    note_id = _seed_note(sqlite_conn, body="note awaiting confirmation")
    _seed_suggestion(
        sqlite_conn,
        note_id,
        suggested_deal_id=deal_id,
        suggested_note_type="agreement",
        llm_confidence=0.91,
        llm_draft="confirmation draft",
    )

    response = client.post(f"/api/notes/{note_id}/confirm")

    assert response.status_code == 200, response.text
    row = _note_row(sqlite_conn, note_id)
    assert row["deal_id"] == deal_id
    assert row["status"] == "attached"
    assert row["note_type"] == "agreement"
    assert row["llm_status"] == "confirmed"
    assert row["llm_draft"] == "confirmation draft"  # preserved

    activity_after = _deal_row(sqlite_conn, deal_id)["last_activity_at"]
    assert activity_after != activity_before

    assert note_id not in _inbox_ids(client)

    feed = client.get(f"/api/deals/{deal_id}").json()
    assert response.status_code == 200
    feed_ids = [n["id"] for n in feed["notes"]]
    assert note_id in feed_ids


def test_confirm_without_any_suggestion_returns_422_and_note_unchanged(
    client, sqlite_conn
):
    note_id = _seed_note(sqlite_conn, body="no suggestion at all")

    response = client.post(f"/api/notes/{note_id}/confirm")

    assert response.status_code == 422
    row = _note_row(sqlite_conn, note_id)
    assert row["deal_id"] is None
    assert row["status"] == "inbox"
    assert row["llm_status"] == "none"


def test_confirm_with_null_suggested_deal_id_returns_422_and_note_unchanged(
    client, sqlite_conn
):
    """The note was parsed, but the LLM found no candidate deal
    (suggested_deal_id=null) — confirm cannot attach to "nowhere"."""
    note_id = _seed_note(sqlite_conn, body="suggestion without a deal")
    _seed_suggestion(sqlite_conn, note_id, suggested_deal_id=None)

    response = client.post(f"/api/notes/{note_id}/confirm")

    assert response.status_code == 422
    row = _note_row(sqlite_conn, note_id)
    assert row["deal_id"] is None
    assert row["status"] == "inbox"
    assert row["llm_status"] == "suggested"  # confirm did not touch the status on error


def test_confirm_nonexistent_note_returns_404(client):
    response = client.post("/api/notes/999999/confirm")
    assert response.status_code == 404


# ===========================================================================
# Criterion 5: POST /api/notes/{id}/change.
# ===========================================================================


def test_change_attaches_to_chosen_deal_and_preserves_llm_suggestion(
    client, sqlite_conn
):
    suggested_deal_id = _seed_deal(sqlite_conn, title="Suggested by LLM", stage_id=1)
    chosen_deal_id = _seed_deal(sqlite_conn, title="Chosen by human", stage_id=4)
    note_id = _seed_note(sqlite_conn, body="note to change")
    _seed_suggestion(
        sqlite_conn,
        note_id,
        suggested_deal_id=suggested_deal_id,
        suggested_note_type="task",
    )

    response = client.post(
        f"/api/notes/{note_id}/change", json={"deal_id": chosen_deal_id}
    )

    assert response.status_code == 200, response.text
    row = _note_row(sqlite_conn, note_id)
    assert row["deal_id"] == chosen_deal_id
    assert row["status"] == "attached"
    assert row["llm_status"] == "rejected"
    # Both facts in the data: what the LLM suggested and what the human chose.
    assert row["suggested_deal_id"] == suggested_deal_id


def test_change_nonexistent_deal_returns_404_and_note_unchanged(client, sqlite_conn):
    suggested_deal_id = _seed_deal(sqlite_conn, title="Suggested by LLM", stage_id=1)
    note_id = _seed_note(sqlite_conn, body="note")
    _seed_suggestion(sqlite_conn, note_id, suggested_deal_id=suggested_deal_id)

    response = client.post(f"/api/notes/{note_id}/change", json={"deal_id": 999999})

    assert response.status_code == 404
    row = _note_row(sqlite_conn, note_id)
    assert row["deal_id"] is None
    assert row["status"] == "inbox"
    assert row["llm_status"] == "suggested"
    assert row["suggested_deal_id"] == suggested_deal_id


def test_change_nonexistent_note_returns_404(client, sqlite_conn):
    deal_id = _seed_deal(sqlite_conn, title="Item", stage_id=1)

    response = client.post("/api/notes/999999/change", json={"deal_id": deal_id})

    assert response.status_code == 404


def test_change_to_a_terminal_but_existing_deal_is_allowed(client, sqlite_conn):
    """Spec risks: "change" does not restrict the choice to only active deals
    (like the ordinary attach_note of Step 1) — the restriction is introduced
    only for the 404 on a nonexistent deal, not for closed ones."""
    suggested_deal_id = _seed_deal(sqlite_conn, title="Suggested by LLM", stage_id=1)
    terminal_deal_id = _seed_deal(
        sqlite_conn, title="Closed item", stage_id=_TERMINAL_STAGE_ID
    )
    note_id = _seed_note(sqlite_conn, body="note")
    _seed_suggestion(sqlite_conn, note_id, suggested_deal_id=suggested_deal_id)

    response = client.post(
        f"/api/notes/{note_id}/change", json={"deal_id": terminal_deal_id}
    )

    assert response.status_code == 200, response.text
    row = _note_row(sqlite_conn, note_id)
    assert row["deal_id"] == terminal_deal_id
    assert row["status"] == "attached"


# ===========================================================================
# Criterion 6: POST /api/notes/{id}/reject.
# ===========================================================================


def test_reject_marks_rejected_note_stays_unattached_in_inbox(client, sqlite_conn):
    suggested_deal_id = _seed_deal(sqlite_conn, title="Suggested by LLM", stage_id=1)
    note_id = _seed_note(sqlite_conn, body="note to reject")
    _seed_suggestion(sqlite_conn, note_id, suggested_deal_id=suggested_deal_id)

    response = client.post(f"/api/notes/{note_id}/reject")

    assert response.status_code == 200, response.text
    row = _note_row(sqlite_conn, note_id)
    assert row["llm_status"] == "rejected"
    assert row["status"] == "inbox"
    assert row["deal_id"] is None
    assert row["suggested_deal_id"] == suggested_deal_id  # kept for history

    assert note_id in _inbox_ids(client)


def test_reject_note_without_prior_suggestion_still_succeeds(client, sqlite_conn):
    note_id = _seed_note(sqlite_conn, body="no suggestion at all")

    response = client.post(f"/api/notes/{note_id}/reject")

    assert response.status_code == 200, response.text
    row = _note_row(sqlite_conn, note_id)
    assert row["llm_status"] == "rejected"
    assert row["status"] == "inbox"
    assert row["deal_id"] is None


def test_reject_nonexistent_note_returns_404(client):
    response = client.post("/api/notes/999999/reject")
    assert response.status_code == 404


# ===========================================================================
# Criterion 7: serializers — new keys, the shape of the old fields is unchanged.
# ===========================================================================


def test_get_notes_feed_exposes_llm_fields_with_none_defaults(client, sqlite_conn):
    note_id = _seed_note(sqlite_conn, body="not parsed yet")

    feed = client.get("/api/notes", params={"status": "inbox"}).json()
    item = next(n for n in feed if n["id"] == note_id)

    for key in (
        "note_type",
        "suggested_deal_id",
        "suggested_note_type",
        "llm_confidence",
        "llm_status",
        "llm_draft",
    ):
        assert key in item, f"{key} missing from the serialized note: {item}"
    assert item["llm_status"] == "none"
    assert item["suggested_deal_id"] is None
    assert item["suggested_note_type"] is None
    assert item["llm_confidence"] is None
    assert item["llm_draft"] is None

    # The shape of the Step 1 old fields is unchanged (regression).
    assert {"id", "body", "status", "deal_id", "created_at", "attachments"} <= set(
        item.keys()
    )


def test_get_notes_feed_exposes_llm_fields_after_suggestion_seeded(client, sqlite_conn):
    deal_id = _seed_deal(sqlite_conn, title="Candidate", stage_id=1)
    note_id = _seed_note(sqlite_conn, body="parsed note")
    _seed_suggestion(
        sqlite_conn,
        note_id,
        suggested_deal_id=deal_id,
        suggested_note_type="reminder",
        llm_confidence=0.66,
        llm_draft="draft text",
    )

    feed = client.get("/api/notes", params={"status": "inbox"}).json()
    item = next(n for n in feed if n["id"] == note_id)

    assert item["llm_status"] == "suggested"
    assert item["suggested_deal_id"] == deal_id
    assert item["suggested_note_type"] == "reminder"
    assert item["llm_confidence"] == pytest.approx(0.66)
    assert item["llm_draft"] == "draft text"


def test_get_deal_feed_exposes_llm_fields_for_attached_note(client, sqlite_conn):
    deal_id = _seed_deal(sqlite_conn, title="Item with a feed", stage_id=1)
    note_id = _seed_note(
        sqlite_conn, body="attached note", status="attached", deal_id=deal_id
    )
    _seed_suggestion(
        sqlite_conn,
        note_id,
        suggested_deal_id=deal_id,
        suggested_note_type="status",
        llm_confidence=0.42,
        llm_draft="draft in the feed",
    )

    response = client.get(f"/api/deals/{deal_id}")
    assert response.status_code == 200
    body = response.json()
    item = next(n for n in body["notes"] if n["id"] == note_id)

    for key in (
        "note_type",
        "suggested_deal_id",
        "suggested_note_type",
        "llm_confidence",
        "llm_status",
        "llm_draft",
    ):
        assert key in item, f"{key} missing from the deal feed: {item}"
    assert item["suggested_deal_id"] == deal_id
    assert item["suggested_note_type"] == "status"
    assert item["llm_confidence"] == pytest.approx(0.42)
    assert item["llm_status"] == "suggested"
    assert item["llm_draft"] == "draft in the feed"

    # The shape of the Step 1 old feed fields is unchanged (regression).
    assert {"id", "body", "status", "deal_id", "created_at", "attachments"} <= set(
        item.keys()
    )


def test_get_deal_feed_exposes_llm_status_none_for_never_parsed_note(
    client, sqlite_conn
):
    deal_id = _seed_deal(sqlite_conn, title="Item", stage_id=1)
    note_id = _seed_note(
        sqlite_conn, body="ordinary note", status="attached", deal_id=deal_id
    )

    body = client.get(f"/api/deals/{deal_id}").json()
    item = next(n for n in body["notes"] if n["id"] == note_id)

    assert item["llm_status"] == "none"
    assert item["suggested_deal_id"] is None
    assert item["suggested_note_type"] is None
    assert item["llm_confidence"] is None
    assert item["llm_draft"] is None


# ===========================================================================
# Criterion 8: GET/PUT /api/settings/parse — the confidence threshold.
# ===========================================================================


def test_get_settings_parse_returns_threshold_from_app_meta_and_default_constant(
    client, sqlite_conn
):
    from app.config import DEFAULT_CONFIDENCE_THRESHOLD

    body = client.get("/api/settings/parse").json()

    stored = float(_app_meta(sqlite_conn, "llm_confidence_threshold"))
    assert body["confidence_threshold"] == pytest.approx(stored)
    assert body["default_confidence_threshold"] == pytest.approx(
        DEFAULT_CONFIDENCE_THRESHOLD
    )


def test_get_settings_parse_reflects_custom_app_meta_value(client, sqlite_conn):
    sqlite_conn.execute(
        "UPDATE app_meta SET value = '0.55' WHERE key = 'llm_confidence_threshold'"
    )
    sqlite_conn.commit()

    body = client.get("/api/settings/parse").json()

    assert body["confidence_threshold"] == pytest.approx(0.55)


def test_get_settings_parse_falls_back_to_default_when_key_missing_not_500(
    client, sqlite_conn
):
    from app.config import DEFAULT_CONFIDENCE_THRESHOLD

    sqlite_conn.execute("DELETE FROM app_meta WHERE key = 'llm_confidence_threshold'")
    sqlite_conn.commit()

    response = client.get("/api/settings/parse")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["confidence_threshold"] == pytest.approx(DEFAULT_CONFIDENCE_THRESHOLD)
    assert body["default_confidence_threshold"] == pytest.approx(
        DEFAULT_CONFIDENCE_THRESHOLD
    )


def test_put_settings_parse_valid_value_persists_to_app_meta_and_get(
    client, sqlite_conn
):
    response = client.put("/api/settings/parse", json={"confidence_threshold": 0.5})

    assert response.status_code == 200, response.text
    assert float(_app_meta(sqlite_conn, "llm_confidence_threshold")) == pytest.approx(
        0.5
    )

    body = client.get("/api/settings/parse").json()
    assert body["confidence_threshold"] == pytest.approx(0.5)


@pytest.mark.parametrize("boundary", [0.0, 1.0])
def test_put_settings_parse_boundary_values_are_valid(client, sqlite_conn, boundary):
    response = client.put(
        "/api/settings/parse", json={"confidence_threshold": boundary}
    )

    assert response.status_code == 200, response.text
    assert float(_app_meta(sqlite_conn, "llm_confidence_threshold")) == pytest.approx(
        boundary
    )


@pytest.mark.parametrize("bad_value", [1.5, -0.1])
def test_put_settings_parse_out_of_range_returns_422_and_keeps_old_value(
    client, sqlite_conn, bad_value
):
    before = _app_meta(sqlite_conn, "llm_confidence_threshold")

    response = client.put(
        "/api/settings/parse", json={"confidence_threshold": bad_value}
    )

    assert response.status_code == 422
    assert _app_meta(sqlite_conn, "llm_confidence_threshold") == before


def test_put_settings_parse_non_numeric_value_returns_422(client, sqlite_conn):
    before = _app_meta(sqlite_conn, "llm_confidence_threshold")

    response = client.put("/api/settings/parse", json={"confidence_threshold": "abc"})

    assert response.status_code == 422
    assert _app_meta(sqlite_conn, "llm_confidence_threshold") == before


# ===========================================================================
# Criterion 9: review-friendly structural check that the router has no direct
# transport (full network review is the reviewer-security task).
# ===========================================================================


def test_parsing_router_source_has_no_direct_http_transport_imports(config):
    path = config.PROJECT_ROOT / "app" / "routers" / "parsing.py"
    assert path.exists(), "app/routers/parsing.py must exist (T4)"
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
            f"app/routers/parsing.py must not contain {forbidden!r} — all "
            "outgoing traffic must go through app/parse_service.py -> "
            "app/llm_client.py"
        )
