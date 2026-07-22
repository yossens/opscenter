"""JSON API for notes and the Inbox: creation and feed.

``POST /api/notes`` — multipart: text and/or files (at least one), optional
``deal_id`` to quickly drop into a card. ``GET /api/notes`` — feed by status
with pagination.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import config
from ..db import get_conn
from ..repo import notes as notes_repo

router = APIRouter(prefix="/api")


class NotePatch(BaseModel):
    """Partial update of a note.

    The endpoint contract allows: ``deal_id`` (attach), ``status``
    (``archived``/``deferred``, and also ``attached`` — but only together with
    ``deal_id``), ``note_type`` (``task``/``reminder``/``info``/``null``), and
    ``is_pinned`` (pinning). Invalid ``status``/``note_type`` values are rejected
    by pydantic validation (422). Which fields were actually supplied is
    determined by ``__fields_set__``.
    """

    deal_id: int | None = None
    status: Literal["attached", "archived", "deferred"] | None = None
    note_type: Literal["task", "reminder", "info"] | None = None
    is_pinned: bool | None = None


class BulkAttach(BaseModel):
    note_ids: list[int]
    deal_id: int


class DeferOld(BaseModel):
    keep: int


# Chunk size for streaming reads of an upload. The file is written to disk as it
# is read, and the size limit is checked on each chunk — a large file is aborted
# immediately on exceeding it, without loading fully into memory (DoS protection).
_CHUNK_SIZE = 64 * 1024


@router.post("/notes")
async def create_note(
    body: str = Form(default=""),
    deal_id: int | None = Form(default=None),
    files: list[UploadFile] = File(default=[]),
    conn: sqlite3.Connection = Depends(get_conn),
) -> JSONResponse:
    """Create a note with text and/or files.

    Files are written in a streaming fashion in chunks (64 KB each) with a limit
    check on every chunk — a large file is aborted immediately on exceeding it,
    without loading into memory (DoS protection). On any error before the DB
    commit, the files are removed from disk so no orphans are left. If ``deal_id``
    is given, the note is immediately attached to the item
    (``status='attached'``). An empty request (no text and no files) returns 422.
    """
    body = body or ""
    real_files = [f for f in files if f.filename]

    if not body.strip() and not real_files:
        raise HTTPException(status_code=422, detail="Text or a file is required")

    if deal_id is not None and not notes_repo.deal_exists(conn, deal_id):
        raise HTTPException(status_code=404, detail="Item not found")

    # (safe_original_name, mime_type, stored_name, size_bytes) for files
    # already written to disk.
    prepared: list[tuple[str, str, str, int]] = []
    written_paths: list[Path] = []
    try:
        for upload in real_files:
            safe_name = notes_repo.sanitize_original_name(upload.filename)
            stored_name = notes_repo.generate_stored_name(safe_name)
            dest = config.ATTACHMENTS_DIR / stored_name
            written_paths.append(dest)

            size = 0
            with dest.open("wb") as fh:
                while True:
                    chunk = await upload.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > config.MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            status_code=413, detail="File is too large"
                        )
                    fh.write(chunk)

            prepared.append(
                (
                    safe_name,
                    upload.content_type or "application/octet-stream",
                    stored_name,
                    size,
                )
            )
    except BaseException:
        # Any failure (including 413) — remove partially written files.
        for path in written_paths:
            path.unlink(missing_ok=True)
        raise

    note = notes_repo.create_note(conn, body, deal_id, prepared)
    return JSONResponse(status_code=201, content=note)


@router.get("/notes")
def list_notes(
    status: str = "inbox",
    limit: int | None = None,
    offset: int = 0,
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    """Feed of notes by status with pagination.

    Notes are returned newest first (by ``created_at DESC``), with each note's
    attachments included. The ``limit`` and ``offset`` parameters let you fetch
    the desired page when working with a large Inbox (>>100 notes).
    """
    return notes_repo.list_notes(conn, status, limit, offset)


@router.patch("/notes/{note_id}")
def patch_note(
    note_id: int,
    patch: NotePatch,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Partial update of a note (attach / status / type).

    Any of these actions updates ``last_triage_at``. Attaching to a nonexistent
    item → 404 (the note is unchanged). ``status='attached'`` without ``deal_id``
    is a forbidden combination → 422.

    The difference between "field not supplied" and "field=null" is determined by
    ``__fields_set__``: for example, ``PATCH {"deal_id": null}`` without
    ``status`` performs no operations (the note is not detached; detaching is not
    implemented in Step 1).

    Limitation: a combined PATCH (e.g. ``{"deal_id": 5, "note_type": "task"}``)
    performs separate updates for each field with its own commit, rather than a
    single transaction. This is not required by the current acceptance criteria.
    """
    if not notes_repo.note_exists(conn, note_id):
        raise HTTPException(status_code=404, detail="Note not found")

    fields = patch.__fields_set__

    # Attaching is set either by an explicit deal_id or by status='attached' — in
    # the latter case deal_id is required.
    if patch.status == "attached" and (
        "deal_id" not in fields or patch.deal_id is None
    ):
        raise HTTPException(status_code=422, detail="status='attached' requires deal_id")

    # Attaching to an item and status archived/deferred are mutually exclusive
    # (CHECK constraint: attached ⇔ deal_id IS NOT NULL) — reject the combination explicitly.
    if (
        "deal_id" in fields
        and patch.deal_id is not None
        and patch.status in ("archived", "deferred")
    ):
        raise HTTPException(
            status_code=422,
            detail="cannot attach to an item and change status to archived/deferred at the same time",
        )

    result: dict | None = None

    if "deal_id" in fields and patch.deal_id is not None:
        if not notes_repo.deal_exists(conn, patch.deal_id):
            raise HTTPException(status_code=404, detail="Item not found")
        # LIMITATION: a combined PATCH (deal_id + note_type) creates separate
        # transactions instead of one (each repo-function call has its own commit).
        # Not required by the current T4 acceptance criteria.
        result = notes_repo.attach_note(conn, note_id, patch.deal_id)
    elif "status" in fields and patch.status in ("archived", "deferred"):
        result = notes_repo.set_status(conn, note_id, patch.status)

    if "note_type" in fields:
        result = notes_repo.set_note_type(conn, note_id, patch.note_type)

    if "is_pinned" in fields:
        result = notes_repo.set_pinned(conn, note_id, bool(patch.is_pinned))

    if result is None:
        # Empty/no-op patch: return the current state of the note.
        result = notes_repo.get_note(conn, note_id)

    return result


