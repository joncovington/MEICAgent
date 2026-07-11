"""Unit tests for tt.py's pure helpers, stream-cache readers (against a tmp
sqlite file matching streamer.py's schema), and mocked async commands.

Nothing here touches a real tastytrade session, DXLink connection, or the
real data/stream_cache.db -- _CACHE_DB is monkeypatched per-test to a tmp_path
file, and Account/Session objects are hand-built fakes.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import tt

_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS stream_chain (
    streamer_symbol   TEXT PRIMARY KEY,
    expiration        TEXT NOT NULL,
    underlying_symbol TEXT,
    data_json         TEXT NOT NULL,
    updated_at        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stream_quotes (
    symbol TEXT PRIMARY KEY, bid REAL, ask REAL, mid REAL,
    bid_size REAL, ask_size REAL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stream_greeks (
    symbol TEXT PRIMARY KEY, delta REAL, gamma REAL, theta REAL,
    vega REAL, rho REAL, iv REAL, price REAL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stream_trades (
    symbol TEXT PRIMARY KEY, last REAL, change REAL, volume REAL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stream_oi (
    symbol TEXT PRIMARY KEY, open_interest INTEGER, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stream_status (
    id INTEGER PRIMARY KEY CHECK (id = 1), pid INTEGER, connected_since TEXT,
    last_event_at TEXT, subscribed_symbols INTEGER DEFAULT 0, reconnect_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orb_ranges (
    symbol TEXT NOT NULL, trade_date TEXT NOT NULL, orb_high REAL, orb_low REAL,
    captured_at REAL, PRIMARY KEY (symbol, trade_date)
);
"""


@pytest.fixture
def cache_db(tmp_path, monkeypatch):
    db_path = tmp_path / "stream_cache.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_CACHE_DDL)
    conn.commit()
    conn.close()
    monkeypatch.setattr(tt, "_CACHE_DB", db_path)
    return db_path


def _insert(db_path, table, **cols):
    conn = sqlite3.connect(str(db_path))
    keys = ", ".join(cols)
    placeholders = ", ".join("?" * len(cols))
    conn.execute(f"INSERT INTO {table} ({keys}) VALUES ({placeholders})", list(cols.values()))
    conn.commit()
    conn.close()


# --- _num / _serialize / _error ---------------------------------------------

def test_num_converts_valid_values():
    assert tt._num("1.5") == 1.5
    assert tt._num(3) == 3.0


def test_num_none_and_invalid_return_none():
    assert tt._num(None) is None
    assert tt._num("not-a-number") is None


def test_serialize_primitives_passthrough():
    assert tt._serialize(None) is None
    assert tt._serialize("x") == "x"
    assert tt._serialize(5) == 5


def test_serialize_list_and_dict_recurse():
    assert tt._serialize([1, "a", None]) == [1, "a", None]
    assert tt._serialize({"k": [1, 2]}) == {"k": [1, 2]}


def test_serialize_model_dump_object():
    class _Model:
        def model_dump(self, mode="json"):
            return {"a": 1}
    assert tt._serialize(_Model()) == {"a": 1}


def test_serialize_falls_back_to_str():
    class _Plain:
        def __str__(self):
            return "plain-repr"
    assert tt._serialize(_Plain()) == "plain-repr"


def test_error_flags_retryable_for_5xx():
    result = tt._error(RuntimeError("upstream returned 503 "))
    assert result["ok"] is False
    assert result.get("retryable") is True


def test_error_not_retryable_for_generic_exception():
    result = tt._error(ValueError("bad input"))
    assert "retryable" not in result


# --- _strike / _atm_window / _nearest_expiration ----------------------------

def test_strike_converts_and_handles_bad_values():
    class _Opt:
        strike_price = "150.5"
    assert tt._strike(_Opt()) == 150.5

    class _BadOpt:
        strike_price = None
    assert tt._strike(_BadOpt()) is None


