"""T2 tests: pinning notes (is_pinned) and the "Info" type (info).

Acceptance criteria come from docs/specs/006-custom-improvements.md, task T2.
At the time this file is written, ``app/repo/notes.py`` (list_notes/_note_dict/
set_pinned) and ``app/routers/notes.py`` (NotePatch/patch_note) do not yet
contain the pinning logic or the ``info`` type — migration 006 (the schema, T1)
has already been applied, but the repository/router code for it has not. Expected
TDD state: the tests collect but fail.

Only fixtures from ``tests/conftest.py`` are used: ``client``, ``sqlite_conn``.
The helper code (inserting notes directly into the DB with control over
``created_at``/``is_pinned``) is written locally, modeled on
``tests/test_inbox.py``/``tests/test_notes.py``.

Scope (see the spec, "Scope note"): the second sort query in
``app/repo/notes.py`` (the defer-old/triage scan,
``SELECT id FROM notes WHERE status='inbox' ORDER BY created_at DESC, id DESC``)
is deliberately NOT made pin-aware — the test at the end of this file locks that
in (a pinned but old note is still deferred alongside the unpinned ones).
"""

from __future__ import annotations

import pytest

from helpers import _insert_note

# ---------------------------------------------------------------------------
# Helper functions for working with the DB directly.
# ---------------------------------------------------------------------------


def _note_row(sqlite_conn, note_id: int):
    row = sqlite_conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    assert row is not None
    return row


def _create_note_via_api(client, body: str) -> dict:
    response = client.post("/api/notes", data={"body": body})
    assert response.status_code == 201
    return response.json()


def _inbox_feed(client) -> list[dict]:
    return client.get("/api/notes", params={"status": "inbox"}).json()


def _inbox_ids(client) -> list[int]:
    return [n["id"] for n in _inbox_feed(client)]


# ===========================================================================
# PATCH /api/notes/{id}: is_pinned true/false, persistence.
# ===========================================================================


def test_patch_is_pinned_true_returns_200_and_note_dict_has_is_pinned_one(
    client, sqlite_conn
):
    note_id = _insert_note(sqlite_conn, body="pin this")

    response = client.patch(f"/api/notes/{note_id}", json={"is_pinned": True})

    assert response.status_code == 200
    assert response.json()["is_pinned"] == 1
    assert _note_row(sqlite_conn, note_id)["is_pinned"] == 1


def test_patch_is_pinned_false_after_true_persists_as_unpinned(client, sqlite_conn):
    note_id = _insert_note(sqlite_conn, body="pin and unpin")
    pinned = client.patch(f"/api/notes/{note_id}", json={"is_pinned": True})
    assert pinned.status_code == 200
    assert pinned.json()["is_pinned"] == 1

    response = client.patch(f"/api/notes/{note_id}", json={"is_pinned": False})

    assert response.status_code == 200
    assert response.json()["is_pinned"] == 0
    assert _note_row(sqlite_conn, note_id)["is_pinned"] == 0


# ===========================================================================
# PATCH /api/notes/{id}: note_type='info' and forbidding 'info' as a status.
# ===========================================================================


def test_patch_note_type_info_succeeds_and_status_is_unchanged(client, sqlite_conn):
    note_id = _insert_note(sqlite_conn, body="for information")

    response = client.patch(f"/api/notes/{note_id}", json={"note_type": "info"})

    assert response.status_code == 200
    body = response.json()
    assert body["note_type"] == "info"
    assert body["status"] == "inbox"
    row = _note_row(sqlite_conn, note_id)
    assert row["note_type"] == "info"
    assert row["status"] == "inbox"


def test_patch_note_type_bogus_value_returns_422(client, sqlite_conn):
    note_id = _insert_note(sqlite_conn, body="bad type")

    response = client.patch(f"/api/notes/{note_id}", json={"note_type": "bogus"})

    assert response.status_code == 422
    assert _note_row(sqlite_conn, note_id)["note_type"] is None


def test_patch_status_info_returns_422_status_literal_unchanged(client, sqlite_conn):
    note_id = _insert_note(sqlite_conn, body="info is not a status")

    response = client.patch(f"/api/notes/{note_id}", json={"status": "info"})

    assert response.status_code == 422
    assert _note_row(sqlite_conn, note_id)["status"] == "inbox"


