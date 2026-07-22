"""T3 tests: note creation, attachments, and the feed.

Acceptance criteria come from docs/specs/001-step1-inbox-pipeline.md, task T3
(and the related edge cases from the "Risks and edge cases" section: path
traversal via the filename, header injection / non-ASCII characters in
``Content-Disposition``). The tests are written against the spec, not the
implementation — the files ``app/routers/notes.py``,
``app/routers/attachments.py`` and ``app/repo/notes.py`` do not yet exist at
the time these tests are written (a correct TDD state: the tests collect but
fail).

Only fixtures from ``tests/conftest.py`` are used: ``client`` (the fully
assembled application on top of an isolated tmp database), ``sqlite_conn`` (a
direct sqlite3 connection to the same database — used to verify invariants on
disk/in the DB that need not be visible through the JSON API), ``config``
(the ``app.config`` module for the ``ATTACHMENTS_DIR``/``DATA_DIR`` paths) and
the new additive fixture ``small_upload_client`` (a TestClient with a reduced
``MAX_UPLOAD_BYTES`` — for the size-limit test, criterion 413).
"""

from __future__ import annotations

import re
import urllib.parse

import pytest
from helpers import _insert_deal

# ---------------------------------------------------------------------------
# Helper functions for working with the DB directly (bypassing the API where a
# criterion explicitly requires checking "in the DB" / "on disk" rather than
# through the JSON response).
# ---------------------------------------------------------------------------


def _first_stage_id(sqlite_conn) -> int:
    row = sqlite_conn.execute(
        "SELECT id FROM stages ORDER BY position LIMIT 1"
    ).fetchone()
    assert row is not None, "expected at least one seeded stage"
    return row["id"]


def _deal_last_activity_at(sqlite_conn, deal_id: int) -> str:
    row = sqlite_conn.execute(
        "SELECT last_activity_at FROM deals WHERE id = ?", (deal_id,)
    ).fetchone()
    assert row is not None
    return row["last_activity_at"]


def _attachment_row(sqlite_conn, attachment_id: int):
    row = sqlite_conn.execute(
        "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
    ).fetchone()
    assert row is not None, f"attachment {attachment_id} must exist in the DB"
    return row


def _notes_count(sqlite_conn) -> int:
    return sqlite_conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]


def _upload_single_file(client, filename: str, content: bytes, mime: str):
    """POST /api/notes with a single file and no text. Returns httpx.Response."""
    return client.post(
        "/api/notes",
        files={"files": (filename, content, mime)},
    )


# ---------------------------------------------------------------------------
# Text note: lands in the inbox, at the top of the feed, and has created_at.
# ---------------------------------------------------------------------------


def test_post_text_only_note_returns_201_with_inbox_status(client):
    response = client.post("/api/notes", data={"body": "first note"})

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "inbox"
    assert payload["body"] == "first note"
    assert payload.get("created_at")
    assert "id" in payload


def test_text_note_appears_first_in_inbox_feed(client):
    first = client.post("/api/notes", data={"body": "old note"}).json()
    second = client.post("/api/notes", data={"body": "new note"}).json()

    feed = client.get("/api/notes", params={"status": "inbox"}).json()

    assert isinstance(feed, list)
    assert feed[0]["id"] == second["id"]
    assert feed[1]["id"] == first["id"]


# ---------------------------------------------------------------------------
# Note with a file and no text: body='', the attachment is saved to disk, the
# metadata is correct, and a comment on the file is optional.
# ---------------------------------------------------------------------------