def test_atm_window_centers_on_around_price():
    class _Opt:
        def __init__(self, strike):
            self.strike_price = strike
    options = [_Opt(s) for s in (90, 95, 100, 105, 110)]
    result = tt._atm_window(options, strike_count=1, around_price=100)
    strikes = sorted(o.strike_price for o in result)
    assert strikes == [95, 100, 105]


def test_atm_window_empty_options_returns_input():
    assert tt._atm_window([], strike_count=5, around_price=100) == []


def test_nearest_expiration_picks_closest_to_target_days():
    today = date.today()
    expirations = [today + timedelta(days=d) for d in (1, 5, 30)]
    assert tt._nearest_expiration(expirations, target_days=0) == expirations[0]


# --- _live_trading_enabled ----------------------------------------------------

def test_live_trading_enabled_reads_config(monkeypatch):
    monkeypatch.setattr(tt, "_load_config", lambda: {"enable_live_trading": True})
    assert tt._live_trading_enabled() is True
    monkeypatch.setattr(tt, "_load_config", lambda: {"enable_live_trading": False})
    assert tt._live_trading_enabled() is False


def test_live_trading_enabled_falls_back_to_env(monkeypatch):
    monkeypatch.setattr(tt, "_load_config", lambda: {})
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    assert tt._live_trading_enabled() is True
    monkeypatch.delenv("ENABLE_LIVE_TRADING", raising=False)
    assert tt._live_trading_enabled() is False


# --- cache readers -------------------------------------------------------------

def test_cache_get_trade_fresh(cache_db):
    _insert(cache_db, "stream_trades", symbol="XSP", last=601.5, change=1.0, volume=100, updated_at=time.time())
    assert tt._cache_get_trade("XSP") == 601.5


def test_cache_get_trade_stale_returns_none(cache_db):
    _insert(cache_db, "stream_trades", symbol="XSP", last=601.5, change=1.0, volume=100, updated_at=time.time() - 3600)
    assert tt._cache_get_trade("XSP") is None


def test_cache_get_trade_missing_symbol_returns_none(cache_db):
    assert tt._cache_get_trade("NOPE") is None


def test_cache_get_trade_no_db_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(tt, "_CACHE_DB", tmp_path / "does_not_exist.db")
    assert tt._cache_get_trade("XSP") is None


def test_cache_get_quotes_filters_stale(cache_db):
    _insert(cache_db, "stream_quotes", symbol="XSP", bid=1.0, ask=1.2, mid=1.1, bid_size=1, ask_size=1, updated_at=time.time())
    _insert(cache_db, "stream_quotes", symbol="SPX", bid=2.0, ask=2.2, mid=2.1, bid_size=1, ask_size=1, updated_at=time.time() - 3600)
    result = tt._cache_get_quotes(["XSP", "SPX"])
    assert list(result.keys()) == ["XSP"]
    assert result["XSP"] == {"bid": 1.0, "ask": 1.2, "mid": 1.1}


def test_cache_get_quotes_any_age_ignores_staleness(cache_db):
    _insert(cache_db, "stream_quotes", symbol="SPX", bid=2.0, ask=2.2, mid=2.1, bid_size=1, ask_size=1, updated_at=time.time() - 999999)
    result = tt._cache_get_quotes_any_age(["SPX"])
    assert result["SPX"]["mid"] == 2.1


def test_cache_get_oi_no_age_filter(cache_db):
    _insert(cache_db, "stream_oi", symbol="XSP", open_interest=500, updated_at=time.time() - 999999)
    assert tt._cache_get_oi(["XSP"]) == {"XSP": 500}


def test_cache_get_greeks_filters_by_greeks_cache_age(cache_db):
    _insert(cache_db, "stream_greeks", symbol="XSP", delta=0.3, gamma=0.01, theta=-0.02,
            vega=0.1, rho=0.0, iv=0.2, price=1.5, updated_at=time.time())
    result = tt._cache_get_greeks(["XSP"])
    assert result["XSP"]["delta"] == 0.3


