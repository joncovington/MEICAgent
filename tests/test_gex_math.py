"""Unit tests for gex_math.py -- the shared dollar-gamma/zero-gamma math
extracted from tt.py and dashboard.py after their two hand-maintained copies
drifted out of sync (dashboard.py's once silently understated GEX ~75x for
SPX by missing the spot**2 * 0.01 scale factor).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import gex_math


def test_dollar_gamma_formula():
    # gamma * qty * multiplier * spot^2 * 0.01
    result = gex_math.dollar_gamma(gamma=0.05, quantity=1000, multiplier=100, spot=600.0)
    assert result == 0.05 * 1000 * 100 * 600.0 * 600.0 * 0.01


def test_dollar_gamma_zero_when_spot_zero():
    assert gex_math.dollar_gamma(0.05, 1000, 100, 0.0) == 0.0


def test_interpolate_zero_gamma_crosses_between_two_strikes():
    strikes = [
        {"strike": 595, "net_gex": -100},
        {"strike": 600, "net_gex": 300},  # cumulative: -100 -> 200, crosses zero here
        {"strike": 605, "net_gex": -50},
    ]
    result = gex_math.interpolate_zero_gamma(strikes)
    assert result is not None
    assert 595 < result < 600


def test_interpolate_zero_gamma_none_when_no_crossing():
    strikes = [{"strike": 595, "net_gex": 100}, {"strike": 600, "net_gex": 50}]
    assert gex_math.interpolate_zero_gamma(strikes) is None


def test_interpolate_zero_gamma_uses_cumulative_not_adjacent():
    """A local sign flip between two adjacent strikes must not be reported if the
    running CUMULATIVE sum never actually crosses zero (regression case for the
    bug this module's docstring documents)."""
    strikes = [
        {"strike": 595, "net_gex": 100},
        {"strike": 600, "net_gex": -10},   # adjacent flip, but cumulative stays positive (90)
        {"strike": 605, "net_gex": 5},
    ]
    assert gex_math.interpolate_zero_gamma(strikes) is None


def test_interpolate_zero_gamma_single_strike_no_crossing():
    assert gex_math.interpolate_zero_gamma([{"strike": 600, "net_gex": 100}]) is None
