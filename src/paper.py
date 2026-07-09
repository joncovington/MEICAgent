"""Synthetic-fill paper-trading engine for MEICAgent.

Pure functions over an already-fetched market snapshot — this module never calls the
broker. The calling skill (.claude/commands/paper-loop.md) fetches each symbol's chain,
quotes, VIX, and GEX once per iteration (exactly like the live loop's Step 4) and hands
the result to `process_symbol`, which evaluates all four risk profiles from
config.risk.json against that one snapshot, synthesizes fills/exits, and writes to the
paper database (data/paper_trades.db, selected via --db or MEIC_DB_PATH).

Design mirrors the live Step 6 hard-stops (CLAUDE.md) and the entry/stop math specified
in .claude/commands/execute-entry.md and .claude/commands/stop-management.md, but applies
them deterministically (fixed policy, no agent judgment) so the four profiles are
compared on identical, reproducible criteria. See docs/paper-trading.md.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

try:
    import pytz
    _ET = pytz.timezone("America/New_York")
    def _now_et():
        return datetime.now(_ET)
except ImportError:
    def _now_et():
        return datetime.now(timezone.utc)

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.join(_HERE, "..")
_RISK_PROFILES_PATH = os.path.join(_REPO_ROOT, "config.risk.json")
_CONFIG_PATH = os.path.join(_REPO_ROOT, "config.json")
_DB_PY = os.path.join(_HERE, "db.py")

ALL_PROFILE_NAMES = ["conservative", "moderate", "aggressive", "very-aggressive"]


# ---------------------------------------------------------------------------
# tastytrade fee schedule (Broad-Based Index Options; see CLAUDE.md
# fee_estimate_fallback_per_contract for the same figures documented in config)
# ---------------------------------------------------------------------------

_EXCHANGE_INDEX_FEE = {
    "SPX": 0.60,
    "XSP": 0.00,   # under 10 contracts/leg
    "NDX": 0.25,
    "RUT": 0.18,
}
_OPEN_COMMISSION_PER_CONTRACT = 1.00
_CLEARING_PER_CONTRACT = 0.10
_ORF_PER_CONTRACT = 0.02
_FINRA_TAF_PER_CONTRACT = 0.00329  # sell legs only


def _tt_fees(symbol: str, quantity: int, action: str, sell_legs: int, total_legs: int = 4) -> float:
    """Exact tastytrade fee stack for one IC action, in dollars (not per-contract).

    action:
      "open"   — all 4 legs open; commission + clearing + ORF + exchange fee per contract
                 per leg, plus FINRA TAF on the `sell_legs` (short put + short call = 2).
      "close"  — an active close (stop, profit-target, or force-close); no commission,
                 but clearing/ORF/exchange fee/TAF still apply on the legs being closed.
      "expire" — expired OTM; no fees at all (no closing transaction occurs).
    total_legs lets a per-side stop (2 legs: one short + its long wing) fee correctly
    instead of assuming all 4.
    """
    symbol = symbol.upper()
    exch = _EXCHANGE_INDEX_FEE.get(symbol, 0.0)
    if action == "expire":
        return 0.0
    per_contract = _CLEARING_PER_CONTRACT + _ORF_PER_CONTRACT + exch
    if action == "open":
        per_contract += _OPEN_COMMISSION_PER_CONTRACT
    fee = per_contract * total_legs * quantity
    fee += _FINRA_TAF_PER_CONTRACT * sell_legs * quantity
    return round(fee, 4)


def open_fees(symbol: str, quantity: int = 1) -> float:
    """Fee to open a full 4-leg IC (2 sell legs: short put + short call)."""
    return _tt_fees(symbol, quantity, action="open", sell_legs=2, total_legs=4)


def close_fees_full_ic(symbol: str, quantity: int = 1) -> float:
    """Fee to actively close a full 4-leg IC (2 sell legs: BTC short put + short call)."""
    return _tt_fees(symbol, quantity, action="close", sell_legs=2, total_legs=4)


def close_fees_one_side(symbol: str, quantity: int = 1) -> float:
    """Fee to actively close one side (2 legs: 1 short being bought back = 1 sell-side TAF)."""
    return _tt_fees(symbol, quantity, action="close", sell_legs=1, total_legs=2)


def expire_fees() -> float:
    return 0.0


# ---------------------------------------------------------------------------
# Risk-profile loading
# ---------------------------------------------------------------------------

def load_profiles() -> dict:
    with open(_RISK_PROFILES_PATH) as f:
        data = json.load(f)
    return data["profiles"]


def load_base_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return json.load(f)


def _merged_params(base_config: dict, profile: dict) -> dict:
    """Profile keys override base config keys, matching /set-risk-profile's partial-override
    semantics — unspecified keys (force_close_time, max_wing_width, etc.) fall through."""
    merged = dict(base_config)
    merged.update({k: v for k, v in profile.items() if not k.startswith("_")})
    return merged


# ---------------------------------------------------------------------------
# Deterministic gate evaluator
# ---------------------------------------------------------------------------

def _time_to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def evaluate_entry(snapshot: dict, params: dict, open_ics: list,
                   account_open_count: int | None = None) -> tuple:
    """Encode the Step-6 hard-stops from CLAUDE.md against one profile's thresholds.

    snapshot: one pre-fetched market snapshot for a symbol/session (see paper-loop.md for
        exact shape) including a `candidates` list — one entry per scanned wing width.
    params: this profile's merged config (base config.json + config.risk.json overrides).
    open_ics: this profile's currently-open paper ICs **on this symbol** — used for the
        strike-overlap hard stop (strikes are only ever compared within the same symbol).
    account_open_count: this profile's total open ICs **across every symbol**, for the
        max_concurrent_ics cap — which CLAUDE.md defines as account-wide ("open ICs across
        every symbol combined"), each profile being its own virtual account. Defaults to
        len(open_ics) when omitted (single-symbol callers / tests).

    Returns (enter: bool, reason: str, chosen_candidate: dict | None).
    """
    symbol = snapshot["symbol"]
    if account_open_count is None:
        account_open_count = len(open_ics)

    # 0DTE hard stop
    if snapshot.get("dte", 0) != 0:
        return False, "no_0dte_expiration", None

    # IV rank floor
    iv_rank = snapshot.get("iv_rank")
    if iv_rank is None or iv_rank < params["min_iv_rank"]:
        return False, "iv_rank_below_floor", None

    # Regime gate (VIX / VIX1D ratio / ATR / GEX)
    vix = snapshot.get("vix")
    if vix is not None and vix > params["regime_vix_pause_threshold"]:
        return False, "regime_vix_elevated", None
    vix1d_ratio = snapshot.get("vix1d_ratio")
    if vix1d_ratio is not None and vix1d_ratio > params.get("regime_vix1d_ratio_pause_threshold", 1.30):
        return False, "regime_vix1d_ratio_elevated", None
    atr = snapshot.get("atr_5day")
    atr_underlying = snapshot.get("underlying_price")
    # ATR gate is percentage-based (5-day ATR as a fraction of spot) so one threshold means
    # the same "elevated realized vol" across symbols spanning ~297 (IWM) to ~7500 (SPX) — a
    # fixed points threshold silently over-blocked SPX and never fired for QQQ/IWM.
    if atr is not None and atr_underlying and (atr / atr_underlying) > params["regime_atr_pause_threshold_pct"]:
        return False, "regime_atr_elevated", None
    gex = snapshot.get("gex") or {}
    if gex.get("ok") and gex.get("gex_positive") is False:
        return False, "regime_gex_negative", None

    # Late-entry bias
    now_min = _time_to_minutes(snapshot["now_et"])
    if params.get("late_entry_bias_enabled") and iv_rank <= params.get("late_entry_bias_iv_rank_max", 0.45):
        if now_min < _time_to_minutes(params["late_entry_bias_start_time"]):
            return False, "late_entry_bias_wait", None

    # Concurrent IC cap — account-wide for this profile (across every symbol), matching the
    # live loop's account-wide max_concurrent_ics; each profile is its own virtual account.
    if account_open_count >= params["max_concurrent_ics"]:
        return False, "max_concurrent_ics_reached", None

    # Quarterly / triple-witching hard stops
    today = snapshot.get("date")
    is_quarterly = today in params.get("quarterly_expiry_dates_2026", [])
    is_witching = today in params.get("triple_witching_dates_2026", [])
    if is_quarterly or is_witching:
        if snapshot.get("session_quality") == "open_volatile":
            return False, "quarterly_open_volatile_skip", None
        if is_witching and now_min > _time_to_minutes("12:30"):
            return False, "triple_witching_no_new_entries", None

    # FOMC blackout
    if today in params.get("fomc_dates_2026", []):
        blackout_start = _time_to_minutes(params.get("fomc_blackout_start", "13:30"))
        blackout_end = _time_to_minutes(params.get("fomc_blackout_end", "14:30"))
        if now_min >= blackout_start and now_min < blackout_end:
            return False, "fomc_blackout", None
        if now_min >= blackout_end:
            if iv_rank < 0.40 or snapshot.get("intraday_range", 0) > 3.5:
                return False, "fomc_post_blackout_insufficient_premium", None

    open_strikes = set()
    for ic in open_ics:
        for k in ("put_strike", "call_strike"):
            if ic.get(k) is not None:
                open_strikes.add(float(ic[k]))

    # Evaluate candidates widest-first so the fee-drag bias (prefer wider width) is
    # satisfied automatically by taking the first one that clears every gate.
    candidates = sorted(snapshot.get("candidates", []), key=lambda c: -c["wing_width"])
    otm_floor_call = params["min_call_otm_pct"]
    otm_floor_put = params["min_put_otm_pct"]
    if is_quarterly:
        otm_floor_call = max(otm_floor_call, params.get("quarterly_expiry_min_call_otm_pct", otm_floor_call))

    session = snapshot.get("session_quality")
    delta_ceiling = params["max_call_delta_entry"]
    if session == "open_volatile":
        delta_ceiling = params.get("max_call_delta_entry_open_volatile", delta_ceiling)
    elif session == "late":
        delta_ceiling = params.get("max_call_delta_entry_late", delta_ceiling)

    underlying = snapshot["underlying_price"]
    last_reason = "no_candidate_cleared_all_gates"

    for cand in candidates:
        sp, lp, sc, lc = cand["short_put"], cand["long_put"], cand["short_call"], cand["long_call"]
        wing_width = cand["wing_width"]

        # Strike overlap hard stop (this profile's own open ICs on this symbol)
        cand_strikes = {sp["strike"], lp["strike"], sc["strike"], lc["strike"]}
        if cand_strikes & open_strikes:
            last_reason = "strike_overlap"
            continue

        # Call delta hard stop
        if sc.get("delta") is None or abs(sc["delta"]) > delta_ceiling:
            last_reason = "call_delta_exceeds_ceiling"
            continue

        # OTM distance hard stops
        call_otm_pct = (sc["strike"] - underlying) / underlying
        put_otm_pct = (underlying - sp["strike"]) / underlying
        if call_otm_pct < otm_floor_call:
            last_reason = "call_otm_below_floor"
            continue
        if put_otm_pct < otm_floor_put:
            last_reason = "put_otm_below_floor"
            continue

        # Credit floor (with low-IV relief) + fee-adjusted floor
        ic_natural_bid = (sp["bid"] + sc["bid"]) - (lp["ask"] + lc["ask"])
        if ic_natural_bid <= 0:
            last_reason = "non_positive_credit"
            continue
        low_iv_relief = iv_rank <= params.get("low_iv_credit_floor_iv_rank_max", 0.35)
        pct_floor = params.get("low_iv_min_credit_pct_of_width", 0.10) if low_iv_relief \
            else params["min_credit_pct_of_width"]
        if ic_natural_bid < pct_floor * wing_width:
            last_reason = "credit_below_floor"
            continue

        multiplier = 100
        gross_credit_dollars = ic_natural_bid * multiplier
        fee = open_fees(symbol, quantity=1)
        if gross_credit_dollars - fee < pct_floor * wing_width * multiplier:
            last_reason = "credit_below_fee_adjusted_floor"
            continue

        chosen = dict(cand)
        chosen["ic_natural_bid"] = round(ic_natural_bid, 4)
        chosen["open_fee"] = fee
        return True, "entered", chosen

    return False, last_reason, None


# ---------------------------------------------------------------------------
# Synthetic fill / mark / exit engine
# ---------------------------------------------------------------------------

def synthetic_entry_fill(snapshot: dict, profile_name: str, chosen: dict, params: dict, execution_mode: str) -> dict:
    """Build the ic_trades row for a synthetic fill at ic_natural_bid."""
    now = str(_now_et())
    today = snapshot["date"]
    sp, lp, sc, lc = chosen["short_put"], chosen["long_put"], chosen["short_call"], chosen["long_call"]
    order_id = f"PAPER-{profile_name}-{snapshot['symbol']}-{_now_et().strftime('%Y%m%d%H%M%S%f')}"
    return {
        "ic_order_id": order_id,
        "trade_date": today,
        "entry_time": now,
        "expiration": snapshot.get("expiration", today),
        "symbol": snapshot["symbol"],
        "put_strike": sp["strike"],
        "call_strike": sc["strike"],
        "wing_width": chosen["wing_width"],
        "put_symbol": sp.get("streamer_symbol"),
        "call_symbol": sc.get("streamer_symbol"),
        "long_put_symbol": lp.get("streamer_symbol"),
        "long_call_symbol": lc.get("streamer_symbol"),
        "put_credit": round(sp["bid"] - lp["ask"], 4),
        "call_credit": round(sc["bid"] - lc["ask"], 4),
        "net_credit": chosen["ic_natural_bid"],
        "quantity": 1,
        "put_delta_at_entry": sp.get("delta"),
        "call_delta_at_entry": sc.get("delta"),
        "long_put_delta_at_entry": lp.get("delta"),
        "long_call_delta_at_entry": lc.get("delta"),
        "underlying_price_entry": snapshot["underlying_price"],
        "iv_rank_at_entry": snapshot.get("iv_rank"),
        "session_quality": snapshot.get("session_quality"),
        "iv_skew_signal": snapshot.get("iv_skew_signal"),
        "price_action_signal": snapshot.get("price_action_signal"),
        "ai_entry_reasoning": f"paper/{execution_mode}/{profile_name}: deterministic entry, "
                               f"widest clearing candidate ({chosen['wing_width']}-wide)",
        "stop_trigger_original": params["stop_trigger_ratio"],
        "stop_limit_original": params.get("stop_limit_ratio", 1.02),
        "stop_trigger_current": params["stop_trigger_ratio"],
        "stop_limit_current": params.get("stop_limit_ratio", 1.02),
        "status": "open",
        "fill_confirmed_at": now,
        "fees": chosen["open_fee"],
        "risk_profile": profile_name,
        "execution_mode": execution_mode,
        "iv_rank_source": snapshot.get("iv_rank_source", "native"),
        "created_at": now,
        "updated_at": now,
    }


def _leg_quote(snapshot_legs: dict, streamer_symbol: str):
    return snapshot_legs.get(streamer_symbol)


# ---------------------------------------------------------------------------
# Physical-settlement exit hardening
# ---------------------------------------------------------------------------
# SPX/XSP are cash-settled European-style — a force-close that misses just settles to
# cash. QQQ/IWM (and any other symbol NOT in cash_settled_symbols) are physically-settled
# American-style: a short leg left open into expiration risks assignment into shares, and a
# strike pinned at-the-money at the bell has an ambiguous ITM/OTM outcome. The live loop
# closes these earlier (physical_settlement_force_close_time) and escalates a failed close
# to CRITICAL; here in paper we mirror the earlier close AND add modeled friction so paper
# P&L for physically-settled symbols isn't misleadingly cleaner than live. See
# docs/paper-trading.md. Friction figures are deliberately conservative, config-tunable, and
# meant to be calibrated against a tiny-live run later (same philosophy as the 20-40% discount).

def _is_cash_settled(symbol: str, base_config: dict) -> bool:
    listed = [s.upper() for s in base_config.get("cash_settled_symbols", [])]
    return symbol.upper() in listed


def _pin_penalty(strike, underlying, wing_width, params: dict) -> float:
    """Extra force-close cost when a short strike is pinned ~ATM at the close — the outcome
    (assigned vs. expires worthless) is a coin flip there, so the modeled cost jumps toward a
    fraction of the wing width. Zero when the strike is comfortably away from spot."""
    if strike is None or not underlying:
        return 0.0
    threshold = params.get("pin_risk_threshold_pct", 0.002)
    if abs(strike - underlying) / underlying < threshold:
        return params.get("pin_risk_penalty_pct_of_width", 0.25) * (wing_width or 0)
    return 0.0


def force_close_active(snapshot: dict, base_config: dict, is_cash_settled: bool) -> tuple:
    """Return (active: bool, reason: str|None) for whether this symbol's open positions must
    be force-closed at the snapshot's time. Mirrors the live loop's force-close cascade
    (stop-management.md Step 7) which paper previously only partially implemented — it had
    just the 15:45 EOD close, missing the FOMC/expiry event closes and the earlier
    physically-settled close entirely."""
    now_min = _time_to_minutes(snapshot["now_et"])
    today = snapshot.get("date")

    # FOMC blackout — all symbols, earliest trigger of the day
    if today in base_config.get("fomc_dates_2026", []) and \
            now_min >= _time_to_minutes(base_config.get("fomc_blackout_start", "13:30")):
        return True, "force_close_fomc"
    # Triple-witching / quarterly expiry — all symbols, 14:00 ET
    if (today in base_config.get("triple_witching_dates_2026", []) or
            today in base_config.get("quarterly_expiry_dates_2026", [])) and now_min >= _time_to_minutes("14:00"):
        return True, "force_close_expiry_event"
    # Earlier close for physically-settled symbols (assignment/pin risk)
    if not is_cash_settled and \
            now_min >= _time_to_minutes(base_config.get("physical_settlement_force_close_time", "15:30")):
        return True, "force_close_physical_settlement"
    # General EOD close — all symbols
    if now_min >= _time_to_minutes(base_config.get("force_close_time", "15:45")):
        return True, "force_close_eod"
    return False, None


def evaluate_open_trade(trade: dict, leg_quotes: dict, params: dict, force_close: bool,
                        underlying_price: float | None = None, is_cash_settled: bool = True,
                        force_close_reason: str = "force_close_eod") -> dict:
    """Mark-to-market one open paper IC and decide profit-target / per-side stop / force-close.

    leg_quotes: {streamer_symbol: {"bid":.., "ask":.., "mid":..}} for this trade's 4 legs,
    taken from the snapshot's per-symbol quote set.

    Returns a dict describing the action: {"action": "hold"|"profit_target"|"stop_put"|
    "stop_call"|"stop_both"|"force_close", ...pricing...}. The caller (process_symbol)
    applies the action to the DB.
    """
    sq = leg_quotes.get(trade["put_symbol"])
    cq = leg_quotes.get(trade["call_symbol"])
    lpq = leg_quotes.get(trade["long_put_symbol"])
    lcq = leg_quotes.get(trade["long_call_symbol"])
    if not all([sq, cq, lpq, lcq]):
        return {"action": "hold", "reason": "quotes_unavailable"}

    net_credit = trade["net_credit"]
    put_open = trade["status"] == "open" or (trade["status"] == "partial" and trade.get("put_stop_order_id") is None)
    call_open = trade["status"] == "open" or (trade["status"] == "partial" and trade.get("call_stop_order_id") is None)

    ic_current_cost_mid = (sq["mid"] + cq["mid"]) - (lpq["mid"] + lcq["mid"])
    if trade["status"] == "open" and ic_current_cost_mid <= params.get("profit_target_pct", 0.50) * net_credit:
        return {
            "action": "profit_target",
            "put_exit_price": max(sq["bid"] - lpq["ask"], 0),
            "call_exit_price": max(cq["bid"] - lcq["ask"], 0),
        }

    if force_close:
        put_exit = max(sq["bid"] - lpq["ask"], 0) if put_open else None
        call_exit = max(cq["bid"] - lcq["ask"], 0) if call_open else None
        friction_applied = False
        if not is_cash_settled:
            # Physically-settled symbols pay a modeled friction on the force-close (wider
            # spreads near the bell + assignment/pin risk) so paper doesn't overstate their
            # safety vs. cash-settled index products. Added to the cost-to-close, so P&L
            # (credit − exit_price) moves the right (worse) direction.
            friction = params.get("physical_settlement_exit_friction", 0.05)
            if put_open:
                put_exit = round(put_exit + friction +
                                 _pin_penalty(trade.get("put_strike"), underlying_price,
                                              trade.get("wing_width"), params), 4)
                friction_applied = True
            if call_open:
                call_exit = round(call_exit + friction +
                                  _pin_penalty(trade.get("call_strike"), underlying_price,
                                               trade.get("wing_width"), params), 4)
                friction_applied = True
        return {
            "action": "force_close",
            "put_open": put_open,
            "call_open": call_open,
            "put_exit_price": put_exit,
            "call_exit_price": call_exit,
            "reason": force_close_reason,
            "physical_friction_applied": friction_applied,
        }

    stop_trigger = trade.get("stop_trigger_current") or params["stop_trigger_ratio"]
    stop_limit = trade.get("stop_limit_current") or params.get("stop_limit_ratio", 1.02)
    call_cost_mid = cq["mid"] - lcq["mid"]
    put_cost_mid = sq["mid"] - lpq["mid"]

    call_trigger = call_open and call_cost_mid >= stop_trigger * net_credit
    put_trigger = put_open and put_cost_mid >= stop_trigger * net_credit

    if call_trigger and put_trigger:
        return {
            "action": "stop_both",
            "put_exit_price": round((sq["ask"] - lpq["bid"]) * stop_limit, 4),
            "call_exit_price": round((cq["ask"] - lcq["bid"]) * stop_limit, 4),
        }
    if call_trigger:
        return {"action": "stop_call", "call_exit_price": round((cq["ask"] - lcq["bid"]) * stop_limit, 4)}
    if put_trigger:
        return {"action": "stop_put", "put_exit_price": round((sq["ask"] - lpq["bid"]) * stop_limit, 4)}

    return {"action": "hold"}


# ---------------------------------------------------------------------------
# DB I/O (shells out to db.py, pointed at the paper DB)
# ---------------------------------------------------------------------------

def _db(args_list: list, db_path: str) -> dict:
    cmd = [sys.executable, _DB_PY, "--db", db_path] + args_list
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip() or f"db.py exited {result.returncode}"}
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"ok": False, "error": f"unparseable db.py output: {result.stdout!r}"}


def _save_trade(row: dict, db_path: str) -> dict:
    return _db(["save_trade", "--data", json.dumps(row)], db_path)


def _update_trade(ic_order_id: str, fields: dict, db_path: str) -> dict:
    args_list = ["update_trade", "--ic_order_id", ic_order_id]
    for k, v in fields.items():
        args_list += [f"--{k}", str(v)]
    return _db(args_list, db_path)


def _get_open_trades(symbol: str, profile: str, trade_date: str, db_path: str) -> list:
    # --date pins the query to the snapshot's own trade_date rather than the real system
    # clock, which is required for replay mode (see db.py's cmd_get_open_trades docstring).
    result = _db(["get_open_trades", "--symbol", symbol, "--date", trade_date], db_path)
    trades = result.get("open_trades", []) if result.get("ok") else []
    return [t for t in trades if t.get("risk_profile") == profile]


def _get_profile_open_count(profile: str, trade_date: str, db_path: str) -> int:
    """This profile's total open ICs across every symbol — the account-wide count the
    max_concurrent_ics cap is checked against (not the per-symbol count)."""
    result = _db(["get_open_trades", "--date", trade_date], db_path)
    trades = result.get("open_trades", []) if result.get("ok") else []
    return sum(1 for t in trades if t.get("risk_profile") == profile)


def process_symbol(snapshot: dict, db_path: str, execution_mode: str, profiles_filter=None) -> dict:
    """Run all four profiles' mark-to-market/exit + entry evaluation for one symbol against
    one already-fetched snapshot. Returns a per-profile action summary for logging."""
    base_config = load_base_config()
    all_profiles = load_profiles()
    names = profiles_filter or ALL_PROFILE_NAMES
    symbol = snapshot["symbol"]
    is_cash = _is_cash_settled(symbol, base_config)
    force_close, force_close_reason = force_close_active(snapshot, base_config, is_cash)
    underlying_price = snapshot.get("underlying_price")
    leg_quotes = snapshot.get("leg_quotes", {})

    results = {}
    for name in names:
        params = _merged_params(base_config, all_profiles[name])
        open_ics = _get_open_trades(symbol, name, snapshot["date"], db_path)
        actions = []

        for trade in open_ics:
            decision = evaluate_open_trade(trade, leg_quotes, params, force_close,
                                           underlying_price=underlying_price,
                                           is_cash_settled=is_cash,
                                           force_close_reason=force_close_reason)
            actions.append({"ic_order_id": trade["ic_order_id"], "decision": decision})
            _apply_exit_decision(trade, decision, symbol, db_path)

        still_open = _get_open_trades(symbol, name, snapshot["date"], db_path)
        account_open = _get_profile_open_count(name, snapshot["date"], db_path)
        if account_open < params["max_concurrent_ics"]:
            entered, reason, chosen = evaluate_entry(snapshot, params, still_open,
                                                     account_open_count=account_open)
            if entered:
                row = synthetic_entry_fill(snapshot, name, chosen, params, execution_mode)
                save_result = _save_trade(row, db_path)
                actions.append({"entry": "filled", "ic_order_id": row["ic_order_id"],
                                 "net_credit": row["net_credit"], "save_result": save_result})
            else:
                actions.append({"entry": "skipped", "reason": reason})
        else:
            actions.append({"entry": "skipped", "reason": "max_concurrent_ics_reached"})

        results[name] = actions

    return {"ok": True, "symbol": symbol, "results": results}


def _apply_exit_decision(trade: dict, decision: dict, symbol: str, db_path: str) -> None:
    action = decision["action"]
    now = str(_now_et())
    ic_order_id = trade["ic_order_id"]
    net_credit = trade["net_credit"]
    mult = 100

    if action == "hold":
        return

    if action == "profit_target":
        put_pnl = round((trade["put_credit"] - decision["put_exit_price"]) * mult, 2)
        call_pnl = round((trade["call_credit"] - decision["call_exit_price"]) * mult, 2)
        fee = close_fees_full_ic(symbol)
        total_pnl = put_pnl + call_pnl
        _db(["record_leg_exit", "--ic_order_id", ic_order_id, "--side", "put",
             "--status", "expired", "--exit_time", now, "--exit_reason", "profit_target_50pct",
             "--exit_price", str(decision["put_exit_price"]), "--pnl", str(put_pnl)], db_path)
        _db(["record_leg_exit", "--ic_order_id", ic_order_id, "--side", "call",
             "--status", "expired", "--exit_time", now, "--exit_reason", "profit_target_50pct",
             "--exit_price", str(decision["call_exit_price"]), "--pnl", str(call_pnl)], db_path)
        _update_trade(ic_order_id, {
            "status": "closed_profit_target", "exit_time": now,
            "exit_reason": "profit_target_50pct", "pnl": total_pnl, "fees": trade.get("fees", 0) + fee,
        }, db_path)
        return

    if action in ("stop_call", "stop_put", "stop_both"):
        fee = close_fees_one_side(symbol) if action != "stop_both" else close_fees_full_ic(symbol)
        updates = {"fees": (trade.get("fees") or 0) + fee}
        if action in ("stop_call", "stop_both"):
            call_pnl = round((trade["call_credit"] - decision["call_exit_price"]) * mult, 2)
            updates["call_stop_cost"] = decision["call_exit_price"]
            _db(["record_leg_exit", "--ic_order_id", ic_order_id, "--side", "call",
                 "--status", "stopped", "--exit_time", now, "--exit_reason", "per_side_stop",
                 "--exit_price", str(decision["call_exit_price"]), "--pnl", str(call_pnl)], db_path)
        if action in ("stop_put", "stop_both"):
            put_pnl = round((trade["put_credit"] - decision["put_exit_price"]) * mult, 2)
            updates["put_stop_cost"] = decision["put_exit_price"]
            _db(["record_leg_exit", "--ic_order_id", ic_order_id, "--side", "put",
                 "--status", "stopped", "--exit_time", now, "--exit_reason", "per_side_stop",
                 "--exit_price", str(decision["put_exit_price"]), "--pnl", str(put_pnl)], db_path)
        remaining_open = not (action == "stop_both")
        updates["status"] = "stopped" if action == "stop_both" else "partial"
        _update_trade(ic_order_id, updates, db_path)
        # Accumulate realized pnl for stopped legs; final pnl reconciled when the other
        # side (if any) later resolves via force_close or a subsequent stop.
        existing_pnl = trade.get("pnl") or 0
        delta_pnl = 0
        if action in ("stop_call", "stop_both"):
            delta_pnl += round((trade["call_credit"] - decision["call_exit_price"]) * mult, 2)
        if action in ("stop_put", "stop_both"):
            delta_pnl += round((trade["put_credit"] - decision["put_exit_price"]) * mult, 2)
        _update_trade(ic_order_id, {"pnl": existing_pnl + delta_pnl}, db_path)
        return

    if action == "force_close":
        fee = close_fees_full_ic(symbol) if (decision["put_open"] and decision["call_open"]) \
            else close_fees_one_side(symbol)
        reason = decision.get("reason") or "force_close_eod"
        existing_pnl = trade.get("pnl") or 0
        delta_pnl = 0
        if decision["put_open"]:
            put_pnl = round((trade["put_credit"] - decision["put_exit_price"]) * mult, 2)
            delta_pnl += put_pnl
            _db(["record_leg_exit", "--ic_order_id", ic_order_id, "--side", "put",
                 "--status", "force_closed", "--exit_time", now, "--exit_reason", reason,
                 "--exit_price", str(decision["put_exit_price"]), "--pnl", str(put_pnl)], db_path)
        if decision["call_open"]:
            call_pnl = round((trade["call_credit"] - decision["call_exit_price"]) * mult, 2)
            delta_pnl += call_pnl
            _db(["record_leg_exit", "--ic_order_id", ic_order_id, "--side", "call",
                 "--status", "force_closed", "--exit_time", now, "--exit_reason", reason,
                 "--exit_price", str(decision["call_exit_price"]), "--pnl", str(call_pnl)], db_path)
        _update_trade(ic_order_id, {
            "status": "force_closed", "exit_time": now, "exit_reason": reason,
            "pnl": existing_pnl + delta_pnl, "fees": (trade.get("fees") or 0) + fee,
        }, db_path)
        return


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="MEICAgent paper-trading engine")
    parser.add_argument("--db", default=os.path.join(_REPO_ROOT, "data", "paper_trades.db"))
    sub = parser.add_subparsers(dest="command")

    p_proc = sub.add_parser("process_symbol",
                             help="Evaluate all four profiles' exits + entries for one symbol "
                                  "against a pre-fetched market snapshot")
    p_proc.add_argument("--snapshot", required=True, help="Snapshot JSON (or @path to a file)")
    p_proc.add_argument("--execution_mode", default="paper", choices=["paper", "replay"])
    p_proc.add_argument("--profiles", default=None,
                         help="Comma-separated subset of profiles; omit for all four")

    p_fees = sub.add_parser("fees", help="Compute the tastytrade fee stack for one action")
    p_fees.add_argument("--symbol", required=True)
    p_fees.add_argument("--action", required=True, choices=["open", "close_full", "close_side", "expire"])
    p_fees.add_argument("--quantity", type=int, default=1)

    args = parser.parse_args()

    if args.command == "process_symbol":
        raw = args.snapshot
        if raw.startswith("@"):
            with open(raw[1:]) as f:
                raw = f.read()
        snapshot = json.loads(raw)
        profiles_filter = args.profiles.split(",") if args.profiles else None
        result = process_symbol(snapshot, args.db, args.execution_mode, profiles_filter)
        print(json.dumps(result, default=str))
    elif args.command == "fees":
        fn = {
            "open": open_fees, "close_full": close_fees_full_ic,
            "close_side": close_fees_one_side, "expire": lambda s, q: expire_fees(),
        }[args.action]
        fee = fn(args.symbol, args.quantity)
        print(json.dumps({"ok": True, "symbol": args.symbol, "action": args.action, "fee": fee}))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
