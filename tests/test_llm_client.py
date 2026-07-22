"""T2 tests: the single outbound gateway ``app/llm_client.py``.

Acceptance criteria source — docs/specs/003-step3-gemini-parsing.md, task T2
(section "Gateway module app/llm_client.py", criteria 1-9). At the time of
writing ``app/llm_client.py`` does not yet exist — this is the expected TDD
state: the file collects, the tests fail until the implementation.

No test touches the real network. The whole set is isolated from the Gemini API
by two independent mechanisms:

1. **monkeypatch seams** (see below) replace the single exit point into the SDK.
2. **The autouse network barrier** ``tests/conftest.py::_block_real_network``
   mutes real sockets for the whole set (except ``LLM_SMOKE=1``) — so that even
   unmocked code, should it try to reach the network, fails offline instead of
   breaking through to the paid API.

The seam contract this set imposes on the implementation (MANDATORY for the
backend-dev — the tests do not relax it):

- ``app.llm_client._generate(*, prompt_text: str, images: list[tuple[str, bytes]],
  response_model: type[BaseModel], timeout_s: float)`` is the single internal
  wrapper function that must perform the actual SDK call
  (``client.models.generate_content(...)``). ``call_structured`` must call
  exactly this function (by name, as an attribute of the ``app.llm_client``
  module) and decide NOTHING about transport itself — this way the tests replace
  it via ``monkeypatch.setattr(llm_client, "_generate", fake)`` without ever
  touching google-genai internals.
- The object ``_generate`` returns on success must be readable by
  ``call_structured`` via the attributes ``.text`` (raw JSON text of the model's
  response) and ``.usage_metadata.prompt_token_count`` /
  ``.usage_metadata.candidates_token_count`` (matching the real field names of
  ``google.genai.types.GenerateContentResponseUsageMetadata`` — verified by
  introspecting the installed package). ``call_structured`` must validate
  ``response_model`` from ``.text`` itself (e.g.
  ``response_model.model_validate_json(response.text)``), and NOT rely on the
  SDK-specific ``.parsed`` attribute — the test fakes do not provide it.
- A pydantic validation error (``.text`` does not pass ``response_model``) and a
  network error (``_generate`` raises an exception) are DIFFERENT branches: the
  first requires no backoff (just a second call to ``_generate``), the second
  must back off via ``time.sleep(...)`` — the ``app.llm_client`` module must
  import ``time`` as a whole (``import time``), not ``from time import sleep``,
  otherwise the test cannot intercept the sleep via
  ``monkeypatch.setattr(llm_client.time, "sleep", ...)``.
- ``call_structured`` does not accept ``conn``: it must open a connection to
  ``config.DB_PATH`` (already initialized by the ``initialized_db`` test fixture
  in an isolated ``tmp_path``) itself and write exactly one ``llm_calls`` row per
  call, regardless of the outcome.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import pytest
from pydantic import BaseModel

from conftest import RealNetworkBlockedError

# Bait markers: if they ever leak into logs/exceptions/stdout, the test catches
# it. Used instead of realistic text so they are not confused with the
# gateway's own service messages.
PROMPT_MARKER = "SEKRET_PROMPT_MARKER_8f3ac1"
RESPONSE_MARKER = "SEKRET_RESPONSE_MARKER_be2210"
NETWORK_ERROR_MARKER = "SEKRET_NETWORK_ERROR_MARKER_11ee02"


class _DummyResult(BaseModel):
    """A minimal pydantic response schema for the tests — deliberately not tied
    to the real ``ParseResult`` from T3, so that the T2 test is independent of T3."""

    value: str
    count: int


@dataclass
class _FakeUsageMetadata:
    prompt_token_count: int
    candidates_token_count: int


@dataclass
class _FakeSdkResponse:
    """A fake response mimicking the fields the gateway actually reads from
    ``google.genai.types.GenerateContentResponse`` (see the seam contract above)."""

    text: str
    usage_metadata: _FakeUsageMetadata


@dataclass
class _RecordingFakeGenerate:
    """A ``_generate`` fake with a call log — for checking the number of attempts
    (retries) and the parameters the gateway passes to the SDK."""

    responses: list
    calls: list = field(default_factory=list)

    def __call__(self, *, prompt_text, images, response_model, timeout_s):
        self.calls.append(
            {
                "prompt_text": prompt_text,
                "images": images,
                "response_model": response_model,
                "timeout_s": timeout_s,
            }
        )
        outcome = self.responses[min(len(self.calls), len(self.responses)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _llm_client(initialized_db):
    """Imports app.llm_client after the test DB is initialized."""
    import app.llm_client as llm_client_module

    return llm_client_module


def _count_llm_calls(sqlite_conn) -> int:
    return sqlite_conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]


def _last_llm_call_row(sqlite_conn):
    row = sqlite_conn.execute(
        "SELECT * FROM llm_calls ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None, "expected at least one llm_calls row"
    return row


# ---------------------------------------------------------------------------
# Criterion 2: successful call.
# ---------------------------------------------------------------------------


def test_call_structured_success_returns_model_instance(
    initialized_db, sqlite_conn, monkeypatch
):
    llm_client = _llm_client(initialized_db)
    fake = _RecordingFakeGenerate(
        responses=[
            _FakeSdkResponse(
                text=json.dumps({"value": "answer", "count": 7}),
                usage_metadata=_FakeUsageMetadata(
                    prompt_token_count=120, candidates_token_count=15
                ),
            )
        ]
    )
    monkeypatch.setattr(llm_client, "_generate", fake)

    result = llm_client.call_structured(
        prompt_text="parse the note",
        images=[],
        response_model=_DummyResult,
        purpose="parse_note",
    )

    assert isinstance(result, _DummyResult)
    assert result.value == "answer"
    assert result.count == 7
    assert len(fake.calls) == 1, "a successful call must not be retried"


def test_call_structured_success_writes_exactly_one_llm_calls_row(
    initialized_db, config, sqlite_conn, monkeypatch
):
    llm_client = _llm_client(initialized_db)
    before = _count_llm_calls(sqlite_conn)
    fake = _RecordingFakeGenerate(
        responses=[
            _FakeSdkResponse(
                text=json.dumps({"value": "x", "count": 1}),
                usage_metadata=_FakeUsageMetadata(
                    prompt_token_count=321, candidates_token_count=44
                ),
            )
        ]
    )
    monkeypatch.setattr(llm_client, "_generate", fake)

    llm_client.call_structured(
        prompt_text="text",
        images=[],
        response_model=_DummyResult,
        purpose="parse_note",
    )

    after = _count_llm_calls(sqlite_conn)
    assert after == before + 1, "expected exactly one new llm_calls row"

    row = _last_llm_call_row(sqlite_conn)
    assert row["status"] == "success"
    assert row["input_tokens"] == 321
    assert row["output_tokens"] == 44
    assert row["duration_ms"] >= 0
    assert row["model"] == config.LLM_MODEL
    assert row["purpose"] == "parse_note"


def test_call_structured_purpose_argument_is_recorded_verbatim(
    initialized_db, sqlite_conn, monkeypatch
):
    llm_client = _llm_client(initialized_db)
    fake = _RecordingFakeGenerate(
        responses=[
            _FakeSdkResponse(
                text=json.dumps({"value": "x", "count": 1}),
                usage_metadata=_FakeUsageMetadata(
                    prompt_token_count=1, candidates_token_count=1
                ),
            )
        ]
    )
    monkeypatch.setattr(llm_client, "_generate", fake)

    llm_client.call_structured(
        prompt_text="text",
        images=[],
        response_model=_DummyResult,
        purpose="custom_purpose_xyz",
    )

    row = _last_llm_call_row(sqlite_conn)
    assert row["purpose"] == "custom_purpose_xyz"


# ---------------------------------------------------------------------------
# Criterion 3: pydantic validation error -> exactly 1 retry -> LLMError.
# ---------------------------------------------------------------------------


def test_call_structured_validation_error_retries_once_then_raises(
    initialized_db, monkeypatch
):
    llm_client = _llm_client(initialized_db)
    invalid = _FakeSdkResponse(
        text=json.dumps({"value": "x"}),  # missing the required "count" field
        usage_metadata=_FakeUsageMetadata(
            prompt_token_count=10, candidates_token_count=5
        ),
    )
    fake = _RecordingFakeGenerate(responses=[invalid, invalid])
    monkeypatch.setattr(llm_client, "_generate", fake)

    with pytest.raises(llm_client.LLMError):
        llm_client.call_structured(
            prompt_text="text",
            images=[],
            response_model=_DummyResult,
            purpose="parse_note",
        )

    assert len(fake.calls) == 2, "expected 1 retry (2 attempts total)"


def test_call_structured_validation_error_writes_error_status_row(
    initialized_db, sqlite_conn, monkeypatch
):
    llm_client = _llm_client(initialized_db)
    invalid = _FakeSdkResponse(
        text="this is not json at all",
        usage_metadata=_FakeUsageMetadata(
            prompt_token_count=0, candidates_token_count=0
        ),
    )
    fake = _RecordingFakeGenerate(responses=[invalid, invalid])
    monkeypatch.setattr(llm_client, "_generate", fake)

    before = _count_llm_calls(sqlite_conn)
    with pytest.raises(llm_client.LLMError):
        llm_client.call_structured(
            prompt_text="text",
            images=[],
            response_model=_DummyResult,
            purpose="parse_note",
        )

    after = _count_llm_calls(sqlite_conn)
    assert after == before + 1, "an error must also write exactly one row"
    row = _last_llm_call_row(sqlite_conn)
    assert row["status"] == "error"


# ---------------------------------------------------------------------------
# Criterion 4: network error -> 1 retry with backoff -> LLMError, tokens 0.
# ---------------------------------------------------------------------------


def test_call_structured_network_error_retries_once_with_backoff_then_raises(
    initialized_db, monkeypatch
):
    llm_client = _llm_client(initialized_db)
    network_exc = ConnectionError(f"boom {NETWORK_ERROR_MARKER}")
    fake = _RecordingFakeGenerate(responses=[network_exc, network_exc])
    monkeypatch.setattr(llm_client, "_generate", fake)

    sleep_calls = []
    monkeypatch.setattr(
        llm_client.time, "sleep", lambda seconds: sleep_calls.append(seconds)
    )

    with pytest.raises(llm_client.LLMError):
        llm_client.call_structured(
            prompt_text="text",
            images=[],
            response_model=_DummyResult,
            purpose="parse_note",
        )

    assert len(fake.calls) == 2, "expected 1 retry (2 attempts total)"
    assert len(sleep_calls) >= 1, "expected a backoff (time.sleep) before the retry"


def test_call_structured_network_error_writes_error_row_with_zero_tokens(
    initialized_db, sqlite_conn, monkeypatch
):
    llm_client = _llm_client(initialized_db)
    network_exc = TimeoutError("network timeout")
    fake = _RecordingFakeGenerate(responses=[network_exc, network_exc])
    monkeypatch.setattr(llm_client, "_generate", fake)
    monkeypatch.setattr(llm_client.time, "sleep", lambda seconds: None)

    before = _count_llm_calls(sqlite_conn)
    with pytest.raises(llm_client.LLMError):
        llm_client.call_structured(
            prompt_text="text",
            images=[],
            response_model=_DummyResult,
            purpose="parse_note",
        )

    after = _count_llm_calls(sqlite_conn)
    assert after == before + 1
    row = _last_llm_call_row(sqlite_conn)
    assert row["status"] == "error"
    assert row["input_tokens"] == 0
    assert row["output_tokens"] == 0


# ---------------------------------------------------------------------------
# Criterion 5: zero logging of content (success and error), including LLMError
# messages, caplog (DEBUG) and stdout/stderr.
# ---------------------------------------------------------------------------


def test_success_path_never_logs_prompt_or_response_content(
    initialized_db, caplog, capsys, monkeypatch
):
    llm_client = _llm_client(initialized_db)
    fake = _RecordingFakeGenerate(
        responses=[
            _FakeSdkResponse(
                text=json.dumps({"value": RESPONSE_MARKER, "count": 1}),
                usage_metadata=_FakeUsageMetadata(
                    prompt_token_count=1, candidates_token_count=1
                ),
            )
        ]
    )
    monkeypatch.setattr(llm_client, "_generate", fake)

    with caplog.at_level(logging.DEBUG):
        result = llm_client.call_structured(
            prompt_text=f"text with marker {PROMPT_MARKER}",
            images=[],
            response_model=_DummyResult,
            purpose="parse_note",
        )

    assert result.value == RESPONSE_MARKER  # the bait actually reached the code

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert PROMPT_MARKER not in log_text
    assert RESPONSE_MARKER not in log_text

    captured = capsys.readouterr()
    assert PROMPT_MARKER not in captured.out
    assert RESPONSE_MARKER not in captured.out
    assert PROMPT_MARKER not in captured.err
    assert RESPONSE_MARKER not in captured.err


def test_validation_error_path_never_logs_or_raises_with_content(
    initialized_db, caplog, capsys, monkeypatch
):
    llm_client = _llm_client(initialized_db)
    invalid = _FakeSdkResponse(
        text=json.dumps({"value": RESPONSE_MARKER}),  # no "count" -> invalid
        usage_metadata=_FakeUsageMetadata(
            prompt_token_count=1, candidates_token_count=1
        ),
    )
    fake = _RecordingFakeGenerate(responses=[invalid, invalid])
    monkeypatch.setattr(llm_client, "_generate", fake)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(llm_client.LLMError) as exc_info:
            llm_client.call_structured(
                prompt_text=f"text with marker {PROMPT_MARKER}",
                images=[],
                response_model=_DummyResult,
                purpose="parse_note",
            )

    error_text = " ".join(str(a) for a in exc_info.value.args) + str(exc_info.value)
    assert PROMPT_MARKER not in error_text
    assert RESPONSE_MARKER not in error_text

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert PROMPT_MARKER not in log_text
    assert RESPONSE_MARKER not in log_text

    captured = capsys.readouterr()
    assert PROMPT_MARKER not in captured.out
    assert RESPONSE_MARKER not in captured.out
    assert PROMPT_MARKER not in captured.err
    assert RESPONSE_MARKER not in captured.err


def test_network_error_path_never_logs_or_raises_with_content(
    initialized_db, caplog, capsys, monkeypatch
):
    llm_client = _llm_client(initialized_db)
    network_exc = ConnectionError(f"low-level socket failure {NETWORK_ERROR_MARKER}")
    fake = _RecordingFakeGenerate(responses=[network_exc, network_exc])
    monkeypatch.setattr(llm_client, "_generate", fake)
    monkeypatch.setattr(llm_client.time, "sleep", lambda seconds: None)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(llm_client.LLMError) as exc_info:
            llm_client.call_structured(
                prompt_text=f"text with marker {PROMPT_MARKER}",
                images=[],
                response_model=_DummyResult,
                purpose="parse_note",
            )

    error_text = " ".join(str(a) for a in exc_info.value.args) + str(exc_info.value)
    assert PROMPT_MARKER not in error_text

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert PROMPT_MARKER not in log_text

    captured = capsys.readouterr()
    assert PROMPT_MARKER not in captured.out
    assert PROMPT_MARKER not in captured.err


def test_llm_calls_table_never_stores_prompt_or_response_content(
    initialized_db, sqlite_conn, monkeypatch
):
    """Extra safeguard: the llm_calls columns (by value, not only by schema —
    the schema is already checked in test_migration_004.py) contain no bait,
    neither on success nor on error."""
    llm_client = _llm_client(initialized_db)
    fake = _RecordingFakeGenerate(
        responses=[
            _FakeSdkResponse(
                text=json.dumps({"value": RESPONSE_MARKER, "count": 1}),
                usage_metadata=_FakeUsageMetadata(
                    prompt_token_count=1, candidates_token_count=1
                ),
            )
        ]
    )
    monkeypatch.setattr(llm_client, "_generate", fake)
    llm_client.call_structured(
        prompt_text=f"text {PROMPT_MARKER}",
        images=[],
        response_model=_DummyResult,
        purpose="parse_note",
    )

    row = _last_llm_call_row(sqlite_conn)
    row_text = " ".join(str(v) for v in tuple(row))
    assert PROMPT_MARKER not in row_text
    assert RESPONSE_MARKER not in row_text


# ---------------------------------------------------------------------------
# Criterion 6: the timeout comes from config.LLM_TIMEOUT_S.
# ---------------------------------------------------------------------------


def test_timeout_passed_to_generate_comes_from_config(
    initialized_db, config, monkeypatch
):
    llm_client = _llm_client(initialized_db)
    monkeypatch.setattr(config, "LLM_TIMEOUT_S", 4242)

    fake = _RecordingFakeGenerate(
        responses=[
            _FakeSdkResponse(
                text=json.dumps({"value": "x", "count": 1}),
                usage_metadata=_FakeUsageMetadata(
                    prompt_token_count=1, candidates_token_count=1
                ),
            )
        ]
    )
    monkeypatch.setattr(llm_client, "_generate", fake)

    llm_client.call_structured(
        prompt_text="text",
        images=[],
        response_model=_DummyResult,
        purpose="parse_note",
    )

    assert len(fake.calls) == 1
    assert fake.calls[0]["timeout_s"] == 4242


# ---------------------------------------------------------------------------
# Criterion 7: the key is not hardcoded as a literal in the gateway source.
# ---------------------------------------------------------------------------


def test_llm_client_source_has_no_hardcoded_api_key(project_root):
    path = project_root / "app" / "llm_client.py"
    assert path.exists(), "app/llm_client.py must exist"
    text = path.read_text(encoding="utf-8")

    # The typical prefix of real Google API keys must not appear in the source
    # in any form (test or production).
    assert "AIza" not in text, (
        "the source must not contain a literal resembling a real API key"
    )

    # The key must not be passed to the client constructor as a string literal
    # (e.g. `api_key="..."`) — only via the environment (the SDK does this itself).
    import re

    literal_api_key_assignment = re.search(r'api_key\s*=\s*["\']', text)
    assert literal_api_key_assignment is None, (
        "app/llm_client.py must not assign api_key a string literal; "
        "the key must be read by the SDK from the environment (GEMINI_API_KEY)"
    )


# ---------------------------------------------------------------------------
# Criterion 8: smoke test of a real call only under LLM_SMOKE=1.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    __import__("os").environ.get("LLM_SMOKE") != "1",
    reason="A real Gemini API call runs only when LLM_SMOKE=1",
)
def test_smoke_real_gemini_call(initialized_db):
    """A real (non-mocked) Gemini call. Requires a real GEMINI_API_KEY in the
    environment (.env) and network. Skipped by default — the very fact of the
    skip in a normal run confirms criterion 8 (see ``pytest -q`` output: the
    line must be marked ``s``/``SKIPPED``)."""
    llm_client = _llm_client(initialized_db)

    class _SmokeAnswer(BaseModel):
        answer: str

    result = llm_client.call_structured(
        prompt_text="Answer in one word: ok",
        images=[],
        response_model=_SmokeAnswer,
        purpose="smoke_test",
    )
    assert isinstance(result, _SmokeAnswer)
    assert result.answer


# ---------------------------------------------------------------------------
# Criterion 9: the set's network barrier — negative test.
# ---------------------------------------------------------------------------


def test_real_external_socket_connect_is_blocked_by_suite_guard():
    """Negative test of the network barrier (the autouse fixture in conftest.py).
    An attempt at a real external TCP connection (not loopback) must fail offline
    with ``RealNetworkBlockedError``, not actually try to connect."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RealNetworkBlockedError):
            sock.connect(("93.184.216.34", 80))  # example.com IP, not loopback
    finally:
        sock.close()


def test_real_create_connection_to_external_host_is_blocked_by_suite_guard():
    import socket

    with pytest.raises(RealNetworkBlockedError):
        socket.create_connection(("generativelanguage.googleapis.com", 443), timeout=1)


def test_loopback_socket_connect_is_not_blocked_by_suite_guard():
    """The barrier must let loopback through (used by the internal self-pipe
    event loop on Windows and by any local ASGI/HTTP test server) — otherwise it
    breaks the existing 419 tests going through TestClient/asyncio."""
    import socket

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client_sock.settimeout(2)
        client_sock.connect(("127.0.0.1", port))  # must not raise
    finally:
        client_sock.close()
        server.close()


def test_llm_error_is_an_exception_subclass(initialized_db):
    llm_client = _llm_client(initialized_db)
    assert issubclass(llm_client.LLMError, Exception)
