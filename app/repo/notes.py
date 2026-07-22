"""SQL functions for notes and attachments.

Data-access layer: pure functions ``(conn, ...) -> dict/list``. Routers stay
thin. Times are stored in UTC ISO-8601 (``YYYY-MM-DDTHH:MM:SS``).
"""

from __future__ import annotations

import re
import sqlite3
import urllib.parse
import uuid
from datetime import datetime, timedelta

from .. import config
from ..workdays import _utc_now

# An extension from original_name is allowed only from this whitelist of
# characters; otherwise stored_name is left without an extension. original_name
# is never used as the on-disk file name (path-traversal protection).
_EXT_RE = re.compile(r"^[A-Za-z0-9]{1,10}$")

# Control characters (including CR/LF) are stripped from original_name on save.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_original_name(name: str) -> str:
    """Normalizes a user-supplied name into a form safe to display.

    Clients (including httpx) percent-encode control characters right into the
    multipart header's ``filename``, so we first decode, then truncate the name
    at the first control character (CR/LF etc.) — this removes any header
    injection attempt (``evil\\r\\nX-Injected: 1``).
    """
    decoded = urllib.parse.unquote(name or "")
    # Everything after the first control character is discarded.
    head = _CONTROL_RE.split(decoded, maxsplit=1)[0]
    return head.strip() or "file"


def _safe_ext(name: str) -> str:
    """A safe extension from the file name, or an empty string.

    The extension must contain only letters and digits (A-Z, a-z, 0-9), up to 10
    characters — this protects ``stored_name`` from dangerous extensions and from
    depending on a MIME type the user could have forged. An invalid extension
    (including ``path``, ``exe``) is ignored: the file is stored without an
    extension.
    """
    if "." not in name:
        return ""
    ext = name.rsplit(".", 1)[1]
    if _EXT_RE.match(ext):
        return "." + ext
    return ""


def generate_stored_name(original_name: str) -> str:
    """``<uuid4hex><.ext>`` — the on-disk file name, independent of original_name."""
    return uuid.uuid4().hex + _safe_ext(original_name)


def deal_exists(conn: sqlite3.Connection, deal_id: int) -> bool:
    row = conn.execute("SELECT 1 FROM deals WHERE id = ?", (deal_id,)).fetchone()
    return row is not None