def test_post_file_only_note_has_empty_body_and_inbox_status(client):
    response = _upload_single_file(
        client, "screenshot.png", b"\x89PNG-fake-bytes", "image/png"
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["body"] == ""
    assert payload["status"] == "inbox"
    assert len(payload["attachments"]) == 1
    assert "id" in payload["attachments"][0]


def test_file_attachment_metadata_stored_correctly_in_db(client, sqlite_conn, config):
    content = b"docx-bytes-payload"
    response = _upload_single_file(
        client,
        "report.docx",
        content,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    attachment_id = response.json()["attachments"][0]["id"]

    row = _attachment_row(sqlite_conn, attachment_id)
    assert row["original_name"] == "report.docx"
    assert (
        row["mime_type"]
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert row["size_bytes"] == len(content)

    stored_path = config.ATTACHMENTS_DIR / row["stored_name"]
    assert stored_path.is_file()
    assert stored_path.read_bytes() == content


def test_get_attachment_returns_identical_bytes(client):
    content = b"binary content, not text \x00\x01\x02"
    response = _upload_single_file(
        client, "blob.bin", content, "application/octet-stream"
    )
    attachment_id = response.json()["attachments"][0]["id"]

    download = client.get(f"/api/attachments/{attachment_id}")

    assert download.status_code == 200
    assert download.content == content


# ---------------------------------------------------------------------------
# Path traversal: original_name never determines the path on disk.
# ---------------------------------------------------------------------------


def test_path_traversal_in_original_name_does_not_escape_attachments_dir(
    client, sqlite_conn, config
):
    malicious_name = "..\\..\\evil.py"
    response = _upload_single_file(
        client, malicious_name, b"malicious payload", "text/x-python"
    )

    assert response.status_code == 201
    attachment_id = response.json()["attachments"][0]["id"]
    row = _attachment_row(sqlite_conn, attachment_id)

    stored_name = row["stored_name"]
    # stored_name is "<uuid4hex><.ext>" and never contains path fragments.
    assert "evil" not in stored_name
    assert ".." not in stored_name
    assert "/" not in stored_name
    assert "\\" not in stored_name
    assert re.fullmatch(r"[0-9a-fA-F]{32}(\.[A-Za-z0-9]{1,10})?", stored_name), (
        stored_name
    )

    # The file lives strictly inside the attachments directory.
    stored_path = config.ATTACHMENTS_DIR / stored_name
    assert stored_path.is_file()
    assert stored_path.resolve().parent == config.ATTACHMENTS_DIR.resolve()

    # No evil.py file appears anywhere outside the attachments directory.
    assert not any(p.name == "evil.py" for p in config.DATA_DIR.rglob("*"))


def test_path_traversal_forward_slash_variant_stays_inside_attachments_dir(
    client, sqlite_conn, config
):
    malicious_name = "../../../etc/evil_passwd"
    response = _upload_single_file(client, malicious_name, b"payload", "text/plain")

    assert response.status_code == 201
    attachment_id = response.json()["attachments"][0]["id"]
    row = _attachment_row(sqlite_conn, attachment_id)

    stored_name = row["stored_name"]
    assert "/" not in stored_name
    assert ".." not in stored_name
    stored_path = config.ATTACHMENTS_DIR / stored_name
    assert stored_path.is_file()
    assert stored_path.resolve().parent == config.ATTACHMENTS_DIR.resolve()
    assert not any(p.name == "evil_passwd" for p in config.DATA_DIR.rglob("*"))


# ---------------------------------------------------------------------------
# 422 on an empty request; 413 when the size limit is exceeded.
# ---------------------------------------------------------------------------


def test_post_note_without_text_or_files_returns_422(client):
    response = client.post("/api/notes", data={"body": ""}, files={})

    assert response.status_code == 422


def test_post_note_completely_empty_multipart_returns_422(client):
    response = client.post("/api/notes", files={})

    assert response.status_code == 422


def test_post_note_file_exceeding_configured_limit_returns_413(small_upload_client):
    # small_upload_client patches app.config.MAX_UPLOAD_BYTES = 10 bytes.
    oversized_content = b"x" * 1024

    response = small_upload_client.post(
        "/api/notes",
        files={"files": ("big.txt", oversized_content, "text/plain")},
    )

    assert response.status_code == 413


# ---------------------------------------------------------------------------
# deal_id on note creation: a quick drop straight onto the card.
# ---------------------------------------------------------------------------


def test_post_note_with_existing_deal_id_is_attached_and_updates_deal_activity(
    client, sqlite_conn
):
    stage_id = _first_stage_id(sqlite_conn)
    deal_id = _insert_deal(sqlite_conn, "Acme", stage_id)
    activity_before = _deal_last_activity_at(sqlite_conn, deal_id)

    response = client.post(
        "/api/notes", data={"body": "note for the item", "deal_id": str(deal_id)}
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "attached"
    assert payload["deal_id"] == deal_id

    activity_after = _deal_last_activity_at(sqlite_conn, deal_id)
    assert activity_after != activity_before


def test_post_note_with_nonexistent_deal_id_returns_404_and_no_note_created(
    client, sqlite_conn
):
    count_before = _notes_count(sqlite_conn)

    response = client.post("/api/notes", data={"body": "note", "deal_id": "999999"})

    assert response.status_code == 404
    assert _notes_count(sqlite_conn) == count_before


# ---------------------------------------------------------------------------
# GET /api/notes: filter by status, pagination, attachments on every note.
# ---------------------------------------------------------------------------


def test_get_notes_inbox_newest_first_with_pagination(client):
    created_ids = []
    for i in range(5):
        payload = client.post("/api/notes", data={"body": f"note {i}"}).json()
        created_ids.append(payload["id"])
    expected_newest_first = list(reversed(created_ids))

    all_inbox = client.get("/api/notes", params={"status": "inbox"}).json()
    assert [n["id"] for n in all_inbox] == expected_newest_first
    for note in all_inbox:
        assert "attachments" in note
        assert isinstance(note["attachments"], list)

    paged = client.get(
        "/api/notes", params={"status": "inbox", "limit": 2, "offset": 1}
    ).json()
    assert [n["id"] for n in paged] == expected_newest_first[1:3]


def test_get_notes_filter_by_status_excludes_other_statuses(client):
    client.post("/api/notes", data={"body": "stays in inbox"})

    deferred_feed = client.get("/api/notes", params={"status": "deferred"}).json()
    archived_feed = client.get("/api/notes", params={"status": "archived"}).json()

    assert deferred_feed == []
    assert archived_feed == []


# ---------------------------------------------------------------------------
# GET /api/attachments/{id}: 404, Content-Disposition inline/attachment, nosniff
# ---------------------------------------------------------------------------


def test_get_attachment_nonexistent_id_returns_404(client):
    response = client.get("/api/attachments/999999")

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("filename", "mime", "expected_disposition_prefix"),
    [
        ("photo.png", "image/png", "inline"),
        ("photo.jpg", "image/jpeg", "inline"),
        ("report.docx", "application/octet-stream", "attachment"),
    ],
)
def test_content_disposition_inline_for_images_attachment_for_others(
    client, filename, mime, expected_disposition_prefix
):
    response = _upload_single_file(client, filename, b"content-bytes", mime)
    attachment_id = response.json()["attachments"][0]["id"]

    download = client.get(f"/api/attachments/{attachment_id}")

    assert download.status_code == 200
    disposition = download.headers.get("content-disposition", "")
    assert disposition.lower().startswith(expected_disposition_prefix)


def test_svg_attachment_is_never_served_inline(client):
    svg_content = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
    response = _upload_single_file(client, "diagram.svg", svg_content, "image/svg+xml")
    attachment_id = response.json()["attachments"][0]["id"]

    download = client.get(f"/api/attachments/{attachment_id}")

    assert download.status_code == 200
    disposition = download.headers.get("content-disposition", "")
    assert disposition.lower().startswith("attachment")
    assert "inline" not in disposition.lower()


def test_attachment_response_has_nosniff_header(client):
    response = _upload_single_file(client, "notes.txt", b"plain text", "text/plain")
    attachment_id = response.json()["attachments"][0]["id"]

    download = client.get(f"/api/attachments/{attachment_id}")

    assert download.status_code == 200
    assert download.headers.get("x-content-type-options") == "nosniff"


def test_svg_attachment_response_also_has_nosniff_header(client):
    response = _upload_single_file(
        client, "diagram.svg", b"<svg></svg>", "image/svg+xml"
    )
    attachment_id = response.json()["attachments"][0]["id"]

    download = client.get(f"/api/attachments/{attachment_id}")

    assert download.headers.get("x-content-type-options") == "nosniff"


# ---------------------------------------------------------------------------
# Non-ASCII filename: RFC 5987 filename*=UTF-8''...
# ---------------------------------------------------------------------------


def test_cyrillic_filename_encoded_via_rfc5987(client):
    original_name = "Résumé café.docx"
    response = _upload_single_file(
        client,
        original_name,
        b"docx bytes",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert response.status_code == 201
    attachment_id = response.json()["attachments"][0]["id"]

    download = client.get(f"/api/attachments/{attachment_id}")

    assert download.status_code == 200
    disposition = download.headers.get("content-disposition", "")
    assert "filename*=UTF-8''" in disposition

    match = re.search(r"filename\*=UTF-8''([^;\s]+)", disposition)
    assert match is not None, disposition
    decoded = urllib.parse.unquote(match.group(1))
    assert decoded == original_name


# ---------------------------------------------------------------------------
# Header injection: CR/LF and quotes in original_name must not create extra
# headers.
# ---------------------------------------------------------------------------


def test_crlf_in_original_name_does_not_inject_header_or_crash(client):
    evil_name = "evil\r\nX-Injected: 1.txt"
    response = _upload_single_file(client, evil_name, b"payload", "text/plain")

    assert response.status_code == 201
    attachment_id = response.json()["attachments"][0]["id"]

    download = client.get(f"/api/attachments/{attachment_id}")

    assert download.status_code == 200
    assert download.status_code != 500
    header_names_lower = {name.lower() for name in download.headers.keys()}
    assert "x-injected" not in header_names_lower

    disposition = download.headers.get("content-disposition", "")
    assert "\r" not in disposition
    assert "\n" not in disposition
    assert "X-Injected" not in disposition


def test_quotes_in_original_name_do_not_break_content_disposition_header(client):
    evil_name = 'my"quoted"file.txt'
    response = _upload_single_file(client, evil_name, b"payload", "text/plain")

    assert response.status_code == 201
    attachment_id = response.json()["attachments"][0]["id"]

    download = client.get(f"/api/attachments/{attachment_id}")

    # The header must be valid and parse in httpx without errors (otherwise the
    # request above would already have raised while parsing the response), and
    # there must be no 500.
    assert download.status_code == 200
    assert "content-disposition" in {k.lower() for k in download.headers.keys()}
