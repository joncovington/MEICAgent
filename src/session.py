"""Lazy tastytrade OAuth session management."""

from __future__ import annotations

import threading

from tastytrade import Session

from credentials import CredentialError, get_secret, CLIENT_SECRET, REFRESH_TOKEN, missing_secrets

_local = threading.local()


def get_session() -> Session:
    """Return a cached OAuth session, building it on first use.

    Thread-local: tastytrade's Session holds an httpx.AsyncClient created at
    construction time, which becomes bound to whichever event loop first uses
    it. The streamer daemon runs the DXLink connection on the main thread's
    loop and the REST poller on a separate thread with its own loop — sharing
    one Session between them silently hangs any awaits from the second loop.
    A session per thread keeps each one bound to a single loop.
    """
    session = getattr(_local, "session", None)
    if session is None:
        missing = missing_secrets()
        if missing:
            raise CredentialError(
                f"Missing credentials: {', '.join(missing)}. "
                "Run `tastytrade-mcp secrets set` to store them."
            )
        client_secret = get_secret(CLIENT_SECRET)
        refresh_token = get_secret(REFRESH_TOKEN)
        session = Session(client_secret, refresh_token, is_test=False)
        _local.session = session
    return session


def reset_session() -> None:
    _local.session = None
