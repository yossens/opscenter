"""JSON API for the hang detector.

``GET /api/pings`` — the "Ping Today" block: computed on the fly on every
request (no cache and no background processes), a fixed number of SQL queries.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, validator

from ..db import get_conn
from ..repo import pings as pings_repo

router = APIRouter(prefix="/api")


class SnoozePayload(BaseModel):
    """Body of ``POST /api/deals/{id}/snooze``.

    ``until`` — a local date ``YYYY-MM-DD`` strictly in the future, or ``None``
    to clear the snooze. An invalid/unparseable date is rejected by pydantic
    (422); a date not later than today by the validator below (422).
    """

    until: date | None = None

    @validator("until", allow_reuse=True)
    
    def _strictly_future(cls, v: date | None) -> date | None:
        if v is not None and v <= date.today():
            raise ValueError("until must be strictly in the future (> today)")
        return v


@router.get("/pings")
def get_pings(
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """The "Ping Today" block: ``{"count": N, "items": [...]}``."""
    return pings_repo.ping_block(conn)


@router.post("/deals/{deal_id}/ping")
def ping_deal(
    deal_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """"Pinged": writes a record to ``deal_pings`` without touching ``last_activity_at``.

    Nonexistent item → 404.
    """
    ok = pings_repo.record_ping(conn, deal_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


@router.post("/deals/{deal_id}/snooze")
def snooze_deal(
    deal_id: int,
    payload: SnoozePayload,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """"Snooze until...": writes ``snoozed_until`` (or clears it when ``null``).

    A date in the past/today/garbage → 422; nonexistent item → 404. Does not
    touch ``last_activity_at``.
    """
    until = payload.until.isoformat() if payload.until is not None else None
    ok = pings_repo.set_snooze(conn, deal_id, until)
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}
