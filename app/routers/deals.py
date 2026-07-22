"""JSON API for items, the board and the archive.

``POST /api/deals`` (create), ``GET /api/deals`` (search dropdown),
``GET /api/deals/archive`` (archive), ``GET /api/deals/{id}`` (card with feed),
``PATCH /api/deals/{id}`` (edit fields), ``POST /api/deals/{id}/move`` (move
between stages), ``GET /api/board`` (board).
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, validator

from .. import config
from ..db import get_conn
from ..repo import deals as deals_repo

router = APIRouter(prefix="/api")


class DealCreate(BaseModel):
    """Create an item. ``title`` is required; the other fields are optional."""

    title: str
    company: str | None = None
    partner: str | None = None
    rate: float | None = None
    jurisdiction: str | None = None
    waiting_on: str | None = None
    description: str | None = None
    stage_id: int | None = None

    @validator("title", allow_reuse=True)
    
    def _title_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("title is required and cannot be empty")
        return v


class DealPatch(BaseModel):
    """Partial update of card fields.

    Changing ``stage_id`` through this endpoint is forbidden (stage moves happen
    only via ``POST /api/deals/{id}/move``): presence of a ``stage_id`` key → 422.
    """

    company: str | None = None
    partner: str | None = None
    rate: float | None = None
    jurisdiction: str | None = None
    waiting_on: str | None = None
    description: str | None = None
    stage_id: int | None = None


class DealMove(BaseModel):
    stage_id: int


@router.post("/deals")
def create_deal(
    payload: DealCreate,
    conn: sqlite3.Connection = Depends(get_conn),
) -> JSONResponse:
    """Creates an item in the first stage by ``position`` (or an explicit ``stage_id``)."""
    if payload.stage_id is not None:
        if not deals_repo.stage_exists(conn, payload.stage_id):
            raise HTTPException(status_code=404, detail="Stage not found")
        stage_id = payload.stage_id
    else:
        stage_id = deals_repo.first_stage_id(conn)

    data = payload.dict(exclude={"stage_id"})
    deal = deals_repo.create_deal(conn, data, stage_id)
    return JSONResponse(status_code=201, content=deal)


@router.get("/deals")
def list_deals(
    q: str = "",
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    """Search dropdown of active items (FTS prefix; empty ``q`` → all active)."""
    return deals_repo.search_deals(conn, q)


@router.get("/deals/archive")
def deals_archive(
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    """Closed items, newest first by ``closed_at``."""
    return deals_repo.archive(conn)


@router.get("/deals/{deal_id}")
def get_deal(
    deal_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Item card with ``days_in_stage``/``aging_level`` and a notes feed."""
    deal = deals_repo.get_deal(conn, deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return deal


@router.patch("/deals/{deal_id}")
def patch_deal(
    deal_id: int,
    patch: DealPatch,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Edit card fields; updates ``last_activity_at``.

    Trying to change ``stage_id`` through this endpoint → 422 (moves happen only via move).
    """
    if "stage_id" in patch.__fields_set__:
        raise HTTPException(
            status_code=422,
            detail="Changing the stage via PATCH is forbidden — use /move",
        )
    updates = patch.dict(include=set(deals_repo.PATCHABLE_FIELDS))
    updates = {k: v for k, v in updates.items() if k in patch.__fields_set__}
    result = deals_repo.patch_deal(conn, deal_id, updates)
    if result is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return result


@router.delete("/deals/{deal_id}", status_code=204)
def delete_deal(
    deal_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    """Hard-deletes an item and everything related (notes, attachments, pings).

    Attachment files are removed from disk. Nonexistent item → 404.
    """
    stored_names = deals_repo.delete_deal(conn, deal_id)
    if stored_names is None:
        raise HTTPException(status_code=404, detail="Item not found")
    for stored_name in stored_names:
        (config.ATTACHMENTS_DIR / stored_name).unlink(missing_ok=True)
    return Response(status_code=204)


@router.post("/deals/{deal_id}/move")
def move_deal(
    deal_id: int,
    payload: DealMove,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Move an item between stages.

    Updates ``stage_entered_at`` and ``last_activity_at``; entering a terminal
    stage sets ``closed_at`` (closes the item), leaving it clears it.
    Nonexistent item → 404; nonexistent stage → 404.
    """
    result = deals_repo.move_deal(conn, deal_id, payload.stage_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Item not found")
    if result.get("__stage_not_found__"):
        raise HTTPException(status_code=404, detail="Stage not found")
    return result


@router.get("/board")
def get_board(
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    """Board columns by ``position`` with cards and counters."""
    return deals_repo.board(conn)
