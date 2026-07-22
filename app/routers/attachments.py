"""Serving attachment files.

``GET /api/attachments/{id}`` — the path is built only from ``stored_name`` in
the DB (no user-supplied path in the request). ``Content-Disposition`` is
encoded via RFC 5987 (``filename*=UTF-8''...``) rather than by manually
concatenating the raw ``original_name``. ``inline`` for images (except SVG,
which is always ``attachment``), ``attachment`` for everything else. Every
response carries ``X-Content-Type-Options: nosniff``.
"""

from __future__ import annotations

import sqlite3
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from .. import config
from ..db import get_conn
from ..repo import notes as notes_repo

router = APIRouter(prefix="/api")

# Image MIME types safe for inline display. SVG is excluded on purpose
# (it can contain active content/scripts — XSS protection).
_INLINE_IMAGE_PREFIX = "image/"
_NEVER_INLINE = {"image/svg+xml"}


def _content_disposition(mime_type: str, original_name: str) -> str:
    """Content-Disposition header with RFC 5987 encoding of non-ASCII names.

    SVG is always served with ``attachment`` (not ``inline``) to avoid executing
    active content / scripts during preview. Other images (JPEG, PNG, etc.) may
    be ``inline``. The ``filename*=UTF-8''<percent-codes>`` encoding lets the
    header carry non-ASCII names without breaking HTTP structure: control and
    special characters are percent-encoded, which guards against header injection
    and ensures non-ASCII names display correctly in browsers.
    """
    disposition_type = (
        "inline"
        if mime_type.startswith(_INLINE_IMAGE_PREFIX) and mime_type not in _NEVER_INLINE
        else "attachment"
    )
    encoded = urllib.parse.quote(original_name, safe="")
    return f"{disposition_type}; filename*=UTF-8''{encoded}"


@router.get("/attachments/{attachment_id}")
def get_attachment(
    attachment_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> FileResponse:
    """Serve an attachment file with a safe Content-Disposition header.

    The on-disk path is built exclusively from the DB ``stored_name``
    (UUID+extension), never from ``attachment_id`` in the URL or from
    ``original_name``. This guards against path traversal
    (``../../../etc/passwd``) and ensures the user cannot access files outside the
    attachments directory. The ``X-Content-Type-Options: nosniff`` header
    prevents the browser from reinterpreting the MIME type (XSS protection
    against a ``.txt`` with HTML content).
    """
    row = notes_repo.get_attachment(conn, attachment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Attachment not found")

    path = config.ATTACHMENTS_DIR / row["stored_name"]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Attachment file is missing")

    mime_type = row["mime_type"] or "application/octet-stream"
    response = FileResponse(str(path), media_type=mime_type)
    response.headers["Content-Disposition"] = _content_disposition(
        mime_type, row["original_name"]
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
