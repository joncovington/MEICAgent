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

# Allow importing dashboard from the project root
sys.path.insert(0, str(Path(__file__).parent.parent))
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
    is_paper INTEGER DEFAULT 0,
    paper_entry_slippage REAL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
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
CREATE TABLE IF NOT EXISTS paper_marks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ic_order_id TEXT NOT NULL,
    mark_time TEXT NOT NULL,
    mark_date TEXT NOT NULL,
    put_spread_value REAL,
    call_spread_value REAL,
    total_spread_value REAL,
    unrealized_pnl REAL,
    notes TEXT,
    created_at TEXT NOT NULL
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
    """Temp DB with schema; monkeypatches dashboard._DB_PATH and _today."""
    path = str(tmp_path / "meic_trades.db")
    conn = _make_db(path)
    conn.close()
    monkeypatch.setattr(dashboard, "_DB_PATH", path)
    monkeypatch.setattr(dashboard, "_today", lambda: _TODAY)
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
    ea = json.dumps({"stopped_spread": "put"})
    put_s, call_s = dashboard._spread_statuses(
        _trade(status="partial", exit_time="2026-06-20T11:21:00", exit_analysis=ea)
    )
    assert put_s["type"] == "stopped"
    assert call_s["type"] == "expired"

def test_status_stopped_call_side():
    ea = json.dumps({"stopped_spread": "call"})
    put_s, call_s = dashboard._spread_statuses(
        _trade(status="partial", exit_time="2026-06-20T14:05:00", exit_analysis=ea)
    )
    assert put_s["type"] == "expired"
    assert call_s["type"] == "stopped"

def test_status_stopped_time_in_label():
    ea = json.dumps({"stopped_spread": "put"})
    put_s, _ = dashboard._spread_statuses(
        _trade(status="partial", exit_time="2026-06-20T11:21:00", exit_analysis=ea)
    )
    assert "11:21" in put_s["label"]

def test_status_stopped_invalid_exit_analysis():
    put_s, call_s = dashboard._spread_statuses(
        _trade(status="stopped", exit_time="2026-06-20T10:00:00",
               exit_analysis="not valid json{{{")
    )
    assert put_s["type"] == "stopped"
    assert call_s["type"] == "stopped"


# ── _today_stats ──────────────────────────────────────────────────────────────

def test_today_stats_empty(db_path):
    conn = dashboard._connect()
    result = dashboard._today_stats(conn, _TODAY)
    conn.close()
    assert result["net_pnl"] == 0.0
    assert result["total_trades"] == 0
    assert result["wl_ratio"] is None

def test_today_stats_with_trades(db_path):
    conn = dashboard._connect()
    _insert_trade(conn, ic_order_id="IC-001", pnl=1.20, status="expired")
    _insert_trade(conn, ic_order_id="IC-002", pnl=-0.80, status="stopped")
    _insert_trade(conn, ic_order_id="IC-003", pnl=None,  status="cancelled")
    result = dashboard._today_stats(conn, _TODAY)
    conn.close()
    assert result["total_trades"] == 2       # cancelled excluded
    assert result["wins"] == 1
    assert result["losses"] == 1
    assert result["wl_ratio"] == 50.0
    assert abs(result["net_pnl"] - 0.40) < 0.01

def test_today_stats_excludes_pending(db_path):
    conn = dashboard._connect()
    _insert_trade(conn, ic_order_id="IC-001", pnl=None, status="pending")
    result = dashboard._today_stats(conn, _TODAY)
    conn.close()
    assert result["total_trades"] == 0


# ── _historical_stats ─────────────────────────────────────────────────────────

def test_historical_stats_empty(db_path):
    conn = dashboard._connect()
    result = dashboard._historical_stats(conn, "2026-06-01", _TODAY)
    conn.close()
    assert result["net_pnl"] == 0.0
    assert result["total_trades"] == 0

