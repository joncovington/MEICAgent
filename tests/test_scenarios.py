"""
Scenario-based pytest tests for MEICAgent MCP response parsing.

Runs against MockMCP — no tastytrade-mock server or credentials required.
Each test asserts a specific response shape or scenario outcome.
"""
from __future__ import annotations

from datetime import date
import pytest
from mock_mcp import (
    MockMCP,
    ALL_STOPS,
    STOP_FILLED_ORDER_ID,
    IC1_CREDIT, IC2_CREDIT, IC3_CREDIT,
    _TRIGGER_RATIO, _LIMIT_RATIO,
)


# ── helper ────────────────────────────────────────────────────────────────────

async def call(mcp: MockMCP, name: str, args: dict | None = None) -> dict:
    _, result = await mcp.call_tool(name, args or {})
    return result


# ── Phase 1: Connection ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connection_ok(mock_midday):
    r = await call(mock_midday, "get_connection_status")
    assert r["ok"] is True

@pytest.mark.asyncio
async def test_connection_is_mock(mock_midday):
    r = await call(mock_midday, "get_connection_status")
    assert r["mock_mode"] is True

@pytest.mark.asyncio
async def test_connection_live_trading_enabled(mock_midday):
    r = await call(mock_midday, "get_connection_status")
    assert r["live_trading_enabled"] is True


# ── Phase 2: Account & market state ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_account_derivative_bp_positive(mock_midday):
    r = await call(mock_midday, "get_account_info")
    assert float(r["balances"]["derivative-buying-power"]) > 0

@pytest.mark.asyncio
async def test_account_nlv_positive(mock_midday):
    r = await call(mock_midday, "get_account_info")
    assert float(r["balances"]["net-liquidating-value"]) > 0

@pytest.mark.asyncio
async def test_positions_three_ics(mock_midday):
    r = await call(mock_midday, "get_positions")
    assert len(r["positions"]) == 12  # 3 ICs × 4 legs

@pytest.mark.asyncio
async def test_working_orders_two_per_ic(mock_midday):
    r = await call(mock_midday, "get_working_orders")
    assert len(r["orders"]) == 6  # 3 ICs × 2 stops (put spread + call spread)

@pytest.mark.asyncio
async def test_working_orders_all_live(mock_midday):
    r = await call(mock_midday, "get_working_orders")
    assert all(o["status"] == "Live" for o in r["orders"])

@pytest.mark.asyncio
async def test_iv_rank_present_and_valid(mock_midday):
    r = await call(mock_midday, "get_market_overview", {"symbols": ["XSP"]})
    xsp = next(m for m in r["metrics"] if m["symbol"] == "XSP")
    assert 0 < float(xsp["implied-volatility-rank"]) < 1


# ── Phase 3: Option chain & strategies ───────────────────────────────────────

@pytest.mark.asyncio
async def test_chain_has_0dte_expiry(mock_midday):
    r = await call(mock_midday, "get_option_chain", {"symbol": "XSP"})
    assert str(date.today()) in r["chain"]

@pytest.mark.asyncio
@pytest.mark.parametrize("width", [1, 2, 3, 5])
async def test_strategies_are_0dte(mock_midday, width):
    r = await call(mock_midday, "get_strategies", {
        "symbol": "XSP", "target_dte": 0, "wing_width": width, "short_delta": 0.15,
    })
    assert r["ok"] is True
    assert r["dte"] == 0
    assert r["expiration"] == str(date.today())

@pytest.mark.asyncio
@pytest.mark.parametrize("width", [1, 2, 3, 5])
async def test_strategies_have_four_legs(mock_midday, width):
    r = await call(mock_midday, "get_strategies", {
        "symbol": "XSP", "target_dte": 0, "wing_width": width, "short_delta": 0.15,
    })
    for leg in ("short_put", "long_put", "short_call", "long_call"):
        assert leg in r["legs"], f"Missing leg '{leg}' for width {width}"
        assert "symbol" in r["legs"][leg]

@pytest.mark.asyncio
async def test_wider_wing_gives_more_credit(mock_midday):
    credits = []
    for width in [1, 2, 3, 5]:
        r = await call(mock_midday, "get_strategies", {
            "symbol": "XSP", "target_dte": 0, "wing_width": width, "short_delta": 0.15,
        })
        credits.append(r["net_credit"])
    assert credits == sorted(credits), f"Credits not ascending with width: {credits}"

@pytest.mark.asyncio
async def test_pop_stable_across_wing_widths(mock_midday):
    """POP is determined by short strike delta, not wing width — must not vary with width."""
    pops = []
    for width in [1, 2, 3, 5]:
        r = await call(mock_midday, "get_strategies", {
            "symbol": "XSP", "target_dte": 0, "wing_width": width, "short_delta": 0.15,
        })
        pops.append(r["estimated_pop"])
    assert len(set(pops)) == 1, f"POP varies with wing width but should not: {pops}"


