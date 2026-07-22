"""JSON API for the dashboard (Step 4, T4).

``GET /api/stats`` — pipeline summary: item counts (total / active / closed),
item age in the current stage for each non-terminal stage (average/maximum
business days) and a rollup of the ``llm_calls`` log. Aggregation lives in
``app/repo/dashboard.py``; the router makes no network calls: it only reads from
the local DB. The page route ``GET /dashboard`` (template rendering) belongs to
T8 and is not here.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ..db import get_conn
from ..repo import dashboard as dashboard_repo
from .pages import _render

router = APIRouter(prefix="/api")

# Page route (T8) — no /api prefix, hence a separate router.
page_router = APIRouter()


@page_router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request) -> HTMLResponse:
    """Renders the dashboard page; dashboard.js loads the data via /api/stats."""
    return _render(request, "dashboard.html", "Dashboard")


@router.get("/stats")
def get_stats(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """Aggregated dashboard statistics (see Design -> GET /api/stats)."""
    stages, deals_closed = dashboard_repo.stage_stats(conn, date.today())
    deals_active = sum(s["deal_count"] for s in stages)

    return {
        "deals_total": deals_active + deals_closed,
        "deals_active": deals_active,
        "deals_closed": deals_closed,
        "stages": stages,
        "llm": dashboard_repo.llm_rollup(conn),
    }