def test_cache_get_chain_filters_by_underlying_symbol(cache_db):
    payload = json.dumps({"strike_price": "600", "option_type": "C", "streamer_symbol": ".XSP1"})
    _insert(cache_db, "stream_chain", streamer_symbol=".XSP1", expiration="2026-07-08",
            underlying_symbol="XSP", data_json=payload, updated_at=time.time())
    result = tt._cache_get_chain("2026-07-08", symbol="XSP")
    assert len(result) == 1
    assert result[0].strike_price == "600"


def test_cache_get_chain_none_when_stale(cache_db):
    payload = json.dumps({"strike_price": "600"})
    _insert(cache_db, "stream_chain", streamer_symbol=".XSP1", expiration="2026-07-08",
            underlying_symbol="XSP", data_json=payload, updated_at=time.time() - 5 * 3600)
    assert tt._cache_get_chain("2026-07-08", symbol="XSP") is None


def test_cache_get_chain_none_when_missing(cache_db):
    assert tt._cache_get_chain("2026-07-08", symbol="XSP") is None


# --- _is_call / _is_put / _closest_by_delta / _nearest_by_strike ------------

class _Opt:
    def __init__(self, option_type, strike_price, streamer_symbol=None):
        self.option_type = option_type
        self.strike_price = strike_price
        self.streamer_symbol = streamer_symbol


def test_is_call_and_is_put():
    assert tt._is_call(_Opt("C", "100")) is True
    assert tt._is_put(_Opt("C", "100")) is False
    assert tt._is_put(_Opt("P", "100")) is True
    assert tt._is_call(_Opt("P", "100")) is False


def test_closest_by_delta_picks_nearest_match():
    options = [_Opt("C", "100", "S1"), _Opt("C", "105", "S2")]
    greeks = {"S1": {"delta": 0.50}, "S2": {"delta": 0.20}}
    result = tt._closest_by_delta(options, target_delta=0.25, greeks=greeks)
    assert result.streamer_symbol == "S2"


def test_closest_by_delta_skips_missing_greeks():
    options = [_Opt("C", "100", "S1"), _Opt("C", "105", "S2")]
    greeks = {"S2": {"delta": 0.20}}
    result = tt._closest_by_delta(options, target_delta=0.25, greeks=greeks)
    assert result.streamer_symbol == "S2"


def test_closest_by_delta_none_when_no_greeks_at_all():
    options = [_Opt("C", "100", "S1")]
    assert tt._closest_by_delta(options, target_delta=0.25, greeks={}) is None


def test_nearest_by_strike_excludes_index():
    options = [_Opt("C", "100"), _Opt("C", "105"), _Opt("C", "110")]
    result = tt._nearest_by_strike(options, target_strike=104, exclude_idx=1)
    assert result.strike_price == "100"


def test_select_call_spread_uses_delta_when_greeks_present():
    calls = [_Opt("C", "100", "S1"), _Opt("C", "105", "S2"), _Opt("C", "110", "S3")]
    greeks = {"S1": {"delta": 0.30}, "S2": {"delta": 0.15}, "S3": {"delta": 0.05}}
    short, long_ = tt._select_call_spread(calls, wing_width=10, short_delta=0.15, greeks=greeks)
    assert short.streamer_symbol == "S2"
    assert long_.strike_price == "110"  # closest to 105+10=115 among remaining


def test_select_call_spread_falls_back_without_greeks():
    calls = [_Opt("C", str(100 + i * 5)) for i in range(6)]
    short, long_ = tt._select_call_spread(calls, wing_width=10, short_delta=0.15, greeks={})
    assert short is not None
    assert long_ is not None


def test_select_put_spread_uses_delta_when_greeks_present():
    puts = [_Opt("P", "90", "S1"), _Opt("P", "95", "S2"), _Opt("P", "100", "S3")]
    greeks = {"S1": {"delta": -0.05}, "S2": {"delta": -0.15}, "S3": {"delta": -0.30}}
    short, long_ = tt._select_put_spread(puts, wing_width=10, short_delta=0.15, greeks=greeks)
    assert short.streamer_symbol == "S2"


