"""JSON API for stages: list, create, rename/threshold, reorder, delete.

``GET /api/stages`` (list by ``position``), ``POST /api/stages`` (append at the
end), ``PATCH /api/stages/{id}`` (``name``/``threshold_days``),
``POST /api/stages/reorder`` (full list of ids in the new order),
``DELETE /api/stages/{id}`` (only an empty, non-terminal stage).
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, validator

from ..db import get_conn
from ..repo import stages as stages_repo

router = APIRouter(prefix="/api")


class StageCreate(BaseModel):
    """Create a stage. ``name`` is required and cannot be empty."""

    name: str
    threshold_days: int = 5

    @validator("name", allow_reuse=True)
    
    def _name_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("name is required and cannot be empty")
        return v


class StagePatch(BaseModel):
    """Partial update of a stage.

    ``name``, ``threshold_days`` and ``track_hangs`` can change; ``is_terminal``
    and ``position`` are not changed through this endpoint (extra fields are
    ignored). ``track_hangs`` in the body is a JSON bool, stored in the DB as 0/1.
    """

    name: str | None = None
    threshold_days: int | None = None
    track_hangs: bool | None = None

    @validator("name", allow_reuse=True)
    
    def _name_not_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("name cannot be empty")
        return v

    @validator("threshold_days", allow_reuse=True)
    
    def _threshold_positive(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("threshold_days must be positive")
        return v


class StageReorder(BaseModel):
    """Reorder stages. ``ordered_ids`` must be a full enumeration of the ids of all existing stages in the new order."""

    ordered_ids: list[int]


@router.get("/stages")
def list_stages(
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[dict]:
    """All stages, sorted by ``position``."""
    return stages_repo.list_stages(conn)


@router.post("/stages")
def create_stage(
    payload: StageCreate,
    conn: sqlite3.Connection = Depends(get_conn),
) -> JSONResponse:
    """Creates a stage last by ``position``; ``threshold_days`` defaults to 5."""
    stage = stages_repo.create_stage(conn, payload.name, payload.threshold_days)
    return JSONResponse(status_code=201, content=stage)


@router.patch("/stages/{stage_id}")
def patch_stage(
    stage_id: int,
    patch: StagePatch,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Changes ``name``/``threshold_days``/``track_hangs``.

    Nonexistent stage → 404; ``track_hangs`` on a terminal stage → 422.
    """
    updates: dict = {}
    for k in ("name", "threshold_days", "track_hangs"):
        if k in patch.__fields_set__:
            value = getattr(patch, k)
            # track_hangs: JSON bool -> plain int 0/1 (flag-column convention).
            updates[k] = int(value) if k == "track_hangs" else value
    result = stages_repo.patch_stage(conn, stage_id, updates)
    if result is None:
        raise HTTPException(status_code=404, detail="Stage not found")
    if result == "terminal_track_hangs":
        raise HTTPException(
            status_code=422,
            detail="track_hangs of a terminal stage cannot be changed",
        )
    return result


_DELETE_CONFLICT_DETAILS = {
    "terminal": "A terminal stage cannot be deleted — the archive depends on it",
    "has_deals": "The stage has items — move them to another stage first",
    "last": "The last working stage cannot be deleted",
}


@router.delete("/stages/{stage_id}")
def delete_stage(
    stage_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Deletes an empty, non-terminal stage.

    Nonexistent stage → 404; terminal, non-empty or last working stage → 409
    with a human-readable reason in ``detail``.
    """
    result = stages_repo.delete_stage(conn, stage_id)
    if result == "not_found":
        raise HTTPException(status_code=404, detail="Stage not found")
    if result != "ok":
        raise HTTPException(status_code=409, detail=_DELETE_CONFLICT_DETAILS[result])
    return {"ok": True}


@router.post("/stages/reorder")
def reorder_stages(
    payload: StageReorder,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Reorders stages by a full list of ids.

    If the list does not cover exactly all existing ids (missing/duplicate/extra)
    → 422, the order is unchanged.
    """
    ok = stages_repo.reorder_stages(conn, payload.ordered_ids)
    if not ok:
        raise HTTPException(
            status_code=422,
            detail="ordered_ids must be a permutation of all stage ids",
        )
    return {"ok": True}
