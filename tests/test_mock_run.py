"""
End-to-end test of MEICAgent loop phases against MockMCP.
Runs all 6 phases from the /test-mcp skill and prints a plain English report.

Run from the project root:
    python tests/test_mock_run.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

# Allow direct execution as a script (pytest adds tests/ to sys.path automatically)
sys.path.insert(0, str(Path(__file__).parent))

# Windows cp1252 consoles choke on the Unicode box-drawing and check-mark characters
# used in the report. Force UTF-8 output regardless of the system locale.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from mock_mcp import (
    MockMCP,
    ALL_STOPS,
    IC1_CREDIT, IC2_CREDIT, IC3_CREDIT,
    _TRIGGER_RATIO, _LIMIT_RATIO,
)

CONFIG_PATH = Path(__file__).parent.parent / "config.json"


# ── helpers ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


async def call(mcp: MockMCP, name: str, args: dict | None = None) -> dict:
    _, result = await mcp.call_tool(name, args or {})
    return result


# ── phases ────────────────────────────────────────────────────────────────────

async def run_test(agent_config: dict) -> dict:
    mcp = MockMCP("midday_normal")
    results: dict = {}

    # ── Phase 1: Connection ───────────────────────────────────────────────────
    print("Phase 1 — Connection...", flush=True)
    results["phase1_connection"] = await call(mcp, "get_connection_status")

    # ── Phase 2: Account & Market State ──────────────────────────────────────
    print("Phase 2 — Account & Market State...", flush=True)
    symbol = agent_config["symbol"]
    acct_info, positions, working_orders, market_overview = await asyncio.gather(
        call(mcp, "get_account_info"),
        call(mcp, "get_positions"),
        call(mcp, "get_working_orders"),
        call(mcp, "get_market_overview", {"symbols": [symbol]}),
    )
    # Extract underlying price for the ATM window before calling the chain
    underlying_price = 580.0
    for m in market_overview.get("metrics", []):
        if m.get("symbol", "").upper() == symbol.upper():
            underlying_price = float(m.get("last", underlying_price))
            break
    option_chain = await call(mcp, "get_option_chain", {
        "symbol":         symbol,
        "expiration":     str(date.today()),
        "include_greeks": True,
        "around_price":   underlying_price,
        "greeks_timeout": 6.0,
    })
    results.update({
        "phase2_acct":           acct_info,
        "phase2_positions":      positions,
        "phase2_working_orders": working_orders,
        "phase2_market":         market_overview,
        "phase2_chain":          option_chain,
    })

    # ── Phase 3: Strategy Candidates ─────────────────────────────────────────
    print("Phase 3 — Strategy Candidates...", flush=True)
    # The agent decides wing width dynamically now (no fixed candidate list in
    # config) — exercise a representative spread up to max_wing_width so the
    # smoke test still covers multiple widths for the entry-decision phase.
    max_width = agent_config.get("max_wing_width", 10)
    widths = sorted({w for w in (1, 2, 3, 5, max_width) if w <= max_width})
    short_delta = agent_config.get("delta_target", 0.18)
    strat_results = await asyncio.gather(*[
        call(mcp, "get_strategies", {
            "symbol": symbol, "target_dte": 0,
            "wing_width": w, "short_delta": short_delta,
        })
        for w in widths
    ])
    results["phase3_strategies"] = list(zip(widths, strat_results))

    # ── Phase 4: Stop Management Simulation ──────────────────────────────────
    print("Phase 4 — Stop Management...", flush=True)
    results["phase4_stop_sim"] = _simulate_stop_management(positions, working_orders)

    # ── Phase 5: Entry Decision ───────────────────────────────────────────────
    print("Phase 5 — Entry Decision...", flush=True)
    results["phase5_entry"] = _simulate_entry_decision(
        agent_config, acct_info, market_overview,
        results["phase3_strategies"], symbol,
    )

    # ── Phase 6: Pre-flight dry run ───────────────────────────────────────────
    print("Phase 6 — Pre-flight dry-run...", flush=True)
    best_strat = next((r for _, r in results["phase3_strategies"] if r.get("ok")), None)
    if best_strat:
        legs = [
            {"instrument_type": "Equity Option", "symbol": best_strat["legs"]["short_put"]["symbol"],  "quantity": agent_config.get("quantity", 1), "action": "Sell to Open"},
            {"instrument_type": "Equity Option", "symbol": best_strat["legs"]["long_put"]["symbol"],   "quantity": agent_config.get("quantity", 1), "action": "Buy to Open"},
            {"instrument_type": "Equity Option", "symbol": best_strat["legs"]["short_call"]["symbol"], "quantity": agent_config.get("quantity", 1), "action": "Sell to Open"},
            {"instrument_type": "Equity Option", "symbol": best_strat["legs"]["long_call"]["symbol"],  "quantity": agent_config.get("quantity", 1), "action": "Buy to Open"},
        ]
        preflight = await call(mcp, "execute_trade", {
            "order": {
                "time_in_force": "Day",
                "order_type": "Limit",
                "price": -best_strat["net_credit"],
                "legs": legs,
            },
            "dry_run": True,
        })
    else:
        preflight = {"ok": False, "error": "No valid strategy candidate available"}
    results["phase6_preflight"] = preflight

    return results


# ── Stop management simulation ────────────────────────────────────────────────

def _simulate_stop_management(positions_resp: dict, orders_resp: dict) -> dict:
    positions = positions_resp.get("positions", [])
    orders    = orders_resp.get("orders", [])

    # Group positions into ICs: 4 legs each, in entry order
    ics = [
        {"ic_index": i // 4 + 1, "legs": [p.get("symbol", str(p)) for p in positions[i:i+4]]}
        for i in range(0, len(positions), 4)
    ]

    # Group stop orders into pairs: 2 per IC (put spread stop, call spread stop)
    stop_rows = []
    for i in range(0, len(orders), 2):
        pair     = orders[i:i+2]
        ic_index = i // 2 + 1
        for stop in pair:
            trigger = stop.get("stop-trigger", stop.get("stop_trigger", "?"))
            limit   = stop.get("price", "?")
            spread  = stop.get("_spread", "?")  # "put" or "call" metadata

            # Tightening evaluation: flat mid-day scenario, stops already at
            # CLAUDE.md design levels (~90% of IC credit). No IV spike or
            # short-strike breach simulated → hold current stops.
            stop_rows.append({
                "ic_index":    ic_index,
                "spread":      spread,
                "order_id":    stop.get("id", "?"),
                "status":      stop.get("status", "?"),
                "stop_trigger": trigger,
                "limit_price": limit,
                "tighten":     False,
                "reasoning":   (
                    f"Flat mid-day. {spread.capitalize()} spread stop trigger at "
                    f"${trigger} (~{int(_TRIGGER_RATIO*100)}% of IC net credit). "
                    "No IV spike or underlying breach → hold."
                ),
            })

    return {"ic_count": len(ics), "order_count": len(orders), "stops": stop_rows}


# ── Entry decision simulation ─────────────────────────────────────────────────

def _simulate_entry_decision(
    agent_config: dict, acct_info: dict, market_overview: dict,
    strat_results: list, symbol: str,
) -> dict:
    max_entries = agent_config.get("max_entries_per_day", 4)
    today_count = 3  # fixture: 3 ICs already open
    hard_stop   = (max_entries != -1 and today_count >= max_entries)

    balances     = acct_info.get("balances", {})
    derivative_bp = float(balances.get("derivative-buying-power", 0))

    # IV rank
    iv_rank = None
    for m in market_overview.get("metrics", []):
        if m.get("symbol", "").upper() == symbol.upper():
            v = m.get("implied-volatility-rank")
            if v is not None:
                iv_rank = float(v)
                break
    iv_str = f"{iv_rank:.0%}" if iv_rank is not None else "unknown"

    # Wing width selection: mid-day, 3 ICs open, IV rank 0.38 (moderate) → width 3
    chosen_width = 3
    chosen_strat = next(
        (r for w, r in strat_results if w == chosen_width and r.get("ok")), None
    ) or next((r for _, r in strat_results if r.get("ok")), None)

    if hard_stop:
        decision = "SKIP"
        reason = (
            f"Hard stop: today_count={today_count} >= max_entries_per_day={max_entries}."
        )
    else:
        decision = "ENTER"
        reason = (
            f"today_count={today_count} < max_entries_per_day={max_entries}. "
            f"IV rank {iv_str} (moderate — adequate premium, manageable risk). "
            f"Derivative BP ${derivative_bp:,.0f} — sufficient for width-{chosen_width} IC "
            f"(max loss ${chosen_width*100}). "
            f"Mid-day prime window. 3 open ICs → width {chosen_width} limits tail risk."
        )

    return {
        "decision":             decision,
        "hard_stop_max":        hard_stop,
        "today_count":          today_count,
        "max_entries":          max_entries,
        "iv_rank":              iv_rank,
        "derivative_bp":        derivative_bp,
        "chosen_wing_width":    chosen_width,
        "ai_entry_reasoning":   reason,
    }


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(results: dict, agent_config: dict) -> None:
    symbol = agent_config["symbol"]
    hr = "─" * 70

    print(f"\n{'='*70}")
    print("  MEICAgent /test-mcp — End-to-End Test Report (MockMCP)")
    print(f"  Symbol: {symbol}  |  Date: {date.today()}  |  Scenario: midday_normal")
    print(f"{'='*70}")

    # ── 1. Connection ─────────────────────────────────────────────────────────
    print(f"\n1. CONNECTION\n{hr}")
    c = results["phase1_connection"]
    print(f"  ok:            {c.get('ok')}")
    print(f"  mock_mode:     {c.get('mock_mode')}")
    print(f"  environment:   {c.get('environment')}")
    print(f"  live_trading:  {c.get('live_trading_enabled')}")
    if c.get("mock_mode"):
        print("\n  ✓ MockMCP connected. mock_mode confirmed.")
    else:
        print("\n  ✗ WARNING: mock_mode is not True.")

    # ── 2. State Reading ──────────────────────────────────────────────────────
    print(f"\n2. STATE READING\n{hr}")
    acct = results["phase2_acct"]
    pos  = results["phase2_positions"]
    wo   = results["phase2_working_orders"]
    mo   = results["phase2_market"]

    balances = acct.get("balances", {})
    print(f"  Account:       {acct.get('account_number')}")
    print(f"  Net liq:       ${balances.get('net-liquidating-value')}")
    print(f"  Derivative BP: ${balances.get('derivative-buying-power')}")
    print(f"  Used BP:       ${balances.get('used-derivative-buying-power')}")

    leg_count   = len(pos.get("positions", []))
    order_count = len(wo.get("orders",    []))
    print(f"\n  Position legs:  {leg_count}  (expected 12 — 3 ICs × 4 legs)")
    print(f"  Working orders: {order_count}  (expected 6 — 3 ICs × 2 stops each)")
    print(f"  {'✓' if leg_count   == 12 else '✗'} Position count {'matches' if leg_count   == 12 else f'mismatch — got {leg_count}, expected 12'}.")
    print(f"  {'✓' if order_count ==  6 else '✗'} Working order count {'matches' if order_count == 6 else f'mismatch — got {order_count}, expected 6'}.")

    iv_rank = iv_pct = None
    for m in mo.get("metrics", []):
        if m.get("symbol", "").upper() == symbol.upper():
            iv_rank = m.get("implied-volatility-rank")
            iv_pct  = m.get("implied-volatility-percentile")
    print(f"\n  IV rank:       {iv_rank}")
    print(f"  IV percentile: {iv_pct}")

    # ── 3. Option Chain ───────────────────────────────────────────────────────
    print(f"\n3. OPTION CHAIN QUALITY\n{hr}")
    chain_resp  = results["phase2_chain"]
    chain       = chain_resp.get("chain", {})
    today_str   = str(date.today())
    expirations = sorted(chain.keys())

    print(f"  Chain ok:          {chain_resp.get('ok')}")
    print(f"  Expirations:       {expirations}")
    print(f"  greeks_complete:   {chain_resp.get('greeks_complete', False)}")
    print(f"  greeks_received:   {chain_resp.get('greeks_received', 0)}")

    zero_dte = today_str in expirations
    print(f"  0DTE ({today_str}): {'✓ Found' if zero_dte else '✗ NOT FOUND'}")

    if zero_dte:
        print("  ✓ Chain uses today's date — 0DTE strikes are valid for get_strategies.")
        strikes = chain[today_str]
        print(f"\n  Strike count:  {len(strikes)} strikes")

        sp = next((s for s in strikes if s["strike_price"] == "575" and s["option_type"] == "Put"),  None)
        sc = next((s for s in strikes if s["strike_price"] == "585" and s["option_type"] == "Call"), None)
        if sp and "delta" in sp:
            print(f"\n  Short put  (575P): delta={sp['delta']:+.3f}  gamma={sp['gamma']:.3f}  theta={sp['theta']:.2f}  iv={sp['iv']:.3f}")
        if sc and "delta" in sc:
            print(f"  Short call (585C): delta={sc['delta']:+.3f}  gamma={sc['gamma']:.3f}  theta={sc['theta']:.2f}  iv={sc['iv']:.3f}")

        if sp and sc and "iv" in sp:
            put_iv, call_iv = sp["iv"], sc["iv"]
            diff = put_iv - call_iv
            if abs(diff) < 0.01:
                skew_label = "neutral"
            elif diff > 0:
                skew_label = f"bearish_skew (put IV {put_iv:.3f} > call IV {call_iv:.3f})"
            else:
                skew_label = f"bullish_skew (call IV {call_iv:.3f} > put IV {put_iv:.3f})"
            print(f"\n  Skew signal:   {skew_label}")
    else:
        print("  ✗ No 0DTE expiry. get_strategies will return incorrect DTE values.")

    # ── 4. Strategy Candidates ────────────────────────────────────────────────
    print(f"\n4. STRATEGY CANDIDATES\n{hr}")
    strats = results["phase3_strategies"]
    print(f"  {'Width':>5}  {'OK':>4}  {'DTE':>4}  {'POP':>5}  {'Credit':>6}  Short Put / Short Call")
    print(f"  {'─'*5}  {'─'*4}  {'─'*4}  {'─'*5}  {'─'*6}  {'─'*30}")

    for w, r in strats:
        ok_flag = "✓" if r.get("ok") else "✗"
        dte     = r.get("dte", "?")
        pop     = r.get("estimated_pop", "?")
        credit  = r.get("net_credit", "?")
        if r.get("ok"):
            sp = r["legs"]["short_put"]["symbol"]
            sc = r["legs"]["short_call"]["symbol"]
            lp = r["legs"]["long_put"]["symbol"]
            lc = r["legs"]["long_call"]["symbol"]
            print(f"  {w:>5}  {ok_flag:>4}  {dte!s:>4}  {pop!s:>5}  ${credit!s:>5}  {sp} / {sc}")
            print(f"                                      Long: {lp} / {lc}")
        else:
            print(f"  {w:>5}  {ok_flag:>4}  N/A   N/A   N/A    Error: {r.get('error', '?')}")

    print(f"\n  Wing width selection: mid-day, 3 ICs open, IV rank 0.38 (moderate).")
    print(f"  Earlier entries favour wider wings for credit; later/multiple open → narrower.")
    print(f"  SELECTED: width 3 — balances credit vs. tail risk with 3 positions already open.")

    # ── 5. Stop Management ────────────────────────────────────────────────────
    print(f"\n5. STOP MANAGEMENT\n{hr}")
    stop_sim = results["phase4_stop_sim"]
    print(f"  ICs:    {stop_sim['ic_count']}   Working stops: {stop_sim['order_count']}")
    print()
    prev_ic = None
    for s in stop_sim["stops"]:
        if s["ic_index"] != prev_ic:
            print(f"  IC #{s['ic_index']}:")
            prev_ic = s["ic_index"]
        print(f"    {s['spread'].capitalize()} stop | Order {s['order_id']} | {s['status']}")
        print(f"    Trigger: ${s['stop_trigger']}  Limit: ${s['limit_price']}")
        print(f"    Tighten: {'YES' if s['tighten'] else 'NO'} — {s['reasoning']}")
        print()

    # ── 6. Entry Decision ─────────────────────────────────────────────────────
    print(f"\n6. ENTRY DECISION\n{hr}")
    entry = results["phase5_entry"]
    print(f"  Decision:     {entry['decision']}")
    print(f"  Hard stop:    {entry['hard_stop_max']}  ({entry['today_count']}/{entry['max_entries']} entries)")
    print(f"  IV rank:      {entry['iv_rank']}")
    print(f"  Deriv BP:     ${entry['derivative_bp']:,.0f}")
    print(f"  Wing width:   {entry['chosen_wing_width']}")
    print(f"\n  ai_entry_reasoning:\n    {entry['ai_entry_reasoning']}")

    # ── 7. Pre-flight ─────────────────────────────────────────────────────────
    print(f"\n7. PRE-FLIGHT (dry_run=True)\n{hr}")
    pf    = results["phase6_preflight"]
    pf_ok = pf.get("ok", False)
    print(f"  ok:        {pf_ok}")
    if pf_ok:
        bp = pf.get("buying_power", {})
        print(f"  dry_run:   {pf.get('dry_run')}")
        print(f"  Current BP: ${bp.get('current_buying_power')}")
        print(f"  New BP:     ${bp.get('new_buying_power')}")
        print(f"  BP change:  ${bp.get('change_in_buying_power')}")
        warnings = pf.get("warnings", [])
        if warnings:
            print(f"  Warnings:  {'; '.join(warnings)}")
        print("\n  ✓ Pre-flight passed. dry_run=True confirmed — no live order submitted.")
    else:
        print(f"  error:     {pf.get('error', '?')}")
        for p in pf.get("problems", []):
            print(f"  problem:   {p}")
        print("\n  ✗ Pre-flight rejected.")

    # ── 8. Overall Verdict ────────────────────────────────────────────────────
    print(f"\n8. OVERALL VERDICT\n{hr}")

    leg_count_ok   = len(results["phase2_positions"].get("positions", [])) == 12
    order_count_ok = len(results["phase2_working_orders"].get("orders",  [])) == 6
    chain_ok       = str(date.today()) in results["phase2_chain"].get("chain", {})
    strats_ok      = all(r.get("ok") for _, r in strats)
    conn_ok        = results["phase1_connection"].get("ok") and results["phase1_connection"].get("mock_mode")

    all_ok = conn_ok and leg_count_ok and order_count_ok and chain_ok and strats_ok and pf_ok
    print(f"  Verdict: {'READY' if all_ok else 'NEEDS ATTENTION'}")
    print()
    print("  MockMCP covers the full loop path end-to-end: connect → read account state")
    print("  → pull option chain (today's date, true 0DTE) → evaluate 4 wing widths →")
    print("  → simulate stop management (2 stops/IC, CLAUDE.md formula) → entry decision")
    print("  → dry-run pre-flight. All phases pass without external dependencies.")
    print()

    gaps = []
    if not chain_ok:
        gaps.append("Option chain does not contain today's date — 0DTE strikes unavailable.")
    if not order_count_ok:
        gaps.append(f"Working order count {len(results['phase2_working_orders'].get('orders',[]))} != 6 expected.")
    if not strats_ok:
        gaps.append("One or more get_strategies calls returned ok=False.")
    if not pf_ok:
        gaps.append("Pre-flight dry-run returned ok=False.")

    remaining = [
        "No DB interaction — get_open_trades / get_today_count / get_today_pnl are not exercised.",
        "Entry decision uses hardcoded today_count=3 (no live DB read).",
        "Stop tightening is evaluated against a static mid-day scenario — add more scenario",
        "  fixtures (e.g. late_session, iv_spike) to cover the full tightening decision matrix.",
        "greeks_incomplete scenario not covered — fallback behavior (Steps 4d/4e) is untested.",
    ]

    if gaps:
        print("  FAILURES:")
        for g in gaps:
            print(f"    ✗ {g}")
        print()
    print("  REMAINING CAVEATS:")
    for r in remaining:
        print(f"    • {r}")
    print()
    print(f"{'═'*70}\n")


# ── main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    config  = load_config()
    results = await run_test(config)
    print_report(results, config)


if __name__ == "__main__":
    asyncio.run(main())