# --- _build_order --------------------------------------------------------------

def test_build_order_credit_price_is_negative():
    spec = {
        "order_type": "Limit", "time_in_force": "Day", "price": 2.5, "price_effect": "Credit",
        "legs": [{"symbol": "XSP_C", "instrument_type": "Equity Option", "action": "Sell to Open", "quantity": 1}],
    }
    order = tt._build_order(spec)
    assert order.price < 0


def test_build_order_debit_price_is_positive():
    spec = {
        "price": 2.5, "price_effect": "Debit",
        "legs": [{"symbol": "XSP_C", "instrument_type": "Equity Option", "action": "Buy to Open", "quantity": 1}],
    }
    order = tt._build_order(spec)
    assert order.price > 0


def test_build_order_includes_stop_trigger_when_given():
    spec = {
        "stop_trigger": 5.0,
        "legs": [{"symbol": "XSP_C", "instrument_type": "Equity Option", "action": "Sell to Open", "quantity": 1}],
    }
    order = tt._build_order(spec)
    assert order.stop_trigger is not None


def test_build_order_maps_all_actions():
    for action in ("buy to open", "sell to open", "buy to close", "sell to close"):
        spec = {"legs": [{"symbol": "X", "instrument_type": "Equity Option", "action": action, "quantity": 1}]}
        order = tt._build_order(spec)
        assert len(order.legs) == 1


# --- cmd_execute_trade (mocked account/session) -------------------------------

def test_cmd_execute_trade_blocks_live_when_disabled(monkeypatch):
    monkeypatch.setattr(tt, "_live_trading_enabled", lambda: False)
    args = type("Args", (), {"dry_run": False, "order": "{}", "account_number": None})()
    result = asyncio.run(tt.cmd_execute_trade(args))
    assert result == {"ok": False, "error": "Live trading is disabled. Set enable_live_trading=true in config.json."}


def test_cmd_execute_trade_dry_run_returns_without_submitting(monkeypatch):
    order_spec = {
        "price": 1.0, "price_effect": "Credit",
        "legs": [{"symbol": "XSP_C", "instrument_type": "Equity Option", "action": "Sell to Open", "quantity": 1}],
    }

    class _Preflight:
        errors = []
        warnings = []
        buying_power_effect = None

    class _FakeAccount:
        account_number = "ACC1"
        async def place_order(self, session_obj, order, dry_run):
            assert dry_run is True
            return _Preflight()

    async def _fake_get_account(account_number=None):
        return _FakeAccount()

    monkeypatch.setattr(tt, "_get_account", _fake_get_account)
    monkeypatch.setattr(tt, "get_session", lambda: object())
    monkeypatch.setattr(tt, "_live_trading_enabled", lambda: False)

    args = type("Args", (), {"dry_run": True, "order": json.dumps(order_spec), "account_number": None})()
    result = asyncio.run(tt.cmd_execute_trade(args))
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["account_number"] == "ACC1"


def test_cmd_execute_trade_preflight_errors_block_submission(monkeypatch):
    order_spec = {"legs": [{"symbol": "XSP_C", "instrument_type": "Equity Option", "action": "Sell to Open", "quantity": 1}]}

    class _Preflight:
        errors = ["insufficient buying power"]
        warnings = []
        buying_power_effect = None

    class _FakeAccount:
        account_number = "ACC1"
        async def place_order(self, session_obj, order, dry_run):
            return _Preflight()

    async def _fake_get_account(account_number=None):
        return _FakeAccount()

    monkeypatch.setattr(tt, "_get_account", _fake_get_account)
    monkeypatch.setattr(tt, "get_session", lambda: object())
    monkeypatch.setattr(tt, "_live_trading_enabled", lambda: False)

    args = type("Args", (), {"dry_run": True, "order": json.dumps(order_spec), "account_number": None})()
    result = asyncio.run(tt.cmd_execute_trade(args))
    assert result["ok"] is False
    assert result["problems"] == ["insufficient buying power"]


