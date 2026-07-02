"""
Tests for dashboard.py data layer.

No HTTP server, no browser, no MCP required — all tests operate on an
in-memory or temp SQLite database and call dashboard functions directly.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import os
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import dashboard


# ── Fixtures ──────────────────────────────────────────────────────────────────

DDL = """
CREATE TABLE IF NOT EXISTS ic_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL, entry_time TEXT, expiration TEXT,
    symbol TEXT NOT NULL, put_strike REAL, call_strike REAL, wing_width REAL,
    put_symbol TEXT, call_symbol TEXT, long_put_symbol TEXT, long_call_symbol TEXT,
    put_credit REAL, call_credit REAL, net_credit REAL, quantity INTEGER DEFAULT 1,
    put_delta_at_entry REAL, call_delta_at_entry REAL,
    long_put_delta_at_entry REAL, long_call_delta_at_entry REAL,
    underlying_price_entry REAL, iv_rank_at_entry REAL, iv_pct_at_entry REAL,
    session_quality TEXT, iv_skew_signal TEXT,
    price_action_signal TEXT, ai_entry_reasoning TEXT,
    ic_order_id TEXT UNIQUE NOT NULL,
    put_spread_entry_order_id TEXT, call_spread_entry_order_id TEXT,
    put_stop_order_id TEXT, call_stop_order_id TEXT,
    stop_trigger_original REAL, stop_limit_original REAL,
    stop_trigger_current REAL, stop_limit_current REAL,
    stop_adjustment_count INTEGER DEFAULT 0,
    stop_adjustment_history TEXT DEFAULT '[]',
    status TEXT DEFAULT 'pending',
    exit_time TEXT, exit_price REAL, exit_reason TEXT, exit_analysis TEXT,
    put_stop_cost REAL, call_stop_cost REAL,
    pnl REAL, fees REAL, fill_confirmed_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ic_spread_legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ic_order_id TEXT NOT NULL REFERENCES ic_trades(ic_order_id),
    side TEXT NOT NULL CHECK (side IN ('put', 'call')),
    status TEXT NOT NULL DEFAULT 'open',
    exit_time TEXT, exit_reason TEXT, exit_price REAL, pnl REAL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(ic_order_id, side)
);
CREATE TABLE IF NOT EXISTS daily_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary_date TEXT UNIQUE NOT NULL, symbol TEXT,
    total_entries INTEGER DEFAULT 0, entries_filled INTEGER DEFAULT 0,
    entries_stopped INTEGER DEFAULT 0, entries_expired INTEGER DEFAULT 0,
    entries_cancelled INTEGER DEFAULT 0,
    gross_credit REAL DEFAULT 0, gross_pnl REAL DEFAULT 0,
    fees REAL DEFAULT 0, net_pnl REAL DEFAULT 0,
    closing_nlv REAL, win_count INTEGER DEFAULT 0,
    win_rate_pct REAL, avg_iv_rank REAL,
    sessions_entered TEXT DEFAULT '[]', ai_day_summary TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loop_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loop_time TEXT NOT NULL, loop_date TEXT NOT NULL,
    action TEXT, reasoning TEXT,
    open_trades_n INTEGER DEFAULT 0, today_count INTEGER DEFAULT 0,
    today_pnl REAL DEFAULT 0, iv_rank REAL, underlying_price REAL,
    session_quality TEXT, mcp_errors TEXT DEFAULT '[]',
    duration_ms INTEGER, created_at TEXT NOT NULL
);
"""

_NOW = "2026-06-20 10:30:00"
_TODAY = "2026-06-20"
_YESTERDAY = "2026-06-19"


def _make_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    for stmt in DDL.split(";"):
        if stmt.strip():
            conn.execute(stmt)
    conn.commit()
    return conn


def _insert_trade(conn, **kwargs):
    defaults = dict(
        trade_date=_TODAY, entry_time=_NOW, symbol="SPX",
        put_strike=5400, call_strike=5600, wing_width=5,
        put_credit=0.55, call_credit=0.65, net_credit=1.20, quantity=1,
        iv_rank_at_entry=0.40, session_quality="prime",
        ic_order_id="IC-001", status="expired", pnl=1.20, fees=0.10,
        exit_time="2026-06-20 16:00:00", exit_reason="expired_eod",
        created_at=_NOW, updated_at=_NOW,
    )
    defaults.update(kwargs)
    cols = ", ".join(defaults)
    placeholders = ", ".join("?" * len(defaults))
    conn.execute(f"INSERT INTO ic_trades ({cols}) VALUES ({placeholders})",
                 list(defaults.values()))
    conn.commit()


def _insert_leg(conn, **kwargs):
    defaults = dict(
        ic_order_id="IC-001", side="put", status="open",
        exit_time=None, exit_reason=None, exit_price=None, pnl=None,
        created_at=_NOW, updated_at=_NOW,
    )
    defaults.update(kwargs)
    cols = ", ".join(defaults)
    placeholders = ", ".join("?" * len(defaults))
    conn.execute(f"INSERT INTO ic_spread_legs ({cols}) VALUES ({placeholders})",
                 list(defaults.values()))
    conn.commit()


def _insert_summary(conn, **kwargs):
    defaults = dict(
        summary_date=_YESTERDAY, symbol="SPX",
        entries_filled=3, win_count=2, net_pnl=2.50,
        closing_nlv=100500.0,
        created_at=_NOW, updated_at=_NOW,
    )
    defaults.update(kwargs)
    cols = ", ".join(defaults)
    placeholders = ", ".join("?" * len(defaults))
    conn.execute(f"INSERT INTO daily_summary ({cols}) VALUES ({placeholders})",
                 list(defaults.values()))
    conn.commit()


@pytest.fixture
def db_path(monkeypatch, tmp_path):
    """Temp DB with schema; monkeypatches dashboard._DB_PATH and the date helpers
    so stats windows are anchored to _TODAY regardless of the real wall-clock date
    the tests happen to run on."""
    path = str(tmp_path / "meic_trades.db")
    conn = _make_db(path)
    conn.close()
    monkeypatch.setattr(dashboard, "_DB_PATH", path)
    monkeypatch.setattr(dashboard, "_today", lambda: _TODAY)
    monkeypatch.setattr(dashboard, "_week_start", lambda: "2026-06-15")
    monkeypatch.setattr(dashboard, "_month_start", lambda: "2026-06-01")
    monkeypatch.setattr(dashboard, "_year_start", lambda: "2026-01-01")
    return path


# ── _wl_ratio ─────────────────────────────────────────────────────────────────

def test_wl_ratio_normal():
    assert dashboard._wl_ratio(3, 1) == 75.0

def test_wl_ratio_all_wins():
    assert dashboard._wl_ratio(5, 0) == 100.0

def test_wl_ratio_all_losses():
    assert dashboard._wl_ratio(0, 4) == 0.0

def test_wl_ratio_no_trades():
    assert dashboard._wl_ratio(0, 0) is None

def test_wl_ratio_none_inputs():
    assert dashboard._wl_ratio(None, None) is None


# ── _spread_statuses ──────────────────────────────────────────────────────────

def _trade(**kwargs):
    base = {"status": "open", "exit_time": None, "exit_analysis": None}
    base.update(kwargs)
    return base


def test_status_open():
    put_s, call_s = dashboard._spread_statuses(_trade(status="open"))
    assert put_s["type"] == "monitoring"
    assert call_s["type"] == "monitoring"

def test_status_expired():
    put_s, call_s = dashboard._spread_statuses(_trade(status="expired"))
    assert put_s["type"] == "expired"
    assert call_s["type"] == "expired"

def test_status_pending():
    put_s, call_s = dashboard._spread_statuses(_trade(status="pending"))
    assert put_s["type"] == "pending"
    assert call_s["type"] == "pending"

def test_status_partial_entry():
    put_s, call_s = dashboard._spread_statuses(_trade(status="partial_entry"))
    assert put_s["type"] == "pending"
    assert call_s["type"] == "pending"

def test_status_cancelled():
    put_s, call_s = dashboard._spread_statuses(_trade(status="cancelled"))
    assert put_s["type"] == "cancelled"
    assert call_s["type"] == "cancelled"

def test_status_force_closed():
    put_s, call_s = dashboard._spread_statuses(_trade(status="force_closed"))
    assert put_s["type"] == "force_closed"
    assert call_s["type"] == "force_closed"

def test_status_stopped_no_exit_analysis():
    put_s, call_s = dashboard._spread_statuses(
        _trade(status="stopped", exit_time="2026-06-20T11:21:00")
    )
    assert put_s["type"] == "stopped"
    assert call_s["type"] == "stopped"

def test_status_stopped_put_side():
    put_leg = {"status": "stopped", "exit_time": "2026-06-20T11:21:00", "pnl": -0.5}
    call_leg = {"status": "open", "exit_time": None, "pnl": None}
    put_s, call_s = dashboard._spread_statuses(
        _trade(status="partial"), put_leg, call_leg
    )
    assert put_s["type"] == "stopped"
    assert call_s["type"] == "monitoring"

def test_status_stopped_call_side():
    put_leg = {"status": "open", "exit_time": None, "pnl": None}
    call_leg = {"status": "stopped", "exit_time": "2026-06-20T14:05:00", "pnl": -0.5}
    put_s, call_s = dashboard._spread_statuses(
        _trade(status="partial"), put_leg, call_leg
    )
    assert put_s["type"] == "monitoring"
    assert call_s["type"] == "stopped"

def test_status_stopped_time_in_label():
    put_leg = {"status": "stopped", "exit_time": "2026-06-20T11:21:00", "pnl": -0.5}
    call_leg = {"status": "open", "exit_time": None, "pnl": None}
    put_s, _ = dashboard._spread_statuses(_trade(status="partial"), put_leg, call_leg)
    assert "11:21" in put_s["label"]


# ── _stats_for_period ────────────────────────────────────────────────────────
# Replaces the old _today_stats/_historical_stats/_merge trio: dashboard.py now
# computes stats for any date range (today, week, ..., all_time) directly from
# ic_trades + ic_spread_legs in one function, rather than pre-aggregating into
# daily_summary and merging today's live numbers on top.

def test_stats_for_period_empty(db_path):
    conn = dashboard._connect()
    result = dashboard._stats_for_period(conn, start=_TODAY, end=_TODAY)
    conn.close()
    assert result["net_pnl"] == 0.0
    assert result["total_trades"] == 0
    assert result["wl_ratio"] is None

def test_stats_for_period_with_trades(db_path):
    conn = dashboard._connect()
    _insert_trade(conn, ic_order_id="IC-001", pnl=1.20, status="expired")
    _insert_trade(conn, ic_order_id="IC-002", pnl=-0.80, status="stopped")
    _insert_trade(conn, ic_order_id="IC-003", pnl=None,  status="cancelled")
    result = dashboard._stats_for_period(conn, start=_TODAY, end=_TODAY)
    conn.close()
    assert result["total_trades"] == 2       # cancelled excluded
    # No ic_spread_legs rows for either trade, so each side of the IC is scored
    # from the whole-trade status: expired -> both sides win, stopped -> both lose.
    assert result["wins"] == 2
    assert result["losses"] == 2
    assert result["wl_ratio"] == 50.0
    assert abs(result["net_pnl"] - 0.40) < 0.01

def test_stats_for_period_excludes_pending(db_path):
    conn = dashboard._connect()
    _insert_trade(conn, ic_order_id="IC-001", pnl=None, status="pending")
    result = dashboard._stats_for_period(conn, start=_TODAY, end=_TODAY)
    conn.close()
    assert result["total_trades"] == 0

def test_stats_for_period_excludes_out_of_range(db_path):
    conn = dashboard._connect()
    _insert_trade(conn, ic_order_id="IC-001", trade_date=_YESTERDAY, pnl=5.00, status="expired")
    result = dashboard._stats_for_period(conn, start=_TODAY, end=_TODAY)
    conn.close()
    assert result["total_trades"] == 0

def test_stats_for_period_uses_leg_rows_when_present(db_path):
    """A per-side stop should count as a win on one side and a loss on the other,
    not a whole-trade win/loss guess, once ic_spread_legs rows exist."""
    conn = dashboard._connect()
    _insert_trade(conn, ic_order_id="IC-001", status="partial", pnl=0.30)
    _insert_leg(conn, ic_order_id="IC-001", side="put", status="stopped", pnl=-0.50)
    _insert_leg(conn, ic_order_id="IC-001", side="call", status="expired", pnl=0.80)
    result = dashboard._stats_for_period(conn, start=_TODAY, end=_TODAY)
    conn.close()
    assert result["wins"] == 1
    assert result["losses"] == 1


# ── _build_api_data ───────────────────────────────────────────────────────────

def test_build_api_data_no_db(monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard, "_DB_PATH", str(tmp_path / "nonexistent.db"))
    result = dashboard._build_api_data()
    assert result["ok"] is False
    assert "error" in result

def test_build_api_data_empty_db(db_path):
    result = dashboard._build_api_data()
    assert result["ok"] is True
    assert "stats" in result
    assert "trades" in result
    assert "nlv_series" in result
    assert "analytics" in result
    assert result["trades"] == []
    assert result["nlv_series"] == []

def test_build_api_data_stats_keys(db_path):
    result = dashboard._build_api_data()
    for period in ("today", "week", "month", "year", "all_time"):
        assert period in result["stats"]
        s = result["stats"][period]
        assert "net_pnl" in s
        assert "total_trades" in s
        assert "wins" in s
        assert "losses" in s
        assert "wl_ratio" in s

def test_build_api_data_with_trade(db_path):
    conn = dashboard._connect()
    _insert_trade(conn, ic_order_id="IC-001", pnl=1.20, status="expired")
    conn.close()
    result = dashboard._build_api_data()
    assert result["ok"] is True
    assert len(result["trades"]) == 1
    t = result["trades"][0]
    assert t["ic_order_id"] == "IC-001"
    assert "put_status" in t
    assert "call_status" in t

def test_build_api_data_trade_has_no_exit_analysis_key(db_path):
    """exit_analysis should be consumed internally, not exposed in trades list."""
    conn = dashboard._connect()
    ea = json.dumps({"stopped_spread": "put"})
    _insert_trade(conn, ic_order_id="IC-001", status="partial",
                  exit_analysis=ea, exit_time="2026-06-20T11:21:00", pnl=None)
    conn.close()
    result = dashboard._build_api_data()
    assert "exit_analysis" not in result["trades"][0]

def test_build_api_data_nlv_series(db_path):
    conn = dashboard._connect()
    _insert_summary(conn, summary_date="2026-06-18", closing_nlv=100200.0, net_pnl=1.00)
    _insert_summary(conn, summary_date="2026-06-19", closing_nlv=100350.0, net_pnl=1.50)
    conn.close()
    result = dashboard._build_api_data()
    assert len(result["nlv_series"]) == 2
    assert result["nlv_series"][0]["closing_nlv"] == 100200.0
    assert result["nlv_series"][1]["date"] == "2026-06-19"

def test_build_api_data_analytics_keys(db_path):
    result = dashboard._build_api_data()
    ana = result["analytics"]
    assert "by_session" in ana
    assert "by_exit" in ana
    assert "by_iv" in ana
    assert "fee_summary" in ana
    fs = ana["fee_summary"]
    assert "gross_credit" in fs
    assert "total_fees" in fs
    assert "net_pnl" in fs
    assert "fee_drag_pct" in fs

def test_build_api_data_fee_drag_none_when_no_trades(db_path):
    result = dashboard._build_api_data()
    assert result["analytics"]["fee_summary"]["fee_drag_pct"] is None

def test_build_api_data_today_merges_into_week(db_path):
    """Stats are computed directly from ic_trades over the period's date range, so a
    trade from earlier in the week and today's trade should both land in 'week'."""
    conn = dashboard._connect()
    _insert_trade(conn, ic_order_id="IC-001", trade_date=_TODAY, pnl=2.00, status="expired")
    _insert_trade(conn, ic_order_id="IC-002", trade_date="2026-06-17", pnl=1.00, status="expired")
    conn.close()
    result = dashboard._build_api_data()
    assert abs(result["stats"]["week"]["net_pnl"] - 3.00) < 0.01
    assert result["stats"]["week"]["total_trades"] == 2
    assert abs(result["stats"]["today"]["net_pnl"] - 2.00) < 0.01
    assert result["stats"]["today"]["total_trades"] == 1




