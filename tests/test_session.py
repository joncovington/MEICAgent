"""Unit tests for session.py's thread-local, lazily-constructed tastytrade
Session cache. tastytrade.Session itself is monkeypatched out entirely --
no real OAuth call is ever made.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import session
import credentials


@pytest.fixture(autouse=True)
def _reset():
    session.reset_session()
    yield
    session.reset_session()


def test_get_session_raises_when_credentials_missing(monkeypatch):
    monkeypatch.setattr(session, "missing_secrets", lambda: [credentials.CLIENT_SECRET])
    with pytest.raises(credentials.CredentialError):
        session.get_session()


def test_get_session_constructs_and_caches_session(monkeypatch):
    created = []

    class _FakeSession:
        def __init__(self, client_secret, refresh_token, is_test):
            created.append((client_secret, refresh_token, is_test))

    monkeypatch.setattr(session, "missing_secrets", lambda: [])
    monkeypatch.setattr(session, "get_secret", lambda key: f"value-{key}")
    monkeypatch.setattr(session, "Session", _FakeSession)

    result1 = session.get_session()
    result2 = session.get_session()

    assert result1 is result2  # cached, not reconstructed
    assert len(created) == 1
    assert created[0] == (f"value-{credentials.CLIENT_SECRET}", f"value-{credentials.REFRESH_TOKEN}", False)


def test_reset_session_clears_cache(monkeypatch):
    class _FakeSession:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(session, "missing_secrets", lambda: [])
    monkeypatch.setattr(session, "get_secret", lambda key: "x")
    monkeypatch.setattr(session, "Session", _FakeSession)

    first = session.get_session()
    session.reset_session()
    second = session.get_session()
    assert first is not second


def test_session_is_thread_local_not_shared(monkeypatch):
    """Deliberate design point (see session.py's docstring): each thread must get
    its own Session instance, since a shared one silently hangs awaits from a
    second event loop."""
    class _FakeSession:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(session, "missing_secrets", lambda: [])
    monkeypatch.setattr(session, "get_secret", lambda key: "x")
    monkeypatch.setattr(session, "Session", _FakeSession)

    main_thread_session = session.get_session()
    other_thread_session = []

    def _in_other_thread():
        other_thread_session.append(session.get_session())

    t = threading.Thread(target=_in_other_thread)
    t.start()
    t.join()

    assert other_thread_session[0] is not main_thread_session
