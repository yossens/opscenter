"""Tests for the config module (app/config.py).

Verify the defaults and env overrides of the remaining parameters (Gemini LLM,
confidence threshold) and that the ``GEMINI_API_KEY`` secret does not leak into
the config module.
"""

from __future__ import annotations

import sys

import pytest


def _purge_app_modules() -> None:
    """Remove all app.* modules from sys.modules so they re-import with a fresh environment."""
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


def test_llm_model_default(monkeypatch) -> None:
    """LLM_MODEL defaults to 'gemini-3.1-flash-lite'."""
    monkeypatch.delenv("OPSCENTER_LLM_MODEL", raising=False)
    _purge_app_modules()

    from app import config

    assert config.LLM_MODEL == "gemini-3.1-flash-lite"


def test_llm_model_override(monkeypatch) -> None:
    """LLM_MODEL is overridden via OPSCENTER_LLM_MODEL."""
    monkeypatch.setenv("OPSCENTER_LLM_MODEL", "gemini-2.0-flash")
    _purge_app_modules()

    from app import config

    assert config.LLM_MODEL == "gemini-2.0-flash"


def test_llm_timeout_s_default(monkeypatch) -> None:
    """LLM_TIMEOUT_S defaults to 30."""
    monkeypatch.delenv("OPSCENTER_LLM_TIMEOUT_S", raising=False)
    _purge_app_modules()

    from app import config

    assert config.LLM_TIMEOUT_S == 30


def test_llm_timeout_s_override(monkeypatch) -> None:
    """LLM_TIMEOUT_S is overridden via OPSCENTER_LLM_TIMEOUT_S."""
    monkeypatch.setenv("OPSCENTER_LLM_TIMEOUT_S", "45")
    _purge_app_modules()

    from app import config

    assert config.LLM_TIMEOUT_S == 45


def test_gemini_api_key_not_in_config_module(monkeypatch) -> None:
    """GEMINI_API_KEY must not be in app/config.py (it is a secret)."""
    monkeypatch.setenv("GEMINI_API_KEY", "secret-gemini-key")
    _purge_app_modules()

    from app import config

    assert not hasattr(config, "GEMINI_API_KEY"), (
        "GEMINI_API_KEY must not be in app/config.py (it is a secret, read directly by the google-genai SDK)"
    )


def test_default_confidence_threshold_default(monkeypatch) -> None:
    """DEFAULT_CONFIDENCE_THRESHOLD defaults to 0.7."""
    monkeypatch.delenv("OPSCENTER_DEFAULT_CONFIDENCE_THRESHOLD", raising=False)
    _purge_app_modules()

    from app import config

    assert config.DEFAULT_CONFIDENCE_THRESHOLD == pytest.approx(0.7)


def test_default_confidence_threshold_override(monkeypatch) -> None:
    """DEFAULT_CONFIDENCE_THRESHOLD is overridden via OPSCENTER_DEFAULT_CONFIDENCE_THRESHOLD."""
    monkeypatch.setenv("OPSCENTER_DEFAULT_CONFIDENCE_THRESHOLD", "0.85")
    _purge_app_modules()

    from app import config

    assert config.DEFAULT_CONFIDENCE_THRESHOLD == pytest.approx(0.85)
