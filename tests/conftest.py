"""Shared OpsCenter test fixtures.

Each test gets its own isolated data directory via the ``OPSCENTER_DATA_DIR``
environment variable (set to the test's ``tmp_path``), so no test ever creates
or touches the production ``data/`` directory at the project root.

Modules from the ``app`` package are deliberately NOT imported at this file's
module level (only inside fixtures/tests) — this lets ``pytest --collect-only``
collect the tests even before ``app/*`` is implemented (a correct TDD state:
collection passes, execution fails).
"""

from __future__ import annotations

import os
import socket as _socket
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RealNetworkBlockedError(RuntimeError):
    """Raised in place of a real network connection during tests.

    Spec 003, T2, criterion 9: the suite's network barrier. Any unmocked code
    that tries to open a real EXTERNAL network connection (including a genuine
    ``google-genai`` client) must fail loudly and offline, instead of silently
    hitting a paid API. FastAPI/Starlette's TestClient is unaffected: it uses an
    in-process ASGI transport (``starlette.testclient._TestClientTransport``)
    that never opens sockets, so the barrier does not interfere with the
    existing tests that go through the ``client`` fixture.

    Loopback connections (127.0.0.1/::1/localhost) are DELIBERATELY left
    unblocked: on Windows ``asyncio`` emulates ``socketpair()`` (a self-pipe to
    wake the event loop) with a real TCP connection on localhost — an internal
    event-loop mechanism used wherever a test contains async code at all (e.g.
    every TestClient), not an attempt to reach the real network. Only a
    NON-loopback destination is blocked — that is the "real network" under test.
    """


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", "::"}

_ORIGINAL_SOCKET_CONNECT = _socket.socket.connect
_ORIGINAL_SOCKET_CONNECT_EX = _socket.socket.connect_ex
_ORIGINAL_CREATE_CONNECTION = _socket.create_connection


def _address_is_loopback(address) -> bool:
    """True if the destination address is loopback (see docstring above)."""
    try:
        host = address[0]
    except (TypeError, IndexError, KeyError):
        return False
    return host in _LOOPBACK_HOSTS


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch):
    """Autouse network barrier for the whole suite (Spec 003, T2, criterion 9).

    Disabled when ``LLM_SMOKE=1`` — then the single real smoke test
    (``tests/test_llm_client.py``) is allowed to reach the actual Gemini API.
    """
    if os.environ.get("LLM_SMOKE") == "1":
        yield
        return

    def _raise_blocked(address) -> None:
        raise RealNetworkBlockedError(
            f"A real network connection to {address!r} is blocked in the test "
            "suite (tests/conftest.py:_block_real_network). Use monkeypatch "
            "fakes instead of the real network. For a deliberate smoke test set "
            "the LLM_SMOKE=1 environment variable."
        )

    def _guarded_connect(self, address):
        if _address_is_loopback(address):
            return _ORIGINAL_SOCKET_CONNECT(self, address)
        _raise_blocked(address)

    def _guarded_connect_ex(self, address):
        if _address_is_loopback(address):
            return _ORIGINAL_SOCKET_CONNECT_EX(self, address)
        _raise_blocked(address)

    def _guarded_create_connection(address, *args, **kwargs):
        if _address_is_loopback(address):
            return _ORIGINAL_CREATE_CONNECTION(address, *args, **kwargs)
        _raise_blocked(address)

    monkeypatch.setattr(_socket.socket, "connect", _guarded_connect, raising=True)
    monkeypatch.setattr(_socket.socket, "connect_ex", _guarded_connect_ex, raising=True)
    monkeypatch.setattr(
        _socket, "create_connection", _guarded_create_connection, raising=True
    )
    yield


def _purge_app_modules() -> None:
    """Drop cached ``app.*`` modules from sys.modules.

    Needed so that each test setting its own ``OPSCENTER_DATA_DIR`` gets an
    ``app.config`` module (and everything depending on it) that re-reads the
    environment variable, rather than a cached version with paths from a
    previous test.
    """
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


@pytest.fixture
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point OPSCENTER_DATA_DIR at the test's temporary directory."""
    monkeypatch.setenv("OPSCENTER_DATA_DIR", str(tmp_path))
    _purge_app_modules()
    yield tmp_path
    _purge_app_modules()


@pytest.fixture
def config(data_dir):
    """The app.config module, imported after the env variable is set."""
    import app.config as config_module

    return config_module


@pytest.fixture
def db_module(data_dir):
    """The app.db module, imported after the env variable is set."""
    from app import db as db_mod

    return db_mod


@pytest.fixture
def initialized_db(db_module):
    """Call init_db() inside the test's isolated directory."""
    db_module.init_db()
    return db_module


@pytest.fixture
def db_path(config, initialized_db) -> Path:
    return Path(config.DB_PATH)


@pytest.fixture
def sqlite_conn(db_path):
    """A plain stdlib sqlite3 connection to the initialized test DB.

    Deliberately does not use ``app.db.get_conn()`` for most schema/data tests,
    to avoid depending on the FastAPI dependency's implementation details
    (generator vs plain function vs context manager) — those are checked
    separately in tests dedicated to ``get_conn()``. PRAGMA foreign_keys is
    enabled by tests as needed.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def app_instance(data_dir):
    """The assembled FastAPI app (create_app) on top of the isolated DB."""
    from app.main import create_app

    return create_app()


@pytest.fixture
def client(app_instance):
    """TestClient over the isolated app (fires startup events)."""
    from fastapi.testclient import TestClient

    with TestClient(app_instance) as test_client:
        yield test_client


@pytest.fixture
def small_upload_client(data_dir, monkeypatch):
    """TestClient with a reduced ``app.config.MAX_UPLOAD_BYTES`` (413 limit test).

    Patches the ``app.config`` module attribute BEFORE importing routers /
    building the app (after ``_purge_app_modules()`` from ``data_dir``), so it
    works regardless of whether the handler reads the value as a module
    attribute at request time or binds it once at router import
    (``config.py`` documents explicitly: "Overridden by tests").
    """
    import app.config as config_module

    monkeypatch.setattr(config_module, "MAX_UPLOAD_BYTES", 10)

    from app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as test_client:
        yield test_client
