"""Lazy tastytrade OAuth session management.

Thin shim over cherrypick-core's shared ``SessionManager`` (see ``cherrypick.core.auth``). Thread-local
(``thread_local=True``): the streamer daemon runs the DXLink connection on the main thread's event
loop and the REST poller on a separate thread with its own loop; tastytrade's Session holds an
httpx.AsyncClient bound to whichever loop first uses it, so a session per thread keeps each bound to a
single loop (sharing one would silently hang awaits from the second loop). The ``get_session`` /
``reset_session`` names are preserved so existing call sites (streamer.py, tt.py) are unchanged.
"""

from __future__ import annotations

import os as _os
import sys as _sys

# Bootstrap the cherrypick-core submodule (src/_core) onto sys.path *before* the cherrypick.core import
# — import-sorting puts it ahead of the local `import credentials` (which also bootstraps _core), and
# importers like streamer.py only add src/, not src/_core. Mirrors the tt.py / paper.py bootstrap.
_CORE = _os.path.join(_os.path.dirname(__file__), "_core")
if _os.path.isdir(_CORE) and _CORE not in _sys.path:
    _sys.path.insert(0, _CORE)

from cherrypick.core.auth import SessionManager  # noqa: E402

import credentials  # noqa: E402

_manager = SessionManager(credentials.store, thread_local=True)

get_session = _manager.get_session
reset_session = _manager.reset_session