def test_historical_stats_sums_daily_summary(db_path):
    conn = dashboard._connect()
    _insert_summary(conn, summary_date="2026-06-18", entries_filled=2, win_count=2, net_pnl=1.50)
    _insert_summary(conn, summary_date="2026-06-19", entries_filled=3, win_count=1, net_pnl=0.80)
    result = dashboard._historical_stats(conn, "2026-06-01", _TODAY)
    conn.close()
    assert result["total_trades"] == 5
    assert result["wins"] == 3
    assert abs(result["net_pnl"] - 2.30) < 0.01

def test_historical_stats_excludes_today(db_path):
    conn = dashboard._connect()
    _insert_summary(conn, summary_date=_TODAY, entries_filled=2, win_count=2, net_pnl=5.00)
    result = dashboard._historical_stats(conn, "2026-06-01", _TODAY)
    conn.close()
    assert result["total_trades"] == 0


# ── _merge ────────────────────────────────────────────────────────────────────

def test_merge_combines_pnl():
    hist = {"net_pnl": 10.0, "total_trades": 5, "wins": 4, "losses": 1}
    today = {"net_pnl": 1.5,  "total_trades": 2, "wins": 1, "losses": 1, "wl_ratio": 50.0}
    result = dashboard._merge(hist, today)
    assert abs(result["net_pnl"] - 11.5) < 0.01
    assert result["total_trades"] == 7
    assert result["wins"] == 5
    assert result["losses"] == 2

def test_merge_wl_ratio_recalculated():
    hist  = {"net_pnl": 0, "total_trades": 3, "wins": 3, "losses": 0}
    today = {"net_pnl": 0, "total_trades": 1, "wins": 0, "losses": 1, "wl_ratio": 0.0}
    result = dashboard._merge(hist, today)
    assert result["wl_ratio"] == 75.0

def test_merge_no_trades_wl_ratio_none():
    hist  = {"net_pnl": 0, "total_trades": 0, "wins": 0, "losses": 0}
    today = {"net_pnl": 0, "total_trades": 0, "wins": 0, "losses": 0, "wl_ratio": None}
    result = dashboard._merge(hist, today)
    assert result["wl_ratio"] is None


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
    conn = dashboard._connect()
    _insert_trade(conn, ic_order_id="IC-001", pnl=2.00, status="expired")
    _insert_summary(conn, summary_date="2026-06-17", entries_filled=1,
                    win_count=1, net_pnl=1.00)
    conn.close()
    result = dashboard._build_api_data()
    # week should include both yesterday's summary and today's live trade
    assert result["stats"]["week"]["net_pnl"] >= 2.00
    assert result["stats"]["week"]["total_trades"] >= 1


# ── paper trade isolation ─────────────────────────────────────────────────────

def test_paper_trades_excluded_from_live_stats(db_path):
    """Paper trades (is_paper=1) must not appear in live today stats."""
    conn = dashboard._connect()
    _insert_trade(conn, ic_order_id="IC-LIVE", pnl=1.20, status="expired")
    _insert_trade(conn, ic_order_id="IC-PAPER", pnl=2.00, status="expired",
                  is_paper=1)
    conn.close()
    result = dashboard._build_api_data()
    assert result["stats"]["today"]["net_pnl"] == 1.20
    assert result["stats"]["today"]["total_trades"] == 1

def test_paper_trades_excluded_from_live_trades_list(db_path):
    """Paper trades must not appear in the live trades table."""
    conn = dashboard._connect()
    _insert_trade(conn, ic_order_id="IC-LIVE",  pnl=1.20, status="expired")
    _insert_trade(conn, ic_order_id="IC-PAPER", pnl=2.00, status="expired",
                  is_paper=1)
    conn.close()
    result = dashboard._build_api_data()
    ids = [t["ic_order_id"] for t in result["trades"]]
    assert "IC-LIVE"  in ids
    assert "IC-PAPER" not in ids


# ── _build_paper_data ─────────────────────────────────────────────────────────

def test_build_paper_data_no_db(monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard, "_DB_PATH", str(tmp_path / "nonexistent.db"))
    result = dashboard._build_paper_data()
    assert result["ok"] is False

