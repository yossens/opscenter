"""Pure functions of the hang detector: ping string rendering, steps, window.

The module deliberately does not depend on FastAPI/sqlite/starlette — only the
stdlib and the single business-day logic from ``app.workdays`` (the same one
used by Step 1 card-aging). Default values are duplicated as constants
(migration 003 + module) — an intentional duplication; byte-for-byte equality is
verified by a cross-check in T3.
"""

from __future__ import annotations

import re
from datetime import date

from app.workdays import workdays_since

# Default template string — byte-for-byte as in app/migrations/003_hang_detector.sql.
DEFAULT_PING_TEMPLATE = (
    "{waiting_for}, reminder about {counterparty}: waiting on {stage} for {days} "
    "business days. Last status: {last_note}. Any progress?"
)
DEFAULT_PING_HIDDEN_DAYS = 2

# Known template placeholders.
_KNOWN_PLACEHOLDERS = ("counterparty", "stage", "days", "waiting_for", "last_note")

# Non-printable sentinel marker for empty values (see design: collapsing is built
# on sentinel regexes rather than sentence splitting, because template phrases may
# themselves contain periods).
_SENTINEL = "\x00"

_PLACEHOLDER_RE = re.compile(r"\{(" + "|".join(_KNOWN_PLACEHOLDERS) + r")\}")

_ESC = re.escape(_SENTINEL)

# Step 1: "label with a colon" before the marker — from the previous sentence
#         boundary (start of string / . ! ?) up to and including `:`, if only
#         whitespace sits between `:` and the marker. The sentence boundary is kept.
_LABEL_COLON_RE = re.compile(r"([.!?]|^)[^.!?:]*:\s*" + _ESC)
# Step 2: marker + comma + spaces (the "{waiting_for}, reminder about…" case).
_MARKER_COMMA_RE = re.compile(_ESC + r",\s*")
# Step 3: remaining markers together with the preceding comma/whitespace.
_MARKER_LEADING_WS_RE = re.compile(r"[,\s]*" + _ESC)

# Step 4: punctuation normalization.
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([.,;:!?])")
_DOT_SPACES_DOT_RE = re.compile(r"\.[ \t]*\.")
_MULTISPACE_RE = re.compile(r" {2,}")
_LEADING_PUNCT_RE = re.compile(r"^[\s.,;:!?]+")

_MAX_LAST_NOTE_LEN = 120


def escalation_step(pings_since: int) -> int:
    """Displayed escalation step: ``min(pings_since + 1, 3)`` (1..3)."""
    return min(pings_since + 1, 3)


def is_hidden_after_ping(
    last_ping_at: str, today: date, hidden_days: int, pings_since: int
) -> bool:
    """Whether the item is hidden by the M-business-day window after a ping.

    The window applies only when ``pings_since ∈ {1, 2}``; at 0 and at >= 3 the
    window does not apply. Hidden while fewer than M business days have passed
    since the last ping.
    """
    if pings_since not in (1, 2):
        return False
    return workdays_since(last_ping_at, today) < hidden_days


def prepare_last_note(body: str | None) -> str:
    """Prepares the ``{last_note}`` value: newlines/tabs → space, strip, truncate.

    ``None`` or an empty string → ``''``. A string longer than 120 characters is
    truncated to 120 characters + "…" (121 characters total).
    """
    if body is None:
        return ""
    text = body.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    text = text.strip()
    if len(text) > _MAX_LAST_NOTE_LEN:
        text = text[:_MAX_LAST_NOTE_LEN] + "…"
    return text


def render_ping(template: str, values: dict[str, str]) -> str:
    """Renders the ping string from the template, collapsing empty placeholders.

    Known placeholders are substituted; an empty value is substituted as a
    non-printable sentinel, then normalized (empty phrases, dangling
    commas/colons and repeated spaces are removed). Unknown ``{...}`` are kept
    literally.
    """

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        value = values.get(name, "")
        return value if value else _SENTINEL

    text = _PLACEHOLDER_RE.sub(_sub, template)

    if _SENTINEL not in text:
        return text

    return _collapse(text)


def _collapse(text: str) -> str:
    # Step 1: "label with a colon" before the marker (sentence boundary is kept).
    text = _LABEL_COLON_RE.sub(r"\1" + _SENTINEL, text)
    # Step 2: marker + comma + spaces.
    text = _MARKER_COMMA_RE.sub("", text)
    # Step 3: remaining markers with the preceding comma/whitespace.
    text = _MARKER_LEADING_WS_RE.sub("", text)
    # Step 4: punctuation normalization.
    text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    text = _DOT_SPACES_DOT_RE.sub(".", text)
    text = _MULTISPACE_RE.sub(" ", text)
    text = _LEADING_PUNCT_RE.sub("", text)
    return text.strip()
