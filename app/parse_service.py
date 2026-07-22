"""Service for LLM parsing of an Inbox note (Step 3, T3).

Assembles a minimized payload for Gemini, goes to the network ONLY through the
``app/llm_client.py`` gateway (module import + attribute call, so that tests can
substitute ``parse_service.llm_client.call_structured``), post-validates the
response, and saves the suggestion via ``app/repo/parsing.py:save_suggestion``.

The "suggestion ≠ change" principle: the service writes only suggestion columns
and does not touch the confirmed note fields (``deal_id``/``status``/``note_type``).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Literal

from pydantic import BaseModel

from . import config, llm_client
from .repo.deals import search_deals
from .repo.parsing import save_suggestion

# Five and only five allowed per-deal fields (data minimization, F2).
_ALLOWED_DEAL_KEYS = ("id", "title", "company", "partner", "stage")


class ParseResult(BaseModel):
    """Structured model response.

    ``confidence`` — a FLAT ``float`` without a pydantic ``ge/le`` constraint:
    clamping into [0, 1] is done by the service's post-validation, not by a schema
    rejection (otherwise the model could never return an out-of-range value and
    the clamping would become dead code).

    ``extracted_text`` — OCR extraction of readable text from the attached images
    (null/empty if there is no text or no images). Recorded as the actual
    extraction in ``notes.ocr_text``; does not touch ``body``.
    """

    suggested_deal_id: int | None
    note_type: Literal["status", "task", "agreement", "reminder", "info"]
    confidence: float
    draft_text: str
    extracted_text: str | None = None


def _deal_prompt_fields(deal: dict) -> dict:
    """The builder's intermediate per-deal representation: EXACTLY the five
    allowed fields ``{id, title, company, partner, stage}`` and nothing more.

    Any other deal column (``rate``, ``jurisdiction``, ``waiting_on``,
    ``description``, ``drive_folder_url``, etc.) does not make it into the prompt.
    """
    return {key: deal.get(key) for key in _ALLOWED_DEAL_KEYS}


def build_prompt_payload(
    active_deals: list[dict],
    note_text: str,
    images: list[tuple[str, bytes]],
) -> tuple[str, list[tuple[str, bytes]], int]:
    """Pure payload assembly (no I/O — reads no files/DB).

    Returns ``(prompt_text, filtered_images, skipped_count)``:
    - ``prompt_text`` — the data block (a directory of active deals from the five
      allowed fields + the already-truncated note text);
    - ``filtered_images`` — images that passed the "Images" section filter
      (mime ``image/*`` except ``image/svg+xml``, size ≤
      ``config.LLM_IMAGE_MAX_BYTES``, no more than ``config.LLM_IMAGE_MAX_COUNT``);
    - ``skipped_count`` — how many attachments were skipped (non-images, svg,
      oversize, extras beyond the count limit).
    """
    deals_repr = [_deal_prompt_fields(deal) for deal in active_deals]
    deals_json = json.dumps(deals_repr, ensure_ascii=False, indent=2)
    prompt_text = (
        "Active items (JSON, reference fields only):\n"
        f"{deals_json}\n\n"
        "Note to parse:\n"
        f"{note_text}"
    )

    kept: list[tuple[str, bytes]] = []
    skipped = 0
    for mime_type, data in images:
        if not mime_type.startswith("image/") or mime_type == "image/svg+xml":
            skipped += 1
            continue
        if len(data) > config.LLM_IMAGE_MAX_BYTES:
            skipped += 1
            continue
        if len(kept) >= config.LLM_IMAGE_MAX_COUNT:
            skipped += 1
            continue
        kept.append((mime_type, data))

    return prompt_text, kept, skipped


def _read_prompt_files() -> tuple[str, str]:
    """Reads the prompt files AFRESH on every call (design decision 7) —
    edits without restarting the process."""
    prompts_dir = config.PROJECT_ROOT / "prompts"
    system = (prompts_dir / "parse_note.md").read_text(encoding="utf-8")
    examples = (prompts_dir / "parse_examples.md").read_text(encoding="utf-8")
    return system, examples


class _OversizePlaceholder:
    """A stub in place of the real bytes of an oversize attachment: it carries
    only the size (via ``len``) so that the size filter in
    ``build_prompt_payload`` rejects it in the normal order (mime → size →
    count) without reading the whole file into memory (an attachment can weigh up
    to 100 MB). The stub is always filtered out and never reaches the gateway."""

    __slots__ = ("_size",)

    def __init__(self, size: int) -> None:
        self._size = size

    def __len__(self) -> int:
        return self._size


def _load_note_images(
    conn: sqlite3.Connection, note_id: int
) -> list[tuple[str, bytes]]:
    """Loads the note's attachment bytes as ``(mime_type, bytes)`` from disk
    (``config.ATTACHMENTS_DIR / stored_name``). Filtering happens in the builder.

    The size is checked against disk (``stat().st_size``) BEFORE reading: an
    oversize file (> ``config.LLM_IMAGE_MAX_BYTES``) is not read into memory;
    instead of the bytes a lightweight stub with the correct length is
    substituted — the builder's size filter rejects it and counts it as skipped
    exactly as before."""
    rows = conn.execute(
        "SELECT stored_name, mime_type FROM attachments WHERE note_id = ? ORDER BY id",
        (note_id,),
    ).fetchall()
    images: list[tuple[str, bytes]] = []
    for row in rows:
        path = config.ATTACHMENTS_DIR / row["stored_name"]
        size = path.stat().st_size
        if size > config.LLM_IMAGE_MAX_BYTES:
            images.append((row["mime_type"], _OversizePlaceholder(size)))
            continue
        images.append((row["mime_type"], path.read_bytes()))
    return images


def parse_note(conn: sqlite3.Connection, note_id: int) -> tuple[ParseResult, int]:
    """Parses a single note via the LLM and saves the suggestion.

    Returns ``(result, skipped_images)``, where ``result`` is an already
    post-validated ``ParseResult`` (an id outside the active list → ``None``,
    ``confidence`` clamped into [0, 1]) that has already been recorded via
    ``save_suggestion``.
    """
    note_row = conn.execute(
        "SELECT body FROM notes WHERE id = ?", (note_id,)
    ).fetchone()
    if note_row is None:
        raise ValueError(f"note {note_id} not found")

    # Active (non-terminal) deals + stage_id -> stage name mapping.
    stage_names = {
        row["id"]: row["name"] for row in conn.execute("SELECT id, name FROM stages")
    }
    active_deals = search_deals(conn, "")
    for deal in active_deals:
        deal["stage"] = stage_names.get(deal["stage_id"])
    active_ids = {deal["id"] for deal in active_deals}

    # Note text truncation happens in the service, BEFORE calling the pure builder.
    note_text = (note_row["body"] or "")[: config.LLM_NOTE_TEXT_MAX_CHARS]

    images = _load_note_images(conn, note_id)

    payload_text, sent_images, skipped_images = build_prompt_payload(
        active_deals, note_text, images
    )

    system, examples = _read_prompt_files()
    prompt_text = f"{system}\n\n{examples}\n\n{payload_text}"

    raw = llm_client.call_structured(
        prompt_text=prompt_text,
        images=sent_images,
        response_model=ParseResult,
        purpose="parse_note",
    )

    # Post-validation: id only from the active list, confidence is clamped.
    suggested_deal_id = (
        raw.suggested_deal_id if raw.suggested_deal_id in active_ids else None
    )
    confidence = max(0.0, min(1.0, raw.confidence))
    result = ParseResult(
        suggested_deal_id=suggested_deal_id,
        note_type=raw.note_type,
        confidence=confidence,
        draft_text=raw.draft_text,
        extracted_text=raw.extracted_text,
    )

    save_suggestion(conn, note_id, result)
    return result, skipped_images
