"""
MockMCP: stub MCP client for MEICAgent tests.

Returns plain dicts matching the tastytrade MCP server's response shapes.
No network, no credentials, no tastytrade_mcp package required.

Scenarios:
  midday_normal  — 3 open ICs, all 6 stops working, pre-flight passes
  stop_filled    — IC #2 put stop (order 910021) is gone (it filled)
  bp_rejected    — execute_trade dry_run returns ok=False
"""
from __future__ import annotations

from datetime import date


# ── OCC symbol helpers ────────────────────────────────────────────────────────

def _dte_str() -> str:
    return date.today().strftime("%y%m%d")


def _occ(opt_type: str, strike: int) -> str:
    """XSP OCC option symbol expiring today, e.g. 'XSP   260619C00585000'."""
    return f"XSP   {_dte_str()}{opt_type}{strike * 1000:08d}"


# ── Net credits for the 3 open ICs ───────────────────────────────────────────

IC1_CREDIT = 1.20   # (short_call 1.05 - long_call 0.45) + (short_put 1.00 - long_put 0.40)
IC2_CREDIT = 1.10
IC3_CREDIT = 1.25

# Stop sizing per CLAUDE.md: trigger = credit × 0.90, limit = credit × 0.95
_TRIGGER_RATIO = 0.90
_LIMIT_RATIO   = 0.95


def _stop(order_id: int, credit: float, spread: str) -> dict:
    return {
        "id": order_id,
        "status": "Live",
        "underlying_symbol": "XSP",
        "order_type": "Stop Limit",
        "time_in_force": "Day",
        "stop-trigger": str(round(credit * _TRIGGER_RATIO, 2)),
        "price":        str(round(credit * _LIMIT_RATIO,   2)),
        "_spread": spread,  # "put" or "call" — informational
    }


# 2 stops per IC: one for the put spread, one for the call spread
ALL_STOPS = [
    _stop(910011, IC1_CREDIT, "put"),
    _stop(910012, IC1_CREDIT, "call"),
    _stop(910021, IC2_CREDIT, "put"),   # this one fills in the stop_filled scenario
    _stop(910022, IC2_CREDIT, "call"),
    _stop(910031, IC3_CREDIT, "put"),
    _stop(910032, IC3_CREDIT, "call"),
]

STOP_FILLED_ORDER_ID = 910021  # IC #2 put stop

# Ordered stop IDs per IC for assertions
IC_STOP_IDS = {
    1: (910011, 910012),
    2: (910021, 910022),
    3: (910031, 910032),
}


# ── Open positions (3 ICs × 4 legs, expiring today) ──────────────────────────

ALL_POSITIONS = [
    # IC #1: short 585C / long 590C / short 575P / long 570P  (5-wide)
    {"symbol": _occ("C", 585), "instrument_type": "Equity Option", "quantity": "1", "quantity_direction": "Short", "average_open_price": "1.05"},
    {"symbol": _occ("C", 590), "instrument_type": "Equity Option", "quantity": "1", "quantity_direction": "Long",  "average_open_price": "0.45"},
    {"symbol": _occ("P", 575), "instrument_type": "Equity Option", "quantity": "1", "quantity_direction": "Short", "average_open_price": "1.00"},
    {"symbol": _occ("P", 570), "instrument_type": "Equity Option", "quantity": "1", "quantity_direction": "Long",  "average_open_price": "0.40"},
    # IC #2: short 586C / long 591C / short 574P / long 569P  (5-wide)
    {"symbol": _occ("C", 586), "instrument_type": "Equity Option", "quantity": "1", "quantity_direction": "Short", "average_open_price": "0.95"},
    {"symbol": _occ("C", 591), "instrument_type": "Equity Option", "quantity": "1", "quantity_direction": "Long",  "average_open_price": "0.40"},
    {"symbol": _occ("P", 574), "instrument_type": "Equity Option", "quantity": "1", "quantity_direction": "Short", "average_open_price": "0.90"},
    {"symbol": _occ("P", 569), "instrument_type": "Equity Option", "quantity": "1", "quantity_direction": "Long",  "average_open_price": "0.35"},
    # IC #3: short 588C / long 593C / short 576P / long 571P  (5-wide)
    {"symbol": _occ("C", 588), "instrument_type": "Equity Option", "quantity": "1", "quantity_direction": "Short", "average_open_price": "1.10"},
    {"symbol": _occ("C", 593), "instrument_type": "Equity Option", "quantity": "1", "quantity_direction": "Long",  "average_open_price": "0.50"},
    {"symbol": _occ("P", 576), "instrument_type": "Equity Option", "quantity": "1", "quantity_direction": "Short", "average_open_price": "1.05"},
    {"symbol": _occ("P", 571), "instrument_type": "Equity Option", "quantity": "1", "quantity_direction": "Long",  "average_open_price": "0.45"},
]