# ── Phase 3b: Option chain greeks ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chain_plain_lookup_has_no_greeks(mock_midday):
    """Plain lookup (no include_greeks) must not return greek fields — keeps the call fast."""
    r = await call(mock_midday, "get_option_chain", {"symbol": "XSP"})
    strikes = list(r["chain"].values())[0]
    assert "delta" not in strikes[0]
    assert "greeks_complete" not in r

@pytest.mark.asyncio
async def test_chain_greeks_present_when_requested(mock_midday):
    r = await call(mock_midday, "get_option_chain", {
        "symbol": "XSP", "expiration": str(date.today()),
        "include_greeks": True, "strike_count": 15, "around_price": 580.0,
    })
    strikes = list(r["chain"].values())[0]
    for s in strikes:
        for field in ("delta", "gamma", "theta", "iv"):
            assert field in s, f"Missing '{field}' in strike {s.get('strike_price')}"

@pytest.mark.asyncio
async def test_chain_greeks_complete_flag(mock_midday):
    r = await call(mock_midday, "get_option_chain", {
        "symbol": "XSP", "expiration": str(date.today()),
        "include_greeks": True, "strike_count": 15, "around_price": 580.0,
    })
    assert r["greeks_complete"] is True
    assert r["greeks_received"] > 0

@pytest.mark.asyncio
async def test_chain_short_put_delta_near_target(mock_midday):
    """575P should have delta close to -0.15 — confirming it matches the short_delta target."""
    r = await call(mock_midday, "get_option_chain", {
        "symbol": "XSP", "expiration": str(date.today()),
        "include_greeks": True, "strike_count": 15, "around_price": 580.0,
    })
    strikes = list(r["chain"].values())[0]
    sp = next(s for s in strikes if s["strike_price"] == "575" and s["option_type"] == "Put")
    assert abs(sp["delta"] - (-0.15)) < 0.02

@pytest.mark.asyncio
async def test_chain_put_iv_exceeds_call_iv_at_equidistant_strikes(mock_midday):
    """Put IV should exceed call IV at equal distance from ATM — realistic equity put skew."""
    r = await call(mock_midday, "get_option_chain", {
        "symbol": "XSP", "expiration": str(date.today()),
        "include_greeks": True, "strike_count": 15, "around_price": 580.0,
    })
    strikes = list(r["chain"].values())[0]
    # 575P and 585C are each 5 points from ATM (580)
    put_iv  = next(s["iv"] for s in strikes if s["strike_price"] == "575" and s["option_type"] == "Put")
    call_iv = next(s["iv"] for s in strikes if s["strike_price"] == "585" and s["option_type"] == "Call")
    assert put_iv > call_iv, f"Expected put IV {put_iv} > call IV {call_iv} (bearish skew)"

@pytest.mark.asyncio
async def test_market_overview_has_underlying_price(mock_midday):
    r = await call(mock_midday, "get_market_overview", {"symbols": ["XSP"]})
    xsp = next(m for m in r["metrics"] if m["symbol"] == "XSP")
    assert float(xsp["last"]) > 0


# ── Phase 4: Stop management ──────────────────────────────────────────────────

def test_trigger_ratio_matches_claude_md():
    assert _TRIGGER_RATIO == 0.90, f"CLAUDE.md specifies 0.90, got {_TRIGGER_RATIO}"

def test_limit_ratio_matches_claude_md():
    assert _LIMIT_RATIO == 0.95, f"CLAUDE.md specifies 0.95, got {_LIMIT_RATIO}"

@pytest.mark.asyncio
async def test_stop_triggers_follow_claude_formula(mock_midday):
    r = await call(mock_midday, "get_working_orders")
    # Each IC has 2 stops (put, call), both sized on the same net credit
    ic_credits = [IC1_CREDIT, IC1_CREDIT, IC2_CREDIT, IC2_CREDIT, IC3_CREDIT, IC3_CREDIT]
    for order, credit in zip(r["orders"], ic_credits):
        expected = round(credit * _TRIGGER_RATIO, 2)
        actual   = float(order["stop-trigger"])
        assert abs(actual - expected) < 0.001, (
            f"Order {order['id']}: trigger {actual} != expected {expected}"
        )

@pytest.mark.asyncio
async def test_stop_limits_follow_claude_formula(mock_midday):
    r = await call(mock_midday, "get_working_orders")
    ic_credits = [IC1_CREDIT, IC1_CREDIT, IC2_CREDIT, IC2_CREDIT, IC3_CREDIT, IC3_CREDIT]
    for order, credit in zip(r["orders"], ic_credits):
        expected = round(credit * _LIMIT_RATIO, 2)
        actual   = float(order["price"])
        assert abs(actual - expected) < 0.001, (
            f"Order {order['id']}: limit {actual} != expected {expected}"
        )

