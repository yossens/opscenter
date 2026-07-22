"""OpsCenter's single outbound gateway to the Gemini API (Step 3, T2).

All of the application's outbound traffic to ``generativelanguage.googleapis.com``
goes strictly through this module. The public function ``call_structured`` is
synchronous (the application is single-user), performs a structured model call
with pydantic validation of the response, retries, and writes one metadata row to
``llm_calls`` per call.

Strict security requirements (spec 003, T2, criterion 5):

- The API key appears nowhere in the code — the ``google-genai`` SDK reads it
  directly from the environment (``GEMINI_API_KEY``); the client is built lazily.
- The prompt content and the model's response go nowhere: not into logs, not into
  ``print``/stdout/stderr, not into exception text/arguments, not into
  ``llm_calls``. Only metadata is logged and stored (model, token count,
  duration, status, purpose).
"""

from __future__ import annotations

import logging
import sqlite3
import time

from pydantic import BaseModel, ValidationError

from . import config
from .workdays import _utc_now

logger = logging.getLogger(__name__)

# Backoff before the single network retry (seconds).
_NETWORK_BACKOFF_S = 1.0


class LLMError(Exception):
    """LLM gateway error.

    Carries only neutral diagnostics (type, call purpose) — it NEVER contains the
    prompt text or the model's response.
    """


def _generate(
    *,
    prompt_text: str,
    images: list[tuple[str, bytes]],
    response_model: type[BaseModel],
    timeout_s: float,
):
    """The only function that performs the real Gemini API call.

    In tests it is substituted via ``monkeypatch.setattr``. It reads the API key
    from the environment (``GEMINI_API_KEY``) itself; there is no key literal here.
    """
    import os
    import httpx
    import base64

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise LLMError("GEMINI_API_KEY is not set in environment")

    # Build the URL for the REST API (v1beta supports structured output)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.LLM_MODEL}:generateContent?key={api_key}"

    parts = [{"text": prompt_text}]
    for mime_type, data in images:
        parts.append({
            "inline_data": {
                "mime_type": mime_type,
                "data": base64.b64encode(data).decode("utf-8")
            }
        })

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "response_schema": response_model.schema(),
        }
    }

    with httpx.Client(timeout=timeout_s) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()

    class _UsageMock:
        def __init__(self, meta):
            self.prompt_token_count = meta.get("promptTokenCount", 0)
            self.candidates_token_count = meta.get("candidatesTokenCount", 0)

    class _ResponseMock:
        def __init__(self, json_data):
            # Extract the response text
            try:
                self.text = json_data["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError):
                self.text = "{}"
            self.usage_metadata = _UsageMock(json_data.get("usageMetadata", {}))

    return _ResponseMock(data)


def _record_call(
    *,
    status: str,
    input_tokens: int,
    output_tokens: int,
    duration_ms: int,
    purpose: str,
) -> None:
    """Writes exactly one metadata row to ``llm_calls`` (no content)."""
    conn = sqlite3.connect(str(config.DB_PATH))
    try:
        conn.execute(
            "INSERT INTO llm_calls "
            "(created_at, model, input_tokens, output_tokens, duration_ms, "
            "status, purpose) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _utc_now(),
                config.LLM_MODEL,
                input_tokens,
                output_tokens,
                max(0, duration_ms),
                status,
                purpose,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def call_structured(
    *,
    prompt_text: str,
    images: list[tuple[str, bytes]],
    response_model: type[BaseModel],
    purpose: str = "parse_note",
) -> BaseModel:
    """Structured model call with validation, retries and accounting.

    Two distinct retry branches:

    - a pydantic validation error on ``.text`` → retry WITHOUT backoff (another
      ``_generate`` call); a repeated failure → ``LLMError``;
    - a network/SDK error (``_generate`` raises) → backoff via ``time.sleep`` and
      retry; a repeated failure → ``LLMError``.

    Per call, EXACTLY one ``llm_calls`` row is written regardless of the outcome:
    success → ``success`` with tokens from the usage metadata; failure →
    ``error`` with zero tokens.
    """
    timeout_s = config.LLM_TIMEOUT_S
    started = time.monotonic()

    validation_retried = False
    network_retried = False

    while True:
        try:
            response = _generate(
                prompt_text=prompt_text,
                images=images,
                response_model=response_model,
                timeout_s=timeout_s,
            )
        except Exception:
            # Network/SDK error: backoff + one retry.
            if not network_retried:
                network_retried = True
                logger.warning(
                    "LLM call transport error, retrying once (purpose=%s)",
                    purpose,
                )
                time.sleep(_NETWORK_BACKOFF_S)
                continue
            duration_ms = int((time.monotonic() - started) * 1000)
            _record_call(
                status="error",
                input_tokens=0,
                output_tokens=0,
                duration_ms=duration_ms,
                purpose=purpose,
            )
            raise LLMError(
                f"LLM transport error after retry (purpose={purpose})"
            ) from None

        try:
            result = response_model.parse_raw(response.text)
        except ValidationError:
            # Validation error: retry WITHOUT backoff.
            if not validation_retried:
                validation_retried = True
                logger.warning(
                    "LLM response failed validation, retrying once (purpose=%s)",
                    purpose,
                )
                continue
            duration_ms = int((time.monotonic() - started) * 1000)
            _record_call(
                status="error",
                input_tokens=0,
                output_tokens=0,
                duration_ms=duration_ms,
                purpose=purpose,
            )
            raise LLMError(
                f"LLM response failed schema validation after retry (purpose={purpose})"
            ) from None

        usage = response.usage_metadata
        input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
        duration_ms = int((time.monotonic() - started) * 1000)
        _record_call(
            status="success",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
            purpose=purpose,
        )
        return result
