"""Wiring tests for MEICAgent's session shim over cherrypit-core's SessionManager.

Session-caching / thread-local behavior is covered exhaustively in cherrypit-core's own test suite.
Here we verify MEIC wires a *thread-local* manager bound to its credential store (the deliberate design
point: the streamer's DXLink loop and REST-poller thread must not share one Session) and preserves the
get_session / reset_session API. tastytrade is never constructed — an injected fake factory stands in.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from cherrypit.auth import SessionManager

import session


@pytest.fixture(autouse=True)
def _reset():
    session.reset_session()
    yield
    session.reset_session()


def test_manager_is_thread_local_and_bound_to_meic_store():
    assert isinstance(session._manager, SessionManager)
    assert session._manager._thread_local is True
    assert session._manager._creds.service_name == "meicagent"


def test_public_api_preserved():
    assert callable(session.get_session)
    assert callable(session.reset_session)


def _stub_creds(monkeypatch):
    monkeypatch.setattr(session._manager._creds, "missing_secrets", lambda: [])
    monkeypatch.setattr(session._manager._creds, "get_secret", lambda key: "x")


def test_get_session_caches_within_a_thread(monkeypatch):
    calls = {"n": 0}

    def factory(cs, rt, is_test):
        calls["n"] += 1
        return object()

    _stub_creds(monkeypatch)
    monkeypatch.setattr(session._manager, "_factory", factory)
    first = session.get_session()
    second = session.get_session()
    assert first is second and calls["n"] == 1


def test_session_is_thread_local_not_shared(monkeypatch):
    """Each thread must get its own Session (a shared one silently hangs awaits from a second loop)."""
    def factory(cs, rt, is_test):
        return object()

    _stub_creds(monkeypatch)
    monkeypatch.setattr(session._manager, "_factory", factory)

    main_thread_session = session.get_session()
    other_thread_session = []

    def _in_other_thread():
        other_thread_session.append(session.get_session())

    t = threading.Thread(target=_in_other_thread)
    t.start()
    t.join()

    assert other_thread_session[0] is not main_thread_session
