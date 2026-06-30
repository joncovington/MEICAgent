"""Lazy tastytrade OAuth session management."""

from __future__ import annotations

import threading

from tastytrade import Session

from credentials import CredentialError, get_secret, CLIENT_SECRET, REFRESH_TOKEN, missing_secrets

_lock = threading.Lock()
_session: Session | None = None


def get_session() -> Session:
    """Return a cached OAuth session, building it on first use."""
    global _session
    with _lock:
        if _session is None:
            missing = missing_secrets()
            if missing:
                raise CredentialError(
                    f"Missing credentials: {', '.join(missing)}. "
                    "Run `tastytrade-mcp secrets set` to store them."
                )
            client_secret = get_secret(CLIENT_SECRET)
            refresh_token = get_secret(REFRESH_TOKEN)
            _session = Session(client_secret, refresh_token, is_test=False)
        return _session


def reset_session() -> None:
    global _session
    with _lock:
        _session = None