def test_cmd_execute_trade_live_submits_order(monkeypatch):
    order_spec = {"legs": [{"symbol": "XSP_C", "instrument_type": "Equity Option", "action": "Sell to Open", "quantity": 1}]}

    class _Preflight:
        errors = []
        warnings = []
        buying_power_effect = None

    class _Response:
        pass

    calls = []

    class _FakeAccount:
        account_number = "ACC1"
        async def place_order(self, session_obj, order, dry_run):
            calls.append(dry_run)
            return _Preflight() if dry_run else _Response()

    async def _fake_get_account(account_number=None):
        return _FakeAccount()

    monkeypatch.setattr(tt, "_get_account", _fake_get_account)
    monkeypatch.setattr(tt, "get_session", lambda: object())
    monkeypatch.setattr(tt, "_live_trading_enabled", lambda: True)

    args = type("Args", (), {"dry_run": False, "order": json.dumps(order_spec), "account_number": None})()
    result = asyncio.run(tt.cmd_execute_trade(args))
    assert result["ok"] is True
    assert result["dry_run"] is False
    assert calls == [True, False]


def test_cmd_execute_trade_deploy_governor_blocks_live_over_cap(monkeypatch):
    # account_deploy_limit_pct wired from config -> cherrypit.broker deploy governor blocks a live
    # order that would push deployed BP over the cap, before any live submit.
    order_spec = {"legs": [{"symbol": "XSP_C", "instrument_type": "Equity Option", "action": "Sell to Open", "quantity": 1}]}

    class _BPE:
        change_in_buying_power = "-5300"  # consumes 5300 BP

    class _Preflight:
        errors = []
        warnings = []
        buying_power_effect = _BPE()

    class _Balances:
        used_derivative_buying_power = "0"
        derivative_buying_power = "10000"  # capacity 10000, 50% cap = 5000 < 5300

    calls = []

    class _FakeAccount:
        account_number = "ACC1"
        async def place_order(self, session_obj, order, dry_run):
            calls.append(dry_run)
            return _Preflight()
        async def get_balances(self, session_obj):
            return _Balances()

    async def _fake_get_account(account_number=None):
        return _FakeAccount()

    monkeypatch.setattr(tt, "_get_account", _fake_get_account)
    monkeypatch.setattr(tt, "get_session", lambda: object())
    monkeypatch.setattr(tt, "_live_trading_enabled", lambda: True)
    monkeypatch.setattr(tt, "_load_config", lambda: {"account_deploy_limit_pct": 50})

    args = type("Args", (), {"dry_run": False, "order": json.dumps(order_spec), "account_number": None})()
    result = asyncio.run(tt.cmd_execute_trade(args))
    assert result["ok"] is False
    assert result["error"] == "account deploy limit exceeded"
    assert result["governor"]["deploy_governor"] == "enforced"
    assert calls == [True]  # blocked before the live submit


# --- cmd_secrets_status -------------------------------------------------------

def test_cmd_secrets_status_reports_ready_when_required_set(monkeypatch):
    monkeypatch.setattr(
        "credentials.secrets_status",
        lambda: {"client_secret": True, "refresh_token": True, "account_number": False},
    )
    result = tt.cmd_secrets_status(None)
    assert result["ok"] is True
    assert result["ready"] is True
    assert result["secrets"]["account_number"] == "missing"


def test_cmd_secrets_status_not_ready_when_required_missing(monkeypatch):
    monkeypatch.setattr(
        "credentials.secrets_status",
        lambda: {"client_secret": True, "refresh_token": False, "account_number": False},
    )
    result = tt.cmd_secrets_status(None)
    assert result["ready"] is False


# --- _compute_gex --------------------------------------------------------------

