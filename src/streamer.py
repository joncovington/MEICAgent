"""MEICAgent DXLink Streamer Daemon.

Maintains a single persistent WebSocket to tastytrade's DXLink feed and writes
the latest Quote, Greeks, and Trade events to data/stream_cache.db, for every
symbol configured in config.json's `symbols` list. Each traded symbol gets its
own subscription window (near-the-money option strikes) on the same connection —
that window doubles as both the entry-strike-selection data and that symbol's
own GEX profile, so GEX is computed per-symbol automatically.

Usage:
  python src/streamer.py                       # start daemon (foreground), symbols from config.json
  python src/streamer.py --symbol XSP --symbol SPX  # override traded symbols for this run
  python src/streamer.py --stop                # send SIGTERM to running daemon
  python src/streamer.py --status               # print cache status and exit

The main loop in tt.py reads from the cache (age < 10s) before falling
back to a fresh DXLink connection. The dashboard reads it for live P&L.
"""

from __future__ import annotations

import argparse
import asyncio
import http.server
import json
import logging
import os
import signal
import socketserver
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Allow running as `python src/streamer.py` from the project root.
sys.path.insert(0, os.path.dirname(__file__))

from session import get_session

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parent.parent
_CACHE_DB  = _ROOT / "data" / "stream_cache.db"
_PID_FILE  = _ROOT / "data" / "streamer.pid"
_CONFIG    = _ROOT / "config.json"
_TRADES_DB = _ROOT / "data" / "meic_trades.db"
_LOG_FILE  = _ROOT / "logs" / "streamer.log"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    (_ROOT / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.FileHandler(_LOG_FILE)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache DB
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS stream_chain (
    streamer_symbol   TEXT PRIMARY KEY,
    expiration        TEXT NOT NULL,
    underlying_symbol TEXT,
    data_json         TEXT NOT NULL,
    updated_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chain_expiration ON stream_chain(expiration);
CREATE TABLE IF NOT EXISTS stream_quotes (
    symbol      TEXT PRIMARY KEY,
    bid         REAL,
    ask         REAL,
    mid         REAL,
    bid_size    REAL,
    ask_size    REAL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stream_greeks (
    symbol      TEXT PRIMARY KEY,
    delta       REAL,
    gamma       REAL,
    theta       REAL,
    vega        REAL,
    rho         REAL,
    iv          REAL,
    price       REAL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stream_trades (
    symbol      TEXT PRIMARY KEY,
    last        REAL,
    change      REAL,
    volume      REAL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stream_oi (
    symbol        TEXT PRIMARY KEY,
    open_interest INTEGER,
    updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stream_rest_cache (
    key         TEXT PRIMARY KEY,
    data_json   TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stream_status (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    pid                 INTEGER,
    connected_since     TEXT,
    last_event_at       TEXT,
    subscribed_symbols  INTEGER DEFAULT 0,
    reconnect_count     INTEGER DEFAULT 0
);
"""


# REST cache TTLs (seconds) per key
_REST_TTL: dict[str, float] = {
    "account_info":    20.0,
    "positions":       20.0,
    "working_orders":  10.0,
    "market_overview": 60.0,
}


def _write_rest_cache(conn: sqlite3.Connection, key: str, data: dict) -> None:
    conn.execute(
        "INSERT INTO stream_rest_cache (key, data_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET data_json=excluded.data_json, updated_at=excluded.updated_at",
        (key, json.dumps(data, default=str), time.time()),
    )
    conn.commit()


def _read_rest_cache(conn: sqlite3.Connection, key: str) -> dict | None:
    row = conn.execute(
        "SELECT data_json, updated_at FROM stream_rest_cache WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    ttl = _REST_TTL.get(key, 30.0)
    if (time.time() - row["updated_at"]) > ttl:
        return None
    return json.loads(row["data_json"])


def _cache_connect() -> sqlite3.Connection:
    _CACHE_DB.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(_CACHE_DB), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    for stmt in _DDL.split(";"):
        s = stmt.strip()
        if s:
            conn.execute(s)
    existing_chain_cols = {row[1] for row in conn.execute("PRAGMA table_info(stream_chain)")}
    if "underlying_symbol" not in existing_chain_cols:
        conn.execute("ALTER TABLE stream_chain ADD COLUMN underlying_symbol TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chain_underlying ON stream_chain(underlying_symbol, expiration)")
    conn.commit()
    return conn


def _upsert_status(conn: sqlite3.Connection, **kwargs) -> None:
    fields = {k: v for k, v in kwargs.items()}
    cols = ", ".join(fields)
    vals = ", ".join(["?" for _ in fields])
    updates = ", ".join(f"{k} = excluded.{k}" for k in fields if k != "id")
    conn.execute(
        f"INSERT INTO stream_status (id, {cols}) VALUES (1, {vals}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}",
        list(fields.values()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Symbol resolution
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    if _CONFIG.exists():
        return json.loads(_CONFIG.read_text())
    return {}


def _open_trade_streamer_symbols() -> list[str]:
    """Return streamer symbols for all legs of currently open ICs."""
    if not _TRADES_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(_TRADES_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT put_symbol, call_symbol, long_put_symbol, long_call_symbol "
            "FROM ic_trades WHERE status IN ('pending','open','partial','partial_entry')"
        ).fetchall()
        conn.close()
        symbols = []
        for r in rows:
            for col in ("put_symbol", "call_symbol", "long_put_symbol", "long_call_symbol"):
                sym = r[col]
                if sym:
                    symbols.append(sym)
        return list(set(symbols))
    except Exception as exc:
        logger.warning("Failed to load open trade symbols: %s", exc)
        return []


def _resolve_subscriptions(symbols: list[str]) -> dict[str, list[str]]:
    """Return {event_type: [streamer_symbols]} for current open positions."""
    option_syms = _open_trade_streamer_symbols()
    return {
        "Trade": list(symbols),
        "Quote": option_syms,
        "Greeks": option_syms,
        # Seed Summary with each underlying so the channel opens at startup even with no open
        # trades. Each underlying's Summary event carries OHLC/volume (not OI); option OI arrives
        # once each symbol's window is subscribed by _symbol_refresher.
        "Summary": list(symbols) + option_syms,
    }


# ---------------------------------------------------------------------------
# Streamer daemon
# ---------------------------------------------------------------------------

_RECONNECT_BASE   = 2.0
_RECONNECT_MAX    = 60.0
_SYMBOL_POLL_S    = 30.0    # how often to check for new open trades
_HTTP_PORT        = 7699    # streamer API port

# Per-symbol subscription window. Every traded symbol gets exactly one window sized to the
# GEX requirement (wider than the ATM-only requirement of ~15 strikes) — GEX profile accuracy
# needs strikes further from the money than entry strike selection does, and a single window
# per symbol comfortably covers both needs rather than maintaining two separate windows.
_WINDOW_STRIKE_COUNT = 20   # strikes each side of center (~40 strikes × 2 types = ~80 symbols)
_WINDOW_REFRESH_PTS  = 1.0  # re-center when the symbol's underlying moves this many points
_WINDOW_POLL_S       = 5.0  # how often to check whether a re-center is due

# Shared state for the HTTP server thread
_loop: asyncio.AbstractEventLoop | None = None           # DXLink event loop
_rest_loop: asyncio.AbstractEventLoop | None = None      # dedicated REST loop
_http_state: "_State | None" = None

# Default arg values for each command (mirrors argparse defaults in tt.py)
_CMD_DEFAULTS: dict[str, dict] = {
    "get_connection_status": {},
    "list_accounts": {},
    "get_account_info":    {"account_number": None},
    "get_positions":       {"account_number": None},
    "get_market_overview": {"quotes_timeout": 4.0},
    "get_quote":           {"timeout": 6.0},
    "get_option_chain":    {"expiration": None, "include_greeks": False, "include_quotes": False,
                            "strike_count": 15, "around_price": None,
                            "greeks_timeout": 6.0, "quotes_timeout": 6.0},
    "get_strategies":      {"target_dte": 0, "wing_width": 5, "short_delta": 0.15,
                            "around_price": None, "greeks_timeout": 6.0, "quotes_timeout": 6.0},
    "get_working_orders":  {"account_number": None},
    "execute_trade":       {"account_number": None, "dry_run": True},
    "adjust_order":        {"account_number": None, "dry_run": True},
    "close_position":      {"account_number": None},
    "stream_status":       {},
    "stream_subscribe":    {"timeout": 6.0},
    "get_gex":             {"strike_count": 20, "around_price": None},
}


class _State:
    def __init__(self) -> None:
        self.stop_event = asyncio.Event()
        self.subscribed: dict[str, list[str]] = {"Trade": [], "Quote": [], "Greeks": [], "Summary": []}
        self.reconnect_count = 0
        self.last_event_at: str | None = None
        self.conn: sqlite3.Connection = _cache_connect()
        # Per-symbol subscription window tracking. Every entry in `symbols` (the config's
        # traded-symbol list) gets its own chain/window/center, keyed by symbol — this is what
        # makes GEX (and entry strike selection) genuinely per-symbol rather than shared across
        # every traded underlying via one global window.
        self.symbols: list[str] = []
        self.chains: dict[str, dict] = {}        # symbol -> {streamer_symbol: option}
        self.window_syms: dict[str, list[str]] = {}   # symbol -> currently subscribed option streamer symbols
        self.centers: dict[str, float] = {}       # symbol -> underlying price the window is centered on
        # Batched-commit bookkeeping: DXLink pushes Trade/Quote/Greeks/Summary
        # events far more often than any reader's freshness gate requires
        # (10s-2700s — see tt.py), so committing on every single event turns
        # market data ingestion into a synchronous disk-sync storm. Batch
        # commits instead; _COMMIT_BATCH_INTERVAL_S bounds the extra staleness.
        self.pending_writes = 0
        self.last_commit_at = 0.0


_COMMIT_BATCH_INTERVAL_S = 0.5
_COMMIT_BATCH_MAX_PENDING = 25


def _maybe_commit(state: "_State") -> None:
    """Commit state.conn if enough writes or time have accumulated since the last commit."""
    state.pending_writes += 1
    now = time.time()
    if state.pending_writes >= _COMMIT_BATCH_MAX_PENDING or (now - state.last_commit_at) >= _COMMIT_BATCH_INTERVAL_S:
        state.conn.commit()
        state.pending_writes = 0
        state.last_commit_at = now


async def _run_stream(state: _State, symbols: list[str]) -> None:
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Greeks, Quote, Summary, Trade

    session = get_session()
    logger.info("Connecting DXLinkStreamer…")

    async with DXLinkStreamer(session) as streamer:
        connected_at = datetime.now(timezone.utc).isoformat()
        _upsert_status(
            state.conn,
            pid=os.getpid(),
            connected_since=connected_at,
            reconnect_count=state.reconnect_count,
        )
        logger.info("DXLinkStreamer connected (reconnects: %d)", state.reconnect_count)

        # Initial subscriptions
        subs = _resolve_subscriptions(symbols)
        await _apply_subscriptions(streamer, state, subs, Trade, Quote, Greeks, Summary)

        # Fan-out listeners + subscription updater as concurrent tasks
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_listen_trade(streamer, state, Trade))
            tg.create_task(_listen_quote(streamer, state, Quote))
            tg.create_task(_listen_greeks(streamer, state, Greeks))
            tg.create_task(_listen_summary(streamer, state, Summary))
            tg.create_task(_poll_subscriptions(streamer, state, symbols, Trade, Quote, Greeks, Summary))
            tg.create_task(_flush_status(state))
            tg.create_task(_watch_stop(state))
            # One subscription-window refresher per traded symbol — each maintains its own
            # near-the-money option window (used for both entry strike selection and that
            # symbol's own GEX profile).
            for sym in symbols:
                tg.create_task(_symbol_refresher(streamer, state, sym, Quote, Greeks, Summary, Trade))


def _total_subscribed(state: _State) -> int:
    """Combined subscription count across both tracking structures: state.subscribed
    (Trade + open-IC-leg Quote/Greeks/Summary, maintained by _apply_subscriptions) and
    state.window_syms (each traded symbol's own ATM/GEX window, maintained by
    _symbol_refresher). These are two independent sets of real streamer.subscribe() calls
    that can overlap (an open leg may also fall inside a symbol's window) — union rather
    than sum them so the reported total doesn't double-count. Both writers call this same
    helper so the reported total never regresses when the other writer's periodic update
    fires (previously each computed its own partial total, and _apply_subscriptions's
    unconditional 30s poll would stomp _symbol_refresher's larger total back down)."""
    window_union: set[str] = set()
    for syms in state.window_syms.values():
        window_union.update(syms)
    total = 0
    for key in ("Trade", "Quote", "Greeks", "Summary"):
        total += len(set(state.subscribed.get(key, [])) | window_union)
    return total


async def _apply_subscriptions(streamer, state: _State, subs: dict, Trade, Quote, Greeks, Summary) -> None:
    cls_map = {"Trade": Trade, "Quote": Quote, "Greeks": Greeks, "Summary": Summary}
    # A strike can be both an open-IC leg (tracked here via state.subscribed) and inside a
    # symbol's live ATM/GEX window (tracked independently in state.window_syms by
    # _symbol_refresher) — very plausible since windows span 20 strikes each side of the
    # money. If the leg closes, this function alone would unsubscribe it, silently killing
    # the window's still-wanted feed for that strike (there's no reference counting on the
    # underlying DXLink subscription — unsubscribe removes it outright, and
    # _symbol_refresher only resubscribes when its computed window changes, which may not
    # happen for a while after the strike goes dark). Protect anything currently claimed by
    # any window from removal here.
    window_union: set[str] = set()
    for syms in state.window_syms.values():
        window_union.update(syms)
    for key, symbols in subs.items():
        current = set(state.subscribed.get(key, []))
        wanted  = set(symbols)
        add     = wanted - current
        remove  = current - wanted
        if key in ("Quote", "Greeks", "Summary"):
            remove -= window_union
        cls = cls_map[key]
        if add:
            await streamer.subscribe(cls, list(add))
            logger.info("Subscribed %s %s", key, list(add))
        if remove:
            await streamer.unsubscribe(cls, list(remove))
            logger.info("Unsubscribed %s %s", key, list(remove))
        state.subscribed[key] = list(wanted)
    _upsert_status(state.conn, subscribed_symbols=_total_subscribed(state))


async def _listen_trade(streamer, state: _State, Trade) -> None:
    now = time.time
    conn = state.conn
    async for event in streamer.listen(Trade):
        if state.stop_event.is_set():
            break
        ts = now()
        try:
            conn.execute(
                "INSERT INTO stream_trades (symbol, last, change, volume, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(symbol) DO UPDATE SET "
                "last=excluded.last, change=excluded.change, volume=excluded.volume, "
                "updated_at=excluded.updated_at",
                (event.event_symbol, _f(event.price), _f(event.change),
                 _f(event.day_volume), ts),
            )
            _maybe_commit(state)
            state.last_event_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except Exception as exc:
            logger.warning("Trade write error: %s", exc)


async def _listen_quote(streamer, state: _State, Quote) -> None:
    now = time.time
    conn = state.conn
    async for event in streamer.listen(Quote):
        if state.stop_event.is_set():
            break
        ts = now()
        bid = _f(event.bid_price)
        ask = _f(event.ask_price)
        mid = round((bid + ask) / 2, 4) if bid is not None and ask is not None else None
        try:
            conn.execute(
                "INSERT INTO stream_quotes (symbol, bid, ask, mid, bid_size, ask_size, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(symbol) DO UPDATE SET "
                "bid=excluded.bid, ask=excluded.ask, mid=excluded.mid, "
                "bid_size=excluded.bid_size, ask_size=excluded.ask_size, "
                "updated_at=excluded.updated_at",
                (event.event_symbol, bid, ask, mid,
                 _f(event.bid_size), _f(event.ask_size), ts),
            )
            _maybe_commit(state)
            state.last_event_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except Exception as exc:
            logger.warning("Quote write error: %s", exc)


async def _listen_greeks(streamer, state: _State, Greeks) -> None:
    now = time.time
    conn = state.conn
    async for event in streamer.listen(Greeks):
        if state.stop_event.is_set():
            break
        ts = now()
        try:
            conn.execute(
                "INSERT INTO stream_greeks "
                "(symbol, delta, gamma, theta, vega, rho, iv, price, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(symbol) DO UPDATE SET "
                "delta=excluded.delta, gamma=excluded.gamma, theta=excluded.theta, "
                "vega=excluded.vega, rho=excluded.rho, iv=excluded.iv, "
                "price=excluded.price, updated_at=excluded.updated_at",
                (event.event_symbol, _f(event.delta), _f(event.gamma),
                 _f(event.theta), _f(event.vega), _f(event.rho),
                 _f(event.volatility), _f(event.price), ts),
            )
            _maybe_commit(state)
            state.last_event_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except Exception as exc:
            logger.warning("Greeks write error: %s", exc)


async def _listen_summary(streamer, state: _State, Summary) -> None:
    now = time.time
    conn = state.conn
    async for event in streamer.listen(Summary):
        if state.stop_event.is_set():
            break
        oi = event.open_interest
        if oi is None:
            continue
        ts = now()
        try:
            conn.execute(
                "INSERT INTO stream_oi (symbol, open_interest, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(symbol) DO UPDATE SET "
                "open_interest=excluded.open_interest, updated_at=excluded.updated_at",
                (event.event_symbol, int(oi), ts),
            )
            _maybe_commit(state)
            state.last_event_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except Exception as exc:
            logger.warning("Summary write error: %s", exc)


async def _poll_subscriptions(streamer, state: _State, symbols: list[str], Trade, Quote, Greeks, Summary) -> None:
    """Periodically refresh subscriptions to pick up newly entered ICs."""
    while not state.stop_event.is_set():
        await asyncio.sleep(_SYMBOL_POLL_S)
        if state.stop_event.is_set():
            break
        try:
            subs = _resolve_subscriptions(symbols)
            await _apply_subscriptions(streamer, state, subs, Trade, Quote, Greeks, Summary)
            if state.last_event_at:
                _upsert_status(state.conn, last_event_at=state.last_event_at)
        except Exception as exc:
            logger.warning("Subscription poll error: %s", exc)


def _current_underlying_price(state: _State, underlying: str) -> float | None:
    """Read the latest underlying price from the stream cache."""
    try:
        row = state.conn.execute(
            "SELECT last FROM stream_trades WHERE symbol = ?", (underlying,)
        ).fetchone()
        return float(row["last"]) if row and row["last"] is not None else None
    except Exception:
        return None


async def _fetch_dte0_chain(underlying: str) -> dict:
    """Fetch the nearest-expiration option chain via REST. Returns {streamer_symbol: option}."""
    from tastytrade.instruments import get_option_chain
    session = get_session()
    chain = await get_option_chain(session, underlying)
    if not chain:
        return {}
    nearest = min(chain.keys(), key=lambda e: abs((e - __import__("datetime").date.today()).days))
    return {o.streamer_symbol: o for o in chain[nearest] if getattr(o, "streamer_symbol", None)}


def _write_chain_to_cache(conn: sqlite3.Connection, option_map: dict) -> None:
    """Persist the option chain structure to stream_cache.db.

    Tags each row with its underlying_symbol (from the option's own data) so
    that lookups can filter by underlying — XSP and SPX chains share the same
    0DTE expiration date, so an expiration-only filter would mix the two.
    """
    import json as _json
    now = time.time()
    rows = []
    for sym, o in option_map.items():
        dump = getattr(o, "model_dump", None)
        data = dump(mode="json") if callable(dump) else {"streamer_symbol": sym}
        rows.append((sym, str(data.get("expiration_date", "")), data.get("underlying_symbol"), _json.dumps(data), now))
    conn.executemany(
        "INSERT INTO stream_chain (streamer_symbol, expiration, underlying_symbol, data_json, updated_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(streamer_symbol) DO UPDATE SET "
        "expiration=excluded.expiration, underlying_symbol=excluded.underlying_symbol, "
        "data_json=excluded.data_json, updated_at=excluded.updated_at",
        rows,
    )
    conn.commit()
    logger.info("Cached %d option chain entries", len(rows))


def _atm_window_syms(option_map: dict, center: float, strike_count: int) -> list[str]:
    """Return streamer symbols within strike_count strikes of center on each side."""
    strikes = sorted({float(o.strike_price) for o in option_map.values()})
    if not strikes:
        return []
    nearest_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - center))
    lo = max(0, nearest_idx - strike_count)
    hi = min(len(strikes), nearest_idx + strike_count + 1)
    keep = set(strikes[lo:hi])
    return [sym for sym, o in option_map.items() if float(o.strike_price) in keep]


async def _symbol_refresher(streamer, state: _State, symbol: str, Quote, Greeks, Summary, Trade) -> None:
    """Fetch symbol's 0DTE chain at startup and keep its subscription window current.

    One instance runs per traded symbol (see _run_stream). Each maintains its own chain,
    window, and center in state.chains/window_syms/centers keyed by symbol — windows for
    different symbols never collide since option streamer symbols are namespaced per
    underlying (e.g. .XSP... vs .SPX...). The window is sized to the wider GEX requirement
    (_WINDOW_STRIKE_COUNT) so the same subscription serves both entry strike selection and
    that symbol's own GEX profile — no separate narrower "trading-only" window needed.

    Also subscribes Trade for the window (alongside Quote/Greeks/Summary) so per-option
    volume is genuinely live rather than the slow-moving average_daily_volume chain-metadata
    field the dashboard/GEX-by-volume used to fall back to — _listen_trade already writes
    any symbol's Trade events (day_volume) to stream_trades keyed by symbol, so no separate
    listener or table is needed here, just widening which symbols get subscribed.
    """
    logger.info("[%s] Fetching 0DTE option chain…", symbol)
    try:
        chain = await _fetch_dte0_chain(symbol)
        state.chains[symbol] = chain
        logger.info("[%s] 0DTE chain loaded: %d options", symbol, len(chain))
        _write_chain_to_cache(state.conn, chain)
    except Exception as exc:
        logger.warning("[%s] Failed to fetch 0DTE chain: %s — window disabled", symbol, exc)
        return

    state.window_syms.setdefault(symbol, [])

    while not state.stop_event.is_set():
        price = _current_underlying_price(state, symbol)

        if price is None:
            # Underlying price not yet available — wait for first Trade event
            await asyncio.sleep(1)
            continue

        center = state.centers.get(symbol)
        if center is None or abs(price - center) >= _WINDOW_REFRESH_PTS:
            new_syms = _atm_window_syms(state.chains[symbol], price, _WINDOW_STRIKE_COUNT)
            current_syms = state.window_syms.get(symbol, [])
            if new_syms != current_syms:
                old_set = set(current_syms)
                new_set = set(new_syms)
                add    = new_set - old_set
                remove = old_set - new_set
                try:
                    if add:
                        await streamer.subscribe(Quote, list(add))
                        await streamer.subscribe(Greeks, list(add))
                        await streamer.subscribe(Summary, list(add))
                        await streamer.subscribe(Trade, list(add))
                    if remove:
                        # Only unsubscribe if not also needed by an open IC leg
                        open_legs = set(_open_trade_streamer_symbols())
                        safe_remove = remove - open_legs
                        if safe_remove:
                            await streamer.unsubscribe(Quote, list(safe_remove))
                            await streamer.unsubscribe(Greeks, list(safe_remove))
                            await streamer.unsubscribe(Summary, list(safe_remove))
                            await streamer.unsubscribe(Trade, list(safe_remove))
                    state.window_syms[symbol] = new_syms
                    _upsert_status(state.conn, subscribed_symbols=_total_subscribed(state))
                    logger.info(
                        "[%s] window re-centered at %.2f (+%d/-%d symbols, total: %d)",
                        symbol, price, len(add), len(remove), len(new_syms),
                    )
                except Exception as exc:
                    logger.warning("[%s] window update error: %s", symbol, exc)
            state.centers[symbol] = price

        await asyncio.sleep(_WINDOW_POLL_S)


async def _flush_status(state: _State) -> None:
    """Periodically write last_event_at to the status table."""
    while not state.stop_event.is_set():
        await asyncio.sleep(5)
        if state.last_event_at:
            try:
                _upsert_status(state.conn, last_event_at=state.last_event_at)
            except Exception:
                pass


async def _watch_stop(state: _State) -> None:
    """Raise CancelledError when the stop event fires to exit the TaskGroup."""
    await state.stop_event.wait()
    raise asyncio.CancelledError("stop requested")


def _f(value) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
        return None if v != v else v  # NaN guard
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Dedicated REST event loop (separate from DXLink loop)
# ---------------------------------------------------------------------------

_REST_POLL_KEYS_STATIC = [
    ("account_info",    "get_account_info",    {"account_number": None}),
    ("positions",       "get_positions",       {"account_number": None}),
    ("working_orders",  "get_working_orders",  {"account_number": None}),
]
_REST_POLL_INTERVAL = 15.0


async def _rest_poller(symbols: list[str]) -> None:
    """Polls REST endpoints on a fixed cadence and writes results to stream_rest_cache.

    Opens its own SQLite connection rather than sharing state.conn with the main DXLink
    event loop thread. The two threads write independently and at different cadences
    (frequent small writes from DXLink listeners vs. a 15s REST poll here); sharing one
    connection meant a write on either thread could block the other's synchronous
    conn.execute() for up to sqlite3's default 5s busy timeout (observed live as a
    "database is locked" warning from this poller — see stall incident 2026-07-02).
    Since the DXLink SDK's connection-keepalive heartbeat runs on that same event loop,
    a long enough block there risks starving it past DXLink's 60s keepaliveTimeout,
    which would silently kill the persistent connection with no client-side exception.
    A dedicated connection removes this cross-thread contention entirely.
    """
    import tt
    conn = _cache_connect()
    fn_map = {
        "get_account_info":    tt.cmd_get_account_info,
        "get_positions":       tt.cmd_get_positions,
        "get_working_orders":  tt.cmd_get_working_orders,
        "get_market_overview": tt.cmd_get_market_overview,
    }
    # VIX is always needed for regime detection regardless of which symbols are traded.
    overview_symbols = list(dict.fromkeys([*symbols, "VIX"]))
    poll_keys = [
        *_REST_POLL_KEYS_STATIC,
        ("market_overview", "get_market_overview", {"symbols": overview_symbols, "quotes_timeout": 4.0}),
    ]
    while True:
        for key, cmd, ns_args in poll_keys:
            fn = fn_map[cmd]
            try:
                result = await fn(argparse.Namespace(**ns_args))
                result["_polled_at"] = time.time()
                _write_rest_cache(conn, key, result)
            except Exception as exc:
                logger.warning("REST poller [%s] error: %s", key, exc)
        await asyncio.sleep(_REST_POLL_INTERVAL)


def _start_rest_loop(symbols: list[str]) -> None:
    """Spin up a dedicated asyncio event loop in a background thread for REST commands."""
    global _rest_loop

    def _run() -> None:
        global _rest_loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _rest_loop = loop
        loop.create_task(_rest_poller(symbols))
        loop.run_forever()

    t = threading.Thread(target=_run, daemon=True, name="streamer-rest-loop")
    t.start()
    # Wait up to 2s for the loop to be ready
    deadline = time.time() + 2.0
    while _rest_loop is None and time.time() < deadline:
        time.sleep(0.01)
    logger.info("REST event loop started (thread: streamer-rest-loop)")


# ---------------------------------------------------------------------------
# HTTP API server
# ---------------------------------------------------------------------------

class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class _ApiHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/api":
            self._respond(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._respond(400, {"ok": False, "error": "invalid JSON"})
            return
        command = body.get("command", "")
        args = body.get("args", {})
        result = self._dispatch(command, args)
        self._respond(200, result)

    def _dispatch(self, command: str, args: dict) -> dict:
        # ── Tier 1: pure sync / stream-cache reads (no event loop) ──────────
        if command == "stream_status":
            import tt
            return tt.cmd_stream_status(argparse.Namespace())

        if command == "get_quote":
            return self._sync_get_quote(args)

        if command == "get_option_chain":
            result = self._sync_get_option_chain(args)
            if result is not None:
                return result
            # partial cache miss — fall through

        if command == "get_strategies":
            result = self._sync_get_strategies(args)
            if result is not None:
                return result

        # ── Tier 2: REST-cache reads (polled every 15s by _rest_poller) ─────
        _REST_CACHE_KEY = {
            "get_account_info":    "account_info",
            "get_positions":       "positions",
            "get_working_orders":  "working_orders",
            "get_market_overview": "market_overview",
        }
        cache_key = _REST_CACHE_KEY.get(command)
        if cache_key is not None:
            conn = self._db()
            try:
                cached = _read_rest_cache(conn, cache_key)
            finally:
                conn.close()
            if cached is not None:
                if command == "get_market_overview" and cached.get("ok") and "metrics" in cached:
                    requested = {s.upper() for s in args.get("symbols", [])}
                    if requested:
                        filtered = [m for m in cached["metrics"] if (m or {}).get("symbol", "").upper() in requested]
                        if len(filtered) == len(requested):
                            result = dict(cached)
                            result["metrics"] = filtered
                            result["source"] = "rest_cache"
                            return result
                        # one or more requested symbols aren't cached yet — fall through to a live call
                    else:
                        cached["source"] = "rest_cache"
                        return cached
                else:
                    cached["source"] = "rest_cache"
                    return cached
            # Cache cold (first poll not yet complete), or requested symbols not covered — fall through to REST loop

        # ── Tier 3: live REST calls via dedicated REST loop ──────────────────
        global _rest_loop
        if _rest_loop is None:
            return {"ok": False, "error": "REST event loop not ready"}
        if command not in _CMD_DEFAULTS:
            return {"ok": False, "error": f"unknown command: {command}"}

        import tt
        fn_map = {
            "get_connection_status": tt.cmd_get_connection_status,
            "list_accounts":         tt.cmd_list_accounts,
            "get_account_info":      tt.cmd_get_account_info,
            "get_positions":         tt.cmd_get_positions,
            "get_market_overview":   tt.cmd_get_market_overview,
            "get_quote":             tt.cmd_get_quote,
            "get_option_chain":      tt.cmd_get_option_chain,
            "get_strategies":        tt.cmd_get_strategies,
            "get_working_orders":    tt.cmd_get_working_orders,
            "execute_trade":         tt.cmd_execute_trade,
            "adjust_order":          tt.cmd_adjust_order,
            "close_position":        tt.cmd_close_position,
            "stream_subscribe":      tt.cmd_stream_subscribe,
            "get_gex":               tt.cmd_get_gex,
        }
        fn = fn_map.get(command)
        if fn is None:
            return {"ok": False, "error": f"unimplemented: {command}"}
        merged = {**_CMD_DEFAULTS.get(command, {}), **args}
        ns = argparse.Namespace(**merged)
        future = asyncio.run_coroutine_threadsafe(fn(ns), _rest_loop)
        try:
            return future.result(timeout=30)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Sync cache readers — bypass asyncio, read SQLite in handler thread
    # ------------------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(_CACHE_DB), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _sync_get_quote(self, args: dict) -> dict:
        sym = args.get("symbol", "").strip().upper()
        now = time.time()
        conn = self._db()
        try:
            row = conn.execute(
                "SELECT last, updated_at FROM stream_trades WHERE symbol = ?", (sym,)
            ).fetchone()
            if row and row["last"] is not None and (now - row["updated_at"]) < 10:
                return {"ok": True, "symbol": sym, "last": float(row["last"]),
                        "source": "stream_cache"}
            return {"ok": True, "symbol": sym, "last": None,
                    "note": "not in stream cache — streamer may not have this symbol subscribed"}
        finally:
            conn.close()

    def _sync_get_option_chain(self, args: dict) -> dict | None:
        """Return chain from cache only; None if any requested data is missing."""
        sym = args.get("symbol", "").strip().upper()
        expiration = args.get("expiration")
        include_quotes = args.get("include_quotes", False)
        include_greeks = args.get("include_greeks", False)
        strike_count = args.get("strike_count", 15)
        around_price = args.get("around_price")
        now = time.time()
        conn = self._db()
        try:
            # Resolve expiration from cache when not supplied
            if not expiration:
                row = conn.execute(
                    "SELECT expiration FROM stream_chain WHERE underlying_symbol = ? "
                    "ORDER BY ABS(JULIANDAY(expiration) - JULIANDAY('now')) LIMIT 1",
                    (sym,),
                ).fetchone()
                if not row:
                    return None
                expiration = row["expiration"]

            chain_rows = conn.execute(
                "SELECT data_json, updated_at FROM stream_chain WHERE expiration = ? AND underlying_symbol = ?",
                (expiration, sym),
            ).fetchall()
            if not chain_rows or (now - min(r["updated_at"] for r in chain_rows)) > 4 * 3600:
                return None

            options = [json.loads(r["data_json"]) for r in chain_rows]

            # ATM window filter
            if strike_count is not None:
                strikes = sorted({float(o.get("strike_price", 0)) for o in options})
                center = around_price if around_price else (strikes[len(strikes)//2] if strikes else None)
                if center and strikes:
                    nearest = min(range(len(strikes)), key=lambda i: abs(strikes[i] - center))
                    keep = set(strikes[max(0, nearest-strike_count): nearest+strike_count+1])
                    options = [o for o in options if float(o.get("strike_price", 0)) in keep]

            # Attach quotes and greeks from cache
            syms = [o["streamer_symbol"] for o in options if o.get("streamer_symbol")]
            quotes_map: dict = {}
            greeks_map: dict = {}
            if include_quotes and syms:
                ph = ",".join("?" * len(syms))
                for row in conn.execute(
                    f"SELECT symbol, bid, ask, mid, updated_at FROM stream_quotes WHERE symbol IN ({ph})",
                    syms,
                ).fetchall():
                    if (now - row["updated_at"]) < 10:
                        quotes_map[row["symbol"]] = dict(row)
                if len(quotes_map) < len(syms):
                    return None  # partial cache — fall back to async

            if include_greeks and syms:
                ph = ",".join("?" * len(syms))
                for row in conn.execute(
                    f"SELECT symbol, delta, gamma, theta, iv, updated_at FROM stream_greeks WHERE symbol IN ({ph})",
                    syms,
                ).fetchall():
                    # Greeks publish far less often than Quotes/Trades on DXLink.
                    if (now - row["updated_at"]) < 2700:
                        greeks_map[row["symbol"]] = dict(row)
                if len(greeks_map) < len(syms):
                    return None

            # Merge into options
            for o in options:
                s = o.get("streamer_symbol")
                if s and s in quotes_map:
                    q = quotes_map[s]
                    o["bid"], o["ask"], o["mid"] = q["bid"], q["ask"], q["mid"]
                if s and s in greeks_map:
                    g = greeks_map[s]
                    o["delta"], o["gamma"], o["theta"], o["iv"] = (
                        g["delta"], g["gamma"], g["theta"], g["iv"])

            return {
                "ok": True, "symbol": sym,
                "chain": {expiration: options},
                "source": "stream_cache",
                "greeks_included": include_greeks,
                "quotes_included": include_quotes,
            }
        finally:
            conn.close()

    def _sync_get_strategies(self, args: dict) -> dict | None:
        """Build an IC candidate from cache; None if greeks or chain are missing."""
        from datetime import date as _date
        sym = args.get("symbol", "").strip().upper()
        wing_width = int(args.get("wing_width", 5))
        short_delta = float(args.get("short_delta", 0.15))
        around_price = args.get("around_price")
        now = time.time()
        conn = self._db()
        try:
            # Nearest expiration from cache
            row = conn.execute(
                "SELECT expiration FROM stream_chain WHERE underlying_symbol = ? "
                "ORDER BY ABS(JULIANDAY(expiration) - JULIANDAY('now')) LIMIT 1",
                (sym,),
            ).fetchone()
            if not row:
                return None
            expiration = row["expiration"]

            chain_rows = conn.execute(
                "SELECT data_json FROM stream_chain WHERE expiration = ? AND underlying_symbol = ?",
                (expiration, sym),
            ).fetchall()
            if not chain_rows:
                return None

            options = [json.loads(r["data_json"]) for r in chain_rows]
            calls = sorted((o for o in options if "C" in o.get("option_type", "")),
                           key=lambda o: float(o.get("strike_price", 0)))
            puts  = sorted((o for o in options if "P" in o.get("option_type", "")),
                           key=lambda o: float(o.get("strike_price", 0)))
            if not calls or not puts:
                return None

            # Load greeks for strike selection
            all_syms = [o["streamer_symbol"] for o in options if o.get("streamer_symbol")]
            ph = ",".join("?" * len(all_syms))
            greeks_map = {}
            for r in conn.execute(
                f"SELECT symbol, delta, updated_at FROM stream_greeks WHERE symbol IN ({ph})",
                all_syms,
            ).fetchall():
                # Greeks update far less often than Quotes/Trades on DXLink — a 10s
                # window would treat every strike as stale and force a non-delta fallback.
                if (now - r["updated_at"]) < 2700:
                    greeks_map[r["symbol"]] = r["delta"]

            def closest_delta(opts, target):
                best, best_diff = None, float("inf")
                for o in opts:
                    d = greeks_map.get(o.get("streamer_symbol"))
                    if d is None:
                        continue
                    diff = abs(float(d) - target)
                    if diff < best_diff:
                        best_diff, best = diff, o
                return best

            def nearest_by_strike(opts, target_strike, exclude_idx=None):
                # wing_width is a point/dollar distance, not a strike-count offset — strike
                # spacing varies by symbol (e.g. $1 near-the-money on XSP vs $5 on SPX), so
                # walking N array slots would silently produce the wrong-width spread.
                best, best_diff = None, float("inf")
                for i, o in enumerate(opts):
                    if i == exclude_idx:
                        continue
                    try:
                        s = float(o.get("strike_price", 0))
                    except (TypeError, ValueError):
                        continue
                    diff = abs(s - target_strike)
                    if diff < best_diff:
                        best_diff, best = diff, o
                return best

            sc = closest_delta(calls, short_delta)
            sp = closest_delta(puts, -short_delta)
            if sc is None or sp is None:
                return None  # no greeks — fall back to async
            sc_idx = calls.index(sc)
            sp_idx = puts.index(sp)
            sc_strike = float(sc.get("strike_price", 0))
            sp_strike = float(sp.get("strike_price", 0))
            lc = nearest_by_strike(calls, sc_strike + wing_width, exclude_idx=sc_idx) or calls[-1]
            lp = nearest_by_strike(puts, sp_strike - wing_width, exclude_idx=sp_idx) or puts[0]

            # Quotes for credit estimate
            leg_syms = [l.get("streamer_symbol") for l in (sp, lp, sc, lc) if l.get("streamer_symbol")]
            ph2 = ",".join("?" * len(leg_syms))
            quotes_map = {}
            for r in conn.execute(
                f"SELECT symbol, bid, ask, mid, updated_at FROM stream_quotes WHERE symbol IN ({ph2})",
                leg_syms,
            ).fetchall():
                if (now - r["updated_at"]) < 10:
                    quotes_map[r["symbol"]] = r["mid"]

            mids = [quotes_map.get(l.get("streamer_symbol")) for l in (sp, lp, sc, lc)]
            net_credit = None
            if all(m is not None for m in mids):
                net_credit = round(float(mids[0]) + float(mids[2]) - float(mids[1]) - float(mids[3]), 4)
            mult = float(sp.get("shares_per_contract") or 100)
            dte = (_date.fromisoformat(expiration) - _date.today()).days

            def _leg(o, mid):
                return {**o, "mid": quotes_map.get(o.get("streamer_symbol"))}

            return {
                "ok": True, "symbol": sym, "strategy": "iron_condor",
                "expiration": expiration, "dte": dte,
                "estimated_pop": round(max(0.0, 1.0 - 2.0 * short_delta), 3),
                "net_credit": net_credit,
                "contract_multiplier": mult,
                "net_credit_per_contract": round(net_credit * mult, 2) if net_credit else None,
                "quotes_complete": all(m is not None for m in mids),
                "greeks_used_for_strike_selection": bool(greeks_map),
                "source": "stream_cache",
                "legs": {
                    "short_put":  _leg(sp, mids[0]),
                    "long_put":   _leg(lp, mids[1]),
                    "short_call": _leg(sc, mids[2]),
                    "long_call":  _leg(lc, mids[3]),
                },
            }
        finally:
            conn.close()

    def _respond(self, code: int, data: dict) -> None:
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass  # suppress per-request access logs


def _start_http_server(state: "_State") -> None:
    global _http_state
    _http_state = state
    try:
        server = _ThreadingHTTPServer(("127.0.0.1", _HTTP_PORT), _ApiHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True, name="streamer-http")
        t.start()
        logger.info("Streamer HTTP API on http://127.0.0.1:%d/api", _HTTP_PORT)
    except OSError as exc:
        logger.warning("Could not start HTTP server on port %d: %s", _HTTP_PORT, exc)


# ---------------------------------------------------------------------------
# Main loop with reconnection
# ---------------------------------------------------------------------------

async def _main_loop(symbols: list[str]) -> None:
    global _loop
    state = _State()
    state.symbols = list(symbols)
    _loop = asyncio.get_running_loop()

    def _handle_signal(sig, frame):
        logger.info("Signal %s received — stopping", sig)
        state.stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    _start_rest_loop(symbols)
    _start_http_server(state)

    # Write PID
    _PID_FILE.parent.mkdir(exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))
    logger.info("Streamer PID %d written to %s", os.getpid(), _PID_FILE)
    logger.info("Trading symbols: %s (±%d strikes each, GEX included)", symbols, _WINDOW_STRIKE_COUNT)

    delay = _RECONNECT_BASE
    while not state.stop_event.is_set():
        try:
            await _run_stream(state, symbols)
            delay = _RECONNECT_BASE
        except asyncio.CancelledError:
            if state.stop_event.is_set():
                break
            logger.warning("Stream cancelled unexpectedly — will reconnect")
        except Exception as exc:
            if state.stop_event.is_set():
                break
            # _run_stream's asyncio.TaskGroup wraps failures in an ExceptionGroup whose own
            # str() is a generic "N sub-exception(s)" summary with no detail — log each real
            # sub-exception (type, message, traceback) so a recurrence is diagnosable instead
            # of a black box (this reconnect fires on effectively every startup; the actual
            # cause has never been visible in logs/streamer.log before now).
            if isinstance(exc, BaseExceptionGroup):
                for i, sub in enumerate(exc.exceptions):
                    logger.warning(
                        "Stream error sub-exception %d/%d: %s",
                        i + 1, len(exc.exceptions),
                        "".join(traceback.format_exception(type(sub), sub, sub.__traceback__)),
                    )
            logger.warning("Stream error: %s — reconnecting in %.0fs", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, _RECONNECT_MAX)
            state.reconnect_count += 1

    _PID_FILE.unlink(missing_ok=True)
    logger.info("Streamer stopped.")


# ---------------------------------------------------------------------------
# CLI: start / stop / status
# ---------------------------------------------------------------------------

def _running_pid() -> int | None:
    if not _PID_FILE.exists():
        return None
    try:
        pid = int(_PID_FILE.read_text().strip())
    except ValueError:
        _PID_FILE.unlink(missing_ok=True)
        return None

    # os.kill(pid, 0) is unreliable on Windows (raises SystemError for some
    # process states). Use psutil when available, else Windows OpenProcess.
    alive = False
    try:
        import psutil  # type: ignore
        alive = psutil.pid_exists(pid)
    except ImportError:
        try:
            import ctypes
            SYNCHRONIZE = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                alive = True
        except Exception:
            try:
                os.kill(pid, 0)
                alive = True
            except PermissionError:
                alive = True
            except (OSError, SystemError):
                alive = False

    if alive:
        return pid
    _PID_FILE.unlink(missing_ok=True)
    return None


def _cmd_status() -> None:
    pid = _running_pid()
    print(json.dumps({"running": pid is not None, "pid": pid}))
    if not _CACHE_DB.exists():
        return
    conn = sqlite3.connect(str(_CACHE_DB))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM stream_status WHERE id = 1").fetchone()
    if row:
        now = time.time()
        qtrades = conn.execute("SELECT COUNT(*) AS n, MAX(updated_at) AS last FROM stream_trades").fetchone()
        qquotes = conn.execute("SELECT COUNT(*) AS n, MAX(updated_at) AS last FROM stream_quotes").fetchone()
        qgreeks = conn.execute("SELECT COUNT(*) AS n, MAX(updated_at) AS last FROM stream_greeks").fetchone()
        conn.close()
        info = dict(row)
        info["trade_symbols"] = qtrades["n"]
        info["quote_symbols"] = qquotes["n"]
        info["greek_symbols"] = qgreeks["n"]
        # info["subscribed_symbols"] (from the stream_status row above) is the total
        # across every traded symbol's window — there's no longer a separate GEX-only
        # channel to report, since every traded symbol's window doubles as its GEX window.
        # Trade prints (underlying fills) are naturally sparse — a healthy connection
        # can go many minutes without one — so staleness must be judged by whichever
        # feed is freshest, not the one that happens to be quietest.
        newest = max(
            x["last"] for x in (qtrades, qquotes, qgreeks) if x["last"] is not None
        ) if any(x["last"] for x in (qtrades, qquotes, qgreeks)) else None
        info["oldest_event_age_s"] = round(now - newest, 1) if newest else None

        # Stale-cache guardrail: flags a silently-dead persistent connection
        # (running=true but no fresh events) — see stall incident 2026-07-01.
        # oldest_event_age_s (numeric, computed above from stream_* updated_at
        # columns) is used rather than the TEXT last_event_at column.
        stale = pid is not None and (info["oldest_event_age_s"] is None or info["oldest_event_age_s"] > 600)
        info["stale_warning"] = stale
        info["stale_age_s"] = info["oldest_event_age_s"]
        print(json.dumps(info, default=str))
    else:
        conn.close()


def _cmd_stop() -> None:
    pid = _running_pid()
    if pid is None:
        print(json.dumps({"ok": False, "error": "Streamer not running"}))
        return
    try:
        os.kill(pid, signal.SIGTERM)
        print(json.dumps({"ok": True, "signal": "SIGTERM", "pid": pid}))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))


def _configured_symbols(cfg: dict, cli_override: list[str] | None = None) -> list[str]:
    """Resolve the traded-symbol list: CLI override > config 'symbols' > deprecated
    single-symbol 'symbol' key (as a one-element list) > default ["XSP"]."""
    if cli_override:
        return [s.strip().upper() for s in cli_override if s.strip()]
    if cfg.get("symbols"):
        return [str(s).strip().upper() for s in cfg["symbols"] if str(s).strip()]
    if cfg.get("symbol"):
        return [str(cfg["symbol"]).strip().upper()]
    return ["XSP"]


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="MEICAgent DXLink streamer daemon")
    parser.add_argument("--stop", action="store_true", help="Stop a running daemon")
    parser.add_argument("--status", action="store_true", help="Print status and exit")
    parser.add_argument("--symbol", action="append", default=None,
                        help="Override a traded symbol (repeatable, e.g. --symbol XSP --symbol SPX). "
                             "Default: 'symbols' (or deprecated 'symbol') from config.json.")
    args = parser.parse_args()

    if args.status:
        _cmd_status()
        return
    if args.stop:
        _cmd_stop()
        return

    existing_pid = _running_pid()
    if existing_pid is not None:
        print(json.dumps({
            "ok": False,
            "error": f"Streamer already running (pid {existing_pid}). "
                     f"Run 'python src/streamer.py --stop' first, or --status to inspect it.",
        }))
        raise SystemExit(1)

    cfg = _load_config()
    symbols = _configured_symbols(cfg, cli_override=args.symbol)

    _setup_logging()
    logger.info("Starting MEICAgent DXLink streamer — symbols: %s", symbols)

    asyncio.run(_main_loop(symbols))


if __name__ == "__main__":
    main()