@pytest.mark.asyncio
async def test_stop_filled_reduces_order_count(mock_stop_filled):
    r = await call(mock_stop_filled, "get_working_orders")
    assert len(r["orders"]) == 5  # 1 of 6 filled

@pytest.mark.asyncio
async def test_stop_filled_correct_order_absent(mock_stop_filled):
    r = await call(mock_stop_filled, "get_working_orders")
    ids = {o["id"] for o in r["orders"]}
    assert STOP_FILLED_ORDER_ID not in ids

@pytest.mark.asyncio
async def test_stop_filled_remaining_orders_intact(mock_stop_filled):
    r = await call(mock_stop_filled, "get_working_orders")
    ids = {o["id"] for o in r["orders"]}
    expected = {o["id"] for o in ALL_STOPS if o["id"] != STOP_FILLED_ORDER_ID}
    assert ids == expected

@pytest.mark.asyncio
async def test_stop_filled_remaining_orders_still_live(mock_stop_filled):
    r = await call(mock_stop_filled, "get_working_orders")
    assert all(o["status"] == "Live" for o in r["orders"])


# ── Phase 6: Pre-flight dry run ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_preflight_passes(mock_midday):
    strat = await call(mock_midday, "get_strategies", {
        "symbol": "XSP", "target_dte": 0, "wing_width": 3, "short_delta": 0.15,
    })
    legs = [
        {"instrument_type": "Equity Option", "symbol": strat["legs"]["short_put"]["symbol"],  "quantity": 1, "action": "Sell to Open"},
        {"instrument_type": "Equity Option", "symbol": strat["legs"]["long_put"]["symbol"],   "quantity": 1, "action": "Buy to Open"},
        {"instrument_type": "Equity Option", "symbol": strat["legs"]["short_call"]["symbol"], "quantity": 1, "action": "Sell to Open"},
        {"instrument_type": "Equity Option", "symbol": strat["legs"]["long_call"]["symbol"],  "quantity": 1, "action": "Buy to Open"},
    ]
    r = await call(mock_midday, "execute_trade", {
        "order": {
            "time_in_force": "Day",
            "order_type": "Limit",
            "price": -strat["net_credit"],
            "legs": legs,
        },
        "dry_run": True,
    })
    assert r["ok"] is True

@pytest.mark.asyncio
async def test_preflight_dry_run_flag_preserved(mock_midday):
    r = await call(mock_midday, "execute_trade", {
        "order": {"time_in_force": "Day", "order_type": "Limit", "price": -1.20, "legs": []},
        "dry_run": True,
    })
    assert r["dry_run"] is True
    assert r.get("problems") == []

@pytest.mark.asyncio
async def test_preflight_bp_decreases(mock_midday):
    r = await call(mock_midday, "execute_trade", {
        "order": {"time_in_force": "Day", "order_type": "Limit", "price": -1.20, "legs": []},
        "dry_run": True,
    })
    assert float(r["buying_power"]["change_in_buying_power"]) < 0

@pytest.mark.asyncio
async def test_preflight_bp_rejected_ok_false(mock_bp_rejected):
    r = await call(mock_bp_rejected, "execute_trade", {
        "order": {"time_in_force": "Day", "order_type": "Limit", "price": -1.20, "legs": []},
        "dry_run": True,
    })
    assert r["ok"] is False

@pytest.mark.asyncio
async def test_preflight_bp_rejected_has_problems(mock_bp_rejected):
    r = await call(mock_bp_rejected, "execute_trade", {
        "order": {"time_in_force": "Day", "order_type": "Limit", "price": -1.20, "legs": []},
        "dry_run": True,
    })
    assert len(r["problems"]) > 0

@pytest.mark.asyncio
async def test_preflight_bp_rejected_still_dry_run(mock_bp_rejected):
    r = await call(mock_bp_rejected, "execute_trade", {
        "order": {"time_in_force": "Day", "order_type": "Limit", "price": -1.20, "legs": []},
        "dry_run": True,
    })
    assert r["dry_run"] is True

@pytest.mark.asyncio
async def test_execute_trade_rejects_malformed_leg(mock_midday):
    """A leg missing required fields should produce ok=False with a problem message."""
    r = await call(mock_midday, "execute_trade", {
        "order": {
            "time_in_force": "Day",
            "order_type": "Limit",
            "price": -1.20,
            "legs": [{"symbol": "XSP   260619C00585000"}],  # missing instrument_type, quantity, action
        },
        "dry_run": True,
    })
    assert r["ok"] is False
    assert len(r.get("problems", [])) > 0
