"""Tastytrade CLI tool for MEICAgent. All commands print JSON to stdout.

Usage:
  python src/tt.py get_connection_status
  python src/tt.py get_account_info [--account_number X]
  python src/tt.py get_positions [--account_number X]
  python src/tt.py list_accounts
  python src/tt.py get_market_overview --symbols XSP [XSP ...]
  python src/tt.py get_quote --symbol XSP
  python src/tt.py get_option_chain --symbol XSP [--expiration YYYY-MM-DD]
      [--include_greeks] [--include_quotes] [--strike_count N] [--around_price F]
  python src/tt.py get_strategies --symbol XSP [--target_dte N] [--wing_width N]
      [--short_delta F] [--around_price F]
  python src/tt.py get_working_orders [--account_number X]
  python src/tt.py execute_trade --order '<JSON>' [--account_number X] [--live]
  python src/tt.py adjust_order --order_id N --order '<JSON>' [--account_number X] [--live]
  python src/tt.py close_position --order_id N [--account_number X]
  python src/tt.py stream_status
  python src/tt.py stream_subscribe --symbols XSP .XSP260630C745 ...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

# Allow running as `python src/tt.py` from any working directory.
sys.path.insert(0, os.path.dirname(__file__))

import credentials as _creds
import gex_math
from session import get_session

# ---------------------------------------------------------------------------
# Logging — each invocation is a short-lived subprocess whose only normal
# output channel is the JSON printed to stdout, so a failed streamer-HTTP
# fallback (see _try_streamer_http) would otherwise be invisible across
# iterations. Append warnings to a file so degraded-mode calls are auditable.
# ---------------------------------------------------------------------------
_LOG_FILE = Path(__file__).parent.parent / "logs" / "tt.log"
_LOG_FILE.parent.mkdir(exist_ok=True)
logger = logging.getLogger(__name__)
if not logger.handlers:
    logger.setLevel(logging.WARNING)
    # Rotate so the log can't grow without bound (10 MB x 5 backups).
    from logging.handlers import RotatingFileHandler
    _fh = RotatingFileHandler(_LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_fh)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    root = os.path.join(os.path.dirname(__file__), "..")
    path = os.path.join(root, "config.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


_STREAMER_PORT = 7699


_last_streamer_http_error: str | None = None


def _try_streamer_http(command: str, args_dict: dict) -> dict | None:
    """POST command to the streamer HTTP API. Returns parsed JSON or None on failure.

    Sets module-level `_last_streamer_http_error` so callers falling back to the
    slow (cold-import asyncio) path can surface *why* in the response JSON —
    a caller-invisible fallback here previously masked a 34+ hour streamer
    stall (silently-dead persistent connection) behind an unremarkable
    per-call latency bump.
    """
    global _last_streamer_http_error
    import socket
    import urllib.request
    import urllib.error
    body = json.dumps({"command": command, "args": args_dict}).encode()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{_STREAMER_PORT}/api",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        _last_streamer_http_error = None
        return result
    except (ConnectionRefusedError, urllib.error.URLError) as exc:
        # Streamer daemon simply isn't running — normal/expected, not worth a warning.
        _last_streamer_http_error = f"streamer not reachable: {exc}"
        return None
    except (socket.timeout, TimeoutError) as exc:
        # Daemon is running but didn't respond within 5s — the interesting case:
        # the same failure shape as the 2026-07-01 session/event-loop stall.
        _last_streamer_http_error = f"streamer HTTP timeout: {exc}"
        logger.warning("streamer HTTP timeout on command=%s: %s", command, exc)
        return None
    except Exception as exc:
        _last_streamer_http_error = f"streamer HTTP error: {exc}"
        logger.warning("streamer HTTP error on command=%s: %s", command, exc)
        return None


def _live_trading_enabled() -> bool:
    cfg = _load_config()
    if "enable_live_trading" in cfg:
        return bool(cfg["enable_live_trading"])
    return os.environ.get("ENABLE_LIVE_TRADING", "").lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Stream cache helpers
# ---------------------------------------------------------------------------

_CACHE_DB = Path(__file__).parent.parent / "data" / "stream_cache.db"
_CACHE_MAX_AGE = 10.0  # seconds; older data falls back to live DXLink call
# Greeks update far less often than Quotes/Trades on the DXLink feed (server recalculates
# on a slower cadence, not tick-by-tick) — a 10s window means every strike is "stale" by
# the time a request arrives, forcing a crude non-delta fallback for strike selection.
_GREEKS_CACHE_MAX_AGE = 2700.0  # DXLink pushes Greeks in batches roughly every ~20-35+ min for this
# feed, not continuously — measured empirically. 45 min sits just above that observed cadence:
# tighter than a full hour for fresher rescans, while still comfortably covering a normal batch
# gap. Strike selection tolerates this because the actual delta is re-verified as a separate hard
# stop right before entry; this filter mainly guards against stale prior-session rows.


def _cache_conn() -> sqlite3.Connection | None:
    if not _CACHE_DB.exists():
        return None
    try:
        conn = sqlite3.connect(str(_CACHE_DB), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
    except Exception:
        return None


def _cache_get_trade(symbol: str) -> float | None:
    """Return cached last trade price if fresh, else None."""
    conn = _cache_conn()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT last, updated_at FROM stream_trades WHERE symbol = ?", (symbol,)
        ).fetchone()
        if row and row["last"] is not None and (time.time() - row["updated_at"]) < _CACHE_MAX_AGE:
            return float(row["last"])
    except Exception:
        pass
    finally:
        conn.close()
    return None


def _cache_get_quotes(symbols: list[str]) -> dict[str, dict]:
    """Return {symbol: {bid, ask, mid}} for symbols with fresh cache entries."""
    conn = _cache_conn()
    if conn is None:
        return {}
    out: dict[str, dict] = {}
    now = time.time()
    try:
        placeholders = ",".join(["?" for _ in symbols])
        rows = conn.execute(
            f"SELECT symbol, bid, ask, mid, updated_at FROM stream_quotes WHERE symbol IN ({placeholders})",
            symbols,
        ).fetchall()
        for row in rows:
            if (now - row["updated_at"]) < _CACHE_MAX_AGE:
                out[row["symbol"]] = {"bid": row["bid"], "ask": row["ask"], "mid": row["mid"]}
    except Exception:
        pass
    finally:
        conn.close()
    return out


_NOMINAL_WING_PRICE = 0.01  # fallback price for a long wing leg with no quote at all


def _cache_get_quotes_any_age(symbols: list[str]) -> dict[str, dict]:
    """Like _cache_get_quotes but ignores staleness — for deep-OTM long wing legs that
    trade so rarely their last quote can be minutes old even though it's still the best
    information available (a leg nobody is trading isn't repricing meaningfully anyway).
    """
    conn = _cache_conn()
    if conn is None:
        return {}
    out: dict[str, dict] = {}
    try:
        placeholders = ",".join(["?" for _ in symbols])
        rows = conn.execute(
            f"SELECT symbol, bid, ask, mid FROM stream_quotes WHERE symbol IN ({placeholders})",
            symbols,
        ).fetchall()
        for row in rows:
            out[row["symbol"]] = {"bid": row["bid"], "ask": row["ask"], "mid": row["mid"]}
    except Exception:
        pass
    finally:
        conn.close()
    return out


def _cache_get_chain(expiration: str, symbol: str | None = None) -> list | None:
    """Return cached option chain for a specific expiration date, or None if stale/missing.

    Chain structure is stable during a trading day so we use a 4-hour TTL.
    Returns a list of _CachedOption objects matching the SDK Option interface.
    `symbol` filters to one underlying — XSP and SPX share the same 0DTE
    expiration date, so an expiration-only lookup would mix both chains.
    """
    conn = _cache_conn()
    if conn is None:
        return None
    try:
        if symbol:
            rows = conn.execute(
                "SELECT data_json, updated_at FROM stream_chain WHERE expiration = ? AND underlying_symbol = ?",
                (expiration, symbol.upper()),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT data_json, updated_at FROM stream_chain WHERE expiration = ?",
                (expiration,),
            ).fetchall()
        if not rows:
            return None
        age = time.time() - min(r["updated_at"] for r in rows)
        if age > 4 * 3600:  # 4-hour TTL
            return None
        return [_CachedOption(json.loads(r["data_json"])) for r in rows]
    except Exception:
        return None
    finally:
        conn.close()


class _CachedOption:
    """Lightweight wrapper around a cached option JSON dict.

    Provides attribute access matching the tastytrade SDK Option model so
    downstream code (strike selection, serialization, greeks merging) works
    identically whether the option came from REST or the cache.
    """

    __slots__ = ("_data",)

    def __init__(self, data: dict) -> None:
        object.__setattr__(self, "_data", data)

    def __getattr__(self, name: str):
        try:
            return object.__getattribute__(self, "_data")[name]
        except KeyError:
            raise AttributeError(name)

    def model_dump(self, mode: str = "json") -> dict:
        return object.__getattribute__(self, "_data")


def _cache_get_oi(symbols: list[str]) -> dict[str, int]:
    """Return {symbol: open_interest} for symbols present in stream_oi (no age filter — OI is daily)."""
    conn = _cache_conn()
    if conn is None:
        return {}
    out: dict[str, int] = {}
    try:
        placeholders = ",".join(["?" for _ in symbols])
        rows = conn.execute(
            f"SELECT symbol, open_interest FROM stream_oi WHERE symbol IN ({placeholders})",
            symbols,
        ).fetchall()
        for row in rows:
            if row["open_interest"] is not None:
                out[row["symbol"]] = int(row["open_interest"])
    except Exception:
        pass
    finally:
        conn.close()
    return out


def _cache_get_greeks(symbols: list[str]) -> dict[str, dict]:
    """Return {symbol: {delta, gamma, theta, iv}} for symbols with fresh cache entries."""
    conn = _cache_conn()
    if conn is None:
        return {}
    out: dict[str, dict] = {}
    now = time.time()
    try:
        placeholders = ",".join(["?" for _ in symbols])
        rows = conn.execute(
            f"SELECT symbol, delta, gamma, theta, vega, rho, iv, price, updated_at "
            f"FROM stream_greeks WHERE symbol IN ({placeholders})",
            symbols,
        ).fetchall()
        for row in rows:
            if (now - row["updated_at"]) < _GREEKS_CACHE_MAX_AGE:
                out[row["symbol"]] = {
                    "delta": row["delta"], "gamma": row["gamma"],
                    "theta": row["theta"], "vega": row["vega"],
                    "rho": row["rho"], "iv": row["iv"], "price": row["price"],
                }
    except Exception:
        pass
    finally:
        conn.close()
    return out


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _out(data: Any) -> None:
    print(json.dumps(data, default=str))


def _error(exc: Exception) -> dict:
    msg = f"{type(exc).__name__}: {exc}"
    retryable = any(f" {c} " in str(exc) or str(exc).endswith(str(c)) for c in (500, 502, 503, 504))
    result: dict = {"ok": False, "error": msg}
    if retryable:
        result["retryable"] = True
    return result


def _serialize(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_serialize(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return str(obj)


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _get_account(account_number: str | None = None):
    from tastytrade.account import Account
    session = get_session()
    number = account_number or _creds.get_secret(_creds.ACCOUNT_NUMBER)
    if number:
        return await Account.get(session, number)
    accounts = await Account.get(session)
    if not accounts:
        raise RuntimeError("No accounts found for these credentials.")
    return accounts[0]


async def _fetch_chain(symbol: str, expiration: str | None = None) -> dict:
    """Fetch option chain, checking the stream cache before making a REST call.

    When `expiration` is provided and matches a cached expiration, the REST call
    is skipped entirely and the cached chain is returned (sub-millisecond).
    When `expiration` is None, the nearest cached expiration is tried first;
    if the cache is empty or stale the full chain is fetched via REST.
    """
    from tastytrade.instruments import get_option_chain

    if not symbol.startswith("/"):
        # Try cache first
        target_exp = expiration
        if target_exp is None:
            # Discover what expiration is cached (nearest to today) for this underlying
            conn = _cache_conn()
            if conn is not None:
                try:
                    row = conn.execute(
                        "SELECT expiration FROM stream_chain WHERE underlying_symbol = ? "
                        "ORDER BY ABS(JULIANDAY(expiration) - JULIANDAY('now')) LIMIT 1",
                        (symbol.upper(),),
                    ).fetchone()
                    if row:
                        target_exp = row["expiration"]
                finally:
                    conn.close()

        if target_exp:
            cached = _cache_get_chain(target_exp, symbol=symbol)
            if cached:
                exp_date = date.fromisoformat(target_exp)
                return {exp_date: cached}

    # Cache miss or futures symbol — fall back to REST
    session = get_session()
    if symbol.startswith("/"):
        from tastytrade.instruments import get_future_option_chain
        return await get_future_option_chain(session, symbol)
    return await get_option_chain(session, symbol)


def _nearest_expiration(expirations: list[date], target_days: int = 0) -> date:
    today = date.today()
    return min(expirations, key=lambda e: abs((e - today).days - target_days))


def _strike(option: Any) -> float | None:
    try:
        return float(option.strike_price)
    except (TypeError, ValueError):
        return None


def _atm_window(options: list, strike_count: int, around_price: float | None) -> list:
    strikes = sorted({s for s in (_strike(o) for o in options) if s is not None})
    if not strikes:
        return options
    center = around_price if around_price is not None else strikes[len(strikes) // 2]
    nearest = min(range(len(strikes)), key=lambda i: abs(strikes[i] - center))
    lo = max(0, nearest - strike_count)
    hi = min(len(strikes), nearest + strike_count + 1)
    keep = set(strikes[lo:hi])
    return [o for o in options if _strike(o) in keep]


async def _collect_events(event_cls, symbols: list[str], timeout: float,
                          extract=None, label: str = "events") -> dict:
    from tastytrade import DXLinkStreamer
    out: dict = {}
    symbols = [s for s in symbols if s]
    if not symbols:
        return out

    async def _drain(streamer):
        remaining = set(symbols)
        async for event in streamer.listen(event_cls):
            value = extract(event) if extract else event
            if value is not None:
                out[event.event_symbol] = value
            remaining.discard(event.event_symbol)
            if not remaining:
                return

    try:
        session = get_session()
        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(event_cls, symbols)
            await asyncio.wait_for(_drain(streamer), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    except Exception:
        pass
    return out


async def _collect_greeks(symbols: list[str], timeout: float) -> dict:
    from tastytrade.dxfeed import Greeks
    return await _collect_events(Greeks, symbols, timeout, label="greeks")


async def _collect_last_prices(symbols: list[str], timeout: float) -> dict:
    from tastytrade.dxfeed import Trade
    return await _collect_events(Trade, symbols, timeout,
                                 extract=lambda e: _num(e.price), label="last-price")


async def _collect_quotes(symbols: list[str], timeout: float) -> dict:
    from tastytrade.dxfeed import Quote
    return await _collect_events(Quote, symbols, timeout, label="quotes")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def cmd_get_connection_status(_args) -> dict:
    status: dict = {
        "ok": True,
        "live_trading_enabled": _live_trading_enabled(),
        "credentials_present": _creds.secrets_present(),
    }
    if not status["credentials_present"]:
        status["ok"] = False
        status["hint"] = "Run `python src/tt.py secrets_set` to store credentials."
        return status
    try:
        from tastytrade.account import Account
        session = get_session()
        accounts = await Account.get(session)
        status["connected"] = True
        status["account_count"] = len(accounts)
    except Exception as exc:
        status["ok"] = False
        status["connected"] = False
        status.update(_error(exc))
    return status


async def cmd_get_account_info(args) -> dict:
    try:
        account = await _get_account(getattr(args, "account_number", None))
        session = get_session()
        balances = await account.get_balances(session)
        return {
            "ok": True,
            "account_number": account.account_number,
            "nickname": getattr(account, "nickname", None),
            "balances": _serialize(balances),
        }
    except Exception as exc:
        return _error(exc)


async def cmd_get_positions(args) -> dict:
    try:
        account = await _get_account(getattr(args, "account_number", None))
        session = get_session()
        positions = await account.get_positions(session)
        return {
            "ok": True,
            "account_number": account.account_number,
            "positions": _serialize(positions),
        }
    except Exception as exc:
        return _error(exc)


async def cmd_list_accounts(_args) -> dict:
    try:
        from tastytrade.account import Account
        session = get_session()
        accounts = await Account.get(session)
        return {
            "ok": True,
            "accounts": [
                {
                    "account_number": a.account_number,
                    "nickname": getattr(a, "nickname", None),
                    "account_type": getattr(a, "account_type_name", None),
                }
                for a in accounts
            ],
        }
    except Exception as exc:
        return _error(exc)


async def cmd_get_market_overview(args) -> dict:
    try:
        from tastytrade.metrics import get_market_metrics
        session = get_session()
        upper = [s.upper() for s in args.symbols]
        futures = [s for s in upper if s.startswith("/")]
        if futures:
            return {"ok": False, "error": f"get_market_overview does not support futures: {', '.join(futures)}"}
        timeout = getattr(args, "quotes_timeout", 4.0)
        metrics, trades = await asyncio.gather(
            get_market_metrics(session, upper),
            _collect_last_prices(upper, timeout),
        )
        serialized = _serialize(metrics)
        for entry in serialized:
            if not isinstance(entry, dict):
                continue
            sym = entry.get("symbol", "")
            last = trades.get(sym)
            if last is not None:
                entry["last"] = last
        return {"ok": True, "metrics": serialized}
    except Exception as exc:
        return _error(exc)


async def cmd_get_vix1d(args) -> dict:
    """Live VIX1D (CBOE S&P 500 1-Day Volatility Index) via a one-off DXLink
    Trade subscription -- not part of the streamer daemon's persistent
    subscription window (it's a regime-gate input, not a traded/managed
    symbol), so this always goes through this project's own live fetch
    rather than the streamer HTTP cache-only path. Verified live 2026-07-06:
    VIX1D streams real Trade events (confirmed price ~8.73 that day) even
    though it has no live Quote/bid-ask (expected -- it's a calculated index
    with no order book, only a Trade tick).

    Intended as a more precise intraday-regime signal for 0DTE-specific
    decisions than the standard 30-day VIX (which measures a much longer
    volatility window than a same-day option actually spans) -- not yet
    wired into any entry gate; CLAUDE.md's regime detection still uses
    standard VIX only. See CLAUDE.md for how this might be incorporated.
    """
    try:
        timeout = getattr(args, "timeout", 6.0)
        trades = await _collect_last_prices(["VIX1D"], timeout)
        last = trades.get("VIX1D")
        result: dict = {"ok": True, "symbol": "VIX1D", "last": last}
        if last is None:
            result["note"] = "DXLink trade feed did not respond within the timeout."
        return result
    except Exception as exc:
        return _error(exc)


async def cmd_get_quote(args) -> dict:
    try:
        sym = args.symbol.strip().upper()
        timeout = getattr(args, "timeout", 6.0)

        # Fast path: stream cache (sub-millisecond)
        cached = _cache_get_trade(sym)
        if cached is not None:
            return {"ok": True, "symbol": sym, "last": cached, "source": "stream_cache"}

        session = get_session()
        active_contract = None
        streamer_symbol = sym

        if sym.startswith("/"):
            from tastytrade.instruments import Future, get_future_option_chain
            chain = await get_future_option_chain(session, sym)
            if not chain:
                return {"ok": False, "error": f"No futures option chain for {sym}."}
            first_opts = next(iter(chain.values()), [])
            active_contract = getattr(first_opts[0], "underlying_symbol", None) if first_opts else None
            if not active_contract:
                return {"ok": False, "error": f"Cannot determine active contract for {sym}."}
            future = await Future.get(session, active_contract)
            streamer_symbol = future.streamer_symbol

        trades = await _collect_last_prices([streamer_symbol], timeout)
        last = trades.get(streamer_symbol)
        result: dict = {"ok": True, "symbol": sym, "last": last}
        if active_contract:
            result["active_contract"] = active_contract
            result["streamer_symbol"] = streamer_symbol
        if last is None:
            result["note"] = "DXLink trade feed did not respond within the timeout."
        return result
    except Exception as exc:
        return _error(exc)


async def cmd_get_option_chain(args) -> dict:
    try:
        expiration_filter = getattr(args, "expiration", None)
        chain = await _fetch_chain(args.symbol.upper(), expiration=expiration_filter)
        if not chain:
            return {"ok": False, "error": f"No option chain for {args.symbol}."}

        expirations = sorted(chain.keys())
        include_greeks = getattr(args, "include_greeks", False)
        include_quotes = getattr(args, "include_quotes", False)
        strike_count = getattr(args, "strike_count", 15)
        around_price = getattr(args, "around_price", None)
        greeks_timeout = getattr(args, "greeks_timeout", 6.0)
        quotes_timeout = getattr(args, "quotes_timeout", 6.0)

        if expiration_filter:
            want = date.fromisoformat(expiration_filter)
            if want not in chain:
                return {
                    "ok": False,
                    "error": f"No {expiration_filter} expiration for {args.symbol}.",
                    "available_expirations": [str(e) for e in expirations],
                }
            selected = [want]
        elif include_greeks or include_quotes:
            selected = [_nearest_expiration(expirations)]
        else:
            selected = expirations

        options_by_exp = {exp: list(chain[exp]) for exp in selected}
        if strike_count is not None:
            options_by_exp = {
                exp: _atm_window(opts, strike_count, around_price)
                for exp, opts in options_by_exp.items()
            }

        serialized = {
            str(exp): [_serialize(o) for o in opts]
            for exp, opts in options_by_exp.items()
        }

        result: dict = {"ok": True, "symbol": args.symbol.upper(), "chain": serialized}

        streamer_symbols = [
            o.streamer_symbol
            for opts in options_by_exp.values()
            for o in opts
            if getattr(o, "streamer_symbol", None)
        ]

        # Prefer stream cache; fall back to live DXLink for missing symbols
        cached_quotes = _cache_get_quotes(streamer_symbols) if include_quotes else {}
        cached_greeks = _cache_get_greeks(streamer_symbols) if include_greeks else {}

        missing_quote_syms = [s for s in streamer_symbols if s not in cached_quotes] if include_quotes else []
        missing_greek_syms = [s for s in streamer_symbols if s not in cached_greeks] if include_greeks else []

        greeks: dict = dict(cached_greeks)
        quotes: dict = dict(cached_quotes)

        if missing_quote_syms and missing_greek_syms:
            live_q, live_g = await asyncio.gather(
                _collect_quotes(missing_quote_syms, quotes_timeout),
                _collect_greeks(missing_greek_syms, greeks_timeout),
            )
            quotes.update(live_q)
            greeks.update(live_g)
        elif missing_quote_syms:
            quotes.update(await _collect_quotes(missing_quote_syms, quotes_timeout))
        elif missing_greek_syms:
            greeks.update(await _collect_greeks(missing_greek_syms, greeks_timeout))

        if include_greeks:
            received = 0
            for entries in serialized.values():
                for entry in entries:
                    g = greeks.get(entry.get("streamer_symbol"))
                    if g is not None:
                        # g is a dict (from cache) or a Greeks event object (from live DXLink)
                        if isinstance(g, dict):
                            entry["delta"] = _num(g.get("delta"))
                            entry["gamma"] = _num(g.get("gamma"))
                            entry["theta"] = _num(g.get("theta"))
                            entry["iv"]    = _num(g.get("iv"))
                        else:
                            entry["delta"] = _num(g.delta)
                            entry["gamma"] = _num(g.gamma)
                            entry["theta"] = _num(g.theta)
                            entry["iv"]    = _num(g.volatility)
                        received += 1
            result["greeks_included"] = True
            result["greeks_complete"] = received == len(streamer_symbols)
            result["greeks_received"] = received

        if include_quotes:
            received = 0
            for entries in serialized.values():
                for entry in entries:
                    q = quotes.get(entry.get("streamer_symbol"))
                    if q is not None:
                        # q is a dict (from cache) or a Quote event object (from live DXLink)
                        if isinstance(q, dict):
                            bid = _num(q.get("bid"))
                            ask = _num(q.get("ask"))
                        else:
                            bid = _num(q.bid_price)
                            ask = _num(q.ask_price)
                        entry["bid"] = bid
                        entry["ask"] = ask
                        entry["mid"] = round((bid + ask) / 2, 4) if bid is not None and ask is not None else None
                        received += 1
            result["quotes_included"] = True
            result["quotes_complete"] = received == len(streamer_symbols)
            result["quotes_received"] = received

        return result
    except Exception as exc:
        return _error(exc)


async def cmd_get_strategies(args) -> dict:
    try:
        symbol = args.symbol.upper()
        target_dte = getattr(args, "target_dte", 0)
        wing_width = getattr(args, "wing_width", 5)
        short_delta = getattr(args, "short_delta", 0.15)
        around_price = getattr(args, "around_price", None)
        greeks_timeout = getattr(args, "greeks_timeout", 6.0)
        quotes_timeout = getattr(args, "quotes_timeout", 6.0)

        # For 0DTE (target_dte=0) hint the cache with today's date so REST is skipped
        exp_hint = date.today().isoformat() if target_dte == 0 else None
        chain = await _fetch_chain(symbol, expiration=exp_hint)
        if not chain:
            return {"ok": False, "error": f"No option chain for {symbol}."}

        expirations = sorted(chain.keys())
        expiration = _nearest_expiration(expirations, target_dte)
        options = list(chain[expiration])

        calls = sorted((o for o in options if _is_call(o)), key=lambda o: float(o.strike_price))
        puts = sorted((o for o in options if _is_put(o)), key=lambda o: float(o.strike_price))
        if not calls or not puts:
            return {"ok": False, "error": f"Incomplete chain for {symbol} {expiration}."}

        window = _atm_window(options, strike_count=40, around_price=around_price)
        window_symbols = [sym for o in window if (sym := getattr(o, "streamer_symbol", None))]

        # Prefer stream cache for greeks; live DXLink for missing symbols
        cached_g = _cache_get_greeks(window_symbols)
        missing_g = [s for s in window_symbols if s not in cached_g]
        live_g = await _collect_greeks(missing_g, greeks_timeout) if missing_g else {}
        # Normalize live Greeks events to dicts matching cache format
        greeks = {**{s: {"delta": _num(v.delta), "gamma": _num(v.gamma),
                         "theta": _num(v.theta), "iv": _num(v.volatility)}
                     for s, v in live_g.items()}, **cached_g}

        short_call, long_call = _select_call_spread(calls, wing_width, short_delta, greeks)
        short_put, long_put = _select_put_spread(puts, wing_width, short_delta, greeks)
        greeks_used = bool(greeks)

        estimated_pop = round(max(0.0, 1.0 - 2.0 * short_delta), 3)

        legs = [short_put, long_put, short_call, long_call]
        streamer_symbols = [getattr(leg, "streamer_symbol", None) for leg in legs]

        # Prefer stream cache for quotes
        cached_q = _cache_get_quotes([s for s in streamer_symbols if s])
        missing_q = [s for s in streamer_symbols if s and s not in cached_q]
        live_q = await _collect_quotes(missing_q, quotes_timeout) if missing_q else {}
        # Normalize live Quote events to dicts
        quotes = {**{s: {"bid": _num(v.bid_price), "ask": _num(v.ask_price),
                         "mid": round((_num(v.bid_price) + _num(v.ask_price)) / 2, 4)
                               if _num(v.bid_price) is not None and _num(v.ask_price) is not None else None}
                     for s, v in live_q.items()}, **cached_q}

        def _mid(leg):
            sym = getattr(leg, "streamer_symbol", None)
            q = quotes.get(sym) if sym else None
            if q is None:
                return None
            if isinstance(q, dict):
                bid, ask = _num(q.get("bid")), _num(q.get("ask"))
            else:
                bid, ask = _num(q.bid_price), _num(q.ask_price)
            return round((bid + ask) / 2, 4) if bid is not None and ask is not None else None

        short_put_mid = _mid(short_put)
        long_put_mid = _mid(long_put)
        short_call_mid = _mid(short_call)
        long_call_mid = _mid(long_call)

        # Long wing legs (bought for protection, not sold for credit) trade so rarely
        # that a fresh quote often never arrives — their last-known price (even minutes
        # stale) or a nominal $0.01 is a fine stand-in, since a contract nobody trades
        # isn't repricing meaningfully anyway. Short legs keep the strict fresh-quote
        # requirement below since real fill/premium accuracy matters there.
        long_wing_fallback_used = False
        if long_put_mid is None or long_call_mid is None:
            stale_syms = [s for leg, mid in ((long_put, long_put_mid), (long_call, long_call_mid))
                          if mid is None and (s := getattr(leg, "streamer_symbol", None))]
            stale_q = _cache_get_quotes_any_age(stale_syms) if stale_syms else {}
            if long_put_mid is None:
                sym = getattr(long_put, "streamer_symbol", None)
                q = stale_q.get(sym) if sym else None
                bid, ask = (_num(q.get("bid")), _num(q.get("ask"))) if q else (None, None)
                long_put_mid = round((bid + ask) / 2, 4) if bid is not None and ask is not None else _NOMINAL_WING_PRICE
                long_wing_fallback_used = True
            if long_call_mid is None:
                sym = getattr(long_call, "streamer_symbol", None)
                q = stale_q.get(sym) if sym else None
                bid, ask = (_num(q.get("bid")), _num(q.get("ask"))) if q else (None, None)
                long_call_mid = round((bid + ask) / 2, 4) if bid is not None and ask is not None else _NOMINAL_WING_PRICE
                long_wing_fallback_used = True

        mids = [short_put_mid, long_put_mid, short_call_mid, long_call_mid]
        net_credit = None
        multiplier = float(getattr(short_put, "shares_per_contract", None) or
                           getattr(short_put, "multiplier", None) or 100)
        if all(m is not None for m in mids):
            net_credit = round(
                (short_put_mid + short_call_mid) - (long_put_mid + long_call_mid), 4
            )

        def _leg(option, mid):
            data = _serialize(option)
            base = data if isinstance(data, dict) else {"symbol": str(data)}
            return {**base, "mid": mid}

        return {
            "ok": True,
            "symbol": symbol,
            "strategy": "iron_condor",
            "expiration": str(expiration),
            "dte": (expiration - date.today()).days,
            "estimated_pop": estimated_pop,
            "net_credit": net_credit,
            "contract_multiplier": multiplier,
            "net_credit_per_contract": round(net_credit * multiplier, 2) if net_credit is not None else None,
            "quotes_complete": short_put_mid is not None and short_call_mid is not None,
            "long_wing_fallback_used": long_wing_fallback_used,
            "greeks_used_for_strike_selection": greeks_used,
            "legs": {
                "short_put": _leg(short_put, short_put_mid),
                "long_put": _leg(long_put, long_put_mid),
                "short_call": _leg(short_call, short_call_mid),
                "long_call": _leg(long_call, long_call_mid),
            },
        }
    except Exception as exc:
        return _error(exc)


def _is_call(option) -> bool:
    ot = str(getattr(option, "option_type", "")).lower()
    return "c" in ot and "p" not in ot


def _is_put(option) -> bool:
    ot = str(getattr(option, "option_type", "")).lower()
    return "p" in ot


def _closest_by_delta(options, target_delta: float, greeks: dict):
    best = None
    best_diff = float("inf")
    for o in options:
        sym = getattr(o, "streamer_symbol", None)
        g = greeks.get(sym) if sym else None
        if g is None:
            continue
        delta = _num(g.get("delta") if isinstance(g, dict) else getattr(g, "delta", None))
        if delta is None:
            continue
        diff = abs(delta - target_delta)
        if diff < best_diff:
            best_diff = diff
            best = o
    return best


def _nearest_by_strike(options, target_strike: float, exclude_idx: int | None = None):
    """Find the option whose strike is closest to target_strike (a dollar/point level,
    not an index offset). Excludes exclude_idx so the long leg can't collapse onto the
    short leg's own strike when the chain doesn't extend far enough.
    """
    best = None
    best_diff = float("inf")
    for i, o in enumerate(options):
        if i == exclude_idx:
            continue
        s = _strike(o)
        if s is None:
            continue
        diff = abs(s - target_strike)
        if diff < best_diff:
            best_diff = diff
            best = o
    return best


def _select_call_spread(calls, wing_width, short_delta, greeks):
    if greeks:
        best = _closest_by_delta(calls, short_delta, greeks)
        if best is not None:
            idx = calls.index(best)
            target = _strike(best) + wing_width
            long_call = _nearest_by_strike(calls, target, exclude_idx=idx) or calls[-1]
            return best, long_call
    idx = max(0, int(len(calls) * 0.66) - 1)
    target = _strike(calls[idx]) + wing_width
    long_call = _nearest_by_strike(calls, target, exclude_idx=idx) or calls[-1]
    return calls[idx], long_call


def _select_put_spread(puts, wing_width, short_delta, greeks):
    if greeks:
        best = _closest_by_delta(puts, -short_delta, greeks)
        if best is not None:
            idx = puts.index(best)
            target = _strike(best) - wing_width
            long_put = _nearest_by_strike(puts, target, exclude_idx=idx) or puts[0]
            return best, long_put
    idx = min(len(puts) - 1, int(len(puts) * 0.33))
    target = _strike(puts[idx]) - wing_width
    long_put = _nearest_by_strike(puts, target, exclude_idx=idx) or puts[0]
    return puts[idx], long_put


async def cmd_get_working_orders(args) -> dict:
    try:
        account = await _get_account(getattr(args, "account_number", None))
        session = get_session()
        orders = await account.get_live_orders(session)
        return {
            "ok": True,
            "account_number": account.account_number,
            "orders": _serialize(orders),
        }
    except Exception as exc:
        return _error(exc)


# ---------------------------------------------------------------------------
# Order tools (gated behind live trading)
# ---------------------------------------------------------------------------

_ACTION_MAP = {
    "buy to open": "BUY_TO_OPEN",
    "sell to open": "SELL_TO_OPEN",
    "buy to close": "BUY_TO_CLOSE",
    "sell to close": "SELL_TO_CLOSE",
}


def _build_order(spec: dict):
    from tastytrade.order import NewOrder, OrderAction, OrderTimeInForce, OrderType, Leg

    tif = OrderTimeInForce(str(spec.get("time_in_force", "Day")))
    otype = OrderType(str(spec.get("order_type", "Limit")))
    legs = []
    for leg in spec.get("legs", []):
        action = OrderAction[_ACTION_MAP[str(leg["action"]).strip().lower()]]
        legs.append(Leg(
            instrument_type=leg["instrument_type"],
            symbol=leg["symbol"],
            action=action,
            quantity=Decimal(str(leg["quantity"])),
        ))
    kwargs: dict = {"time_in_force": tif, "order_type": otype, "legs": legs}
    if spec.get("price") is not None:
        price = Decimal(str(spec["price"]))
        effect = spec.get("price_effect")
        if effect is not None:
            magnitude = abs(price)
            price = -magnitude if str(effect).strip().lower() == "credit" else magnitude
        kwargs["price"] = price
    if spec.get("stop_trigger") is not None:
        kwargs["stop_trigger"] = Decimal(str(spec["stop_trigger"]))
    return NewOrder(**kwargs)


async def cmd_execute_trade(args) -> dict:
    if not _live_trading_enabled() and not getattr(args, "dry_run", True):
        return {"ok": False, "error": "Live trading is disabled. Set enable_live_trading=true in config.json."}
    try:
        spec = json.loads(args.order)
        account = await _get_account(getattr(args, "account_number", None))
        session = get_session()
        order = _build_order(spec)
        dry_run = getattr(args, "dry_run", True) or not _live_trading_enabled()

        preflight = await account.place_order(session, order, dry_run=True)
        errors = [str(e) for e in (getattr(preflight, "errors", None) or [])]
        warnings = [str(w) for w in (getattr(preflight, "warnings", None) or [])]
        bpe = getattr(preflight, "buying_power_effect", None)
        bp_summary: dict = {"warnings": warnings}
        if bpe:
            bp_summary.update({
                "current_buying_power": str(getattr(bpe, "current_buying_power", None)),
                "new_buying_power": str(getattr(bpe, "new_buying_power", None)),
                "change_in_buying_power": str(getattr(bpe, "change_in_buying_power", None)),
            })
        if errors:
            return {"ok": False, "error": "pre-flight validation failed", "problems": errors, "buying_power": bp_summary}

        if dry_run:
            return {"ok": True, "dry_run": True, "account_number": account.account_number,
                    "buying_power": bp_summary, "response": _serialize(preflight)}

        response = await account.place_order(session, order, dry_run=False)
        return {"ok": True, "dry_run": False, "account_number": account.account_number,
                "buying_power": bp_summary, "response": _serialize(response)}
    except Exception as exc:
        return _error(exc)


async def cmd_adjust_order(args) -> dict:
    if not _live_trading_enabled():
        return {"ok": False, "error": "Live trading is disabled. Set enable_live_trading=true in config.json."}
    try:
        spec = json.loads(args.order)
        account = await _get_account(getattr(args, "account_number", None))
        session = get_session()
        order = _build_order(spec)
        dry_run = getattr(args, "dry_run", True)

        preflight = await account.replace_order(session, args.order_id, order, dry_run=True)
        errors = [str(e) for e in (getattr(preflight, "errors", None) or [])]
        if errors:
            return {"ok": False, "error": "pre-flight validation failed", "problems": errors}

        if dry_run:
            return {"ok": True, "dry_run": True, "order_id": args.order_id, "response": _serialize(preflight)}

        response = await account.replace_order(session, args.order_id, order, dry_run=False)
        return {"ok": True, "dry_run": False, "order_id": args.order_id, "response": _serialize(response)}
    except Exception as exc:
        return _error(exc)


async def cmd_close_position(args) -> dict:
    if not _live_trading_enabled():
        return {"ok": False, "error": "Live trading is disabled. Set enable_live_trading=true in config.json."}
    try:
        account = await _get_account(getattr(args, "account_number", None))
        session = get_session()
        await account.delete_order(session, args.order_id)
        return {"ok": True, "cancelled_order_id": args.order_id}
    except Exception as exc:
        return _error(exc)


# ---------------------------------------------------------------------------
# Stream commands (sync — no async needed)
# ---------------------------------------------------------------------------

def cmd_get_calendar(args) -> dict:
    """Shared market calendar for a year (single source of truth from cherrypit.calendar):
    NYSE holidays, FOMC days, quarterly + triple-witching expiries. Pure computation, no broker."""
    import datetime as _dt
    from cherrypit import calendar as _cal
    year = args.year or _dt.date.today().year
    iso = lambda ds: [d.isoformat() for d in ds]
    return {
        "ok": True,
        "year": year,
        "nyse_holidays": iso(sorted(_cal.nyse_holidays(year))),
        "fomc_dates": iso(_cal.fomc_dates(year)),
        "fomc_year_known": _cal.fomc_year_known(year),
        "quarterly_expiry_dates": iso(_cal.quarterly_expiry_dates(year)),
        "triple_witching_dates": iso(_cal.triple_witching_dates(year)),
    }


def cmd_secrets_status(_args) -> dict:
    from credentials import secrets_status
    status = secrets_status()
    return {
        "ok": True,
        "secrets": {k: ("set" if v else "missing") for k, v in status.items()},
        "ready": all(status[k] for k in ("client_secret", "refresh_token")),
    }


def cmd_secrets_set(args) -> dict:
    """Interactively prompt for and store credentials in the OS keyring."""
    import getpass
    from credentials import ALL_SECRETS, REQUIRED_SECRETS, set_secret, get_secret

    label = {
        "client_secret":  "Client Secret",
        "refresh_token":  "Refresh Token",
        "account_number": "Account Number (optional — press Enter to skip)",
    }
    updated = []
    skipped = []

    keys_to_set = args.keys if hasattr(args, "keys") and args.keys else list(ALL_SECRETS)

    for key in keys_to_set:
        current = get_secret(key)
        hint = " [already set — press Enter to keep]" if current else ""
        prompt = f"  {label.get(key, key)}{hint}: "
        value = getpass.getpass(prompt)
        if not value:
            if current:
                skipped.append(key)
            elif key not in REQUIRED_SECRETS:
                skipped.append(key)
            else:
                print(f"  ✗ {key} is required and was not provided.")
                return {"ok": False, "error": f"{key} is required"}
        else:
            set_secret(key, value)
            updated.append(key)

    return {"ok": True, "updated": updated, "skipped": skipped}


def _compute_gex(chain_entries: list[dict], greeks: dict, oi: dict, spot: float) -> dict:
    """Compute GEX profile from option chain entries, greeks cache, and OI cache.

    Returns net_gex, gamma_flip (zero-gamma level), call_wall, put_wall, and per-strike breakdown.
    GEX per strike = gamma × OI × 100 × spot² × 0.01
    Call GEX is positive (dealer counter-trend); put GEX is positive magnitude but subtracted for net.
    """
    multiplier = 100
    per_strike: dict[float, dict] = {}

    for entry in chain_entries:
        strike = _num(entry.get("strike_price"))
        sym = entry.get("streamer_symbol")
        if sym is None or strike is None or strike <= 0:
            continue
        g = greeks.get(sym)
        open_interest = oi.get(sym)
        if g is None or open_interest is None or open_interest == 0:
            continue
        gamma = _num(g.get("gamma") if isinstance(g, dict) else getattr(g, "gamma", None))
        if gamma is None:
            continue
        gex_val = gex_math.dollar_gamma(gamma, open_interest, multiplier, spot)
        opt_type = entry.get("option_type", "")
        is_call = "C" in opt_type.upper()
        if strike not in per_strike:
            per_strike[strike] = {"strike": strike, "call_gex": 0.0, "put_gex": 0.0}
        if is_call:
            per_strike[strike]["call_gex"] += gex_val
        else:
            per_strike[strike]["put_gex"] += gex_val

    if not per_strike:
        return {"ok": False, "error": "insufficient GEX data — OI not yet cached (streamer must run first)"}

    strikes_sorted = sorted(per_strike.values(), key=lambda x: x["strike"])
    for s in strikes_sorted:
        s["net_gex"] = s["call_gex"] - s["put_gex"]

    net_gex = sum(s["net_gex"] for s in strikes_sorted)
    call_wall = max(strikes_sorted, key=lambda x: x["call_gex"])["strike"]
    put_wall  = max(strikes_sorted, key=lambda x: x["put_gex"])["strike"]

    # Gamma flip: interpolate where cumulative net GEX crosses zero (scanning low→high strike)
    gamma_flip = gex_math.interpolate_zero_gamma(strikes_sorted)

    return {
        "ok": True,
        "net_gex": round(net_gex, 2),
        "gex_positive": net_gex > 0,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "gamma_flip": gamma_flip,
        "strikes_with_data": len(per_strike),
        "per_strike": [
            {
                "strike": s["strike"],
                "call_gex": round(s["call_gex"], 2),
                "put_gex": round(s["put_gex"], 2),
                "net_gex": round(s["net_gex"], 2),
            }
            for s in strikes_sorted
        ],
    }


async def cmd_get_gex(args) -> dict:
    """Compute GEX profile for the trading symbol from streamer cache (greeks + OI)."""
    try:
        symbol = args.symbol.strip().upper()
        strike_count = getattr(args, "strike_count", 20)
        around_price = getattr(args, "around_price", None)

        # Get current underlying price for centering and GEX formula
        spot: float | None = None
        cached_trade = _cache_get_trade(symbol)
        if cached_trade is not None:
            spot = cached_trade
        else:
            trades = await _collect_last_prices([symbol], 5.0)
            spot = trades.get(symbol)
        if spot is None:
            return {"ok": False, "error": f"Cannot determine spot price for {symbol}"}

        center = around_price if around_price is not None else spot

        # Fetch chain structure
        chain = await _fetch_chain(symbol)
        if not chain:
            return {"ok": False, "error": f"No option chain for {symbol}"}
        expiration = _nearest_expiration(sorted(chain.keys()))
        options = list(chain[expiration])
        options = _atm_window(options, strike_count, center)

        streamer_symbols = [
            o.streamer_symbol for o in options if getattr(o, "streamer_symbol", None)
        ]
        if not streamer_symbols:
            return {"ok": False, "error": "No streamer symbols in chain window"}

        # Load greeks and OI from cache
        greeks = _cache_get_greeks(streamer_symbols)
        oi     = _cache_get_oi(streamer_symbols)

        # Fall back to live DXLink for missing greeks (OI only comes from Summary — no live fallback)
        missing_g = [s for s in streamer_symbols if s not in greeks]
        if missing_g:
            live_g = await _collect_greeks(missing_g, 6.0)
            greeks.update({s: {"delta": _num(v.delta), "gamma": _num(v.gamma),
                               "theta": _num(v.theta), "iv": _num(v.volatility)}
                           for s, v in live_g.items()})

        chain_entries = [_serialize(o) for o in options]
        result = _compute_gex(chain_entries, greeks, oi, spot)
        if result.get("ok"):
            result["symbol"] = symbol
            result["expiration"] = str(expiration)
            result["spot"] = spot
            result["oi_symbols_found"] = len(oi)
            result["greeks_symbols_found"] = len(greeks)
        return result
    except Exception as exc:
        return _error(exc)


def cmd_get_orb_range(args) -> dict:
    """Read the day's ORB (Opening Range Breakout) high/low, captured by the streamer
    from live Trade events during 9:30-9:35 ET (see streamer.py's _track_orb) rather than
    by the AI loop's own iterations, which aren't guaranteed to land inside that window."""
    import pytz
    symbol = args.symbol.strip().upper()
    et_today = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d")
    conn = _cache_conn()
    if conn is None:
        return {"ok": False, "error": "stream cache not found — is the streamer running?"}
    try:
        row = conn.execute(
            "SELECT orb_high, orb_low, captured_at FROM orb_ranges WHERE symbol = ? AND trade_date = ?",
            (symbol, et_today),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"ok": False, "error": "not yet captured — before 9:35 ET, or the streamer wasn't running through the 9:30-9:35 window today"}
    return {
        "ok": True,
        "symbol": symbol,
        "orb_high": row["orb_high"],
        "orb_low": row["orb_low"],
        "captured_at": row["captured_at"],
    }


def cmd_stream_status(_args) -> dict:
    # os.kill(pid, 0) is unreliable on Windows (raises SystemError for some
    # process states) — reuse streamer.py's cross-platform _running_pid(),
    # which already handles this via psutil/OpenProcess. streamer.py's
    # top-level imports are all stdlib, so this import is cheap.
    import streamer as _streamer
    pid = _streamer._running_pid()
    running = pid is not None

    result: dict = {"ok": True, "running": running, "pid": pid}

    conn = _cache_conn()
    if conn is None:
        result["cache"] = "no cache db"
        return result
    try:
        now = time.time()
        status_row = conn.execute("SELECT * FROM stream_status WHERE id = 1").fetchone()
        if status_row:
            result.update(dict(status_row))
            # status_row has its own stale "pid" column (written at daemon
            # startup) that would otherwise clobber the freshly-computed,
            # authoritative running/pid values checked above.
            result["running"] = running
            result["pid"] = pid

        ages = []
        for table, label in (("stream_trades", "trades"), ("stream_quotes", "quotes"), ("stream_greeks", "greeks")):
            try:
                row = conn.execute(f"SELECT COUNT(*) AS n, MAX(updated_at) AS last FROM {table}").fetchone()
                age = round(now - row["last"], 1) if row["last"] else None
                result[f"{label}_symbols"] = row["n"]
                result[f"{label}_oldest_age_s"] = age
                if age is not None:
                    ages.append(age)
            except Exception:
                pass

        # Stale-cache guardrail: if the daemon reports running but hasn't
        # written any event in a long time, the persistent connection may be
        # silently dead (e.g. the session/event-loop deadlock seen 2026-07-01,
        # which persisted 34+ hours across restarts before manual diagnosis).
        # Uses the numeric per-table ages above rather than the TEXT
        # last_event_at column (which is an ISO string, not an epoch float).
        # Each entry in `ages` is already `now - MAX(updated_at)` for its own
        # table (computed above, per table) -- taking min() here correctly
        # picks the freshest feed's age. (Not the same shape as the bug fixed
        # earlier in streamer.py's own --status output, which took min/max of
        # raw *timestamps* before converting to age -- this code was already
        # doing it the right way round.)
        stale_age_s = min(ages) if ages else None
        stale = running and (stale_age_s is None or stale_age_s > 600)
        result["stale_warning"] = stale
        result["stale_age_s"] = stale_age_s
        if stale:
            result["stale_reason"] = (
                "No stream event written in over 10 minutes despite streamer reporting running"
                if stale_age_s is not None else
                "Streamer reports running but has never recorded a stream event"
            )
    except Exception as exc:
        result["cache_error"] = str(exc)
    finally:
        conn.close()
    return result


async def cmd_stream_subscribe(args) -> dict:
    """Subscribe the stream daemon to additional symbols immediately via direct DXLink call.

    This is a one-shot command that fetches the current values for the given symbols
    and writes them to the cache — useful for warming up the cache after a new IC entry
    before the daemon's 30-second poll fires.
    """
    symbols = args.symbols
    timeout = getattr(args, "timeout", 6.0)
    if not symbols:
        return {"ok": False, "error": "No symbols provided"}

    conn = _cache_conn()
    if conn is None:
        return {"ok": False, "error": "Stream cache DB not found — start the streamer first"}

    try:
        greeks, quotes = await asyncio.gather(
            _collect_greeks(symbols, timeout),
            _collect_quotes(symbols, timeout),
        )
        now = time.time()
        written_q = written_g = 0
        for sym, q in quotes.items():
            bid = _num(q.bid_price)
            ask = _num(q.ask_price)
            mid = round((bid + ask) / 2, 4) if bid is not None and ask is not None else None
            conn.execute(
                "INSERT INTO stream_quotes (symbol, bid, ask, mid, bid_size, ask_size, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(symbol) DO UPDATE SET "
                "bid=excluded.bid, ask=excluded.ask, mid=excluded.mid, "
                "bid_size=excluded.bid_size, ask_size=excluded.ask_size, updated_at=excluded.updated_at",
                (sym, bid, ask, mid, _num(q.bid_size), _num(q.ask_size), now),
            )
            written_q += 1
        for sym, g in greeks.items():
            conn.execute(
                "INSERT INTO stream_greeks (symbol, delta, gamma, theta, vega, rho, iv, price, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(symbol) DO UPDATE SET "
                "delta=excluded.delta, gamma=excluded.gamma, theta=excluded.theta, "
                "vega=excluded.vega, rho=excluded.rho, iv=excluded.iv, price=excluded.price, "
                "updated_at=excluded.updated_at",
                (sym, _num(g.delta), _num(g.gamma), _num(g.theta), _num(g.vega),
                 _num(g.rho), _num(g.volatility), _num(g.price), now),
            )
            written_g += 1
        conn.commit()
        return {"ok": True, "symbols": symbols, "quotes_written": written_q, "greeks_written": written_g}
    except Exception as exc:
        return _error(exc)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="MEICAgent tastytrade CLI")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("get_connection_status")
    sub.add_parser("list_accounts")

    p_ai = sub.add_parser("get_account_info")
    p_ai.add_argument("--account_number", default=None)

    p_pos = sub.add_parser("get_positions")
    p_pos.add_argument("--account_number", default=None)

    p_mo = sub.add_parser("get_market_overview")
    p_mo.add_argument("--symbols", nargs="+", required=True)
    p_mo.add_argument("--quotes_timeout", type=float, default=4.0)

    p_gq = sub.add_parser("get_quote")
    p_gq.add_argument("--symbol", required=True)
    p_gq.add_argument("--timeout", type=float, default=6.0)

    p_oc = sub.add_parser("get_option_chain")
    p_oc.add_argument("--symbol", required=True)
    p_oc.add_argument("--expiration", default=None)
    p_oc.add_argument("--include_greeks", action="store_true")
    p_oc.add_argument("--include_quotes", action="store_true")
    p_oc.add_argument("--strike_count", type=int, default=15)
    p_oc.add_argument("--around_price", type=float, default=None)
    p_oc.add_argument("--greeks_timeout", type=float, default=6.0)
    p_oc.add_argument("--quotes_timeout", type=float, default=6.0)

    p_gs = sub.add_parser("get_strategies")
    p_gs.add_argument("--symbol", required=True)
    p_gs.add_argument("--target_dte", type=int, default=0)
    p_gs.add_argument("--wing_width", type=int, default=5)
    p_gs.add_argument("--short_delta", type=float, default=0.15)
    p_gs.add_argument("--around_price", type=float, default=None)
    p_gs.add_argument("--greeks_timeout", type=float, default=6.0)
    p_gs.add_argument("--quotes_timeout", type=float, default=6.0)

    p_wo = sub.add_parser("get_working_orders")
    p_wo.add_argument("--account_number", default=None)

    p_et = sub.add_parser("execute_trade")
    p_et.add_argument("--order", required=True, help="JSON order spec")
    p_et.add_argument("--account_number", default=None)
    p_et.add_argument("--live", dest="dry_run", action="store_false",
                      help="Submit live (default is dry run)")

    p_ao = sub.add_parser("adjust_order")
    p_ao.add_argument("--order_id", required=True, type=int)
    p_ao.add_argument("--order", required=True, help="JSON order spec")
    p_ao.add_argument("--account_number", default=None)
    p_ao.add_argument("--live", dest="dry_run", action="store_false")

    p_cp = sub.add_parser("close_position")
    p_cp.add_argument("--order_id", required=True, type=int)
    p_cp.add_argument("--account_number", default=None)

    sub.add_parser("secrets_status")

    p_cal = sub.add_parser("get_calendar")
    p_cal.add_argument("--year", type=int, default=None, help="Calendar year (default: current year)")

    p_sec = sub.add_parser("secrets_set")
    p_sec.add_argument(
        "--keys", nargs="+", default=None,
        metavar="KEY",
        help="Specific keys to set (default: all). Choices: client_secret refresh_token account_number",
    )

    p_gex = sub.add_parser("get_gex")
    p_gex.add_argument("--symbol", required=True, help="Trading symbol (e.g. XSP)")
    p_gex.add_argument("--strike_count", type=int, default=20,
                       help="Strikes each side of ATM to include in GEX window")
    p_gex.add_argument("--around_price", type=float, default=None,
                       help="Center the window around this price (default: current spot)")

    sub.add_parser("stream_status")

    p_vix1d = sub.add_parser("get_vix1d")
    p_vix1d.add_argument("--timeout", type=float, default=6.0)

    p_orb = sub.add_parser("get_orb_range")
    p_orb.add_argument("--symbol", required=True, help="Trading symbol (e.g. XSP)")

    p_ss = sub.add_parser("stream_subscribe")
    p_ss.add_argument("--symbols", nargs="+", required=True, help="Streamer symbols to warm up in cache")
    p_ss.add_argument("--timeout", type=float, default=6.0)

    args = parser.parse_args()

    # Sync commands (no asyncio needed)
    sync_dispatch = {
        "secrets_status": cmd_secrets_status,
        "secrets_set":    cmd_secrets_set,
        "stream_status":  cmd_stream_status,
        "get_orb_range":  cmd_get_orb_range,
        "get_calendar":   cmd_get_calendar,
    }
    if args.command in sync_dispatch:
        _out(sync_dispatch[args.command](args))
        return

    # Route through the streamer HTTP API when it's running.
    # This reuses the streamer's already-initialized OAuth session and cache,
    # avoiding 1.3s of Python import overhead per call. get_vix1d is excluded:
    # the daemon doesn't recognize it and replies with a normal (non-None)
    # {"ok": false, "error": "unknown command"} response rather than failing
    # the HTTP call outright, so _try_streamer_http can't tell the difference
    # between "daemon answered" and "daemon doesn't know this command" --
    # skip the shortcut entirely for commands the daemon was never meant to
    # own (VIX1D isn't a traded/managed symbol in its subscription window).
    args_dict = {k: v for k, v in vars(args).items() if k != "command" and v is not None}
    http_result = None if args.command == "get_vix1d" else _try_streamer_http(args.command, args_dict)
    if http_result is not None:
        _out(http_result)
        return

    dispatch = {
        "get_connection_status": cmd_get_connection_status,
        "list_accounts": cmd_list_accounts,
        "get_account_info": cmd_get_account_info,
        "get_positions": cmd_get_positions,
        "get_market_overview": cmd_get_market_overview,
        "get_quote": cmd_get_quote,
        "get_vix1d": cmd_get_vix1d,
        "get_option_chain": cmd_get_option_chain,
        "get_strategies": cmd_get_strategies,
        "get_gex": cmd_get_gex,
        "get_working_orders": cmd_get_working_orders,
        "execute_trade": cmd_execute_trade,
        "adjust_order": cmd_adjust_order,
        "close_position": cmd_close_position,
        "stream_subscribe": cmd_stream_subscribe,
    }

    fn = dispatch.get(args.command)
    if fn is None:
        parser.print_help()
        sys.exit(1)

    result = asyncio.run(fn(args))
    if isinstance(result, dict) and _last_streamer_http_error is not None:
        result.setdefault("streamer_http_fallback", _last_streamer_http_error)
    _out(result)


if __name__ == "__main__":
    main()