def _attachments_for(conn: sqlite3.Connection, note_id: int) -> list[dict]:
    """The note's attachments in API format.

    Returns an array with ``stored_name`` excluded (the on-disk path is built
    only on the router side), but including ``original_name`` (to show the user)
    and ``mime_type``, ``size_bytes``, ``created_at`` for the UI and download
    links.
    """
    rows = conn.execute(
        """
        SELECT id, note_id, original_name, stored_name, mime_type, size_bytes, created_at
        FROM attachments WHERE note_id = ? ORDER BY id
        """,
        (note_id,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "original_name": r["original_name"],
            "mime_type": r["mime_type"],
            "size_bytes": r["size_bytes"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def _note_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "body": row["body"],
        "status": row["status"],
        "deal_id": row["deal_id"],
        "note_type": row["note_type"],
        "created_at": row["created_at"],
        "suggested_deal_id": row["suggested_deal_id"],
        "suggested_note_type": row["suggested_note_type"],
        "llm_confidence": row["llm_confidence"],
        "llm_status": row["llm_status"],
        "llm_draft": row["llm_draft"],
        "is_pinned": row["is_pinned"],
        "ocr_text": row["ocr_text"],
        "attachments": _attachments_for(conn, row["id"]),
    }


def create_note(
    conn: sqlite3.Connection,
    body: str,
    deal_id: int | None,
    files: list[tuple[str, str, str, int]],
) -> dict:
    """Creates a note and its attachments in a single transaction.

    ``files`` is a list of ``(safe_original_name, mime_type, stored_name,
    size_bytes)``. The files are already written to disk in ``ATTACHMENTS_DIR``
    under ``stored_name`` (streamed, with size-limit enforcement in the router).
    Here only the transactional metadata write happens; on a DB error the
    already-written files are removed so no orphans are left behind. When
    ``deal_id`` is present the note is immediately ``attached`` and bumps the
    item's ``last_activity_at``.
    """
    now = _utc_now()
    status = "attached" if deal_id is not None else "inbox"

    try:
        cur = conn.execute(
            "INSERT INTO notes (body, status, deal_id, created_at) VALUES (?, ?, ?, ?)",
            (body, status, deal_id, now),
        )
        note_id = cur.lastrowid

        for safe_name, mime_type, stored_name, size_bytes in files:
            conn.execute(
                """
                INSERT INTO attachments
                    (note_id, original_name, stored_name, mime_type, size_bytes, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    note_id,
                    safe_name,
                    stored_name,
                    mime_type or "application/octet-stream",
                    size_bytes,
                    now,
                ),
            )

        if deal_id is not None:
            conn.execute(
                "UPDATE deals SET last_activity_at = ? WHERE id = ?",
                (now, deal_id),
            )

        conn.commit()
    except Exception:
        conn.rollback()
        for _safe_name, _mime_type, stored_name, _size_bytes in files:
            (config.ATTACHMENTS_DIR / stored_name).unlink(missing_ok=True)
        raise

    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    return _note_dict(conn, row)


def list_notes(
    conn: sqlite3.Connection,
    status: str,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """Notes of the given status: pinned first, then newest, with attachments."""
    sql = (
        "SELECT * FROM notes WHERE status = ? "
        "ORDER BY is_pinned DESC, created_at DESC, id DESC"
    )
    params: list = [status]
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    elif offset:
        sql += " LIMIT -1 OFFSET ?"
        params.append(offset)
    rows = conn.execute(sql, params).fetchall()
    return [_note_dict(conn, row) for row in rows]


def get_attachment(conn: sqlite3.Connection, attachment_id: int) -> sqlite3.Row | None:
    """Attachment metadata by id, including ``stored_name`` for path building.

    Returns the full row from the ``attachments`` table. The router uses
    ``stored_name`` to safely build the on-disk path (avoiding any dependency on
    ``original_name`` and protecting against path traversal).
    """
    return conn.execute(
        "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
    ).fetchone()


def note_exists(conn: sqlite3.Connection, note_id: int) -> bool:
    row = conn.execute("SELECT 1 FROM notes WHERE id = ?", (note_id,)).fetchone()
    return row is not None


def get_note(conn: sqlite3.Connection, note_id: int) -> dict | None:
    """Note by id in API format (with attachments) or ``None``."""
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        return None
    return _note_dict(conn, row)


def _touch_triage(conn: sqlite3.Connection) -> None:
    """Updates ``app_meta.last_triage_at`` (without commit).

    Called inside every triage action (attach/status/type, delete, bulk-attach,
    defer-old) before commit, so the last-triage timestamp and the change itself
    are committed in a single transaction.
    """
    conn.execute(
        "UPDATE app_meta SET value = ? WHERE key = 'last_triage_at'",
        (_utc_now(),),
    )


def attach_note(
    conn: sqlite3.Connection, note_id: int, deal_id: int, commit: bool = True
) -> dict:
    """Attaches the note to an item: ``status='attached'`` + item update.

    Updates the item's ``last_activity_at`` and ``last_triage_at`` in a single
    transaction. Also works for notes in ``deferred`` status (a deferred note
    can be attached).

    ``commit=False`` leaves the transaction open so the caller can commit the
    attach together with its own writes in one ``conn.commit()`` (used in
    ``confirm_suggestion``/``change_suggestion`` for atomicity of the attach and
    the ``llm_status`` update).
    """
    now = _utc_now()
    conn.execute(
        "UPDATE notes SET deal_id = ?, status = 'attached' WHERE id = ?",
        (deal_id, note_id),
    )
    conn.execute(
        "UPDATE deals SET last_activity_at = ? WHERE id = ?",
        (now, deal_id),
    )
    _touch_triage(conn)
    if commit:
        conn.commit()
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    return _note_dict(conn, row)


def set_status(conn: sqlite3.Connection, note_id: int, status: str) -> dict:
    """Changes the note's status (``archived``/``deferred``)."""
    conn.execute(
        "UPDATE notes SET status = ? WHERE id = ?",
        (status, note_id),
    )
    _touch_triage(conn)
    conn.commit()
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    return _note_dict(conn, row)


def set_note_type(
    conn: sqlite3.Connection, note_id: int, note_type: str | None
) -> dict:
    """Sets/clears the type marker (``task``/``reminder``/``None``).

    The note's status does not change (per F1, the type marker does not remove
    the note from the Inbox).
    """
    conn.execute(
        "UPDATE notes SET note_type = ? WHERE id = ?",
        (note_type, note_id),
    )
    _touch_triage(conn)
    conn.commit()
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    return _note_dict(conn, row)


def set_pinned(conn: sqlite3.Connection, note_id: int, is_pinned: bool) -> dict:
    """Pins/unpins the note (``is_pinned`` 0/1).

    Status and other fields are unchanged; pinning affects only the order in the
    feed (see ``list_notes``).
    """
    conn.execute(
        "UPDATE notes SET is_pinned = ? WHERE id = ?",
        (1 if is_pinned else 0, note_id),
    )
    _touch_triage(conn)
    conn.commit()
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    return _note_dict(conn, row)


def delete_note(conn: sqlite3.Connection, note_id: int) -> list[str] | None:
    """Hard-deletes the note and its attachments (DB rows).

    Returns the list of ``stored_name`` values for the deleted attachments (for
    the router to subsequently delete the files from disk) or ``None`` if the
    note does not exist. The ``attachments`` rows are deleted via cascade
    (``ON DELETE CASCADE`` with ``PRAGMA foreign_keys=ON``).
    """
    if not note_exists(conn, note_id):
        return None
    stored_names = [
        r["stored_name"]
        for r in conn.execute(
            "SELECT stored_name FROM attachments WHERE note_id = ?", (note_id,)
        ).fetchall()
    ]
    conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    _touch_triage(conn)
    conn.commit()
    return stored_names


def bulk_attach(conn: sqlite3.Connection, note_ids: list[int], deal_id: int) -> dict:
    """Attaches several notes to an item (partial success).

    All existing notes are attached in a single transaction. Non-existent
    ``note_id`` values are skipped and listed in ``skipped`` (in input-list
    order). Successfully attached notes update the item's ``last_activity_at``
    (one update at the end). The item's existence is verified by the caller (the
    router) before this call.
    """
    now = _utc_now()
    attached = 0
    skipped: list[int] = []
    for nid in note_ids:
        if not note_exists(conn, nid):
            skipped.append(nid)
            continue
        conn.execute(
            "UPDATE notes SET deal_id = ?, status = 'attached' WHERE id = ?",
            (deal_id, nid),
        )
        attached += 1
    if attached:
        conn.execute(
            "UPDATE deals SET last_activity_at = ? WHERE id = ?",
            (now, deal_id),
        )
    _touch_triage(conn)
    conn.commit()
    return {"attached": attached, "skipped": skipped}


def defer_old(conn: sqlite3.Connection, keep: int) -> dict:
    """Moves all ``inbox`` notes, except the ``keep`` newest, to ``deferred``.

    Runs in a single transaction. The newest are determined by ``(created_at
    DESC, id DESC)`` — the sequence matches the feed order. Does not touch notes
    in ``deferred``/``attached``/``archived`` status.
    """
    keep = max(keep, 0)
    rows = conn.execute(
        "SELECT id FROM notes WHERE status = 'inbox' ORDER BY created_at DESC, id DESC"
    ).fetchall()
    to_defer = [r["id"] for r in rows[keep:]]
    for nid in to_defer:
        conn.execute("UPDATE notes SET status = 'deferred' WHERE id = ?", (nid,))
    _touch_triage(conn)
    conn.commit()
    return {"deferred": len(to_defer)}


def inbox_summary(conn: sqlite3.Connection) -> dict:
    """Inbox summary for the recovery banner.

    ``recovery_needed = inbox_count > 40 OR (inbox_count > 0 AND last_triage_at
    older than 72 hours)``; exactly 72 hours is not yet "older" (the boundary is
    exclusive). When ``inbox_count == 0`` the flag is ``false``, regardless of
    how long ago the last triage was. Also returns ``deferred_count`` and
    ``last_triage_at`` for the UI.
    """
    inbox_count = conn.execute(
        "SELECT COUNT(*) AS c FROM notes WHERE status = 'inbox'"
    ).fetchone()["c"]
    deferred_count = conn.execute(
        "SELECT COUNT(*) AS c FROM notes WHERE status = 'deferred'"
    ).fetchone()["c"]
    triage_row = conn.execute(
        "SELECT value FROM app_meta WHERE key = 'last_triage_at'"
    ).fetchone()
    last_triage_at = triage_row["value"] if triage_row is not None else None

    recovery_needed = False
    if inbox_count > 40:
        recovery_needed = True
    elif inbox_count > 0 and last_triage_at:
        try:
            triage_dt = datetime.strptime(last_triage_at, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            triage_dt = None
        if triage_dt is not None:
            age = datetime.utcnow() - triage_dt
            if age > timedelta(hours=72):
                recovery_needed = True

    return {
        "inbox_count": inbox_count,
        "deferred_count": deferred_count,
        "last_triage_at": last_triage_at,
        "recovery_needed": recovery_needed,
    }
