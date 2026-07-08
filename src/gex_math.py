"""Shared GEX (gamma exposure) math used by both tt.py's live get_gex command
and dashboard.py's GEX chart endpoint.

Extracted after the two independently hand-maintained copies of this math
drifted out of sync: dashboard.py's copy of the dollar-gamma formula was
once missing the spot**2 * 0.01 scale factor, silently understating GEX by
roughly spot/100 (~75x for SPX) relative to what tt.py's get_gex (which the
trading loop's entry gate actually reads) reported for the same data. A
single shared implementation makes that class of divergence impossible.
"""

from __future__ import annotations


def dollar_gamma(gamma: float, quantity: float, multiplier: float, spot: float) -> float:
    """Standard dollar-gamma-per-1%-move formula: gamma * quantity * contract
    size * spot^2 * 0.01. `quantity` is typically open interest (a
    "positioning" reading), but the same formula applies with traded volume
    substituted in for a "flow" reading (see dashboard.py's gex_vol).
    """
    return gamma * quantity * multiplier * spot * spot * 0.01


def interpolate_zero_gamma(strikes: list[dict]) -> float | None:
    """Interpolate the strike where CUMULATIVE net GEX crosses zero.

    `strikes` must already be sorted ascending by "strike" and each entry
    must have a "net_gex" key. Scans the running cumulative sum rather than
    comparing adjacent strikes' individual net_gex values -- an individual
    strike's net_gex can flip sign strike-to-strike from local OI/volume
    noise without that representing the actual point where aggregate dealer
    exposure flips, and if more than one such local flip exists across the
    chain, comparing only adjacent values would return whichever is hit
    first (leftmost), not necessarily the real zero-gamma level.
    """
    cumulative = 0.0
    prev_cumulative = 0.0
    prev_strike = None
    for i, s in enumerate(strikes):
        prev_cumulative = cumulative
        cumulative += s["net_gex"]
        if i > 0 and ((prev_cumulative < 0 <= cumulative) or (prev_cumulative >= 0 > cumulative)):
            denom = cumulative - prev_cumulative
            t = (-prev_cumulative / denom) if denom != 0 else 0.5
            return round(prev_strike + t * (s["strike"] - prev_strike), 2)
        prev_strike = s["strike"]
    return None
