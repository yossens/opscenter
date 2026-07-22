"""T4 tests: triaging the Inbox — triage, bulk-attach, defer-old, recovery-summary.

Acceptance criteria come from docs/specs/001-step1-inbox-pipeline.md, task T4
(and the related edge cases from the "Risks and edge cases" section: deleting an
attachment file that is missing from disk). The tests are written against the
spec, not the implementation — ``app/routers/notes.py``/``app/repo/notes.py`` do
not yet contain the triage code (PATCH/DELETE/bulk-attach/defer-old/summary) at
the time these tests are written; a correct TDD state — the tests collect but
fail.

Only fixtures from ``tests/conftest.py`` are used: ``client``, ``sqlite_conn``,
``config``. No new fixture is added to conftest.py — all the helper code
(inserting deals/notes directly into the DB, reading/writing ``app_meta``) lives
locally in this file, modeled on ``tests/test_db.py`` and ``tests/test_notes.py``.

The criterion "the note has deal_id = N, status='attached', and the deal's
last_activity_at is updated (checked against the deals row in the DB, without
hitting the T5 endpoints)" is taken literally: the note/deal state after a PATCH
is verified with a direct DB query through ``sqlite_conn``, not through
``GET /api/deals/{id}`` (which does not exist yet — it arrives in T5).

The time boundaries (72h/73h) are inevitably sensitive to how long the test runs
in a black box (the implementation compares the stored ``last_triage_at`` against
the "current" time at the moment the request is processed, which the test cannot
freeze without access to the implementation internals). To avoid a flaky test
right on the literal 72:00:00.000 boundary, the "not older than 72h" test uses a
timestamp slightly YOUNGER than 72 hours (with margin for test-execution delays),
while "older than 72h" uses a timestamp older than 73 hours. This preserves the
boundary semantics under test (exclusive) without racing on milliseconds.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from helpers import _deal_row, _insert_deal, _insert_note

# ---------------------------------------------------------------------------
# Helper functions for working with the DB directly.
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _first_stage_id(sqlite_conn) -> int:
    row = sqlite_conn.execute(
        "SELECT id FROM stages ORDER BY position LIMIT 1"
    ).fetchone()
    assert row is not None, "expected at least one seeded stage"
    return row["id"]


def _note_row(sqlite_conn, note_id: int):
    row = sqlite_conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    assert row is not None
    return row


def _note_exists(sqlite_conn, note_id: int) -> bool:
    row = sqlite_conn.execute("SELECT 1 FROM notes WHERE id = ?", (note_id,)).fetchone()
    return row is not None


def _attachment_rows_for_note(sqlite_conn, note_id: int):
    return sqlite_conn.execute(
        "SELECT * FROM attachments WHERE note_id = ?", (note_id,)
    ).fetchall()


def _get_last_triage_at(sqlite_conn) -> str:
    row = sqlite_conn.execute(
        "SELECT value FROM app_meta WHERE key = 'last_triage_at'"
    ).fetchone()
    assert row is not None
    return row["value"]


def _set_last_triage_at(sqlite_conn, value: str) -> None:
    sqlite_conn.execute(
        "UPDATE app_meta SET value = ? WHERE key = 'last_triage_at'", (value,)
    )
    sqlite_conn.commit()


def _create_note_via_api(client, body: str) -> dict:
    response = client.post("/api/notes", data={"body": body})
    assert response.status_code == 201
    return response.json()


def _inbox_ids(client) -> list[int]:
    feed = client.get("/api/notes", params={"status": "inbox"}).json()
    return [n["id"] for n in feed]


def _deferred_ids(client) -> list[int]:
    feed = client.get("/api/notes", params={"status": "deferred"}).json()
    return [n["id"] for n in feed]


# ===========================================================================
# PATCH /api/notes/{id}: attach to a deal ({"deal_id": N})
# ===========================================================================


def test_patch_deal_id_attaches_note_updates_status_and_deal_activity(
    client, sqlite_conn
):
    stage_id = _first_stage_id(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "Acme", stage_id)
    activity_before = _deal_row(sqlite_conn, deal_id)["last_activity_at"]
    note_id = _insert_note(sqlite_conn, body="to be triaged")

    response = client.patch(f"/api/notes/{note_id}", json={"deal_id": deal_id})

    assert response.status_code == 200

    row = _note_row(sqlite_conn, note_id)
    assert row["deal_id"] == deal_id
    assert row["status"] == "attached"

    activity_after = _deal_row(sqlite_conn, deal_id)["last_activity_at"]
    assert activity_after != activity_before


def test_patch_deal_id_attached_note_disappears_from_inbox_feed(client, sqlite_conn):
    stage_id = _first_stage_id(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "Acme", stage_id)
    note = _create_note_via_api(client, "will leave the inbox")

    assert note["id"] in _inbox_ids(client)

    response = client.patch(f"/api/notes/{note['id']}", json={"deal_id": deal_id})
    assert response.status_code == 200

    assert note["id"] not in _inbox_ids(client)


def test_patch_deal_id_nonexistent_returns_404_and_note_unchanged(client, sqlite_conn):
    note_id = _insert_note(sqlite_conn, body="must not change")

    response = client.patch(f"/api/notes/{note_id}", json={"deal_id": 999999})

    assert response.status_code == 404

    row = _note_row(sqlite_conn, note_id)
    assert row["status"] == "inbox"
    assert row["deal_id"] is None


# ===========================================================================
# PATCH /api/notes/{id}: status change (archived / deferred), then attaching a
# deferred note.
# ===========================================================================


@pytest.mark.parametrize("target_status", ["archived", "deferred"])
def test_patch_status_archived_or_deferred_changes_status(
    client, sqlite_conn, target_status
):
    note_id = _insert_note(sqlite_conn, body="for triage")

    response = client.patch(f"/api/notes/{note_id}", json={"status": target_status})

    assert response.status_code == 200
    row = _note_row(sqlite_conn, note_id)
    assert row["status"] == target_status


def test_deferred_note_can_then_be_attached_to_a_deal(client, sqlite_conn):
    stage_id = _first_stage_id(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "Acme", stage_id)
    note_id = _insert_note(sqlite_conn, body="deferred")

    deferred_response = client.patch(
        f"/api/notes/{note_id}", json={"status": "deferred"}
    )
    assert deferred_response.status_code == 200
    assert _note_row(sqlite_conn, note_id)["status"] == "deferred"

    attach_response = client.patch(f"/api/notes/{note_id}", json={"deal_id": deal_id})
    assert attach_response.status_code == 200

    row = _note_row(sqlite_conn, note_id)
    assert row["status"] == "attached"
    assert row["deal_id"] == deal_id


def test_patch_status_attached_without_deal_id_returns_422(client, sqlite_conn):
    note_id = _insert_note(sqlite_conn, body="invalid transition")

    response = client.patch(f"/api/notes/{note_id}", json={"status": "attached"})

    assert response.status_code == 422
    row = _note_row(sqlite_conn, note_id)
    assert row["status"] == "inbox"
    assert row["deal_id"] is None


def test_patch_deal_id_and_archived_status_together_returns_422(client, sqlite_conn):
    deal_id = _insert_deal(sqlite_conn, "Acme", _first_stage_id(sqlite_conn))
    note_id = _insert_note(sqlite_conn, body="contradictory PATCH")

    response = client.patch(
        f"/api/notes/{note_id}",
        json={"deal_id": deal_id, "status": "archived"},
    )

    assert response.status_code == 422
    row = _note_row(sqlite_conn, note_id)
    assert row["status"] == "inbox"
    assert row["deal_id"] is None


# ===========================================================================
# PATCH /api/notes/{id}: note_type (task/reminder/null), invalid type
# ===========================================================================


@pytest.mark.parametrize("note_type", ["task", "reminder"])
def test_patch_note_type_sets_type_and_keeps_inbox_status(
    client, sqlite_conn, note_type
):
    note_id = _insert_note(sqlite_conn, body="to label")

    response = client.patch(f"/api/notes/{note_id}", json={"note_type": note_type})

    assert response.status_code == 200
    row = _note_row(sqlite_conn, note_id)
    assert row["note_type"] == note_type
    assert row["status"] == "inbox"


def test_patch_note_type_null_unsets_previously_set_type(client, sqlite_conn):
    note_id = _insert_note(sqlite_conn, body="clear the label")
    first = client.patch(f"/api/notes/{note_id}", json={"note_type": "task"})
    assert first.status_code == 200
    assert _note_row(sqlite_conn, note_id)["note_type"] == "task"

    response = client.patch(f"/api/notes/{note_id}", json={"note_type": None})

    assert response.status_code == 200
    assert _note_row(sqlite_conn, note_id)["note_type"] is None


def test_patch_note_type_invalid_value_returns_422(client, sqlite_conn):
    note_id = _insert_note(sqlite_conn, body="bad type")

    response = client.patch(f"/api/notes/{note_id}", json={"note_type": "bogus_type"})

    assert response.status_code == 422
    assert _note_row(sqlite_conn, note_id)["note_type"] is None


def test_patch_note_type_status_or_agreement_not_in_endpoint_contract_returns_422(
    client, sqlite_conn
):
    """The spec lists only "task"|"reminder"|null for PATCH.

    ``status``/``agreement`` are permitted for the column at the DB level
    (groundwork for F5), but are not part of this endpoint's contract — a value
    that is invalid per the T4 contract must return 422 rather than be silently
    accepted.
    """
    note_id = _insert_note(sqlite_conn, body="F5 groundwork, not via this endpoint")

    response = client.patch(f"/api/notes/{note_id}", json={"note_type": "status"})

    assert response.status_code == 422


# ===========================================================================
# DELETE /api/notes/{id}
# ===========================================================================


def test_delete_note_removes_row_attachment_rows_and_files_from_disk(
    client, sqlite_conn, config
):
    upload = client.post(
        "/api/notes",
        files={"files": ("to_delete.txt", b"payload", "text/plain")},
    )
    assert upload.status_code == 201
    payload = upload.json()
    note_id = payload["id"]
    attachment_id = payload["attachments"][0]["id"]
    stored_name = _attachment_rows_for_note(sqlite_conn, note_id)[0]["stored_name"]
    stored_path = config.ATTACHMENTS_DIR / stored_name
    assert stored_path.is_file()

    response = client.delete(f"/api/notes/{note_id}")

    assert response.status_code == 204
    assert not _note_exists(sqlite_conn, note_id)
    assert len(_attachment_rows_for_note(sqlite_conn, note_id)) == 0
    assert not stored_path.exists()
    # sanity: the attachment id no longer appears in the table.
    assert (
        sqlite_conn.execute(
            "SELECT 1 FROM attachments WHERE id = ?", (attachment_id,)
        ).fetchone()
        is None
    )


def test_delete_note_twice_returns_404_on_second_call(client, sqlite_conn):
    note_id = _insert_note(sqlite_conn, body="delete twice")

    first = client.delete(f"/api/notes/{note_id}")
    assert first.status_code == 204

    second = client.delete(f"/api/notes/{note_id}")
    assert second.status_code == 404


def test_delete_note_nonexistent_id_returns_404(client):
    response = client.delete("/api/notes/999999")
    assert response.status_code == 404


def test_delete_note_does_not_crash_when_attachment_file_missing_on_disk(
    client, sqlite_conn, config
):
    """Risk: the attachment file was deleted from disk by hand — DELETE must not crash."""
    upload = client.post(
        "/api/notes",
        files={"files": ("gone.txt", b"payload", "text/plain")},
    )
    assert upload.status_code == 201
    note_id = upload.json()["id"]
    stored_name = _attachment_rows_for_note(sqlite_conn, note_id)[0]["stored_name"]
    stored_path = config.ATTACHMENTS_DIR / stored_name
    os.remove(stored_path)
    assert not stored_path.exists()

    response = client.delete(f"/api/notes/{note_id}")

    assert response.status_code == 204
    assert not _note_exists(sqlite_conn, note_id)


# ===========================================================================
# POST /api/notes/bulk-attach
# ===========================================================================


def test_bulk_attach_all_existing_notes_succeeds_fully(client, sqlite_conn):
    stage_id = _first_stage_id(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "Acme", stage_id)
    note_ids = [_create_note_via_api(client, f"note {i}")["id"] for i in range(3)]

    response = client.post(
        "/api/notes/bulk-attach",
        json={"note_ids": note_ids, "deal_id": deal_id},
    )

    assert response.status_code == 200
    assert response.json() == {"attached": 3, "skipped": []}

    for note_id in note_ids:
        row = _note_row(sqlite_conn, note_id)
        assert row["status"] == "attached"
        assert row["deal_id"] == deal_id


def test_bulk_attach_nonexistent_deal_returns_404_and_nothing_changes(
    client, sqlite_conn
):
    note_ids = [_create_note_via_api(client, f"note {i}")["id"] for i in range(2)]

    response = client.post(
        "/api/notes/bulk-attach",
        json={"note_ids": note_ids, "deal_id": 999999},
    )

    assert response.status_code == 404
    for note_id in note_ids:
        row = _note_row(sqlite_conn, note_id)
        assert row["status"] == "inbox"
        assert row["deal_id"] is None


def test_bulk_attach_mixed_existing_and_nonexistent_notes_partial_success(
    client, sqlite_conn
):
    stage_id = _first_stage_id(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "Acme", stage_id)
    existing_ids = [
        _create_note_via_api(client, f"note {i}")["id"] for i in range(2)
    ]
    nonexistent_id = max(existing_ids) + 12345

    response = client.post(
        "/api/notes/bulk-attach",
        json={"note_ids": [*existing_ids, nonexistent_id], "deal_id": deal_id},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["attached"] == 2
    assert body["skipped"] == [nonexistent_id]

    for note_id in existing_ids:
        row = _note_row(sqlite_conn, note_id)
        assert row["status"] == "attached"
        assert row["deal_id"] == deal_id


# ===========================================================================
# POST /api/notes/defer-old
# ===========================================================================


def test_defer_old_keeps_15_newest_defers_5_oldest_of_20(client, sqlite_conn):
    note_ids = [
        _insert_note(
            sqlite_conn,
            body=f"note {i}",
            created_at=f"2026-01-01T00:00:{i:02d}",
        )
        for i in range(20)
    ]
    oldest_5 = note_ids[:5]
    newest_15 = note_ids[5:]

    response = client.post("/api/notes/defer-old", json={"keep": 15})

    assert response.status_code == 200
    assert response.json() == {"deferred": 5}

    for note_id in oldest_5:
        assert _note_row(sqlite_conn, note_id)["status"] == "deferred"
    for note_id in newest_15:
        assert _note_row(sqlite_conn, note_id)["status"] == "inbox"


def test_defer_old_does_not_touch_notes_already_deferred_or_attached(
    client, sqlite_conn
):
    stage_id = _first_stage_id(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "Acme", stage_id)
    already_deferred = _insert_note(
        sqlite_conn,
        body="already deferred",
        status="deferred",
        created_at="2026-01-01T00:00:01",
    )
    already_attached = _insert_note(
        sqlite_conn,
        body="already attached",
        status="attached",
        deal_id=deal_id,
        created_at="2026-01-01T00:00:02",
    )
    inbox_notes = [
        _insert_note(
            sqlite_conn, body=f"inbox {i}", created_at=f"2026-01-01T01:00:{i:02d}"
        )
        for i in range(3)
    ]

    response = client.post("/api/notes/defer-old", json={"keep": 1})

    assert response.status_code == 200
    # keep=1 out of 3 inbox notes -> 2 become deferred; notes already out of the
    # inbox are not counted and do not change status again.
    assert response.json() == {"deferred": 2}
    assert _note_row(sqlite_conn, already_deferred)["status"] == "deferred"
    assert _note_row(sqlite_conn, already_attached)["status"] == "attached"
    assert _note_row(sqlite_conn, inbox_notes[-1])["status"] == "inbox"


# ===========================================================================
# GET /api/inbox/summary: recovery_needed boundaries
# ===========================================================================


def _seed_inbox_notes(sqlite_conn, count: int) -> None:
    for i in range(count):
        _insert_note(
            sqlite_conn, body=f"note {i}", created_at=f"2026-01-01T00:{i:02d}:00"
        )


def test_summary_reports_inbox_and_deferred_counts_and_required_keys(
    client, sqlite_conn
):
    _seed_inbox_notes(sqlite_conn, 2)
    _insert_note(sqlite_conn, body="deferred", status="deferred")

    response = client.get("/api/inbox/summary")

    assert response.status_code == 200
    body = response.json()
    assert set(
        ["inbox_count", "deferred_count", "last_triage_at", "recovery_needed"]
    ) <= set(body.keys())
    assert body["inbox_count"] == 2
    assert body["deferred_count"] == 1


def test_summary_recovery_not_needed_at_exactly_40_inbox_notes_fresh_triage(
    client, sqlite_conn
):
    _seed_inbox_notes(sqlite_conn, 40)
    _set_last_triage_at(sqlite_conn, _iso(datetime.utcnow()))

    response = client.get("/api/inbox/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["inbox_count"] == 40
    assert body["recovery_needed"] is False


def test_summary_recovery_needed_at_41_inbox_notes_fresh_triage(client, sqlite_conn):
    _seed_inbox_notes(sqlite_conn, 41)
    _set_last_triage_at(sqlite_conn, _iso(datetime.utcnow()))

    response = client.get("/api/inbox/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["inbox_count"] == 41
    assert body["recovery_needed"] is True


def test_summary_recovery_not_needed_when_triage_slightly_under_72_hours_old(
    client, sqlite_conn
):
    """Timestamp slightly younger than 72h (with margin for test-execution delays).

    Verifies that the "older than 72 hours" boundary is exclusive: a value that
    has not reached 72 hours of age must not trip recovery_needed.
    """
    _seed_inbox_notes(sqlite_conn, 5)
    almost_72h_ago = datetime.utcnow() - timedelta(hours=72) + timedelta(seconds=30)
    _set_last_triage_at(sqlite_conn, _iso(almost_72h_ago))

    response = client.get("/api/inbox/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["inbox_count"] == 5
    assert body["recovery_needed"] is False


def test_summary_recovery_needed_when_triage_73_hours_old(client, sqlite_conn):
    _seed_inbox_notes(sqlite_conn, 5)
    seventy_three_hours_ago = datetime.utcnow() - timedelta(hours=73)
    _set_last_triage_at(sqlite_conn, _iso(seventy_three_hours_ago))

    response = client.get("/api/inbox/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["inbox_count"] == 5
    assert body["recovery_needed"] is True


def test_summary_recovery_not_needed_at_zero_inbox_notes_regardless_of_staleness(
    client, sqlite_conn
):
    ancient = datetime(2000, 1, 1)
    _set_last_triage_at(sqlite_conn, _iso(ancient))

    response = client.get("/api/inbox/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["inbox_count"] == 0
    assert body["recovery_needed"] is False


# ===========================================================================
# last_triage_at is updated on every triage action.
# ===========================================================================

_SENTINEL_OLD_TRIAGE = "2000-01-01T00:00:00"


def test_patch_deal_id_updates_last_triage_at(client, sqlite_conn):
    stage_id = _first_stage_id(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "Acme", stage_id)
    note_id = _insert_note(sqlite_conn, body="triage")
    _set_last_triage_at(sqlite_conn, _SENTINEL_OLD_TRIAGE)

    response = client.patch(f"/api/notes/{note_id}", json={"deal_id": deal_id})

    assert response.status_code == 200
    assert _get_last_triage_at(sqlite_conn) != _SENTINEL_OLD_TRIAGE


def test_patch_status_updates_last_triage_at(client, sqlite_conn):
    note_id = _insert_note(sqlite_conn, body="status triage")
    _set_last_triage_at(sqlite_conn, _SENTINEL_OLD_TRIAGE)

    response = client.patch(f"/api/notes/{note_id}", json={"status": "archived"})

    assert response.status_code == 200
    assert _get_last_triage_at(sqlite_conn) != _SENTINEL_OLD_TRIAGE


def test_delete_note_updates_last_triage_at(client, sqlite_conn):
    note_id = _insert_note(sqlite_conn, body="delete triage")
    _set_last_triage_at(sqlite_conn, _SENTINEL_OLD_TRIAGE)

    response = client.delete(f"/api/notes/{note_id}")

    assert response.status_code == 204
    assert _get_last_triage_at(sqlite_conn) != _SENTINEL_OLD_TRIAGE


def test_bulk_attach_updates_last_triage_at(client, sqlite_conn):
    stage_id = _first_stage_id(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "Acme", stage_id)
    note_id = _insert_note(sqlite_conn, body="bulk triage")
    _set_last_triage_at(sqlite_conn, _SENTINEL_OLD_TRIAGE)

    response = client.post(
        "/api/notes/bulk-attach", json={"note_ids": [note_id], "deal_id": deal_id}
    )

    assert response.status_code == 200
    assert _get_last_triage_at(sqlite_conn) != _SENTINEL_OLD_TRIAGE


def test_defer_old_updates_last_triage_at(client, sqlite_conn):
    for i in range(3):
        _insert_note(
            sqlite_conn, body=f"note {i}", created_at=f"2026-01-01T00:00:{i:02d}"
        )
    _set_last_triage_at(sqlite_conn, _SENTINEL_OLD_TRIAGE)

    response = client.post("/api/notes/defer-old", json={"keep": 1})

    assert response.status_code == 200
    assert _get_last_triage_at(sqlite_conn) != _SENTINEL_OLD_TRIAGE
