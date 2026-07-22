"""JSON API for hang-detector settings: ping template and hide window M.

``GET /api/settings/ping`` — current ``template``/``hidden_days`` from
``app_meta`` (with a fallback to defaults) plus ``default_template`` for the
"reset to default" button. ``PUT /api/settings/ping`` — writes validated values
to ``app_meta``.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, validator

from ..db import get_conn
from ..repo import pings as pings_repo

router = APIRouter(prefix="/api")


class PingSettingsPut(BaseModel):
    """Body of ``PUT /api/settings/ping``.

    ``template`` — a non-empty (after strip) ping template; ``hidden_days`` — an
    integer ``0 <= M <= 365`` (M=0 is allowed — the item returns immediately;
    upper bound 365). Violating any condition → 422, ``app_meta`` is unchanged.
    """

    template: str
    hidden_days: int = Field(ge=0, le=365)

    @validator("template", allow_reuse=True)
    
    def _template_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("template cannot be empty")
        return v


@router.get("/settings/ping")
def get_ping_settings(
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Current detector settings + default template."""
    return pings_repo.get_ping_settings_view(conn)


@router.put("/settings/ping")
def put_ping_settings(
    payload: PingSettingsPut,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Saves ``template``/``hidden_days`` to ``app_meta``."""
    pings_repo.set_ping_settings(conn, payload.template, payload.hidden_days)
    return {"ok": True}
