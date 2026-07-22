"""JSON API for LLM parsing of the Inbox (Step 3, T4).

Endpoints for parsing a single note and working with its suggestion:

- ``POST /api/notes/{id}/parse`` — runs parsing through the service
  (``parse_service → llm_client``), stores the suggestion and returns the
  serialized note with llm fields plus ``skipped_images``. All outbound traffic
  goes ONLY through ``app/parse_service.py`` → ``app/llm_client.py``; the router
  itself makes no network calls.
- ``POST /api/notes/{id}/confirm|change|reject`` — suggestion transitions driven
  by an explicit human action (the "a suggestion is not a change" principle).
- ``GET/PUT /api/settings/parse`` — confidence threshold (mirrors the ping
  settings pattern: falls back to a constant when the key is absent).
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import config, parse_service
from ..db import get_conn
from ..llm_client import LLMError
from ..repo import notes as notes_repo
from ..repo import parsing as parsing_repo

router = APIRouter(prefix="/api")


class ChangeBody(BaseModel):
    """Body of ``POST /api/notes/{id}/change`` — the item chosen by a human."""

    deal_id: int


class ParseSettingsPut(BaseModel):
    """Body of ``PUT /api/settings/parse``.

    ``confidence_threshold`` — a float in ``[0.0, 1.0]``; a violation (including a
    non-numeric value) → 422, ``app_meta`` is unchanged.
    """

    confidence_threshold: float = Field(ge=0.0, le=1.0)


@router.post("/notes/{note_id}/parse")
def parse_note(
    note_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Parses a single note through the service and returns a suggestion."""
    try:
        _, skipped_images = parse_service.parse_note(conn, note_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="note not found") from exc
    except LLMError as exc:
        raise HTTPException(
            status_code=502, detail="LLM gateway failure while parsing the note"
        ) from exc

    note = notes_repo.get_note(conn, note_id)
    note["skipped_images"] = skipped_images
    return note


@router.post("/notes/{note_id}/confirm")
def confirm_note(
    note_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Confirms the suggestion: attaches to ``suggested_deal_id``."""
    note = notes_repo.get_note(conn, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="note not found")
    if note["suggested_deal_id"] is None:
        raise HTTPException(status_code=422, detail="nothing to confirm")

    parsing_repo.confirm_suggestion(conn, note_id)
    return notes_repo.get_note(conn, note_id)


@router.post("/notes/{note_id}/change")
def change_note(
    note_id: int,
    payload: ChangeBody,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Attaches the note to the item chosen by a human (the suggestion is kept)."""
    if not notes_repo.note_exists(conn, note_id):
        raise HTTPException(status_code=404, detail="note not found")
    if not notes_repo.deal_exists(conn, payload.deal_id):
        raise HTTPException(status_code=404, detail="item not found")

    parsing_repo.change_suggestion(conn, note_id, payload.deal_id)
    return notes_repo.get_note(conn, note_id)


@router.post("/notes/{note_id}/reject")
def reject_note(
    note_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Rejects the suggestion: the note stays unattached in the Inbox."""
    if not notes_repo.note_exists(conn, note_id):
        raise HTTPException(status_code=404, detail="note not found")

    parsing_repo.reject_suggestion(conn, note_id)
    return notes_repo.get_note(conn, note_id)


@router.get("/settings/parse")
def get_parse_settings(
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Current confidence threshold + the default constant."""
    return {
        "confidence_threshold": parsing_repo.get_confidence_threshold(conn),
        "default_confidence_threshold": config.DEFAULT_CONFIDENCE_THRESHOLD,
    }


@router.put("/settings/parse")
def put_parse_settings(
    payload: ParseSettingsPut,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Saves the confidence threshold to ``app_meta``."""
    parsing_repo.set_confidence_threshold(conn, payload.confidence_threshold)
    return {"ok": True}