# ── _load_symbols / _load_symbol ─────────────────────────────────────────────

def _write_config(monkeypatch, tmp_path, cfg):
    path = str(tmp_path / "config.json")
    with open(path, "w") as f:
        json.dump(cfg, f)
    monkeypatch.setattr(dashboard, "_CONFIG_PATH", path)


def test_load_symbols_list(monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, {"symbols": ["xsp", "spx"]})
    assert dashboard._load_symbols() == ["XSP", "SPX"]
    assert dashboard._load_symbol() == "XSP"

def test_load_symbols_deprecated_singular_alias(monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, {"symbol": "xsp"})
    assert dashboard._load_symbols() == ["XSP"]

def test_load_symbols_default_when_missing(monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, {})
    assert dashboard._load_symbols() == ["XSP"]

def test_load_symbols_missing_config_file(monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard, "_CONFIG_PATH", str(tmp_path / "nonexistent.json"))
    assert dashboard._load_symbols() == ["XSP"]


# ── _build_api_data symbol filtering ─────────────────────────────────────────

def test_build_api_data_reports_configured_symbols(db_path, monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, {"symbols": ["XSP", "SPX"]})
    result = dashboard._build_api_data()
    assert result["symbols"] == ["XSP", "SPX"]
    assert result["selected_symbol"] == "ALL"

