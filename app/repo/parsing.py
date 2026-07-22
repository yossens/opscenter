"""Repository for writing LLM Inbox-triage suggestions (Step 3, T3).

The "suggestion != change" principle: ``save_suggestion`` writes the suggestion
columns (``suggested_deal_id``, ``suggested_note_type``, ``llm_confidence``,
``llm_draft``, ``llm_status='suggested'``) plus ``ocr_text`` (a factual
extraction of text from images, not a suggestion) and does NOT touch the
note's confirmed fields (``deal_id``, ``status``, ``note_type``) — those change
only on explicit human confirmation (T4).
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from .. import config
from .notes import attach_note

if TYPE_CHECKING:
    from ..parse_service import ParseResult

_THRESHOLD_KEY = "llm_confidence_threshold"


def save_suggestion(
    conn: sqlite3.Connection, note_id: int, result: "ParseResult"
) -> None:
    """Writes the LLM suggestion into the note's suggestion columns.

    ``result.note_type`` maps to the ``suggested_note_type`` column (the
    suggested type, separate from the confirmed ``note_type``).

    ``result.extracted_text`` is written to ``notes.ocr_text`` as the factual
    OCR extraction (not a suggestion); it does not touch ``body``, ``deal_id``,
    ``status``, or ``note_type``, per the "suggestion != change" principle.
    """
    conn.execute(
        """
        UPDATE notes
           SET suggested_deal_id  = ?,
               suggested_note_type = ?,
               llm_confidence      = ?,
               llm_draft           = ?,
               llm_status          = 'suggested',
               ocr_text            = ?
         WHERE id = ?
        """,
        (
            result.suggested_deal_id,
            result.note_type,
            result.confidence,
            result.draft_text,
            result.extracted_text,
            note_id,
        ),
    )
    conn.commit()


def confirm_suggestion(conn: sqlite3.Connection, note_id: int) -> None:
    """Confirms the suggestion: attach to ``suggested_deal_id``, copy
    ``suggested_note_type`` -> ``note_type``, set ``llm_status='confirmed'``.

    The caller (router) has already verified that the note exists and that its
    ``suggested_deal_id`` is not NULL. Attaching goes through the existing
    ``attach_note`` (reusing the invariant ``(deal_id NOT NULL) =
    (status='attached')`` and the ``deals.last_activity_at`` bump).
    """
    row = conn.execute(
        "SELECT suggested_deal_id, suggested_note_type FROM notes WHERE id = ?",
        (note_id,),
    ).fetchone()
    attach_note(conn, note_id, row["suggested_deal_id"], commit=False)
    conn.execute(
        "UPDATE notes SET note_type = ?, llm_status = 'confirmed' WHERE id = ?",
        (row["suggested_note_type"], note_id),
    )
    conn.commit()


def change_suggestion(conn: sqlite3.Connection, note_id: int, deal_id: int) -> None:
    """Attaches the note to the item chosen by the human and marks
    ``llm_status='rejected'``. ``suggested_deal_id`` is NOT overwritten — the
    data retains both the LLM suggestion and the human's choice.
    """
    attach_note(conn, note_id, deal_id, commit=False)
    conn.execute(
        "UPDATE notes SET llm_status = 'rejected' WHERE id = ?",
        (note_id,),
    )
    conn.commit()


def reject_suggestion(conn: sqlite3.Connection, note_id: int) -> None:
    """Rejects the suggestion: ``llm_status='rejected'``. The note stays
    unattached (``status``/``deal_id`` are not touched), and ``suggested_deal_id``
    is kept for history.
    """
    conn.execute(
        "UPDATE notes SET llm_status = 'rejected' WHERE id = ?",
        (note_id,),
    )
    conn.commit()


def get_confidence_threshold(conn: sqlite3.Connection) -> float:
    """Reads the confidence threshold from ``app_meta`` with a constant fallback.

    If the ``llm_confidence_threshold`` key is missing (or its value is
    non-numeric), ``config.DEFAULT_CONFIDENCE_THRESHOLD`` is returned (the read
    does not crash).
    """
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key = ?",
        (_THRESHOLD_KEY,),
    ).fetchone()
    if row is None:
        return config.DEFAULT_CONFIDENCE_THRESHOLD
    try:
        return float(row["value"])
    except (TypeError, ValueError):
        return config.DEFAULT_CONFIDENCE_THRESHOLD


def set_confidence_threshold(conn: sqlite3.Connection, threshold: float) -> None:
    """Upserts the confidence threshold into ``app_meta`` (the value has already
    been validated by the router to be within ``[0.0, 1.0]``)."""
    conn.execute(
        "INSERT INTO app_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_THRESHOLD_KEY, str(threshold)),
    )
    conn.commit()
