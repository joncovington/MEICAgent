"""Lazy tastytrade OAuth session management.

Thin shim over cherrypick-core's shared ``SessionManager`` (see ``cherrypick.core.auth``). Thread-local
(``thread_local=True``): the streamer daemon runs the DXLink connection on the main thread's event
loop and the REST poller on a separate thread with its own loop; tastytrade's Session holds an
httpx.AsyncClient bound to whichever loop first uses it, so a session per thread keeps each bound to a
single loop (sharing one would silently hang awaits from the second loop). The ``get_session`` /
``reset_session`` names are preserved so existing call sites (streamer.py, tt.py) are unchanged.
"""

from __future__ import annotations

from cherrypick.core.auth import SessionManager

import credentials

_manager = SessionManager(credentials.store, thread_local=True)

get_session = _manager.get_session
reset_session = _manager.reset_session