def _gex_entry(strike, opt_type, sym):
    return {"strike_price": str(strike), "option_type": opt_type, "streamer_symbol": sym}


def test_compute_gex_insufficient_data_when_no_matches():
    result = tt._compute_gex([], greeks={}, oi={}, spot=600.0)
    assert result["ok"] is False


def test_compute_gex_computes_net_and_walls():
    entries = [
        _gex_entry(595, "P", "P1"),
        _gex_entry(600, "C", "C1"),
        _gex_entry(605, "C", "C2"),
    ]
    greeks = {"P1": {"gamma": 0.02}, "C1": {"gamma": 0.05}, "C2": {"gamma": 0.01}}
    oi = {"P1": 1000, "C1": 2000, "C2": 500}
    result = tt._compute_gex(entries, greeks, oi, spot=600.0)
    assert result["ok"] is True
    assert result["call_wall"] == 600
    assert result["put_wall"] == 595
    assert result["strikes_with_data"] == 3


def test_compute_gex_skips_entries_missing_oi_or_greeks():
    entries = [_gex_entry(600, "C", "C1"), _gex_entry(605, "C", "C2")]
    greeks = {"C1": {"gamma": 0.05}}  # C2 has no greeks
    oi = {"C1": 1000, "C2": 500}
    result = tt._compute_gex(entries, greeks, oi, spot=600.0)
    assert result["ok"] is True
    assert result["strikes_with_data"] == 1


# --- cmd_get_orb_range ---------------------------------------------------------

def test_cmd_get_orb_range_not_captured_yet(cache_db, monkeypatch):
    args = type("Args", (), {"symbol": "xsp"})()
    result = tt.cmd_get_orb_range(args)
    assert result["ok"] is False


def test_cmd_get_orb_range_returns_captured_values(cache_db):
    import pytz
    et_today = __import__("datetime").datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d")
    _insert(cache_db, "orb_ranges", symbol="XSP", trade_date=et_today, orb_high=605.0, orb_low=598.0, captured_at=time.time())
    args = type("Args", (), {"symbol": "xsp"})()
    result = tt.cmd_get_orb_range(args)
    assert result["ok"] is True
    assert result["orb_high"] == 605.0
    assert result["orb_low"] == 598.0


def test_cmd_get_orb_range_no_cache_db(tmp_path, monkeypatch):
    monkeypatch.setattr(tt, "_CACHE_DB", tmp_path / "missing.db")
    args = type("Args", (), {"symbol": "XSP"})()
    result = tt.cmd_get_orb_range(args)
    assert result["ok"] is False
    assert "streamer running" in result["error"]


# --- cmd_stream_status (streamer._running_pid mocked) --------------------------

def test_cmd_stream_status_not_running_no_cache(tmp_path, monkeypatch):
    import streamer
    monkeypatch.setattr(streamer, "_running_pid", lambda: None)
    monkeypatch.setattr(tt, "_CACHE_DB", tmp_path / "missing.db")
    result = tt.cmd_stream_status(None)
    assert result["running"] is False
    assert result["cache"] == "no cache db"


def test_cmd_stream_status_running_with_fresh_data(cache_db, monkeypatch):
    import streamer
    monkeypatch.setattr(streamer, "_running_pid", lambda: 4242)
    _insert(cache_db, "stream_trades", symbol="XSP", last=600.0, change=0, volume=10, updated_at=time.time())
    result = tt.cmd_stream_status(None)
    assert result["running"] is True
    assert result["pid"] == 4242
    assert result["trades_symbols"] == 1
    assert result["stale_warning"] is False


def test_cmd_stream_status_stale_when_running_but_old_data(cache_db, monkeypatch):
    import streamer
    monkeypatch.setattr(streamer, "_running_pid", lambda: 4242)
    _insert(cache_db, "stream_trades", symbol="XSP", last=600.0, change=0, volume=10, updated_at=time.time() - 3000)
    result = tt.cmd_stream_status(None)
    assert result["stale_warning"] is True
