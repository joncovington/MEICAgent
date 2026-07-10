"""Shared GEX math — thin shim over cherrypit.gex (src/_core submodule).

The dollar-gamma / cumulative zero-gamma math and the compute_gex orchestration now live in the shared
core, so tt.py's get_gex and dashboard.py's GEX chart use one implementation (the reason this math was
extracted in the first place: two hand-maintained copies once drifted ~75x apart). This module
re-exports the API both call sites import. See cherrypit.gex for the implementation and rationale.
"""

from __future__ import annotations

# Make the cherrypit-core submodule importable without an install (mirrors credentials.py).
import sys as _sys
from pathlib import Path as _Path

_CORE = _Path(__file__).resolve().parent / "_core"
if _CORE.is_dir() and str(_CORE) not in _sys.path:
    _sys.path.insert(0, str(_CORE))

from cherrypit.gex import (  # noqa: E402
    compute_gex,
    dollar_gamma,
    interpolate_zero_gamma,
)

__all__ = ["dollar_gamma", "interpolate_zero_gamma", "compute_gex"]