@pytest.mark.parametrize("note_type", ["task", "reminder"])
def test_existing_task_reminder_note_type_still_works_unchanged(
    client, sqlite_conn, note_type
):
    """Regression: extending the Literal with info does not break task/reminder (F1)."""
    note_id = _insert_note(sqlite_conn, body="old behavior")

    response = client.patch(f"/api/notes/{note_id}", json={"note_type": note_type})

    assert response.status_code == 200
    body = response.json()
    assert body["note_type"] == note_type
    assert body["status"] == "inbox"
    row = _note_row(sqlite_conn, note_id)
    assert row["note_type"] == note_type
    assert row["status"] == "inbox"


# ===========================================================================
# GET /api/notes: pinned notes first, order within a group preserved.
# ===========================================================================


def test_list_notes_orders_pinned_before_unpinned_regardless_of_created_at(
    client, sqlite_conn
):
    """Spec, T2: A<B<C by created_at, B pinned -> order [B, C, A]."""
    note_a = _insert_note(sqlite_conn, body="A", created_at="2026-01-01T00:00:01")
    note_b = _insert_note(
        sqlite_conn, body="B", created_at="2026-01-01T00:00:02", is_pinned=1
    )
    note_c = _insert_note(sqlite_conn, body="C", created_at="2026-01-01T00:00:03")

    assert _inbox_ids(client) == [note_b, note_c, note_a]


def test_list_notes_pin_ordering_survives_multiple_pinned_notes(client, sqlite_conn):
    """Multiple pinned notes: all up front, each group newest-first."""
    note_a = _insert_note(sqlite_conn, body="A", created_at="2026-01-01T00:00:01")
    note_b = _insert_note(
        sqlite_conn, body="B", created_at="2026-01-01T00:00:02", is_pinned=1
    )
    note_c = _insert_note(sqlite_conn, body="C", created_at="2026-01-01T00:00:03")
    note_d = _insert_note(
        sqlite_conn, body="D", created_at="2026-01-01T00:00:04", is_pinned=1
    )

    # Both pinned (D newer than B) up front, then the unpinned (C newer than A).
    assert _inbox_ids(client) == [note_d, note_b, note_c, note_a]


# ===========================================================================
# Every note from the feed/list contains is_pinned and ocr_text.
# ===========================================================================


def test_get_notes_feed_entries_contain_is_pinned_and_ocr_text_keys(client):
    _create_note_via_api(client, "checking response keys")

    feed = _inbox_feed(client)

    assert len(feed) == 1
    note = feed[0]
    assert "is_pinned" in note
    assert "ocr_text" in note
    assert note["is_pinned"] == 0
    assert note["ocr_text"] is None


def test_get_note_repo_function_includes_is_pinned_and_ocr_text(sqlite_conn):
    """The spec criterion covers get_note too (e.g. used in the PATCH response)."""
    note_id = _insert_note(sqlite_conn, body="repo-level get_note")

    from app.repo import notes as notes_repo

    note = notes_repo.get_note(sqlite_conn, note_id)

    assert note is not None
    assert "is_pinned" in note
    assert "ocr_text" in note
    assert note["is_pinned"] == 0
    assert note["ocr_text"] is None


# ===========================================================================
# Out of scope for T2: the defer-old/triage scan does NOT become pin-aware.
# ===========================================================================


def test_defer_old_scan_has_no_pin_exemption_out_of_scope_for_t2(client, sqlite_conn):
    """Spec, T2 "Scope note": an old pinned note is still deferred alongside the
    unpinned ones — is_pinned DESC was added ONLY to list_notes, not to the
    defer-old/triage scan.
    """
    oldest_pinned = _insert_note(
        sqlite_conn, body="old pinned", created_at="2026-01-01T00:00:00", is_pinned=1
    )
    second_oldest = _insert_note(
        sqlite_conn, body="second oldest", created_at="2026-01-01T00:00:01"
    )
    newer_one = _insert_note(
        sqlite_conn, body="newer 1", created_at="2026-01-01T00:00:02"
    )
    newer_two = _insert_note(
        sqlite_conn, body="newer 2", created_at="2026-01-01T00:00:03"
    )

    response = client.post("/api/notes/defer-old", json={"keep": 2})

    assert response.status_code == 200
    # keep=2 out of 4 -> the 2 oldest are deferred, including the pinned one.
    assert response.json() == {"deferred": 2}
    assert _note_row(sqlite_conn, oldest_pinned)["status"] == "deferred"
    assert _note_row(sqlite_conn, second_oldest)["status"] == "deferred"
    assert _note_row(sqlite_conn, newer_one)["status"] == "inbox"
    assert _note_row(sqlite_conn, newer_two)["status"] == "inbox"
