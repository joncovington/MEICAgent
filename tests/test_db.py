"""Unit tests for db.py's multi-symbol support: loop_log.symbol column (with
migration from pre-multi-symbol databases), and --symbol filters on the
account-wide read commands (get_open_trades, get_today_count, get_today_pnl).

No credentials or live connection required — all tests operate on a temp
SQLite database file.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import db


@pytest.fixture
def db_path(monkeypatch, tmp_path):
    path = str(tmp_path / "meic_trades.db")
    monkeypatch.setattr(db, "_DB_PATH", path)
    monkeypatch.setattr(db, "_today_et", lambda: "2026-07-02")
    db.cmd_init_db(None)
    return path


def _insert_trade(db_path, **kwargs):
    defaults = dict(
        trade_date="2026-07-02", entry_time="2026-07-02T10:00:00", symbol="XSP",
        put_strike=590, call_strike=600, wing_width=2, net_credit=0.5, quantity=1,
        status="expired", pnl=1.0, fees=0.2, ic_order_id="IC-1",
        created_at="2026-07-02T10:00:00", updated_at="2026-07-02T10:00:00",
    )
    defaults.update(kwargs)
    conn = sqlite3.connect(db_path)
    cols = ", ".join(defaults)
    placeholders = ", ".join("?" * len(defaults))
    conn.execute(f"INSERT INTO ic_trades ({cols}) VALUES ({placeholders})", list(defaults.values()))
    conn.commit()
    conn.close()


# ── loop_log.symbol ─────────────────────────────────────────────────────────

def test_init_db_creates_loop_log_symbol_column(db_path):
    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(loop_log)")}
    conn.close()
    assert "symbol" in cols


def test_init_db_creates_loop_log_symbol_index(db_path):
    conn = sqlite3.connect(db_path)
    idx = {row[1] for row in conn.execute("PRAGMA index_list(loop_log)")}
    conn.close()
    assert "idx_loop_log_symbol_date" in idx


def test_init_db_migrates_preexisting_loop_log_without_symbol(monkeypatch, tmp_path):
    """A database created before multi-symbol support has no `symbol` column on
    loop_log; init_db must add it (and the index) without erroring."""
    path = str(tmp_path / "meic_trades.db")
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE loop_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loop_time TEXT NOT NULL, loop_date TEXT NOT NULL,
            action TEXT, reasoning TEXT,
            open_trades_n INTEGER DEFAULT 0, today_count INTEGER DEFAULT 0, today_pnl REAL DEFAULT 0,
            iv_rank REAL, underlying_price REAL, session_quality TEXT,
            mcp_errors TEXT DEFAULT '[]', duration_ms INTEGER, created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "_DB_PATH", path)
    db.cmd_init_db(None)

    conn = sqlite3.connect(path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(loop_log)")}
    idx = {row[1] for row in conn.execute("PRAGMA index_list(loop_log)")}
    conn.close()
    assert "symbol" in cols
    assert "idx_loop_log_symbol_date" in idx


def test_log_loop_action_stores_symbol(db_path):
    args = argparse.Namespace(
        symbol="XSP", action="entry", reasoning="test", market_context="{}",
        iv_rank=0.4, session_quality="prime", underlying_price=600.0,
        open_trades=1, today_count=1, today_pnl=0.5, duration_ms=None,
    )
    db.cmd_log_loop_action(args)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT symbol, action FROM loop_log ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    assert row["symbol"] == "XSP"
    assert row["action"] == "entry"


def test_log_loop_action_symbol_none_for_iteration_summary(db_path):
    """An iteration-level summary row (not tied to one symbol) stores symbol=NULL."""
    args = argparse.Namespace(
        symbol=None, action="iteration_summary", reasoning="", market_context="{}",
        iv_rank=None, session_quality=None, underlying_price=None,
        open_trades=None, today_count=None, today_pnl=None, duration_ms=None,
    )
    db.cmd_log_loop_action(args)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT symbol FROM loop_log ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    assert row["symbol"] is None


def _log_action(symbol, action, duration_ms):
    db.cmd_log_loop_action(argparse.Namespace(
        symbol=symbol, action=action, reasoning="", market_context="{}",
        iv_rank=None, session_quality=None, underlying_price=None,
        open_trades=None, today_count=None, today_pnl=None, duration_ms=duration_ms,
    ))


def test_log_loop_action_stores_duration_ms(db_path):
    _log_action(None, "timing_stop_management", 842)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT duration_ms FROM loop_log ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    assert row["duration_ms"] == 842


def test_get_step_timing_summarizes_by_action(db_path, capsys):
    _log_action(None, "timing_stop_management", 800)
    _log_action(None, "timing_stop_management", 1200)
    _log_action("XSP", "timing_entry_evaluation", 3000)

    db.cmd_get_step_timing(argparse.Namespace(action=None, symbol=None, lookback_days=None))
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert out["ok"] is True
    assert out["by_action"]["timing_stop_management"]["sample_size"] == 2
    assert out["by_action"]["timing_stop_management"]["avg_ms"] == 1000.0
    assert out["by_action"]["timing_entry_evaluation"]["sample_size"] == 1


def test_get_step_timing_filters_by_action(db_path, capsys):
    _log_action(None, "timing_stop_management", 500)
    _log_action("SPX", "timing_entry_evaluation", 2500)

    db.cmd_get_step_timing(argparse.Namespace(action="timing_entry_evaluation", symbol=None, lookback_days=None))
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert list(out["by_action"].keys()) == ["timing_entry_evaluation"]


# ── --symbol filters on account-wide read commands ──────────────────────────

def test_get_open_trades_no_filter_returns_all_symbols(db_path, capsys):
    _insert_trade(db_path, ic_order_id="IC-X", symbol="XSP", status="open")
    _insert_trade(db_path, ic_order_id="IC-S", symbol="SPX", status="open")
    db.cmd_get_open_trades(argparse.Namespace(symbol=None))
    out = json.loads(capsys.readouterr().out)
    assert len(out["open_trades"]) == 2


def test_get_open_trades_filtered_by_symbol(db_path, capsys):
    _insert_trade(db_path, ic_order_id="IC-X", symbol="XSP", status="open")
    _insert_trade(db_path, ic_order_id="IC-S", symbol="SPX", status="open")
    db.cmd_get_open_trades(argparse.Namespace(symbol="XSP"))
    out = json.loads(capsys.readouterr().out)
    assert len(out["open_trades"]) == 1
    assert out["open_trades"][0]["symbol"] == "XSP"


def test_get_open_trades_filter_is_case_insensitive(db_path, capsys):
    _insert_trade(db_path, ic_order_id="IC-X", symbol="XSP", status="open")
    db.cmd_get_open_trades(argparse.Namespace(symbol="xsp"))
    out = json.loads(capsys.readouterr().out)
    assert len(out["open_trades"]) == 1


def test_get_today_count_no_filter_counts_all_symbols(db_path, capsys):
    _insert_trade(db_path, ic_order_id="IC-X", symbol="XSP")
    _insert_trade(db_path, ic_order_id="IC-S", symbol="SPX")
    db.cmd_get_today_count(argparse.Namespace(symbol=None))
    out = json.loads(capsys.readouterr().out)
    assert out["today_count"] == 2


def test_get_today_count_filtered_by_symbol(db_path, capsys):
    _insert_trade(db_path, ic_order_id="IC-X", symbol="XSP")
    _insert_trade(db_path, ic_order_id="IC-S", symbol="SPX")
    db.cmd_get_today_count(argparse.Namespace(symbol="SPX"))
    out = json.loads(capsys.readouterr().out)
    assert out["today_count"] == 1


def test_get_today_pnl_no_filter_sums_all_symbols(db_path, capsys):
    _insert_trade(db_path, ic_order_id="IC-X", symbol="XSP", pnl=1.0)
    _insert_trade(db_path, ic_order_id="IC-S", symbol="SPX", pnl=3.0)
    db.cmd_get_today_pnl(argparse.Namespace(symbol=None))
    out = json.loads(capsys.readouterr().out)
    assert out["today_pnl"] == 4.0


def test_get_today_pnl_filtered_by_symbol(db_path, capsys):
    _insert_trade(db_path, ic_order_id="IC-X", symbol="XSP", pnl=1.0)
    _insert_trade(db_path, ic_order_id="IC-S", symbol="SPX", pnl=3.0)
    db.cmd_get_today_pnl(argparse.Namespace(symbol="XSP"))
    out = json.loads(capsys.readouterr().out)
    assert out["today_pnl"] == 1.0


def test_get_eod_summary_spans_all_symbols(db_path, capsys):
    """EOD summary is intentionally account-wide (one combined report per day covering
    every symbol) — see CLAUDE.md Step 8 and eod-report.md."""
    _insert_trade(db_path, ic_order_id="IC-X", symbol="XSP", pnl=1.0, fees=0.1)
    _insert_trade(db_path, ic_order_id="IC-S", symbol="SPX", pnl=3.0, fees=0.3)
    db.cmd_get_eod_summary(None)
    out = json.loads(capsys.readouterr().out)
    assert out["total_entries"] == 2
    assert abs(out["net_pnl"] - 3.6) < 0.01
