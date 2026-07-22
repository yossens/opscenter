"""JSON API for LLM cost statistics (Step 3, T5).

``GET /api/llm/stats`` — aggregation of the ``llm_calls`` log for "today" and
the "last 30 days" (calls, input/output tokens, $ estimate). The router makes
no network calls: it only reads from the local DB.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from ..db import get_conn
from ..repo import llm_calls as llm_calls_repo

router = APIRouter(prefix="/api")


@router.get("/llm/stats")
def get_llm_stats(
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """LLM cost statistics for today and the last 30 days."""
    return llm_calls_repo.get_stats(conn)