@router.delete("/notes/{note_id}", status_code=204)
def delete_note(
    note_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Hard delete of a note: DB rows + attachment files from disk.

    Deleting a file that is missing on disk (removed by hand) does not raise an
    error. A repeated delete / nonexistent id → 404.
    """
    stored_names = notes_repo.delete_note(conn, note_id)
    if stored_names is None:
        raise HTTPException(status_code=404, detail="Note not found")
    for stored_name in stored_names:
        (config.ATTACHMENTS_DIR / stored_name).unlink(missing_ok=True)
    return Response(status_code=204)


@router.post("/notes/bulk-attach")
def bulk_attach_notes(
    payload: BulkAttach,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Bulk-attach notes to an item (partial success).

    All existing notes from ``note_ids`` are attached to ``deal_id`` in a single
    transaction. Nonexistent item → 404, no note is changed. Nonexistent
    ``note_id`` values are skipped and listed in ``skipped``; the result is the
    count of successfully attached notes.
    """
    if not notes_repo.deal_exists(conn, payload.deal_id):
        raise HTTPException(status_code=404, detail="Item not found")
    return notes_repo.bulk_attach(conn, payload.note_ids, payload.deal_id)


@router.post("/notes/defer-old")
def defer_old_notes(
    payload: DeferOld,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Defers all inbox notes except the ``keep`` newest.

    Runs in a single transaction. Returns the count of notes moved to the
    ``deferred`` status. Updates ``last_triage_at``.
    """
    return notes_repo.defer_old(conn, payload.keep)


@router.get("/inbox/summary")
def inbox_summary(
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Inbox summary: counters and the ``recovery_needed`` flag.

    ``recovery_needed = inbox_count > 40 OR (inbox_count > 0 AND last_triage_at
    older than 72 hours)``; the boundary is exclusive (exactly 72 hours is still
    ``false``). When ``inbox_count == 0`` the flag is always ``false``, regardless
    of how long ago the last triage was.
    """
    return notes_repo.inbox_summary(conn)
