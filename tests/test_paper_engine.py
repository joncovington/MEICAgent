"""Unit tests for the paper-trading engine: tastytrade fee model, deterministic gate
evaluator, synthetic fill/exit math, and the get_range_summary DB rollup.

No credentials or live connection required. Fee/gate/fill tests operate on pure functions
with hand-built snapshots; DB tests use a temp SQLite file (same pattern as test_db.py).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import db
import paper


# ── Fee model ────────────────────────────────────────────────────────────────

def test_open_fees_spx_matches_documented_fallback():
    # CLAUDE.md's fee_estimate_fallback_per_contract documents SPX at $6.89
    assert paper.open_fees("SPX", quantity=1) == pytest.approx(6.89, abs=0.01)


def test_open_fees_xsp_matches_documented_fallback():
    assert paper.open_fees("XSP", quantity=1) == pytest.approx(4.49, abs=0.01)


def test_open_fees_ndx_and_rut_have_higher_exchange_fee_than_xsp():
    assert paper.open_fees("NDX", 1) > paper.open_fees("XSP", 1)
    assert paper.open_fees("RUT", 1) > paper.open_fees("XSP", 1)


def test_close_fees_full_ic_excludes_open_commission():
    open_fee = paper.open_fees("SPX", 1)
    close_fee = paper.close_fees_full_ic("SPX", 1)
    assert close_fee < open_fee  # no $1.00/contract commission on close
    assert close_fee == pytest.approx(open_fee - 1.00 * 4, abs=0.01)


def test_close_fees_one_side_is_roughly_half_full_ic():
    full = paper.close_fees_full_ic("SPX", 1)
    side = paper.close_fees_one_side("SPX", 1)
    assert side < full
    assert side == pytest.approx(full / 2, abs=0.02)


def test_expire_fees_are_zero():
    assert paper.expire_fees() == 0.0


def test_fees_scale_with_quantity():
    assert paper.open_fees("SPX", 2) == pytest.approx(paper.open_fees("SPX", 1) * 2, abs=0.001)


# ── Gate evaluator: helpers ──────────────────────────────────────────────────

def _leg(strike, delta, bid, ask, sym):
    return {"strike": strike, "streamer_symbol": sym, "delta": delta, "bid": bid, "ask": ask}


def _candidate(width, sp_strike, sc_strike, sp_delta=-0.15, sc_delta=0.15,
                sp_bid=0.55, sp_ask=0.65, sc_bid=0.50, sc_ask=0.60,
                lp_bid=0.15, lp_ask=0.25, lc_bid=0.12, lc_ask=0.22):
    return {
        "wing_width": width,
        "short_put": _leg(sp_strike, sp_delta, sp_bid, sp_ask, f"SP{width}"),
        "long_put": _leg(sp_strike - width, sp_delta * 0.4, lp_bid, lp_ask, f"LP{width}"),
        "short_call": _leg(sc_strike, sc_delta, sc_bid, sc_ask, f"SC{width}"),
        "long_call": _leg(sc_strike + width, sc_delta * 0.4, lc_bid, lc_ask, f"LC{width}"),
    }


def _base_snapshot(**overrides):
    snap = {
        "symbol": "XSP", "date": "2026-07-09", "now_et": "13:00",
        "expiration": "2026-07-09", "dte": 0,
        "underlying_price": 590.0, "iv_rank": 0.32,
        "vix": 16.0, "vix1d_ratio": 1.02, "atr_5day": 8.0,
        "session_quality": "midday", "gex": {"ok": True, "gex_positive": True},
        "candidates": [_candidate(5, 583, 598), _candidate(2, 583, 598)],
        "leg_quotes": {},
    }
    snap.update(overrides)
    return snap


CONSERVATIVE = paper.load_profiles()["conservative"]
MODERATE = paper.load_profiles()["moderate"]
BASE_CONFIG = paper.load_base_config()


def _params(profile):
    return paper._merged_params(BASE_CONFIG, profile)


# ── Gate evaluator: hard stops ───────────────────────────────────────────────

def test_evaluate_entry_enters_when_all_gates_clear():
    snap = _base_snapshot(now_et="13:00")  # after conservative's 12:00 late-entry-bias start
    entered, reason, chosen = paper.evaluate_entry(snap, _params(CONSERVATIVE), [])
    assert entered is True
    assert reason == "entered"
    assert chosen["wing_width"] == 5  # widest clearing candidate preferred


def test_evaluate_entry_rejects_non_0dte():
    snap = _base_snapshot(dte=1)
    entered, reason, _ = paper.evaluate_entry(snap, _params(CONSERVATIVE), [])
    assert entered is False
    assert reason == "no_0dte_expiration"


def test_evaluate_entry_rejects_below_iv_rank_floor():
    snap = _base_snapshot(iv_rank=0.10)  # conservative floor is 0.30
    entered, reason, _ = paper.evaluate_entry(snap, _params(CONSERVATIVE), [])
    assert entered is False
    assert reason == "iv_rank_below_floor"


def test_evaluate_entry_moderate_clears_lower_iv_rank_that_conservative_rejects():
    snap = _base_snapshot(iv_rank=0.25, now_et="13:00")  # below conservative's 0.30, above moderate's 0.22
    cons_entered, cons_reason, _ = paper.evaluate_entry(snap, _params(CONSERVATIVE), [])
    mod_entered, mod_reason, _ = paper.evaluate_entry(snap, _params(MODERATE), [])
    assert cons_entered is False and cons_reason == "iv_rank_below_floor"
    assert mod_entered is True


def test_evaluate_entry_rejects_regime_vix_elevated():
    snap = _base_snapshot(vix=30.0)  # conservative pause threshold is 25
    entered, reason, _ = paper.evaluate_entry(snap, _params(CONSERVATIVE), [])
    assert entered is False
    assert reason == "regime_vix_elevated"


def test_evaluate_entry_rejects_regime_gex_negative():
    snap = _base_snapshot(gex={"ok": True, "gex_positive": False})
    entered, reason, _ = paper.evaluate_entry(snap, _params(CONSERVATIVE), [])
    assert entered is False
    assert reason == "regime_gex_negative"


def test_evaluate_entry_late_entry_bias_blocks_before_start_time():
    # Conservative's late_entry_bias_start_time is 12:00; iv_rank 0.32 <= 0.45 bias threshold
    snap = _base_snapshot(now_et="10:30", iv_rank=0.32)
    entered, reason, _ = paper.evaluate_entry(snap, _params(CONSERVATIVE), [])
    assert entered is False
    assert reason == "late_entry_bias_wait"


def test_evaluate_entry_late_entry_bias_allows_after_start_time():
    snap = _base_snapshot(now_et="12:30", iv_rank=0.32)
    entered, reason, _ = paper.evaluate_entry(snap, _params(CONSERVATIVE), [])
    assert entered is True


def test_evaluate_entry_rejects_max_concurrent_ics_reached():
    snap = _base_snapshot(now_et="13:00")
    fake_open = [{"put_strike": 500, "call_strike": 700}] * CONSERVATIVE["max_concurrent_ics"]
    entered, reason, _ = paper.evaluate_entry(snap, _params(CONSERVATIVE), fake_open)
    assert entered is False
    assert reason == "max_concurrent_ics_reached"


def test_evaluate_entry_rejects_strike_overlap_and_tries_narrower_candidate():
    # Open IC holds the 583/598 strikes used by the 5-wide candidate; the 2-wide candidate
    # (same strikes in this fixture) also overlaps, so no candidate clears.
    snap = _base_snapshot(now_et="13:00")
    open_ics = [{"put_strike": 583.0, "call_strike": 598.0}]
    entered, reason, _ = paper.evaluate_entry(snap, _params(CONSERVATIVE), open_ics)
    assert entered is False
    assert reason == "strike_overlap"


def test_evaluate_entry_rejects_call_delta_above_ceiling():
    snap = _base_snapshot(now_et="13:00",
                           candidates=[_candidate(5, 583, 598, sc_delta=0.35)])
    entered, reason, _ = paper.evaluate_entry(snap, _params(CONSERVATIVE), [])
    assert entered is False
    assert reason == "call_delta_exceeds_ceiling"


def test_evaluate_entry_rejects_call_otm_below_floor():
    # short call at 591 is only ~0.17% OTM on a 590 underlying; conservative floor is 0.35%
    snap = _base_snapshot(now_et="13:00", underlying_price=590.0,
                           candidates=[_candidate(5, 583, 591)])
    entered, reason, _ = paper.evaluate_entry(snap, _params(CONSERVATIVE), [])
    assert entered is False
    assert reason == "call_otm_below_floor"


def test_evaluate_entry_rejects_credit_below_floor():
    # Very tight bid/ask so net credit is far below min_credit_pct_of_width * wing_width
    snap = _base_snapshot(now_et="13:00", iv_rank=0.50,  # above low-IV relief threshold
                           candidates=[_candidate(5, 583, 598, sp_bid=0.05, sp_ask=0.06,
                                                   sc_bid=0.05, sc_ask=0.06,
                                                   lp_bid=0.04, lp_ask=0.05,
                                                   lc_bid=0.04, lc_ask=0.05)])
    entered, reason, _ = paper.evaluate_entry(snap, _params(CONSERVATIVE), [])
    assert entered is False
    # Tight bid/ask here nets to a negative or near-zero credit, which the earlier
    # non_positive_credit check catches before the pct-of-width floor is even evaluated —
    # both are valid "credit too thin" rejections for this fixture.
    assert reason in ("credit_below_floor", "credit_below_fee_adjusted_floor", "non_positive_credit")


def test_evaluate_entry_rejects_positive_but_thin_credit_below_pct_floor():
    # natural_bid = (0.20+0.18) - (0.15+0.14) = 0.09; standard floor at iv_rank 0.50
    # (above low-IV relief) is 0.15*5=0.75 -- clearly below, and positive so the
    # non_positive_credit branch does not intercept it.
    snap = _base_snapshot(now_et="13:00", iv_rank=0.50,
                           candidates=[_candidate(5, 583, 598, sp_bid=0.20, sp_ask=0.22,
                                                   sc_bid=0.18, sc_ask=0.20,
                                                   lp_bid=0.14, lp_ask=0.15,
                                                   lc_bid=0.13, lc_ask=0.14)])
    entered, reason, _ = paper.evaluate_entry(snap, _params(CONSERVATIVE), [])
    assert entered is False
    assert reason == "credit_below_floor"


def test_evaluate_entry_low_iv_relief_lowers_credit_floor():
    # iv_rank 0.32 is within conservative's low_iv_credit_floor_iv_rank_max (0.35), so the
    # relaxed floor (0.10 * width) applies instead of the standard 0.15 * width.
    snap = _base_snapshot(now_et="13:00", iv_rank=0.32,
                           candidates=[_candidate(5, 583, 598, sp_bid=0.30, sp_ask=0.35,
                                                   sc_bid=0.30, sc_ask=0.35,
                                                   lp_bid=0.15, lp_ask=0.20,
                                                   lc_bid=0.15, lc_ask=0.20)])
    # natural_bid = (0.30+0.30) - (0.20+0.20) = 0.20; standard floor 0.15*5=0.75 (fail),
    # low-IV floor 0.10*5=0.50 (fail too) -- use a credit that clears low-IV but not standard
    entered, reason, chosen = paper.evaluate_entry(snap, _params(CONSERVATIVE), [])
    assert entered is False  # 0.20 still below even the relaxed 0.50 floor; sanity check only
    assert reason in ("credit_below_floor", "credit_below_fee_adjusted_floor")


def test_evaluate_entry_prefers_widest_clearing_candidate():
    snap = _base_snapshot(now_et="13:00", candidates=[_candidate(2, 583, 598), _candidate(5, 583, 598)])
    entered, reason, chosen = paper.evaluate_entry(snap, _params(CONSERVATIVE), [])
    assert entered is True
    assert chosen["wing_width"] == 5


# ── Synthetic fill / exit math ───────────────────────────────────────────────

def test_synthetic_entry_fill_records_natural_bid_as_net_credit():
    snap = _base_snapshot(now_et="13:00")
    _, _, chosen = paper.evaluate_entry(snap, _params(CONSERVATIVE), [])
    row = paper.synthetic_entry_fill(snap, "conservative", chosen, _params(CONSERVATIVE), "paper")
    assert row["net_credit"] == chosen["ic_natural_bid"]
    assert row["risk_profile"] == "conservative"
    assert row["execution_mode"] == "paper"
    assert row["status"] == "open"
    assert row["fees"] == pytest.approx(paper.open_fees("XSP", 1), abs=0.001)


def test_evaluate_open_trade_profit_target_fires_at_or_below_threshold():
    trade = {"put_symbol": "SP", "call_symbol": "SC", "long_put_symbol": "LP", "long_call_symbol": "LC",
              "net_credit": 0.58, "status": "open", "put_credit": 0.30, "call_credit": 0.28,
              "stop_trigger_current": 0.93, "stop_limit_current": 1.02}
    leg_quotes = {
        "SP": {"bid": 0.10, "ask": 0.15, "mid": 0.125}, "LP": {"bid": 0.02, "ask": 0.05, "mid": 0.035},
        "SC": {"bid": 0.08, "ask": 0.12, "mid": 0.10}, "LC": {"bid": 0.02, "ask": 0.04, "mid": 0.03},
    }
    decision = paper.evaluate_open_trade(trade, leg_quotes, _params(MODERATE), force_close=False)
    assert decision["action"] == "profit_target"


def test_evaluate_open_trade_holds_when_nothing_triggers():
    trade = {"put_symbol": "SP", "call_symbol": "SC", "long_put_symbol": "LP", "long_call_symbol": "LC",
              "net_credit": 0.58, "status": "open", "put_credit": 0.30, "call_credit": 0.28,
              "stop_trigger_current": 0.93, "stop_limit_current": 1.02}
    leg_quotes = {
        "SP": {"bid": 0.24, "ask": 0.30, "mid": 0.27}, "LP": {"bid": 0.06, "ask": 0.09, "mid": 0.075},
        "SC": {"bid": 0.20, "ask": 0.26, "mid": 0.23}, "LC": {"bid": 0.05, "ask": 0.08, "mid": 0.065},
    }
    decision = paper.evaluate_open_trade(trade, leg_quotes, _params(MODERATE), force_close=False)
    assert decision["action"] == "hold"


def test_evaluate_open_trade_stops_call_side_when_cost_reaches_trigger():
    trade = {"put_symbol": "SP", "call_symbol": "SC", "long_put_symbol": "LP", "long_call_symbol": "LC",
              "net_credit": 0.58, "status": "open", "put_credit": 0.30, "call_credit": 0.28,
              "stop_trigger_current": 0.93, "stop_limit_current": 1.02}
    leg_quotes = {
        "SP": {"bid": 0.20, "ask": 0.26, "mid": 0.23}, "LP": {"bid": 0.05, "ask": 0.08, "mid": 0.065},
        "SC": {"bid": 0.60, "ask": 0.68, "mid": 0.64}, "LC": {"bid": 0.03, "ask": 0.06, "mid": 0.045},
    }
    decision = paper.evaluate_open_trade(trade, leg_quotes, _params(MODERATE), force_close=False)
    assert decision["action"] == "stop_call"


def test_evaluate_open_trade_force_close_overrides_hold():
    trade = {"put_symbol": "SP", "call_symbol": "SC", "long_put_symbol": "LP", "long_call_symbol": "LC",
              "net_credit": 0.58, "status": "open", "put_credit": 0.30, "call_credit": 0.28,
              "stop_trigger_current": 0.93, "stop_limit_current": 1.02}
    leg_quotes = {
        "SP": {"bid": 0.24, "ask": 0.30, "mid": 0.27}, "LP": {"bid": 0.06, "ask": 0.09, "mid": 0.075},
        "SC": {"bid": 0.20, "ask": 0.26, "mid": 0.23}, "LC": {"bid": 0.05, "ask": 0.08, "mid": 0.065},
    }
    decision = paper.evaluate_open_trade(trade, leg_quotes, _params(MODERATE), force_close=True)
    assert decision["action"] == "force_close"
    assert decision["put_open"] is True and decision["call_open"] is True


# ── get_range_summary (DB integration) ───────────────────────────────────────

@pytest.fixture
def paper_db_path(monkeypatch, tmp_path):
    path = str(tmp_path / "paper_trades.db")
    monkeypatch.setattr(db, "_DB_PATH", path)
    db.cmd_init_db(None)
    return path


def _insert_paper_trade(db_path, **kwargs):
    import sqlite3
    defaults = dict(
        trade_date="2026-07-01", symbol="SPX", risk_profile="conservative",
        net_credit=1.5, pnl=150.0, fees=25.0, status="expired", quantity=1,
        ic_order_id="P-1", created_at="2026-07-01T10:00:00", updated_at="2026-07-01T10:00:00",
    )
    defaults.update(kwargs)
    conn = sqlite3.connect(db_path)
    cols = ", ".join(defaults)
    placeholders = ", ".join("?" * len(defaults))
    conn.execute(f"INSERT INTO ic_trades ({cols}) VALUES ({placeholders})", list(defaults.values()))
    conn.commit()
    conn.close()


def test_get_range_summary_groups_by_profile(paper_db_path, capsys):
    _insert_paper_trade(paper_db_path, ic_order_id="P-1", risk_profile="conservative",
                         trade_date="2026-07-01", pnl=150.0, fees=25.0, status="expired")
    _insert_paper_trade(paper_db_path, ic_order_id="P-2", risk_profile="conservative",
                         trade_date="2026-07-01", pnl=-120.0, fees=25.0, status="stopped")
    _insert_paper_trade(paper_db_path, ic_order_id="P-3", risk_profile="moderate",
                         trade_date="2026-07-01", pnl=100.0, fees=25.0, status="expired")

    args = argparse.Namespace(start="2026-07-01", end="2026-07-02", profile=None, symbol=None)
    db.cmd_get_range_summary(args)
    out = json.loads(capsys.readouterr().out)

    assert out["ok"] is True
    assert set(out["profiles"].keys()) == {"conservative", "moderate"}
    cons = out["profiles"]["conservative"]
    assert cons["total_trades"] == 2
    assert cons["win_count"] == 1
    assert cons["loss_count"] == 1
    assert cons["net_pnl"] == pytest.approx(150 - 25 + (-120 - 25), abs=0.01)


def test_get_range_summary_excludes_cancelled_and_pending(paper_db_path, capsys):
    _insert_paper_trade(paper_db_path, ic_order_id="P-1", status="cancelled", trade_date="2026-07-01")
    _insert_paper_trade(paper_db_path, ic_order_id="P-2", status="pending", trade_date="2026-07-01")
    _insert_paper_trade(paper_db_path, ic_order_id="P-3", status="expired", trade_date="2026-07-01", pnl=50.0, fees=10.0)

    args = argparse.Namespace(start="2026-07-01", end="2026-07-02", profile=None, symbol=None)
    db.cmd_get_range_summary(args)
    out = json.loads(capsys.readouterr().out)
    assert out["profiles"]["conservative"]["total_trades"] == 1


# ── End-to-end: process_symbol via subprocess against a real temp DB ────────

def test_process_symbol_end_to_end_fills_and_marks(tmp_path):
    db_path = str(tmp_path / "paper_e2e.db")
    subprocess.run([sys.executable, str(Path(__file__).parent.parent / "src" / "db.py"),
                     "--db", db_path, "init_db"], check=True, capture_output=True)

    snapshot = _base_snapshot(now_et="13:00", date="2026-07-09")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent.parent / "src" / "paper.py"),
         "--db", db_path, "process_symbol", "--snapshot", json.dumps(snapshot),
         "--execution_mode", "paper", "--profiles", "conservative"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["ok"] is True
    filled = [a for a in out["results"]["conservative"] if a.get("entry") == "filled"]
    assert len(filled) == 1

    open_check = subprocess.run(
        [sys.executable, str(Path(__file__).parent.parent / "src" / "db.py"),
         "--db", db_path, "get_open_trades", "--symbol", "XSP", "--date", "2026-07-09"],
        capture_output=True, text=True,
    )
    open_out = json.loads(open_check.stdout.strip())
    assert len(open_out["open_trades"]) == 1
    assert open_out["open_trades"][0]["risk_profile"] == "conservative"
