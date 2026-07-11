"""Unit tests for streamer cache helpers and sync dispatch handlers.

No live network, no streamer process — all tests seed a temp SQLite DB
and call functions directly.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import streamer as _streamer
from streamer import (
    _DDL,
    _REST_TTL,
    _read_rest_cache,
    _write_rest_cache,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    for stmt in _DDL.split(";"):
        s = stmt.strip()
        if s:
            conn.execute(s)
    conn.commit()
    return conn


def _handler(db_path: str) -> _streamer._ApiHandler:
    """Return a handler instance whose _db() opens the given file DB."""
    h = object.__new__(_streamer._ApiHandler)
    # Patch _db() to use our temp file
    def _db_override():
        c = sqlite3.connect(db_path, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c
    h._db = _db_override
    return h


def _make_file_db() -> tuple[sqlite3.Connection, str]:
    """Return (conn, path) for a temp file DB."""
    tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tf.close()
    conn = sqlite3.connect(tf.name, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    for stmt in _DDL.split(";"):
        s = stmt.strip()
        if s:
            conn.execute(s)
    conn.commit()
    return conn, tf.name


# ---------------------------------------------------------------------------
# REST cache helpers
# ---------------------------------------------------------------------------

class TestRestCache:
    def test_write_and_read(self):
        conn = _make_db()
        data = {"ok": True, "nlv": 10000.0}
        _write_rest_cache(conn, "account_info", data)
        result = _read_rest_cache(conn, "account_info")
        assert result is not None
        assert result["nlv"] == 10000.0

    def test_read_missing_key_returns_none(self):
        conn = _make_db()
        assert _read_rest_cache(conn, "account_info") is None

    def test_read_expired_returns_none(self):
        conn = _make_db()
        _write_rest_cache(conn, "working_orders", {"ok": True, "orders": []})
        # Backdate the timestamp past TTL
        ttl = _REST_TTL["working_orders"]
        conn.execute(
            "UPDATE stream_rest_cache SET updated_at = ? WHERE key = ?",
            (time.time() - ttl - 1, "working_orders"),
        )
        conn.commit()
        assert _read_rest_cache(conn, "working_orders") is None

    def test_read_fresh_within_ttl(self):
        conn = _make_db()
        _write_rest_cache(conn, "market_overview", {"ok": True, "iv_rank": 42.0})
        result = _read_rest_cache(conn, "market_overview")
        assert result["iv_rank"] == 42.0

    def test_overwrite(self):
        conn = _make_db()
        _write_rest_cache(conn, "positions", {"ok": True, "count": 1})
        _write_rest_cache(conn, "positions", {"ok": True, "count": 3})
        result = _read_rest_cache(conn, "positions")
        assert result["count"] == 3


# ---------------------------------------------------------------------------
# Sync get_quote
# ---------------------------------------------------------------------------

class TestSyncGetQuote:
    def setup_method(self):
        self.conn, self.path = _make_file_db()
        self.h = _handler(self.path)

    def teardown_method(self):
        self.conn.close()

    def _seed_trade(self, symbol: str, last: float, age: float = 0.0):
        self.conn.execute(
            "INSERT INTO stream_trades (symbol, last, change, volume, updated_at) "
            "VALUES (?, ?, 0, 0, ?) ON CONFLICT(symbol) DO UPDATE SET "
            "last=excluded.last, updated_at=excluded.updated_at",
            (symbol, last, time.time() - age),
        )
        self.conn.commit()

    def test_cache_hit(self):
        self._seed_trade("XSP", 590.25)
        result = self.h._sync_get_quote({"symbol": "XSP"})
        assert result["ok"] is True
        assert result["last"] == 590.25
        assert result["source"] == "stream_cache"

    def test_cache_miss_no_row(self):
        result = self.h._sync_get_quote({"symbol": "XSP"})
        assert result["ok"] is True
        assert result["last"] is None

    def test_stale_data_returns_none_last(self):
        self._seed_trade("XSP", 590.25, age=15.0)  # older than 10s TTL
        result = self.h._sync_get_quote({"symbol": "XSP"})
        assert result["last"] is None

    def test_symbol_uppercased(self):
        self._seed_trade("XSP", 591.0)
        result = self.h._sync_get_quote({"symbol": "xsp"})
        assert result["last"] == 591.0


# ---------------------------------------------------------------------------
# Sync get_option_chain
# ---------------------------------------------------------------------------

_SAMPLE_OPTION = {
    "streamer_symbol": ".XSP260630C590",
    "strike_price": "590",
    "option_type": "C",
    "expiration_date": "2026-06-30",
    "shares_per_contract": 100,
}


class TestSyncGetOptionChain:
    def setup_method(self):
        self.conn, self.path = _make_file_db()
        self.h = _handler(self.path)
        self.exp = "2026-06-30"

    def teardown_method(self):
        self.conn.close()

    def _seed_chain(self, count: int = 4, underlying: str = "XSP"):
        now = time.time()
        strikes = [585, 590, 595, 600]
        for k in range(count):
            for otype in ("C", "P"):
                strike = strikes[k % len(strikes)]
                sym = f".XSP260630{otype}{strike}"
                opt = {**_SAMPLE_OPTION,
                       "streamer_symbol": sym,
                       "option_type": otype,
                       "strike_price": str(strike)}
                self.conn.execute(
                    "INSERT INTO stream_chain (streamer_symbol, expiration, underlying_symbol, data_json, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(streamer_symbol) DO UPDATE SET "
                    "data_json=excluded.data_json, updated_at=excluded.updated_at",
                    (sym, self.exp, underlying, json.dumps(opt), now),
                )
        self.conn.commit()

    def _seed_quotes(self, syms: list[str]):
        now = time.time()
        for sym in syms:
            self.conn.execute(
                "INSERT INTO stream_quotes (symbol, bid, ask, mid, bid_size, ask_size, updated_at) "
                "VALUES (?, 0.5, 0.7, 0.6, 10, 10, ?) ON CONFLICT(symbol) DO UPDATE SET "
                "mid=excluded.mid, updated_at=excluded.updated_at",
                (sym, now),
            )
        self.conn.commit()

    def _seed_greeks(self, syms: list[str]):
        now = time.time()
        for sym in syms:
            self.conn.execute(
                "INSERT INTO stream_greeks (symbol, delta, gamma, theta, vega, rho, iv, price, updated_at) "
                "VALUES (?, 0.15, 0.01, -0.05, 0.1, 0.0, 0.20, 0.6, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET delta=excluded.delta, updated_at=excluded.updated_at",
                (sym, now),
            )
        self.conn.commit()

    def test_no_chain_returns_none(self):
        result = self.h._sync_get_option_chain({"symbol": "XSP"})
        assert result is None

    def test_chain_no_quotes_no_greeks(self):
        self._seed_chain()
        result = self.h._sync_get_option_chain({"symbol": "XSP"})
        assert result is not None
        assert result["ok"] is True
        assert self.exp in result["chain"]

    def test_chain_with_quotes(self):
        self._seed_chain(4)
        syms = [row["streamer_symbol"]
                for row in self.conn.execute("SELECT streamer_symbol FROM stream_chain").fetchall()]
        self._seed_quotes(syms)
        result = self.h._sync_get_option_chain({"symbol": "XSP", "include_quotes": True})
        assert result is not None
        options = result["chain"][self.exp]
        assert all(o.get("mid") == 0.6 for o in options)

    def test_partial_quotes_returns_none(self):
        self._seed_chain(4)
        syms = [row["streamer_symbol"]
                for row in self.conn.execute("SELECT streamer_symbol FROM stream_chain").fetchall()]
        self._seed_quotes(syms[:2])  # only half the symbols
        result = self.h._sync_get_option_chain({"symbol": "XSP", "include_quotes": True})
        assert result is None

    def test_chain_with_greeks(self):
        self._seed_chain(4)
        syms = [row["streamer_symbol"]
                for row in self.conn.execute("SELECT streamer_symbol FROM stream_chain").fetchall()]
        self._seed_greeks(syms)
        result = self.h._sync_get_option_chain({"symbol": "XSP", "include_greeks": True})
        assert result is not None
        options = result["chain"][self.exp]
        assert all(o.get("delta") == 0.15 for o in options)

    def test_stale_chain_returns_none(self):
        self._seed_chain()
        self.conn.execute(
            "UPDATE stream_chain SET updated_at = ?", (time.time() - 5 * 3600,)
        )
        self.conn.commit()
        result = self.h._sync_get_option_chain({"symbol": "XSP"})
        assert result is None


# ---------------------------------------------------------------------------
# Sync get_strategies
# ---------------------------------------------------------------------------

class TestSyncGetStrategies:
    def setup_method(self):
        self.conn, self.path = _make_file_db()
        self.h = _handler(self.path)
        self.exp = "2026-06-30"
        self._seed()

    def teardown_method(self):
        self.conn.close()

    def _seed(self):
        now = time.time()
        # Seed a small chain: puts 585/590, calls 595/600
        legs = [
            (".XSP260630P585", "P", 585, -0.10),
            (".XSP260630P590", "P", 590, -0.15),
            (".XSP260630C595", "C", 595,  0.15),
            (".XSP260630C600", "C", 600,  0.10),
        ]
        for sym, otype, strike, delta in legs:
            opt = {
                "streamer_symbol": sym,
                "option_type": otype,
                "strike_price": str(strike),
                "expiration_date": self.exp,
                "shares_per_contract": 100,
            }
            self.conn.execute(
                "INSERT INTO stream_chain (streamer_symbol, expiration, underlying_symbol, data_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(streamer_symbol) DO UPDATE SET "
                "data_json=excluded.data_json, updated_at=excluded.updated_at",
                (sym, self.exp, "XSP", json.dumps(opt), now),
            )
            self.conn.execute(
                "INSERT INTO stream_greeks (symbol, delta, gamma, theta, vega, rho, iv, price, updated_at) "
                "VALUES (?, ?, 0.01, -0.05, 0.1, 0.0, 0.20, 0.5, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET delta=excluded.delta, updated_at=excluded.updated_at",
                (sym, delta, now),
            )
            self.conn.execute(
                "INSERT INTO stream_quotes (symbol, bid, ask, mid, bid_size, ask_size, updated_at) "
                "VALUES (?, 0.4, 0.6, 0.5, 10, 10, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET mid=excluded.mid, updated_at=excluded.updated_at",
                (sym, now),
            )
        self.conn.commit()

    def test_returns_iron_condor(self):
        result = self.h._sync_get_strategies({"symbol": "XSP", "wing_width": 1, "short_delta": 0.15})
        assert result is not None
        assert result["strategy"] == "iron_condor"
        assert result["source"] == "stream_cache"

    def test_legs_present(self):
        result = self.h._sync_get_strategies({"symbol": "XSP", "wing_width": 1, "short_delta": 0.15})
        assert result is not None
        legs = result["legs"]
        assert set(legs.keys()) == {"short_put", "long_put", "short_call", "long_call"}

    def test_net_credit_computed(self):
        result = self.h._sync_get_strategies({"symbol": "XSP", "wing_width": 1, "short_delta": 0.15})
        assert result is not None
        assert result["net_credit"] is not None
        assert result["quotes_complete"] is True

    def test_no_greeks_returns_none(self):
        self.conn.execute("DELETE FROM stream_greeks")
        self.conn.commit()
        result = self.h._sync_get_strategies({"symbol": "XSP", "wing_width": 1, "short_delta": 0.15})
        assert result is None

    def test_no_chain_returns_none(self):
        self.conn.execute("DELETE FROM stream_chain")
        self.conn.commit()
        result = self.h._sync_get_strategies({"symbol": "XSP", "wing_width": 1, "short_delta": 0.15})
        assert result is None

    def test_stale_greeks_skipped(self):
        # Greeks use a 2700s (45-min) TTL here, not the 10s quote/trade TTL — DXLink
        # publishes Greeks in infrequent batches, not tick-by-tick. Backdate past
        # that window so the cache is treated as having no valid greeks.
        self.conn.execute(
            "UPDATE stream_greeks SET updated_at = ?", (time.time() - 2701,)
        )
        self.conn.commit()
        result = self.h._sync_get_strategies({"symbol": "XSP", "wing_width": 1, "short_delta": 0.15})
        assert result is None


# ---------------------------------------------------------------------------
# Multi-symbol config resolution
# ---------------------------------------------------------------------------

class TestConfiguredSymbols:
    def test_symbols_list_uppercased(self):
        assert _streamer._configured_symbols({"symbols": ["xsp", "spx"]}) == ["XSP", "SPX"]

    def test_deprecated_singular_symbol_alias(self):
        assert _streamer._configured_symbols({"symbol": "xsp"}) == ["XSP"]

    def test_symbols_takes_precedence_over_singular(self):
        assert _streamer._configured_symbols({"symbols": ["SPX"], "symbol": "XSP"}) == ["SPX"]

    def test_default_when_config_empty(self):
        assert _streamer._configured_symbols({}) == ["XSP"]

    def test_cli_override_takes_precedence(self):
        assert _streamer._configured_symbols(
            {"symbols": ["XSP"]}, cli_override=["spx", "ndx"]
        ) == ["SPX", "NDX"]

    def test_blank_entries_filtered(self):
        assert _streamer._configured_symbols({"symbols": ["XSP", " ", "SPX"]}) == ["XSP", "SPX"]


# ---------------------------------------------------------------------------
# Multi-symbol subscription resolution
# ---------------------------------------------------------------------------

class TestResolveSubscriptionsMultiSymbol:
    def test_trade_subscribes_every_symbol(self):
        subs = _streamer._resolve_subscriptions(["XSP", "SPX", "NDX"])
        assert subs["Trade"] == ["XSP", "SPX", "NDX"]

    def test_summary_seeds_every_symbol(self):
        subs = _streamer._resolve_subscriptions(["XSP", "SPX"])
        assert "XSP" in subs["Summary"]
        assert "SPX" in subs["Summary"]

    def test_single_symbol_still_a_list(self):
        subs = _streamer._resolve_subscriptions(["XSP"])
        assert subs["Trade"] == ["XSP"]


# ---------------------------------------------------------------------------
# Multi-symbol cache isolation — two symbols' chains must never cross-contaminate
# ---------------------------------------------------------------------------

class TestMultiSymbolChainIsolation:
    def setup_method(self):
        self.conn, self.path = _make_file_db()
        self.h = _handler(self.path)
        self.exp = "2026-06-30"

    def teardown_method(self):
        self.conn.close()

    def _seed(self, underlying: str, strikes: list[float]):
        now = time.time()
        for strike in strikes:
            for otype in ("C", "P"):
                sym = f".{underlying}260630{otype}{int(strike)}"
                opt = {
                    "streamer_symbol": sym, "option_type": otype,
                    "strike_price": str(strike), "expiration_date": self.exp,
                    "shares_per_contract": 100,
                }
                self.conn.execute(
                    "INSERT INTO stream_chain (streamer_symbol, expiration, underlying_symbol, data_json, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (sym, self.exp, underlying, json.dumps(opt), now),
                )
        self.conn.commit()

    def test_chain_query_scoped_to_requested_underlying(self):
        self._seed("XSP", [590, 595])
        self._seed("SPX", [5900, 5950])
        xsp_result = self.h._sync_get_option_chain({"symbol": "XSP"})
        spx_result = self.h._sync_get_option_chain({"symbol": "SPX"})
        assert xsp_result is not None and spx_result is not None
        xsp_syms = {o["streamer_symbol"] for o in xsp_result["chain"][self.exp]}
        spx_syms = {o["streamer_symbol"] for o in spx_result["chain"][self.exp]}
        assert all(".XSP" in s for s in xsp_syms)
        assert all(".SPX" in s for s in spx_syms)
        assert xsp_syms.isdisjoint(spx_syms)

    def test_missing_symbol_unaffected_by_other_symbols_data(self):
        self._seed("XSP", [590])
        # NDX was never seeded — must not accidentally pick up XSP's chain
        result = self.h._sync_get_option_chain({"symbol": "NDX"})
        assert result is None
