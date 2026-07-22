"""JSON API for global search and the text "Slice".

``GET /api/search`` — search across items and notes (groups ``deals``/``notes``,
snippets, MATCH sanitization via the shared sanitizer ``app/fts.py``, also used
by the T5 search dropdown); ``GET /api/board/slice`` — flat text of active items
for a messenger.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from ..db import get_conn
from ..repo import search as search_repo

router = APIRouter(prefix="/api")


@router.get("/search")
def search(
    q: str = "",
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Search across items and notes.

    Empty/garbage ``q`` → empty groups, not a 500.
    """
    return search_repo.search(conn, q)


@router.get("/board/slice")
def board_slice(
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Text "Slice": flat text of active items."""
    return {"text": search_repo.board_slice(conn)}
