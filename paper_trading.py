"""Paper trading simulation helpers for MEICAgent.

All functions are pure (no DB calls). The loop calls these helpers
and persists results via db.py commands.

Entry slippage: random 0–20% of the bid-ask half-spread per leg.
Stop model: iteration-cadence — if spread value >= trigger at iteration
start, assume fill at stop_limit price during the prior interval.
"""

import random
from typing import Optional


def slippage(bid: float, ask: float) -> float:
    """Adverse fill slippage: random 0–20% of half-spread."""
    if bid is None or ask is None or ask <= bid:
        return 0.0
    return random.uniform(0, 0.20) * (ask - bid) / 2.0


def simulate_entry_credit(legs: list) -> float:
    """
    Net credit per share after entry slippage.

    Each leg: {"action": "Sell to Open"|"Buy to Open", "bid": float, "ask": float}
    Sell legs fill below mid; buy legs fill above mid.
    """
    net = 0.0
    for leg in legs:
        bid = float(leg.get("bid") or 0)
        ask = float(leg.get("ask") or 0)
        mid = (bid + ask) / 2.0
        slip = slippage(bid, ask)
        if "Sell" in leg.get("action", ""):
            net += mid - slip
        else:
            net -= mid + slip
    return round(net, 4)


def simulate_close_cost(legs: list) -> float:
    """
    Debit cost to close a spread after slippage.

    Each leg: {"action": "Buy to Close"|"Sell to Close", "bid": float, "ask": float}
    Returns cost as a positive number (debit paid to close).
    """
    cost = 0.0
    for leg in legs:
        bid = float(leg.get("bid") or 0)
        ask = float(leg.get("ask") or 0)
        mid = (bid + ask) / 2.0
        slip = slippage(bid, ask)
        if "Buy" in leg.get("action", ""):
            cost += mid + slip
        else:
            cost -= mid - slip
    return round(cost, 4)


def spread_mid_from_chain(short_symbol: str, long_symbol: str, chain_by_symbol: dict) -> Optional[float]:
    """
    Current spread value (cost to close) from chain data.

    chain_by_symbol: {symbol: {"bid": float, "ask": float, "mid": float}}
    Returns short_mid - long_mid (what it costs to BTC the short, net of STC the long).
    """
    s = chain_by_symbol.get(short_symbol)
    l = chain_by_symbol.get(long_symbol)
    if not s or not l:
        return None
    s_mid = s.get("mid") or (((s.get("bid") or 0) + (s.get("ask") or 0)) / 2.0)
    l_mid = l.get("mid") or (((l.get("bid") or 0) + (l.get("ask") or 0)) / 2.0)
    return round(float(s_mid) - float(l_mid), 4)


def check_stops(trade: dict, put_spread_value: Optional[float], call_spread_value: Optional[float]) -> dict:
    """
    Evaluate whether paper stops would have triggered this iteration.

    Iteration-cadence model: if spread_value >= trigger at iteration start,
    the stop is considered to have filled at stop_limit during the prior interval.

    Returns:
      {
        "put_stopped": bool, "put_exit_cost": float | None,
        "call_stopped": bool, "call_exit_cost": float | None,
      }
    """
    net_credit = float(trade.get("net_credit") or 0)
    trigger = float(
        trade.get("stop_trigger_current")
        or trade.get("stop_trigger_original")
        or net_credit * 0.90
    )
    limit = float(
        trade.get("stop_limit_current")
        or trade.get("stop_limit_original")
        or net_credit * 0.95
    )

    result = {
        "put_stopped": False,  "put_exit_cost": None,
        "call_stopped": False, "call_exit_cost": None,
    }

    if put_spread_value is not None and put_spread_value >= trigger:
        result["put_stopped"] = True
        result["put_exit_cost"] = limit

    if call_spread_value is not None and call_spread_value >= trigger:
        result["call_stopped"] = True
        result["call_exit_cost"] = limit

    return result


def ic_pnl(net_credit: float, put_exit_cost: float, call_exit_cost: float,
           quantity: int = 1, dollar_multiplier: float = 100) -> float:
    """
    Realized P&L in dollars for a fully closed paper IC.

    pnl = (credit_collected - put_exit_cost - call_exit_cost) * dollar_multiplier * quantity
    For expired spreads, pass exit_cost=0.
    dollar_multiplier: 100 for equity options; futures point value (e.g. 50 for /ES).
    """
    return round((net_credit - put_exit_cost - call_exit_cost) * dollar_multiplier * quantity, 2)


def unrealized_pnl(net_credit: float, put_spread_value: Optional[float],
                   call_spread_value: Optional[float], quantity: int = 1,
                   dollar_multiplier: float = 100) -> Optional[float]:
    """
    Mark-to-market unrealized P&L in dollars for an open paper IC.

    pnl = (credit_collected - current_spread_values) * dollar_multiplier * quantity
    Returns None if either spread value is unavailable.
    dollar_multiplier: 100 for equity options; futures point value (e.g. 50 for /ES).
    """
    if put_spread_value is None or call_spread_value is None:
        return None
    total_spread = put_spread_value + call_spread_value
    return round((net_credit - total_spread) * dollar_multiplier * quantity, 2)