def test_build_api_data_no_filter_includes_all_symbols(db_path):
    conn = dashboard._connect()
    _insert_trade(conn, ic_order_id="IC-X", symbol="XSP", pnl=1.00, status="expired")
    _insert_trade(conn, ic_order_id="IC-S", symbol="SPX", pnl=3.00, status="expired")
    conn.close()
    result = dashboard._build_api_data()
    assert len(result["trades"]) == 2
    assert abs(result["stats"]["today"]["net_pnl"] - 4.00) < 0.01

def test_build_api_data_filtered_by_symbol(db_path):
    conn = dashboard._connect()
    _insert_trade(conn, ic_order_id="IC-X", symbol="XSP", pnl=1.00, status="expired")
    _insert_trade(conn, ic_order_id="IC-S", symbol="SPX", pnl=3.00, status="expired")
    conn.close()
    result = dashboard._build_api_data("XSP")
    assert result["selected_symbol"] == "XSP"
    assert len(result["trades"]) == 1
    assert result["trades"][0]["symbol"] == "XSP"
    assert abs(result["stats"]["today"]["net_pnl"] - 1.00) < 0.01

def test_build_api_data_symbol_filter_case_insensitive(db_path):
    conn = dashboard._connect()
    _insert_trade(conn, ic_order_id="IC-X", symbol="XSP", pnl=1.00, status="expired")
    conn.close()
    result = dashboard._build_api_data("xsp")
    assert len(result["trades"]) == 1