def test_build_paper_data_empty(db_path):
    result = dashboard._build_paper_data()
    assert result["ok"] is True
    assert result["paper_trades"] == []
    assert result["mark_series"] == []
    assert result["stats"]["today"]["net_pnl"] == 0.0
    assert result["stats"]["today"]["total_trades"] == 0

def test_build_paper_data_stats_keys(db_path):
    result = dashboard._build_paper_data()
    for period in ("today", "all_time"):
        s = result["stats"][period]
        for key in ("net_pnl", "total_trades", "wins", "losses", "wl_ratio"):
            assert key in s, f"Missing key '{key}' in paper stats.{period}"

def test_build_paper_data_with_paper_trade(db_path):
    conn = dashboard._connect()
    _insert_trade(conn, ic_order_id="PAPER-001", pnl=1.50, status="expired",
                  is_paper=1, paper_entry_slippage=0.02)
    conn.close()
    result = dashboard._build_paper_data()
    assert result["stats"]["today"]["net_pnl"] == 1.50
    assert result["stats"]["today"]["total_trades"] == 1
    assert result["stats"]["today"]["wins"] == 1
    assert len(result["paper_trades"]) == 1
    t = result["paper_trades"][0]
    assert t["ic_order_id"] == "PAPER-001"
    assert "put_status" in t
    assert "call_status" in t

def test_build_paper_data_excludes_live_trades(db_path):
    """Live trades (is_paper=0) must not appear in paper stats."""
    conn = dashboard._connect()
    _insert_trade(conn, ic_order_id="IC-LIVE",  pnl=5.00, status="expired",
                  is_paper=0)
    _insert_trade(conn, ic_order_id="IC-PAPER", pnl=1.50, status="expired",
                  is_paper=1)
    conn.close()
    result = dashboard._build_paper_data()
    assert result["stats"]["today"]["net_pnl"] == 1.50
    assert result["stats"]["today"]["total_trades"] == 1
    ids = [t["ic_order_id"] for t in result["paper_trades"]]
    assert "IC-PAPER" in ids
    assert "IC-LIVE"  not in ids

def test_build_paper_data_alltime_spans_days(db_path):
    conn = dashboard._connect()
    _insert_trade(conn, ic_order_id="PAPER-001", trade_date="2026-06-19",
                  pnl=1.00, status="expired", is_paper=1)
    _insert_trade(conn, ic_order_id="PAPER-002", trade_date=_TODAY,
                  pnl=2.00, status="expired", is_paper=1)
    conn.close()
    result = dashboard._build_paper_data()
    assert result["stats"]["all_time"]["total_trades"] == 2
    assert abs(result["stats"]["all_time"]["net_pnl"] - 3.00) < 0.01

def test_build_paper_data_mark_series_empty(db_path):
    result = dashboard._build_paper_data()
    assert result["mark_series"] == []

def test_build_paper_data_mark_series_aggregated(db_path):
    conn = dashboard._connect()
    _insert_trade(conn, ic_order_id="PAPER-001", status="open",
                  net_credit=1.20, pnl=None, is_paper=1)
    _insert_trade(conn, ic_order_id="PAPER-002", status="open",
                  net_credit=1.00, pnl=None, is_paper=1)
    # Two marks at the same time → should be summed
    mark_time = _TODAY + " 10:30:00"
    conn.execute("""INSERT INTO paper_marks
        (ic_order_id, mark_time, mark_date, put_spread_value, call_spread_value,
         total_spread_value, unrealized_pnl, created_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        ("PAPER-001", mark_time, _TODAY, 0.30, 0.25, 0.55, 65.0, _NOW))
    conn.execute("""INSERT INTO paper_marks
        (ic_order_id, mark_time, mark_date, put_spread_value, call_spread_value,
         total_spread_value, unrealized_pnl, created_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        ("PAPER-002", mark_time, _TODAY, 0.20, 0.15, 0.35, 45.0, _NOW))
    conn.commit()
    conn.close()
    result = dashboard._build_paper_data()
    assert len(result["mark_series"]) == 1
    assert abs(result["mark_series"][0]["total_unrealized"] - 110.0) < 0.01