# ── MockMCP ───────────────────────────────────────────────────────────────────

class MockMCP:
    """
    Stub MCP client. Instantiate with a scenario name, then call `call_tool`
    exactly as you would a real MCP server. No network or credentials needed.

    Returns (None, result_dict) to match the tastytrade MCP call_tool signature.
    """

    SCENARIOS = ("midday_normal", "stop_filled", "bp_rejected")

    def __init__(self, scenario: str = "midday_normal"):
        if scenario not in self.SCENARIOS:
            raise ValueError(f"Unknown scenario {scenario!r}. Choose from: {self.SCENARIOS}")
        self.scenario = scenario

    async def call_tool(self, name: str, args: dict | None = None):
        """Returns (None, dict) — mirrors the tastytrade MCP call_tool return value."""
        handler = getattr(self, "_tool_" + name.replace("-", "_"), None)
        if handler is None:
            raise ValueError(f"MockMCP has no stub for tool '{name}'")
        return None, handler(args or {})

    # ── tool stubs ────────────────────────────────────────────────────────────

    def _tool_get_connection_status(self, _: dict) -> dict:
        return {
            "ok": True,
            "connected": True,
            "mock_mode": True,
            "environment": "mock",
            "account_count": 1,
            "live_trading_enabled": True,
        }

    def _tool_get_account_info(self, _: dict) -> dict:
        return {
            "ok": True,
            "account_number": "5WX12345",
            "balances": {
                "net-liquidating-value":        "100190.00",
                "derivative-buying-power":      "98800.00",
                "used-derivative-buying-power": "1200.00",
            },
        }

    def _tool_get_positions(self, _: dict) -> dict:
        return {"ok": True, "positions": list(ALL_POSITIONS)}

    def _tool_get_working_orders(self, _: dict) -> dict:
        if self.scenario == "stop_filled":
            orders = [o for o in ALL_STOPS if o["id"] != STOP_FILLED_ORDER_ID]
        else:
            orders = list(ALL_STOPS)
        return {"ok": True, "orders": orders}

    def _tool_get_market_overview(self, _: dict) -> dict:
        return {
            "ok": True,
            "metrics": [
                {
                    "symbol": "XSP",
                    "last":                          580.0,
                    "implied-volatility-rank":       "0.38",
                    "implied-volatility-percentile": "0.55",
                }
            ],
        }

    # Per-strike greek data for the 8 strikes in the mock chain.
    # Put IV slightly exceeds call IV at equidistant strikes — mild realistic put skew.
    _GREEK_DATA: dict = {
        ("Put",  565): {"delta": -0.05, "gamma": 0.015, "theta": -0.40, "iv": 0.195},
        ("Put",  570): {"delta": -0.09, "gamma": 0.025, "theta": -0.65, "iv": 0.182},
        ("Put",  575): {"delta": -0.15, "gamma": 0.038, "theta": -0.95, "iv": 0.172},
        ("Put",  580): {"delta": -0.28, "gamma": 0.055, "theta": -1.35, "iv": 0.163},
        ("Call", 580): {"delta":  0.28, "gamma": 0.055, "theta": -1.35, "iv": 0.158},
        ("Call", 585): {"delta":  0.15, "gamma": 0.038, "theta": -0.95, "iv": 0.165},
        ("Call", 590): {"delta":  0.09, "gamma": 0.025, "theta": -0.65, "iv": 0.172},
        ("Call", 595): {"delta":  0.05, "gamma": 0.015, "theta": -0.40, "iv": 0.178},
    }

    _CHAIN_DEFS: list = [
        (565, "Put"), (570, "Put"), (575, "Put"), (580, "Put"),
        (580, "Call"), (585, "Call"), (590, "Call"), (595, "Call"),
    ]

    def _tool_get_option_chain(self, args: dict) -> dict:
        today = str(date.today())
        include_greeks = args.get("include_greeks", False)

        strikes = []
        for strike, opt_type in self._CHAIN_DEFS:
            entry: dict = {
                "strike_price":    str(strike),
                "option_type":     opt_type,
                "symbol":          _occ(opt_type[0], strike),
                "streamer_symbol": f".XSP{_dte_str()}{opt_type[0]}{strike}",
            }
            if include_greeks:
                entry.update(self._GREEK_DATA[(opt_type, strike)])
            strikes.append(entry)

        result: dict = {"ok": True, "chain": {today: strikes}}
        if include_greeks:
            result["greeks_complete"] = True
            result["greeks_received"] = len(strikes)
        return result

    def _tool_get_strategies(self, args: dict) -> dict:
        width = int(args.get("wing_width", 5))
        short_put  = 575
        short_call = 585
        long_put   = short_put  - width * 5  # 5-point strike spacing
        long_call  = short_call + width * 5
        # POP depends on short strike delta, not wing width — constant for fixed short_delta=0.15
        short_delta = float(args.get("short_delta", 0.15))
        return {
            "ok": True,
            "expiration": str(date.today()),
            "dte": 0,
            "estimated_pop": round(1.0 - 2 * short_delta, 3),
            "net_credit":    round(1.10 + width * 0.05,  2),
            "legs": {
                "short_put":  {"symbol": _occ("P", short_put),  "strike": short_put,  "delta": -0.15},
                "long_put":   {"symbol": _occ("P", long_put),   "strike": long_put,   "delta": -0.07},
                "short_call": {"symbol": _occ("C", short_call), "strike": short_call, "delta":  0.15},
                "long_call":  {"symbol": _occ("C", long_call),  "strike": long_call,  "delta":  0.07},
            },
        }

    def _tool_execute_trade(self, args: dict) -> dict:
        dry_run = args.get("dry_run", True)
        if self.scenario == "bp_rejected":
            return {
                "ok": False,
                "dry_run": dry_run,
                "problems": ["Insufficient derivative buying power for this order."],
                "buying_power": {"current_buying_power": "375.00", "effect": "Debit"},
            }
        # Validate that each leg has the required fields
        _REQUIRED_LEG_FIELDS = {"instrument_type", "symbol", "quantity", "action"}
        problems = []
        for i, leg in enumerate(args.get("order", {}).get("legs", [])):
            missing = _REQUIRED_LEG_FIELDS - set(leg)
            if missing:
                problems.append(f"Leg {i}: missing required fields: {', '.join(sorted(missing))}")
        if problems:
            return {"ok": False, "dry_run": dry_run, "problems": problems}
        return {
            "ok": True,
            "dry_run": dry_run,
            "buying_power": {
                "current_buying_power":   "98800.00",
                "new_buying_power":       "98425.00",
                "change_in_buying_power": "-375.00",
                "effect": "Debit",
            },
            "warnings":  [],
            "problems":  [],
        }