def test_build_api_data_all_keyword_is_unfiltered(db_path):
    conn = dashboard._connect()
    _insert_trade(conn, ic_order_id="IC-X", symbol="XSP", pnl=1.00, status="expired")
    _insert_trade(conn, ic_order_id="IC-S", symbol="SPX", pnl=3.00, status="expired")
    conn.close()
    result = dashboard._build_api_data("ALL")
    assert len(result["trades"]) == 2

def test_build_api_data_symbol_filter_applies_to_recent_trades(db_path):
    conn = dashboard._connect()
    _insert_trade(conn, ic_order_id="IC-X", symbol="XSP", pnl=1.00, status="expired")
    _insert_trade(conn, ic_order_id="IC-S", symbol="SPX", pnl=3.00, status="expired")
    conn.close()
    result = dashboard._build_api_data("SPX")
    recent = result["analytics"]["recent_trades"]
    assert len(recent) == 1
    assert recent[0]["symbol"] == "SPX"

def test_build_api_data_symbol_filter_applies_to_fee_summary(db_path):
    conn = dashboard._connect()
    _insert_trade(conn, ic_order_id="IC-X", symbol="XSP", net_credit=0.50, fees=0.10, pnl=1.00, status="expired")
    _insert_trade(conn, ic_order_id="IC-S", symbol="SPX", net_credit=1.50, fees=0.30, pnl=3.00, status="expired")
    conn.close()
    xsp_result = dashboard._build_api_data("XSP")
    spx_result = dashboard._build_api_data("SPX")
    assert xsp_result["analytics"]["fee_summary"]["gross_credit"] == 0.50
    assert spx_result["analytics"]["fee_summary"]["gross_credit"] == 1.50
