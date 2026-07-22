"""OpsCenter path and limit configuration.

All paths are derived from the ``OPSCENTER_DATA_DIR`` environment variable
(default ``<project root>/data``). There are no secrets in Step 1. Values are
read at module import time — tests re-import it with an isolated directory.
"""

from __future__ import annotations

import os
from pathlib import Path

# Project root: .../opscenter (one level above the app package).
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _data_dir() -> Path:
    """Returns the data directory, overridable via env or default ``<root>/data``."""
    override = os.environ.get("OPSCENTER_DATA_DIR")
    if override:
        return Path(override).resolve()
    return (PROJECT_ROOT / "data").resolve()


DATA_DIR: Path = _data_dir()
DB_PATH: Path = DATA_DIR / "opscenter.db"
ATTACHMENTS_DIR: Path = DATA_DIR / "attachments"
BACKUPS_DIR: Path = DATA_DIR / "backups"

# Size limit for a single upload (100 MB). Overridable by tests.
MAX_UPLOAD_BYTES: int = 100 * 1024 * 1024

# Local server network parameters.
HOST = "127.0.0.1"
PORT = 8765

# --- LLM Inbox parsing (Step 3, Gemini) -------------------------------------
# All parameters are non-secret and overridable via OPSCENTER_*.
# The API key secret does NOT appear here — the google-genai SDK reads it
# directly from the environment.
LLM_MODEL: str = os.environ.get("OPSCENTER_LLM_MODEL", "gemini-3.1-flash-lite")
LLM_TIMEOUT_S: int = int(os.environ.get("OPSCENTER_LLM_TIMEOUT_S", "30"))
LLM_NOTE_TEXT_MAX_CHARS: int = int(
    os.environ.get("OPSCENTER_LLM_NOTE_TEXT_MAX_CHARS", "8000")
)
LLM_IMAGE_MAX_BYTES: int = int(
    os.environ.get("OPSCENTER_LLM_IMAGE_MAX_BYTES", str(7 * 1024 * 1024))
)
LLM_IMAGE_MAX_COUNT: int = int(os.environ.get("OPSCENTER_LLM_IMAGE_MAX_COUNT", "4"))
LLM_PRICE_INPUT_PER_1M: float = float(
    os.environ.get("OPSCENTER_LLM_PRICE_INPUT_PER_1M", "1.50")
)
LLM_PRICE_OUTPUT_PER_1M: float = float(
    os.environ.get("OPSCENTER_LLM_PRICE_OUTPUT_PER_1M", "9.00")
)
DEFAULT_CONFIDENCE_THRESHOLD: float = float(
    os.environ.get("OPSCENTER_DEFAULT_CONFIDENCE_THRESHOLD", "0.7")
)
