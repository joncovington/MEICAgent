"""MEICAgent Dashboard — local HTTP server serving a trading dashboard."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

# ── Timezone helpers ─────────────────────────────────────────────────────────

try:
    import pytz as _pytz
    _ET = _pytz.timezone("America/New_York")
    def _today() -> str:
        return datetime.now(_ET).strftime("%Y-%m-%d")
    def _now_iso() -> str:
        return datetime.now(_ET).isoformat()
    def _week_start() -> str:
        now = datetime.now(_ET)
        return (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    def _month_start() -> str:
        return datetime.now(_ET).strftime("%Y-%m-01")
    def _year_start() -> str:
        return datetime.now(_ET).strftime("%Y-01-01")
except ImportError:
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
    def _week_start() -> str:
        now = datetime.now(timezone.utc)
        return (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    def _month_start() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-01")
    def _year_start() -> str:
        return datetime.now(timezone.utc).strftime("%Y-01-01")

# ── DB helpers ────────────────────────────────────────────────────────────────

_DB_PATH        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "meic_trades.db")
_CACHE_DB_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "stream_cache.db")
_CONFIG_PATH    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")
_LOG_PATH       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "agent.log")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict | None:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


# ── Stats helpers ─────────────────────────────────────────────────────────────

def _wl_ratio(wins: int, losses: int) -> float | None:
    total = (wins or 0) + (losses or 0)
    return round((wins or 0) / total * 100, 1) if total > 0 else None


def _fetch_spread_legs(conn: sqlite3.Connection, ic_order_ids: list[str]) -> dict[str, dict[str, dict]]:
    """Return {ic_order_id: {'put': leg_row, 'call': leg_row}} for the given IC ids.
    Sides with no recorded leg (legacy trades, or a side still open) are simply absent."""
    if not ic_order_ids:
        return {}
    placeholders = ", ".join(["?"] * len(ic_order_ids))
    rows = _rows(conn,
        f"SELECT * FROM ic_spread_legs WHERE ic_order_id IN ({placeholders})",
        ic_order_ids)
    legs: dict[str, dict[str, dict]] = {}
    for r in rows:
        legs.setdefault(r["ic_order_id"], {})[r["side"]] = r
    return legs


def _leg_outcome(leg_status: str | None, leg_pnl: float | None) -> tuple[int, int]:
    """Return (win, loss) for a single resolved spread leg; (0, 0) if still open/unresolved."""
    status = (leg_status or "").lower()
    if status == "expired":
        return (1, 0)
    if status in ("force_closed", "closed_profit_target"):
        return (1, 0) if (leg_pnl is None or leg_pnl >= 0) else (0, 1)
    if status == "stopped":
        return (0, 1)
    return (0, 0)


def _spread_wins_losses(trade_status: str, trade_pnl: float | None, put_leg: dict | None, call_leg: dict | None) -> tuple[int, int]:
    """Return (spread_wins, spread_losses) for one IC, counting each leg separately.
    Prefers real per-leg records. A side with no leg row is either (a) part of a
    legacy trade recorded before per-leg tracking existed — both sides then share
    the whole-trade guess — or (b) a side that closed together with its sibling in
    the same event (expired/force-closed together) and so was never given its own
    row — it inherits that shared outcome. A side still genuinely open (trade status
    'partial'/'open') is left uncounted until it resolves."""
    status = (trade_status or "").lower()

    if put_leg is None and call_leg is None:
        if status == "expired":
            return (2, 0)
        if status in ("force_closed", "closed_profit_target"):
            return (2, 0) if (trade_pnl or 0) >= 0 else (0, 2)
        if status in ("stopped", "partial"):
            return (0, 2)
        return (0, 0)

    def side_outcome(leg: dict | None) -> tuple[int, int]:
        if leg is not None:
            return _leg_outcome(leg.get("status"), leg.get("pnl"))
        if status == "expired":
            return (1, 0)
        if status in ("force_closed", "closed_profit_target"):
            return (1, 0) if (trade_pnl or 0) >= 0 else (0, 1)
        return (0, 0)

    pw, pl_ = side_outcome(put_leg)
    cw, cl_ = side_outcome(call_leg)
    return (pw + cw, pl_ + cl_)


def _stats_for_period(conn: sqlite3.Connection, start: str | None = None, end: str | None = None,
                       symbol: str | None = None) -> dict:
    """Compute stats for a date range, querying ic_trades directly for accuracy.
    start/end are inclusive YYYY-MM-DD strings; omit to mean unbounded. symbol filters to
    one traded symbol; omit (or "ALL") for the account-wide total across every symbol —
    this is what the global risk caps (max_concurrent_ics, max_entries_per_day) are checked
    against, so "ALL" is the economically meaningful default, not just a UI convenience."""
    where = ["status NOT IN ('cancelled', 'pending', 'partial_entry')"]
    params: list = []
    if start:
        where.append("trade_date >= ?")
        params.append(start)
    if end:
        where.append("trade_date <= ?")
        params.append(end)
    if symbol and symbol.upper() != "ALL":
        where.append("symbol = ?")
        params.append(symbol.upper())
    rows = _rows(conn,
        f"SELECT ic_order_id, pnl, status FROM ic_trades WHERE {' AND '.join(where)}",
        params)
    legs = _fetch_spread_legs(conn, [r["ic_order_id"] for r in rows])
    net_pnl = 0.0
    total_trades = 0
    wins = 0
    losses = 0
    for r in rows:
        net_pnl += float(r.get("pnl") or 0)
        total_trades += 1
        trade_legs = legs.get(r["ic_order_id"], {})
        w, l = _spread_wins_losses(r.get("status"), r.get("pnl"), trade_legs.get("put"), trade_legs.get("call"))
        wins += w
        losses += l
    result = {
        "net_pnl":      round(net_pnl, 2),
        "total_trades": total_trades,
        "wins":         wins,
        "losses":       losses,
    }
    result["wl_ratio"] = _wl_ratio(wins, losses)
    return result


# ── Per-spread status ─────────────────────────────────────────────────────────

def _badge(label: str, btype: str) -> dict:
    return {"label": label, "type": btype}


def _leg_badge(leg: dict | None) -> dict | None:
    """Badge for a single resolved leg, or None if there's no per-leg record for it."""
    if leg is None:
        return None
    status = (leg.get("status") or "").lower()
    exit_time = leg.get("exit_time") or ""
    time_str = ""
    if exit_time:
        s = str(exit_time).replace("T", " ")
        time_str = s[11:16] if len(s) >= 16 else ""
    if status == "stopped":
        return _badge(f"STOPPED {time_str}".strip(), "stopped")
    if status == "expired":
        return _badge("expired", "expired")
    if status in ("force_closed", "closed_profit_target"):
        return _badge("force closed", "force_closed")
    if status == "open":
        return _badge("monitoring", "monitoring")
    return _badge(status or "unknown", "unknown")


def _spread_statuses(trade: dict, put_leg: dict | None = None, call_leg: dict | None = None) -> tuple[dict, dict]:
    """Per-spread status badges. Uses real ic_spread_legs rows when available. A side
    with no leg row is either (a) part of a legacy trade recorded before per-leg
    tracking existed — both sides then show the same whole-trade-derived badge — or
    (b) a side that closed together with its sibling (expired/force-closed together,
    never given its own row) and so inherits that shared badge, or is genuinely
    still open and shows 'monitoring'."""
    status = (trade.get("status") or "").lower()
    exit_time = trade.get("exit_time") or ""
    time_str = ""
    if exit_time:
        s = str(exit_time).replace("T", " ")
        time_str = s[11:16] if len(s) >= 16 else ""

    monitoring = _badge("monitoring", "monitoring")
    expired    = _badge("expired",    "expired")
    pending    = _badge("pending",    "pending")
    cancelled  = _badge("cancelled",  "cancelled")
    force      = _badge("force closed", "force_closed")
    stopped    = _badge(f"STOPPED {time_str}".strip(), "stopped")

    if put_leg is None and call_leg is None:
        if status in ("pending", "partial_entry"):
            return pending, pending
        if status == "open":
            return monitoring, monitoring
        if status == "expired":
            return expired, expired
        if status == "cancelled":
            return cancelled, cancelled
        if status == "force_closed":
            return force, force
        if status in ("stopped", "partial"):
            return stopped, stopped
        return _badge(status, "unknown"), _badge(status, "unknown")

    def side_badge(leg: dict | None) -> dict:
        if leg is not None:
            return _leg_badge(leg)
        if status == "expired":
            return expired
        if status == "force_closed":
            return force
        if status == "cancelled":
            return cancelled
        if status in ("pending", "partial_entry"):
            return pending
        return monitoring

    return side_badge(put_leg), side_badge(call_leg)


# ── Log tail ──────────────────────────────────────────────────────────────────

def _build_log_data(n: int = 200) -> dict:
    if not os.path.exists(_LOG_PATH):
        return {"ok": True, "lines": [], "note": "Log file not found"}
    try:
        with open(_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = all_lines[-n:] if len(all_lines) > n else all_lines
        lines = []
        for raw in tail:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                ts  = str(obj.get("timestamp", ""))
                # shorten ISO timestamp to HH:MM:SS
                if "T" in ts:
                    ts = ts.split("T")[1][:8]
                lines.append({"ts": ts, "level": obj.get("level", "INFO"), "msg": obj.get("message", raw)})
            except (json.JSONDecodeError, ValueError):
                lines.append({"ts": "", "level": "INFO", "msg": raw})
        return {"ok": True, "lines": lines}
    except OSError as exc:
        return {"ok": False, "error": str(exc), "lines": []}


# ── API data builder ──────────────────────────────────────────────────────────

def _build_api_data(symbol: str | None = None) -> dict:
    """symbol filters trades/stats/analytics to one traded symbol; omit (or "ALL") for the
    account-wide view across every symbol — the economically meaningful default, since the
    account's actual risk caps (max_concurrent_ics, max_entries_per_day, buying power) are
    checked against the combined total, not any one symbol in isolation."""
    if not os.path.exists(_DB_PATH):
        return {"ok": False, "error": "Database not found — run: python src/db.py init_db"}

    sym_filter = symbol.upper() if symbol and symbol.upper() != "ALL" else None

    conn = _connect()
    today = _today()

    stats = {
        "today":    _stats_for_period(conn, start=today, end=today, symbol=sym_filter),
        "week":     _stats_for_period(conn, start=_week_start(),  end=today, symbol=sym_filter),
        "month":    _stats_for_period(conn, start=_month_start(), end=today, symbol=sym_filter),
        "year":     _stats_for_period(conn, start=_year_start(),  end=today, symbol=sym_filter),
        "all_time": _stats_for_period(conn, end=today, symbol=sym_filter),
    }

    trades_sql = """
        SELECT ic_order_id, symbol, entry_time, fill_confirmed_at,
               put_strike, call_strike, wing_width, net_credit, quantity,
               put_credit, call_credit, status, session_quality,
               iv_rank_at_entry, iv_skew_signal, price_action_signal,
               stop_trigger_current, stop_limit_current, stop_adjustment_count,
               exit_time, exit_price, exit_reason, pnl, fees,
               ai_entry_reasoning
        FROM ic_trades
        WHERE trade_date = ?
    """
    trades_params: list = [today]
    if sym_filter:
        trades_sql += " AND symbol = ?"
        trades_params.append(sym_filter)
    trades_sql += " ORDER BY entry_time"
    raw_trades = _rows(conn, trades_sql, trades_params)

    today_legs = _fetch_spread_legs(conn, [t["ic_order_id"] for t in raw_trades])
    trades = []
    for t in raw_trades:
        trade_legs = today_legs.get(t["ic_order_id"], {})
        put_s, call_s = _spread_statuses(t, trade_legs.get("put"), trade_legs.get("call"))
        row = dict(t)
        row["put_status"]  = put_s
        row["call_status"] = call_s
        trades.append(row)

    last_loop = _one(conn, """
        SELECT loop_time, action, open_trades_n, today_pnl,
               iv_rank, underlying_price, session_quality
        FROM loop_log
        WHERE loop_date = ?
        ORDER BY loop_time DESC LIMIT 1
    """, (today,))

    nlv_series = _rows(conn, """
        SELECT summary_date AS date, closing_nlv, net_pnl
        FROM daily_summary
        WHERE closing_nlv IS NOT NULL
        ORDER BY summary_date ASC
    """)

    sym_clause = " AND symbol = ?" if sym_filter else ""
    sym_params = [sym_filter] if sym_filter else []

    by_session = _rows(conn, f"""
        SELECT session_quality,
               COUNT(*) AS total,
               SUM(CASE WHEN status = 'expired' THEN 1 ELSE 0 END) AS wins,
               ROUND(AVG(pnl), 2) AS avg_pnl
        FROM ic_trades
        WHERE status NOT IN ('cancelled','pending','partial_entry')
          AND session_quality IS NOT NULL{sym_clause}
        GROUP BY session_quality
        ORDER BY total DESC
    """, sym_params)

    by_exit = _rows(conn, f"""
        SELECT COALESCE(exit_reason, 'open') AS exit_reason, COUNT(*) AS count
        FROM ic_trades
        WHERE status NOT IN ('cancelled','pending','partial_entry'){sym_clause}
        GROUP BY exit_reason
        ORDER BY count DESC
    """, sym_params)

    by_iv = _rows(conn, f"""
        SELECT
            CASE
                WHEN iv_rank_at_entry < 0.25 THEN '<25%'
                WHEN iv_rank_at_entry < 0.50 THEN '25-50%'
                WHEN iv_rank_at_entry < 0.75 THEN '50-75%'
                ELSE '>75%'
            END AS iv_bucket,
            COUNT(*) AS trades,
            ROUND(AVG(pnl), 2) AS avg_pnl,
            SUM(CASE WHEN status = 'expired' THEN 1 ELSE 0 END) AS wins
        FROM ic_trades
        WHERE pnl IS NOT NULL AND iv_rank_at_entry IS NOT NULL
          AND status NOT IN ('cancelled','pending','partial_entry'){sym_clause}
        GROUP BY iv_bucket
        ORDER BY MIN(iv_rank_at_entry)
    """, sym_params)

    fee_row = _one(conn, f"""
        SELECT COALESCE(SUM(net_credit * quantity), 0) AS gross_credit,
               COALESCE(SUM(fees), 0)                  AS total_fees,
               COALESCE(SUM(pnl), 0)                   AS net_pnl
        FROM ic_trades
        WHERE status NOT IN ('cancelled','pending','partial_entry'){sym_clause}
    """, sym_params) or {}
    gross = float(fee_row.get("gross_credit") or 0)
    fees  = float(fee_row.get("total_fees") or 0)
    net   = float(fee_row.get("net_pnl") or 0)
    fee_summary = {
        "gross_credit":  round(gross, 2),
        "total_fees":    round(fees, 2),
        "net_pnl":       round(net, 2),
        "fee_drag_pct":  round(fees / gross * 100, 1) if gross > 0 else None,
    }

    raw_recent = _rows(conn, f"""
        SELECT trade_date, ic_order_id, symbol, entry_time, exit_time,
               put_strike, call_strike, wing_width,
               net_credit, put_credit, call_credit,
               status, exit_reason, pnl, fees, session_quality
        FROM ic_trades
        WHERE status NOT IN ('cancelled','pending','partial_entry'){sym_clause}
        ORDER BY trade_date DESC, entry_time DESC
        LIMIT 60
    """, sym_params)

    recent_legs = _fetch_spread_legs(conn, [t["ic_order_id"] for t in raw_recent])
    recent_trades = []
    for t in raw_recent:
        trade_legs = recent_legs.get(t["ic_order_id"], {})
        put_s, call_s = _spread_statuses(t, trade_legs.get("put"), trade_legs.get("call"))
        w, l = _spread_wins_losses(t.get("status"), t.get("pnl"), trade_legs.get("put"), trade_legs.get("call"))
        recent_trades.append({
            "trade_date":    t.get("trade_date"),
            "ic_order_id":   t.get("ic_order_id"),
            "symbol":        t.get("symbol"),
            "entry_time":    t.get("entry_time"),
            "exit_time":     t.get("exit_time"),
            "put_strike":    t.get("put_strike"),
            "call_strike":   t.get("call_strike"),
            "wing_width":    t.get("wing_width"),
            "net_credit":    t.get("net_credit"),
            "put_credit":    t.get("put_credit"),
            "call_credit":   t.get("call_credit"),
            "status":        t.get("status"),
            "exit_reason":   t.get("exit_reason"),
            "pnl":           t.get("pnl"),
            "fees":          t.get("fees"),
            "session_quality": t.get("session_quality"),
            "put_status":    put_s,
            "call_status":   call_s,
            "spread_wins":   w,
            "spread_losses": l,
        })

    conn.close()

    return {
        "ok":         True,
        "as_of":      _now_iso(),
        "today":      today,
        "symbols":         _load_symbols(),   # every configured symbol, for the selector
        "selected_symbol": sym_filter or "ALL",
        "stats":      stats,
        "trades":     trades,
        "last_loop":  last_loop,
        "nlv_series": nlv_series,
        "analytics": {
            "by_session":    by_session,
            "by_exit":       by_exit,
            "by_iv":         by_iv,
            "fee_summary":   fee_summary,
            "recent_trades": recent_trades,
        },
    }


# ── GEX data builder ──────────────────────────────────────────────────────────

def _load_symbols() -> list[str]:
    """Every traded symbol, in config order. Falls back to the deprecated
    single-symbol 'symbol' key, then to ["XSP"], if 'symbols' is absent."""
    try:
        with open(_CONFIG_PATH) as f:
            cfg = json.load(f)
    except Exception:
        return ["XSP"]
    if cfg.get("symbols"):
        return [str(s).strip().upper() for s in cfg["symbols"] if str(s).strip()]
    if cfg.get("symbol"):
        return [str(cfg["symbol"]).strip().upper()]
    return ["XSP"]


def _load_symbol() -> str:
    """The default/first traded symbol — used when no symbol is specified explicitly."""
    return _load_symbols()[0]


def _compute_zero_gamma(series: list[dict]) -> float | None:
    """Interpolate the strike where cumulative net GEX crosses zero."""
    for i in range(len(series) - 1):
        a, b = series[i], series[i + 1]
        if a["net_gex"] != 0 and b["net_gex"] != 0 and a["net_gex"] * b["net_gex"] < 0:
            t = abs(a["net_gex"]) / (abs(a["net_gex"]) + abs(b["net_gex"]))
            return round(a["strike"] + t * (b["strike"] - a["strike"]), 2)
    return None


_TT_CMD = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tt.py")]
_STREAMER_API = "http://127.0.0.1:7699/api"

# GEX preview is restricted to actively traded symbols (config's `symbols` list). Open
# interest — required for any meaningful GEX number — only ever comes from a live DXLink
# Summary subscription; REST never carries it. Every traded symbol already has a permanent,
# OI-backed subscription window (see streamer.py's _symbol_refresher), so there is no
# REST-only "preview any symbol" mode anymore — an arbitrary non-traded symbol would just
# show a flat zero-OI GEX profile, which is worse than not offering it at all.


def _fetch_spot_rest(symbol: str) -> float | None:
    """Get the current spot price for a symbol via tt.py."""
    try:
        proc = subprocess.run(
            _TT_CMD + ["get_quote", "--symbol", symbol],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(proc.stdout)
        price = data.get("last") or data.get("mid")
        return float(price) if price else None
    except Exception:
        return None


def _parse_streamer_underlying(streamer_symbol: str) -> str | None:
    """Extract the underlying ticker from a streamer option symbol.
    E.g. '.XSP260630C740' -> 'XSP', '.SPXW260630P5500' -> 'SPXW'
    """
    import re
    m = re.match(r'\.([A-Z]+)\d{6}[CP]', streamer_symbol)
    return m.group(1) if m else None


def _fetch_chain_rest(symbol: str, around_price: float | None = None) -> tuple[list[dict], dict, dict, str | None]:
    """Fetch option chain via tt.py subprocess for symbols not in the stream cache.
    Returns (options_list, greeks_map, quotes_map, actual_underlying_ticker).
    actual_underlying_ticker is the ticker embedded in the returned streamer symbols
    (may differ from symbol — e.g. 'SPX' request returns 'XSP' streamer symbols).
    """
    cmd = _TT_CMD + ["get_option_chain", "--symbol", symbol,
                     "--include_greeks", "--include_quotes",
                     "--strike_count", "60"]
    if around_price is not None:
        cmd += ["--around_price", str(around_price)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        data = json.loads(proc.stdout)
    except Exception as exc:
        raise RuntimeError(f"REST chain fetch failed: {exc}") from exc

    if not data.get("ok"):
        raise RuntimeError(data.get("error", "chain fetch returned ok=false"))

    options: list[dict] = []
    greeks: dict[str, dict] = {}
    quotes: dict[str, dict] = {}
    actual_underlying: str | None = None

    for _exp, legs in (data.get("chain") or {}).items():
        for leg in legs:
            sym = leg.get("streamer_symbol") or ""
            options.append(leg)
            if actual_underlying is None and sym:
                actual_underlying = _parse_streamer_underlying(sym)
            if leg.get("delta") is not None:
                greeks[sym] = {
                    "gamma": leg.get("gamma"),
                    "iv":    (leg.get("iv") or 0) * 100,
                }
            if leg.get("bid") is not None:
                quotes[sym] = {"bid": leg["bid"], "ask": leg["ask"], "mid": leg.get("mid")}

    return options, greeks, quotes, actual_underlying


def _build_gex_data(symbol: str | None = None) -> dict:
    symbol = (symbol or _load_symbol()).strip().upper()

    # Load stream cache if available
    cache_conn = None
    if os.path.exists(_CACHE_DB_PATH):
        cache_conn = sqlite3.connect(_CACHE_DB_PATH)
        cache_conn.row_factory = sqlite3.Row

    # Underlying spot price — try cache first, then skip gracefully
    spot: float | None = None
    if cache_conn:
        tr = cache_conn.execute(
            "SELECT last FROM stream_trades WHERE symbol = ?", (symbol,)
        ).fetchone()
        spot = float(tr["last"]) if tr and tr["last"] is not None else None

    # Check if this symbol's chain is in the cache
    chain_rows = []
    expiration: str | None = None
    greeks: dict[str, dict] = {}
    quotes: dict[str, dict] = {}
    oi_cache: dict[str, int] = {}
    source = "stream_cache"

    if cache_conn:
        exp_row = cache_conn.execute(
            "SELECT expiration FROM stream_chain WHERE underlying_symbol = ? "
            "ORDER BY ABS(JULIANDAY(expiration) - JULIANDAY('now')) LIMIT 1",
            (symbol,),
        ).fetchone()
        if exp_row:
            expiration = exp_row["expiration"]
            # underlying_symbol filter matters: XSP and SPX (and other index
            # symbols) share the same 0DTE expiration date, so an
            # expiration-only WHERE clause mixes both chains together.
            chain_rows = cache_conn.execute(
                "SELECT data_json FROM stream_chain WHERE expiration = ? AND underlying_symbol = ?",
                (expiration, symbol),
            ).fetchall()
            # Filter greeks/quotes/OI to just this chain's own option symbols — with
            # multiple traded symbols, an unfiltered SELECT * would scan and load every
            # other symbol's cached rows too on every GEX refresh, for no benefit.
            chain_syms: list[str] = []
            for row in chain_rows:
                try:
                    opt = json.loads(row["data_json"])
                except Exception:
                    continue
                s = opt.get("streamer_symbol")
                if s:
                    chain_syms.append(s)
            if chain_syms:
                placeholders = ", ".join(["?"] * len(chain_syms))
                for r in cache_conn.execute(
                    f"SELECT * FROM stream_greeks WHERE symbol IN ({placeholders})", chain_syms
                ).fetchall():
                    greeks[r["symbol"]] = dict(r)
                for r in cache_conn.execute(
                    f"SELECT * FROM stream_quotes WHERE symbol IN ({placeholders})", chain_syms
                ).fetchall():
                    quotes[r["symbol"]] = dict(r)
                # Live open interest comes from DXLink Summary events (stream_oi),
                # not the static chain snapshot — the chain's own open_interest
                # field is never populated by the initial metadata fetch, so
                # reading it directly always yields zero.
                for r in cache_conn.execute(
                    f"SELECT * FROM stream_oi WHERE symbol IN ({placeholders})", chain_syms
                ).fetchall():
                    oi_cache[r["symbol"]] = r["open_interest"]

    if cache_conn:
        cache_conn.close()

    # Scale factor applied to strikes after series is built (default 1.0 = no scaling)
    strike_scale: float = 1.0

    # Fall back to REST fetch if symbol not in cache
    if not chain_rows:
        try:
            # Probe fetch: detect the actual streamer underlying ticker.
            # tastytrade maps several symbols to a scaled equivalent
            # (e.g. SPX/SPXW → XSP options at 1/10 scale).
            probe_opts, _, _, actual_und = _fetch_chain_rest(symbol)
            probe_ticker = actual_und or symbol

            # Get spot of the actual streamer underlying to center the strike range
            chain_spot = _fetch_spot_rest(probe_ticker)
            if chain_spot is None:
                chain_spot = _fetch_spot_rest(symbol)

            # If tastytrade mapped us to a different underlying, compute a scale factor
            # so the chart displays strikes in the requested symbol's price domain.
            if probe_ticker and probe_ticker.upper() != symbol.upper() and chain_spot:
                requested_spot = _fetch_spot_rest(symbol)
                if requested_spot and chain_spot:
                    strike_scale = requested_spot / chain_spot

            # Re-fetch centered on the real underlying's spot price
            rest_opts, greeks, quotes, _ = _fetch_chain_rest(symbol, around_price=chain_spot)
            # Display spot in the requested symbol's price domain
            spot = chain_spot * strike_scale if chain_spot else None
            source = "rest"
            # Infer expiration from options
            expirations = sorted({o.get("expiration_date", "") for o in rest_opts if o.get("expiration_date")})
            expiration = expirations[0] if expirations else None
            chain_rows = rest_opts  # already dicts
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if not expiration:
        return {"ok": False, "error": f"No chain data found for {symbol}"}

    # Normalise chain_rows: accept sqlite3.Row objects or plain dicts
    def _opt(row) -> dict:
        if isinstance(row, dict):
            return row
        try:
            return json.loads(row["data_json"])
        except Exception:
            return {}

    # spot is the display price (scaled to requested symbol's domain).
    # gex_spot is the actual chain underlying price used in GEX math.
    gex_spot = (spot / strike_scale) if (spot and strike_scale != 1.0) else spot

    # Aggregate per-strike
    strikes: dict[float, dict] = {}
    for row in chain_rows:
        try:
            opt = _opt(row)
        except Exception:
            continue
        strike = float(opt.get("strike_price") or 0)
        otype  = (opt.get("option_type") or "").upper()
        sym    = opt.get("streamer_symbol") or ""
        mult   = float(opt.get("shares_per_contract") or 100)
        if source == "stream_cache":
            oi = int(oi_cache.get(sym) or 0)
        else:
            oi = int(opt.get("open_interest") or 0)
        vol    = int(opt.get("average_daily_volume") or 0)

        g = greeks.get(sym, {})
        q = quotes.get(sym, {})
        gamma = float(g.get("gamma") or 0)
        raw_iv = float(g.get("iv") or 0)
        # cache stores raw decimal (0.20); REST path stores already-pct (20.0)
        iv = raw_iv if (source == "rest" or raw_iv > 1) else raw_iv * 100

        # Standard dollar-gamma-per-1%-move formula: gamma * OI * contract
        # size * spot^2 * 0.01. Must match tt.py's _compute_gex exactly —
        # this used to be gamma * oi * mult * spot (missing the spot^2 and
        # 0.01 scale), which understated GEX by roughly spot/100 (~75x for
        # SPX at 7483) relative to the number the trading loop actually
        # gates entries on via `get_gex`.
        _s = gex_spot or 0
        gex = gamma * oi * mult * _s * _s * 0.01
        if "P" in otype:
            gex = -gex

        if strike not in strikes:
            strikes[strike] = {
                "call_gamma": 0, "call_iv": 0, "call_oi": 0, "call_vol": 0, "call_gex": 0,
                "put_gamma":  0, "put_iv":  0, "put_oi":  0, "put_vol":  0, "put_gex":  0,
            }
        d = strikes[strike]
        if "C" in otype:
            d["call_gamma"] = gamma; d["call_iv"] = round(iv, 2)
            d["call_oi"] = oi;       d["call_vol"] = vol; d["call_gex"] = gex
        elif "P" in otype:
            d["put_gamma"]  = gamma; d["put_iv"]  = round(iv, 2)
            d["put_oi"]  = oi;       d["put_vol"]  = vol; d["put_gex"]  = gex

    series = []
    for strike in sorted(strikes):
        d = strikes[strike]
        net = d["call_gex"] + d["put_gex"]
        series.append({
            "strike":      round(strike * strike_scale, 2),
            "call_iv":     d["call_iv"],   "put_iv":      d["put_iv"],
            "call_oi":     d["call_oi"],   "put_oi":      d["put_oi"],
            "call_vol":    d["call_vol"],  "put_vol":     d["put_vol"],
            "total_vol":   d["call_vol"] + d["put_vol"],
            "call_gex":    round(d["call_gex"]),
            "put_gex":     round(d["put_gex"]),   # negative value
            "net_gex":     round(net),
            "abs_gex":     round(abs(net)),
        })

    total_call_gex = sum(s["call_gex"] for s in series if s["call_gex"] > 0)
    total_put_gex  = abs(sum(s["put_gex"] for s in series if s["put_gex"] < 0))
    net_gex_total  = sum(s["net_gex"] for s in series)
    max_gex_s      = max(series, key=lambda s: s["abs_gex"], default=None)
    # _compute_zero_gamma interpolates from series which already has scaled strikes
    zero_gamma     = _compute_zero_gamma(series)
    # Call/put walls: the strike with the largest gamma concentration on each side —
    # dealer resistance levels. series stores put_gex as a negative value (see above),
    # so the wall is the most negative entry, not the largest.
    call_wall_s = max(series, key=lambda s: s["call_gex"], default=None)
    put_wall_s  = min(series, key=lambda s: s["put_gex"], default=None)

    return {
        "ok":               True,
        "symbol":           symbol,
        "expiration":       expiration,
        "underlying_price": spot,
        "source":           source,
        "series":           series,
        "totals": {
            "total_call_gex": round(total_call_gex),
            "total_put_gex":  round(total_put_gex),
            "net_gex":        round(net_gex_total),
            "max_gex_strike": max_gex_s["strike"] if max_gex_s else None,
            "zero_gamma":     zero_gamma,
            "call_wall":      call_wall_s["strike"] if call_wall_s else None,
            "put_wall":       put_wall_s["strike"] if put_wall_s else None,
        },
    }


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MEICAgent</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#0a0d12;color:#e6edf3;height:100vh;overflow:hidden}
.app{display:flex;height:100vh}

/* Sidebar */
.sidebar{width:210px;flex-shrink:0;background:#0d1117;border-right:1px solid #1e2430;
          display:flex;flex-direction:column}
.brand{padding:22px 18px 18px;border-bottom:1px solid #1e2430}
.brand h1{font-size:15px;font-weight:700;letter-spacing:.3px;color:#fff}
.brand p{font-size:10px;color:#6b7280;margin-top:3px;letter-spacing:1px;text-transform:uppercase}
nav{flex:1;padding:10px 0}
.nav-item{display:flex;align-items:center;gap:9px;padding:9px 18px;cursor:pointer;
           font-size:13px;color:#6b7280;transition:all .15s;
           border-left:3px solid transparent}
.nav-item:hover{color:#e6edf3;background:#111519}
.nav-item.active{color:#e6edf3;background:#111519;border-left-color:#00c896}
.nav-icon{font-size:14px;width:16px;text-align:center}
.sidebar-footer{padding:14px 18px;border-top:1px solid #1e2430;font-size:11px}
.status-pill{display:flex;align-items:center;gap:6px;font-weight:600;margin-bottom:5px}
.dot{width:7px;height:7px;border-radius:50%}
.dot.live{background:#00c896;animation:pulse 2s infinite}
.dot.idle{background:#6b7280}
.dot.err{background:#e8423a}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.status-meta{color:#6b7280;line-height:1.7}

/* Content */
.content{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
.disc-banner{display:none;background:#3d1a1a;color:#e8423a;padding:7px 18px;
              font-size:11px;font-weight:600;border-bottom:1px solid #e8423a}
.view{display:none;flex-direction:column;height:100%}
.view.active{display:flex}

/* Frames */
.frame{overflow-y:auto;border-bottom:1px solid #1e2430}
.frame:last-child{flex:1;min-height:0;border-bottom:none}
.frame-hdr{padding:10px 18px 0;display:flex;align-items:center;justify-content:space-between}
.frame-title{font-size:10px;font-weight:600;color:#6b7280;letter-spacing:1.5px;text-transform:uppercase}
.frame-sub{font-size:10px;color:#6b7280}

/* Stats grid */
.stats-wrap{padding:10px 18px 14px;overflow-x:auto}
.sgrid{border-collapse:collapse;width:100%;font-size:12px}
.sgrid th,.sgrid td{padding:7px 14px;text-align:right;white-space:nowrap}
.sgrid th{font-size:9px;font-weight:700;color:#6b7280;letter-spacing:1px;
           text-transform:uppercase;border-bottom:1px solid #1e2430}
.sgrid td{border-bottom:1px solid #111519;color:#e6edf3}
.sgrid tr:last-child td{border-bottom:none}
.sgrid .lbl{text-align:left;color:#6b7280;font-size:11px;padding-left:0;font-weight:500}
.sgrid .today-col{background:#111519}
.sgrid .today-col th{color:#e6edf3}
.pos{color:#00c896}.neg{color:#e8423a}.neu{color:#e6edf3}
.wh{color:#00c896}.wm{color:#f5a623}.wl{color:#e8423a}
.dash{color:#2d3441}

/* Trades table */
.tbl-wrap{overflow-x:auto;height:100%}
.ttbl{border-collapse:collapse;width:100%;font-size:12px;min-width:780px}
.ttbl th{padding:8px 11px;text-align:left;font-size:9px;font-weight:700;
          color:#e6edf3;letter-spacing:1px;text-transform:uppercase;
          background:#0d1117;border-bottom:2px solid #1e2430;
          position:sticky;top:0;z-index:1}
.ttbl td{padding:9px 11px;border-bottom:1px solid #111519;vertical-align:middle}
.ttbl tr:hover td{background:#0f1318}
.tr{text-align:right}
.tcredit{font-weight:600;color:#00c896}
.tppos{font-weight:700;color:#00c896;text-align:right}
.tpneg{font-weight:700;color:#e8423a;text-align:right}
.tpnil{color:#2d3441;text-align:right}
.empty{padding:36px 18px;text-align:center;color:#2d3441;font-size:13px}

/* Badges */
.bdg{display:inline-block;padding:2px 7px;border-radius:3px;
      font-size:9px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;white-space:nowrap}
.bdg-monitoring{background:#131920;color:#3d4451}
.bdg-expired{background:#1a1f2a;color:#8b949e}
.bdg-stopped{background:#3d1a1a;color:#e8423a}
.bdg-pending{background:#2a2510;color:#f5a623}
.bdg-cancelled{background:#1a1f2a;color:#3d4451;text-decoration:line-through}
.bdg-force_closed{background:#2a1a08;color:#f97316}
.bdg-unknown{background:#1a1f2a;color:#6b7280}

/* History charts */
.chart-wrap{padding:14px 18px;position:relative}
.chart-wrap canvas{max-height:175px}
.ana-grid{display:grid;grid-template-columns:1fr 1fr;gap:1px;
           background:#1e2430;flex:1;min-height:0;height:100%}
.apanel{background:#0a0d12;padding:14px 18px;overflow-y:auto}
.ptitle{font-size:9px;font-weight:700;color:#6b7280;letter-spacing:1.5px;
         text-transform:uppercase;margin-bottom:10px}
.atable{width:100%;border-collapse:collapse;font-size:11px}
.atable th{font-size:9px;color:#6b7280;font-weight:600;text-transform:uppercase;
            letter-spacing:.5px;padding:3px 8px;text-align:right;
            border-bottom:1px solid #1e2430}
.atable th:first-child{text-align:left}
.atable td{padding:5px 8px;border-bottom:1px solid #111519;text-align:right;color:#e6edf3}
.atable td:first-child{text-align:left;color:#8b949e}
.atable tr:last-child td{border-bottom:none}
.fee-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.fee-card{padding:10px 12px;background:#111519;border-radius:5px}
.fee-lbl{font-size:9px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px}
.fee-val{font-size:17px;font-weight:700;color:#e6edf3}
.fee-val.neg{color:#e8423a}.fee-val.warn{color:#f5a623}

/* GEX view */
.gex-view{overflow-y:auto;padding:0 0 24px}
.gex-section{padding:20px 24px 0}
.gex-section-title{font-size:15px;font-weight:700;color:#e6edf3;margin-bottom:4px;display:flex;align-items:center;gap:8px}
.gex-section-sub{font-size:11px;color:#6b7280;margin-bottom:14px}
.gex-divider{height:1px;background:#1e2430;margin:20px 24px 0}
.gex-row{display:grid;gap:16px;margin-bottom:16px}
.gex-row-2{grid-template-columns:1fr 1fr}
.gex-row-main{grid-template-columns:1fr 280px}
.gex-body{display:flex;align-items:flex-start}
.gex-tabs{display:flex;flex-direction:column;gap:2px;padding:12px 8px;flex:0 0 84px;
          border-right:1px solid #1e2430;align-self:stretch}
.gex-tab{font-size:11px;font-weight:700;color:#6b7280;padding:8px 10px;cursor:pointer;
         border-right:2px solid transparent;margin-right:-1px;text-transform:uppercase;
         letter-spacing:.8px;transition:color .15s,border-color .15s;border-radius:4px 0 0 4px}
.gex-tab:hover{color:#e6edf3}
.gex-tab.active{color:#00c896;border-right-color:#00c896;background:#0d2018}
.gex-tab-panels{flex:1;min-width:0}
.gex-tab-panel{display:none}.gex-tab-panel.active{display:block}
.chart-card{background:#0d1117;border:1px solid #1e2430;border-radius:6px;padding:14px 16px}
.chart-card-title{font-size:10px;font-weight:700;color:#6b7280;letter-spacing:1.2px;
                   text-transform:uppercase;margin-bottom:10px}
.chart-card canvas{display:block;width:100%!important}
.radio-group{display:flex;gap:4px;margin-bottom:10px}
.radio-group label{display:flex;align-items:center;gap:5px;cursor:pointer;
                    font-size:11px;color:#6b7280;padding:4px 10px;
                    border:1px solid #1e2430;border-radius:4px;transition:all .15s}
.radio-group label:hover{color:#e6edf3;border-color:#3d4451}
.radio-group input{display:none}
.radio-group input:checked+span{color:#e6edf3}
.radio-group label:has(input:checked){color:#e6edf3;border-color:#00c896;background:#0d2018}
.metrics-panel{background:#0d1117;border:1px solid #1e2430;border-radius:6px;padding:16px}
.metrics-panel-title{font-size:10px;font-weight:700;color:#6b7280;letter-spacing:1.2px;
                      text-transform:uppercase;margin-bottom:14px;display:flex;align-items:center;gap:6px}
.metric-row{margin-bottom:14px}
.metric-lbl{font-size:10px;color:#6b7280;text-transform:uppercase;letter-spacing:.8px;margin-bottom:2px}
.metric-val{font-size:22px;font-weight:700;color:#e6edf3;line-height:1.1}
.metric-val.pos{color:#00c896}.metric-val.neg{color:#e8423a}
.metric-divider{height:1px;background:#1e2430;margin:10px 0}

/* Log view */
.log-toolbar{display:flex;align-items:center;gap:10px;padding:10px 18px;
              border-bottom:1px solid #1e2430;flex-shrink:0}
.log-toolbar .frame-title{flex:1}
.log-filter{display:flex;gap:6px}
.log-filter label{font-size:11px;color:#6b7280;cursor:pointer;display:flex;align-items:center;gap:3px}
.log-filter input{accent-color:#00c896;cursor:pointer}
.log-paused-badge{font-size:9px;font-weight:700;letter-spacing:.5px;color:#f5a623;
                   background:#2a2510;padding:2px 7px;border-radius:3px;
                   text-transform:uppercase;display:none}
.log-scroll{flex:1;overflow-y:auto;padding:8px 0;font-family:'Cascadia Code','Fira Mono',
             'Consolas',monospace;font-size:11.5px;line-height:1.55}
.log-line{display:flex;gap:0;padding:1px 18px;white-space:pre-wrap;word-break:break-all}
.log-line:hover{background:#0f1318}
.log-ts{color:#3d4451;min-width:70px;flex-shrink:0}
.log-lvl{min-width:46px;flex-shrink:0;font-weight:700}
.log-lvl.INFO{color:#3d4451}.log-lvl.WARN{color:#f5a623}.log-lvl.ERROR{color:#e8423a}
.log-msg{color:#c9d1d9;flex:1}
.log-msg.WARN{color:#f5a623}.log-msg.ERROR{color:#e8423a}
</style>
</head>
<body>
<div class="app">

<aside class="sidebar">
  <div class="brand">
    <h1>MEICAgent</h1>
    <p>0DTE MEIC Strategy</p>
  </div>
  <nav>
    <div class="nav-item active" data-view="today">
      <span class="nav-icon">&#9670;</span> Today
    </div>
    <div class="nav-item" data-view="history">
      <span class="nav-icon">&#9711;</span> History
    </div>
    <div class="nav-item" data-view="gex">
      <span class="nav-icon">&#9699;</span> GEX
    </div>
    <div class="nav-item" data-view="logs">
      <span class="nav-icon">&#9776;</span> Logs
    </div>
    <div class="nav-item" data-view="settings">
      <span class="nav-icon">&#9881;</span> Settings
    </div>
  </nav>
  <div class="sidebar-footer">
    <div class="status-pill">
      <div class="dot idle" id="sdot"></div>
      <span id="slabel">Loading&hellip;</span>
    </div>
    <div class="status-meta">
      <div id="smeta">&mdash;</div>
      <div id="scountdown" style="color:#4a5568"></div>
    </div>
  </div>
</aside>

<main class="content">
  <div class="disc-banner" id="disc-banner">&#9679; DISCONNECTED &mdash; unable to reach dashboard server</div>

  <!-- TODAY VIEW -->
  <div class="view active" id="view-today">
    <div class="frame" style="flex:0 0 auto">
      <div class="frame-hdr">
        <span class="frame-title">Performance</span>
        <select id="main-symbol-select" style="background:#0d1117;color:#e6edf3;border:1px solid #1e2430;
                border-radius:4px;padding:3px 8px;font-size:11px;cursor:pointer;outline:none;margin-left:10px">
          <option value="ALL" selected>All symbols</option>
        </select>
        <span class="frame-sub" id="as-of"></span>
      </div>
      <div class="stats-wrap">
        <table class="sgrid">
          <thead>
            <tr>
              <th class="lbl" style="padding-left:0"></th>
              <th class="today-col">TODAY</th>
              <th>THIS WEEK</th>
              <th>THIS MONTH</th>
              <th>THIS YEAR</th>
              <th>ALL-TIME</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td class="lbl">Net P&amp;L</td>
              <td class="today-col" id="pnl-today"></td>
              <td id="pnl-week"></td><td id="pnl-month"></td>
              <td id="pnl-year"></td><td id="pnl-all"></td>
            </tr>
            <tr>
              <td class="lbl">Total Trades</td>
              <td class="today-col" id="tr-today"></td>
              <td id="tr-week"></td><td id="tr-month"></td>
              <td id="tr-year"></td><td id="tr-all"></td>
            </tr>
            <tr>
              <td class="lbl">Wins (spreads)</td>
              <td class="today-col" id="w-today"></td>
              <td id="w-week"></td><td id="w-month"></td>
              <td id="w-year"></td><td id="w-all"></td>
            </tr>
            <tr>
              <td class="lbl">Losses (spreads)</td>
              <td class="today-col" id="l-today"></td>
              <td id="l-week"></td><td id="l-month"></td>
              <td id="l-year"></td><td id="l-all"></td>
            </tr>
            <tr>
              <td class="lbl">W/L Ratio</td>
              <td class="today-col" id="wl-today"></td>
              <td id="wl-week"></td><td id="wl-month"></td>
              <td id="wl-year"></td><td id="wl-all"></td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
    <div class="frame" style="flex:1;min-height:0">
      <div class="frame-hdr" style="padding-bottom:7px">
        <span class="frame-title">Today&rsquo;s Trades</span>
        <span class="frame-sub" id="trade-count"></span>
      </div>
      <div class="tbl-wrap" style="height:calc(100% - 34px)">
        <table class="ttbl">
          <thead>
            <tr>
              <th>TIME</th><th>SYMBOL</th><th>WIDTH</th><th>PUT STRIKE</th><th>CALL STRIKE</th>
              <th>PUT $</th><th>CALL $</th><th>NET CREDIT</th>
              <th>PUT STATUS</th><th>CALL STATUS</th>
              <th style="text-align:right">P&amp;L</th>
            </tr>
          </thead>
          <tbody id="tbody">
            <tr><td colspan="11" class="empty">Loading&hellip;</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- HISTORY VIEW -->
  <div class="view" id="view-history">
    <div class="frame" style="flex:0 0 230px">
      <div class="frame-hdr" style="padding-bottom:4px">
        <span class="frame-title">Account Value (NLV) Over Time</span>
      </div>
      <div class="chart-wrap">
        <canvas id="nlv-canvas"></canvas>
        <div class="empty" id="nlv-empty" style="display:none;padding:18px 0">
          No closing NLV data yet &mdash; appears after the first EOD sequence runs.
        </div>
      </div>
    </div>
    <div class="frame" style="flex:1;min-height:0;overflow:hidden">
      <div class="ana-grid">
        <div class="apanel">
          <div class="ptitle">Win Rate by Session</div>
          <table class="atable" id="sess-tbl">
            <thead><tr><th>Session</th><th>Trades</th><th>Wins</th><th>Win %</th><th>Avg P&amp;L</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
        <div class="apanel">
          <div class="ptitle">Exit Reasons</div>
          <table class="atable" id="exit-tbl">
            <thead><tr><th>Reason</th><th>Count</th><th>%</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
        <div class="apanel">
          <div class="ptitle">Avg P&amp;L by IV Rank</div>
          <table class="atable" id="iv-tbl">
            <thead><tr><th>IV Rank</th><th>Trades</th><th>Wins</th><th>Avg P&amp;L</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
        <div class="apanel">
          <div class="ptitle">Fee Drag (All-Time)</div>
          <div class="fee-grid">
            <div class="fee-card"><div class="fee-lbl">Gross Credit</div><div class="fee-val" id="f-gross">&mdash;</div></div>
            <div class="fee-card"><div class="fee-lbl">Total Fees</div><div class="fee-val neg" id="f-fees">&mdash;</div></div>
            <div class="fee-card"><div class="fee-lbl">Net P&amp;L</div><div class="fee-val" id="f-net">&mdash;</div></div>
            <div class="fee-card"><div class="fee-lbl">Fee Drag</div><div class="fee-val warn" id="f-drag">&mdash;</div></div>
          </div>
        </div>
      </div>
      <div class="apanel" style="margin-top:10px">
        <div class="ptitle">Recent Trades (last 60)</div>
        <div style="overflow-x:auto">
          <table class="atable" id="recent-tbl" style="width:100%;min-width:700px">
            <thead><tr>
              <th>Date</th><th>Symbol</th><th>Strikes</th><th>Width</th>
              <th>Credit</th><th>Put</th><th>Put $</th>
              <th>Call</th><th>Call $</th>
              <th>P&amp;L</th><th>Session</th>
            </tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <!-- GEX VIEW -->
  <div class="view" id="view-gex">
    <div class="gex-view" id="gex-inner">

      <div class="gex-body">
      <!-- Sub-tabs: narrow vertical nav on the left, one panel visible at a time -->
      <div class="gex-tabs">
        <div class="gex-tab active" data-gex-tab="gex">GEX</div>
        <div class="gex-tab" data-gex-tab="ivskew">IV Skew</div>
        <div class="gex-tab" data-gex-tab="volume">Volume</div>
      </div>
      <div class="gex-tab-panels">

      <!-- Tab: GEX -->
      <div class="gex-tab-panel active" id="gex-panel-gex">
        <div class="gex-section">
          <div class="gex-section-sub" id="gex-main-sub">&nbsp;</div>
          <div class="gex-row gex-row-main">
            <div class="chart-card">
              <div class="chart-card-title" id="gex-chart-title">GEX by Strike &mdash; Net GEX</div>
              <div style="position:relative;height:260px"><canvas id="gex-main-chart"></canvas></div>
            </div>
            <div id="gex-main-sidebar">
              <!-- Symbol selector — restricted to actively traded symbols (config.json's
                   `symbols`); GEX needs live open interest, which only ever comes from a
                   subscribed symbol, so there's no "preview any symbol" free-text option
                   anymore. Lives here (GEX tab only) rather than page-level, so it can only
                   be changed while this tab is active — IV Skew/Volume read whatever symbol
                   was last selected here. -->
              <div style="display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:12px">
                <span style="font-size:10px;color:#6b7280;text-transform:uppercase;letter-spacing:.8px">Symbol</span>
                <select id="gex-symbol-select" style="background:#0d1117;color:#e6edf3;border:1px solid #1e2430;
                        border-radius:4px;padding:4px 8px;font-size:12px;cursor:pointer;outline:none">
                </select>
                <span id="gex-source-badge" style="font-size:10px;color:#6b7280"></span>
              </div>
              <div style="display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:12px">
                <span style="font-size:10px;color:#6b7280;text-transform:uppercase;letter-spacing:.8px">GEX View</span>
                <div class="radio-group" id="gex-view-group">
                  <label><input type="radio" name="gex_view" value="split"><span>Calls vs Puts</span></label>
                  <label><input type="radio" name="gex_view" value="net" checked><span>&#11044; Net GEX</span></label>
                  <label><input type="radio" name="gex_view" value="abs"><span>Absolute GEX</span></label>
                </div>
              </div>
            <div class="metrics-panel">
              <div class="metrics-panel-title">&#128202; Total GEX</div>
              <div class="metric-row">
                <div class="metric-lbl">Total Call GEX</div>
                <div class="metric-val pos" id="m-call-gex">&mdash;</div>
              </div>
              <div class="metric-row">
                <div class="metric-lbl">Total Put GEX</div>
                <div class="metric-val neg" id="m-put-gex">&mdash;</div>
              </div>
              <div class="metric-divider"></div>
              <div class="metric-row">
                <div class="metric-lbl">Net GEX</div>
                <div class="metric-val" id="m-net-gex">&mdash;</div>
              </div>
              <div class="metric-divider"></div>
              <div class="metric-row">
                <div class="metric-lbl">Max GEX Strike</div>
                <div class="metric-val" id="m-max-strike">&mdash;</div>
              </div>
              <div class="metric-divider"></div>
              <div class="metric-row">
                <div class="metric-lbl">Call Wall <span title="Strike with the largest call-side gamma concentration — dealer resistance above spot" style="cursor:help;color:#3d4451">&#9432;</span></div>
                <div class="metric-val pos" id="m-call-wall">&mdash;</div>
              </div>
              <div class="metric-row">
                <div class="metric-lbl">Put Wall <span title="Strike with the largest put-side gamma concentration — dealer support below spot" style="cursor:help;color:#3d4451">&#9432;</span></div>
                <div class="metric-val neg" id="m-put-wall">&mdash;</div>
              </div>
              <div class="metric-divider"></div>
              <div class="metric-row" style="margin-bottom:0">
                <div class="metric-lbl">Zero Gamma (Flip) <span title="Strike where dealer GEX transitions from negative to positive" style="cursor:help;color:#3d4451">&#9432;</span></div>
                <div class="metric-val" id="m-zero-gamma">&mdash;</div>
              </div>
            </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Tab: IV Skew -->
      <div class="gex-tab-panel" id="gex-panel-ivskew">
        <div class="gex-section">
          <div class="gex-section-sub" id="gex-iv-sub">&nbsp;</div>
          <div class="gex-row gex-row-2">
            <div class="chart-card">
              <div class="chart-card-title">Call IV vs Put IV by Strike</div>
              <div style="position:relative;height:220px"><canvas id="gex-iv-chart"></canvas></div>
            </div>
            <div class="chart-card">
              <div class="chart-card-title">Open Interest by Strike</div>
              <div style="position:relative;height:220px"><canvas id="gex-oi-chart"></canvas></div>
            </div>
          </div>
        </div>
      </div>

      <!-- Tab: Volume -->
      <div class="gex-tab-panel" id="gex-panel-volume">
        <div class="gex-section">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
            <div class="gex-section-title" style="margin:0">&#128200; Volume by Strike</div>
            <div class="radio-group" id="vol-view-group" style="margin-bottom:0">
              <label><input type="radio" name="vol_view" value="split"><span>Calls vs Puts</span></label>
              <label><input type="radio" name="vol_view" value="total" checked><span>&#11044; Total Volume</span></label>
            </div>
          </div>
          <div class="chart-card">
            <div style="position:relative;height:260px"><canvas id="gex-vol-chart"></canvas></div>
          </div>
        </div>
      </div>

      </div>
      </div>

    </div>
  </div>

  <!-- LOGS VIEW -->
  <div class="view" id="view-logs">
    <div class="log-toolbar">
      <span class="frame-title">Agent Log</span>
      <div class="log-filter">
        <label><input type="checkbox" id="log-warn" checked> WARN</label>
        <label><input type="checkbox" id="log-error" checked> ERROR</label>
        <label><input type="checkbox" id="log-info" checked> INFO</label>
      </div>
      <span class="log-paused-badge" id="log-paused">&#9646;&#9646; PAUSED</span>
    </div>
    <div class="log-scroll" id="log-scroll">
      <div class="log-line"><span class="log-msg">Loading&hellip;</span></div>
    </div>
  </div>

  <!-- SETTINGS VIEW -->
  <div class="view" id="view-settings">
    <div style="padding:36px;color:#6b7280">
      <h2 style="color:#e6edf3;margin-bottom:8px;font-size:16px">Settings</h2>
      <p style="font-size:13px">Configuration is managed via <code style="background:#111519;padding:1px 6px;border-radius:3px">config.json</code> in the project root.</p>
    </div>
  </div>

</main>
</div>

<script>
// ── state ────────────────────────────────────────────────────────────────────
let cache = null;
let nlvChart = null;
let cd = 30;

// ── nav ───────────────────────────────────────────────────────────────────────
document.querySelectorAll('.nav-item').forEach(el => {
  el.addEventListener('click', () => {
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    el.classList.add('active');
    document.getElementById('view-' + el.dataset.view).classList.add('active');
    if (el.dataset.view === 'history' && cache) renderHistory(cache);
    if (el.dataset.view === 'gex') { _initGexSymbol(); fetchGex(); }
    if (el.dataset.view === 'logs') fetchLog();
  });
});

// ── formatters ────────────────────────────────────────────────────────────────
function fPnl(v, cls) {
  if (v == null) return '<span class="dash">—</span>';
  const c = cls || (v > 0 ? 'pos' : v < 0 ? 'neg' : 'neu');
  const s = v > 0 ? '+' : '';
  return '<span class="' + c + '">' + s + '$' + Math.abs(v).toFixed(2) + '</span>';
}
function fNum(v) {
  if (v == null) return '<span class="dash">—</span>';
  return v.toLocaleString();
}
function fWl(v) {
  if (v == null) return '<span class="dash">—</span>';
  const c = v >= 70 ? 'wh' : v >= 50 ? 'wm' : 'wl';
  return '<span class="' + c + '">' + v.toFixed(1) + '%</span>';
}
function fTime(ts) {
  if (!ts) return '—';
  return String(ts).replace('T', ' ').substring(11, 16);
}
function fMoney(v) {
  if (v == null) return '—';
  return (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(2);
}
function bdg(b) {
  return '<span class="bdg bdg-' + (b.type || 'unknown') + '">' + b.label + '</span>';
}

// ── stats grid ────────────────────────────────────────────────────────────────
function renderStats(s) {
  const map = [
    ['today','today'], ['week','week'], ['month','month'], ['year','year'], ['all_time','all']
  ];
  map.forEach(([key, sfx]) => {
    const d = s[key] || {};
    document.getElementById('pnl-' + sfx).innerHTML = fPnl(d.net_pnl);
    document.getElementById('tr-'  + sfx).innerHTML = fNum(d.total_trades);
    document.getElementById('w-'   + sfx).innerHTML = d.wins  != null ? '<span class="pos">' + d.wins  + '</span>' : '<span class="dash">—</span>';
    document.getElementById('l-'   + sfx).innerHTML = d.losses != null ? '<span class="neg">' + d.losses + '</span>' : '<span class="dash">—</span>';
    document.getElementById('wl-'  + sfx).innerHTML = fWl(d.wl_ratio);
  });
}

// ── trades table ──────────────────────────────────────────────────────────────
function renderTrades(trades) {
  const tbody = document.getElementById('tbody');
  const lbl   = document.getElementById('trade-count');
  if (!trades || !trades.length) {
    tbody.innerHTML = '<tr><td colspan="11" class="empty">No trades today — agent is monitoring</td></tr>';
    lbl.textContent = '';
    return;
  }
  lbl.textContent = trades.length + ' trade' + (trades.length !== 1 ? 's' : '');
  tbody.innerHTML = trades.map(t => {
    const pc = t.put_credit  != null ? '$' + Number(t.put_credit).toFixed(2)  : '—';
    const cc = t.call_credit != null ? '$' + Number(t.call_credit).toFixed(2) : '—';
    const pnlCell = t.pnl != null
      ? '<td class="' + (t.pnl > 0 ? 'tppos' : 'tpneg') + '">' + fMoney(t.pnl) + '</td>'
      : '<td class="tpnil">—</td>';
    const tip = (t.ai_entry_reasoning || '')
      .replace(/&/g, '&amp;').replace(/"/g, '&quot;')
      .replace(/</g, '&lt;').replace(/>/g, '&gt;');
    return '<tr title="' + tip + '">' +
      '<td>' + fTime(t.entry_time) + '</td>' +
      '<td style="color:#6b7280;font-size:10px">' + (t.symbol || '—') + '</td>' +
      '<td class="tr">' + (t.wing_width != null ? t.wing_width : '—') + '</td>' +
      '<td class="tr">' + (t.put_strike  != null ? t.put_strike  : '—') + '</td>' +
      '<td class="tr">' + (t.call_strike != null ? t.call_strike : '—') + '</td>' +
      '<td class="tr">' + pc + '</td>' +
      '<td class="tr">' + cc + '</td>' +
      '<td class="tr tcredit">$' + Number(t.net_credit || 0).toFixed(2) + '</td>' +
      '<td>' + bdg(t.put_status)  + '</td>' +
      '<td>' + bdg(t.call_status) + '</td>' +
      pnlCell + '</tr>';
  }).join('');
}

// ── sidebar status ────────────────────────────────────────────────────────────
function renderStatus(d) {
  const dot   = document.getElementById('sdot');
  const label = document.getElementById('slabel');
  const meta  = document.getElementById('smeta');
  const loop  = d.last_loop;
  if (!loop) {
    dot.className = 'dot idle'; label.textContent = 'NO DATA';
    meta.textContent = 'No loop activity today'; return;
  }
  const t = new Date(String(loop.loop_time).replace(' ', 'T'));
  const ageMin = (Date.now() - t) / 60000;
  if (ageMin < 10) { dot.className = 'dot live'; label.textContent = 'LIVE'; }
  else             { dot.className = 'dot idle'; label.textContent = 'IDLE'; }
  const iv = loop.iv_rank != null ? ' · IV ' + Math.round(loop.iv_rank * 100) + '%' : '';
  const px = loop.underlying_price != null ? ' · $' + Number(loop.underlying_price).toFixed(2) : '';
  meta.textContent = 'Last: ' + fTime(loop.loop_time) + ' ET' + iv + px;
}

// ── NLV chart ─────────────────────────────────────────────────────────────────
function renderNlv(series) {
  const canvas = document.getElementById('nlv-canvas');
  const empty  = document.getElementById('nlv-empty');
  if (!series || !series.length) {
    canvas.style.display = 'none'; empty.style.display = 'block'; return;
  }
  canvas.style.display = 'block'; empty.style.display = 'none';
  const labels = series.map(r => r.date);
  const vals   = series.map(r => r.closing_nlv);
  const color  = vals[vals.length - 1] >= vals[0] ? '#00c896' : '#e8423a';
  if (nlvChart) {
    nlvChart.data.labels = labels;
    nlvChart.data.datasets[0].data = vals;
    nlvChart.data.datasets[0].borderColor = color;
    nlvChart.data.datasets[0].backgroundColor = color + '22';
    nlvChart.update(); return;
  }
  nlvChart = new Chart(canvas, {
    type: 'line',
    data: { labels, datasets: [{ label: 'NLV', data: vals, borderColor: color,
      backgroundColor: color + '22', borderWidth: 2, pointRadius: 3,
      pointHoverRadius: 5, fill: true, tension: 0.3 }] },
    options: {
      responsive: true, maintainAspectRatio: true,
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: ctx => {
          const r = series[ctx.dataIndex];
          return '$' + ctx.parsed.y.toLocaleString() + (r.net_pnl != null ? '  (day P&L: ' + fMoney(r.net_pnl) + ')' : '');
        }}}},
      scales: {
        x: { grid: { color: '#1e2430' }, ticks: { color: '#6b7280', font: { size: 10 } } },
        y: { grid: { color: '#1e2430' }, ticks: { color: '#6b7280', font: { size: 10 },
               callback: v => '$' + v.toLocaleString() } }
      }
    }
  });
}

// ── history panels ────────────────────────────────────────────────────────────
function renderHistory(d) {
  renderNlv(d.nlv_series);
  const a = d.analytics || {};

  // Session
  const sb = document.querySelector('#sess-tbl tbody');
  const sess = a.by_session || [];
  sb.innerHTML = !sess.length ? '<tr><td colspan="5" class="empty">No data</td></tr>'
    : sess.map(s => {
        const wr = s.total > 0 ? (s.wins / s.total * 100).toFixed(1) + '%' : '—';
        const wc = s.total > 0 ? (s.wins/s.total >= 0.7 ? 'wh' : s.wins/s.total >= 0.5 ? 'wm' : 'wl') : '';
        return '<tr><td>' + (s.session_quality || '—') + '</td><td>' + s.total + '</td>' +
          '<td class="pos">' + s.wins + '</td><td class="' + wc + '">' + wr + '</td>' +
          '<td>' + (s.avg_pnl != null ? fMoney(s.avg_pnl) : '—') + '</td></tr>';
      }).join('');

  // Exit
  const eb = document.querySelector('#exit-tbl tbody');
  const exits = a.by_exit || [];
  const etotal = exits.reduce((s, e) => s + (e.count || 0), 0);
  eb.innerHTML = !exits.length ? '<tr><td colspan="3" class="empty">No data</td></tr>'
    : exits.map(e => {
        const pct = etotal > 0 ? (e.count / etotal * 100).toFixed(1) + '%' : '—';
        return '<tr><td>' + (e.exit_reason || '—').replace(/_/g, ' ') +
          '</td><td>' + e.count + '</td><td>' + pct + '</td></tr>';
      }).join('');

  // IV
  const ib = document.querySelector('#iv-tbl tbody');
  const ivs = a.by_iv || [];
  ib.innerHTML = !ivs.length ? '<tr><td colspan="4" class="empty">No data</td></tr>'
    : ivs.map(iv => {
        const ac = iv.avg_pnl > 0 ? 'pos' : iv.avg_pnl < 0 ? 'neg' : '';
        return '<tr><td>' + iv.iv_bucket + '</td><td>' + iv.trades + '</td>' +
          '<td class="pos">' + iv.wins + '</td>' +
          '<td class="' + ac + '">' + (iv.avg_pnl != null ? fMoney(iv.avg_pnl) : '—') + '</td></tr>';
      }).join('');

  // Fees
  const f = a.fee_summary || {};
  document.getElementById('f-gross').textContent = f.gross_credit != null ? '$' + f.gross_credit.toFixed(2) : '—';
  document.getElementById('f-fees').textContent  = f.total_fees   != null ? '$' + f.total_fees.toFixed(2)   : '—';
  const fnet = document.getElementById('f-net');
  fnet.textContent  = f.net_pnl != null ? fMoney(f.net_pnl) : '—';
  fnet.className    = 'fee-val ' + (f.net_pnl >= 0 ? 'pos' : 'neg');
  document.getElementById('f-drag').textContent = f.fee_drag_pct != null ? f.fee_drag_pct.toFixed(1) + '%' : '—';

  // Recent trades
  const rb = document.querySelector('#recent-tbl tbody');
  const rt = a.recent_trades || [];
  rb.innerHTML = !rt.length ? '<tr><td colspan="11" class="empty">No trade history</td></tr>'
    : rt.map(t => {
        const pnlNet = (t.pnl != null && t.fees != null) ? t.pnl - t.fees : t.pnl;
        const pc = pnlNet != null ? (pnlNet >= 0 ? 'pos' : 'neg') : '';
        const dateStr = (t.trade_date || '').substring(5);  // MM-DD
        const strikes = (t.put_strike != null ? t.put_strike : '—') + '/' + (t.call_strike != null ? t.call_strike : '—');
        return '<tr>' +
          '<td>' + dateStr + '</td>' +
          '<td style="color:#6b7280;font-size:10px">' + (t.symbol || '—') + '</td>' +
          '<td class="tr">' + strikes + '</td>' +
          '<td class="tr">' + (t.wing_width != null ? t.wing_width : '—') + '</td>' +
          '<td class="tr tcredit">$' + Number(t.net_credit || 0).toFixed(2) + '</td>' +
          '<td>' + bdg(t.put_status)  + '</td>' +
          '<td class="tr" style="color:#6b7280">$' + Number(t.put_credit || 0).toFixed(2)  + '</td>' +
          '<td>' + bdg(t.call_status) + '</td>' +
          '<td class="tr" style="color:#6b7280">$' + Number(t.call_credit || 0).toFixed(2) + '</td>' +
          '<td class="tr ' + pc + '">' + (pnlNet != null ? fMoney(pnlNet) : '—') + '</td>' +
          '<td style="color:#6b7280;font-size:10px">' + (t.session_quality || '—') + '</td>' +
          '</tr>';
      }).join('');
}

// ── symbol selectors ─────────────────────────────────────────────────────────
// Populated once from the traded-symbols list the server reports (config.json's
// `symbols`) — both the main-view filter and the GEX picker draw from the same
// list, since GEX preview is restricted to actively traded symbols (open interest
// only ever comes from a live subscription, never REST, so previewing a
// non-traded symbol's GEX would just show a flat zero profile).
let symbolsPopulated = false;
function populateSymbolSelectors(symbols) {
  if (symbolsPopulated || !symbols || !symbols.length) return;
  symbolsPopulated = true;
  const mainSel = document.getElementById('main-symbol-select');
  const gexSel = document.getElementById('gex-symbol-select');
  symbols.forEach(sym => {
    const o1 = document.createElement('option'); o1.value = sym; o1.textContent = sym;
    mainSel.appendChild(o1);
    const o2 = document.createElement('option'); o2.value = sym; o2.textContent = sym;
    gexSel.appendChild(o2);
  });
  gexSel.value = symbols[0];
}

// ── render all ────────────────────────────────────────────────────────────────
function renderAll(d) {
  cache = d;
  document.getElementById('disc-banner').style.display = 'none';
  document.getElementById('as-of').textContent = d.as_of
    ? d.as_of.substring(0, 19).replace('T', ' ') + ' ET' : '';
  populateSymbolSelectors(d.symbols);
  renderStats(d.stats || {});
  renderTrades(d.trades || []);
  renderStatus(d);
  if (document.getElementById('view-history').classList.contains('active')) renderHistory(d);
}

// ── fetch ─────────────────────────────────────────────────────────────────────
async function fetchData() {
  try {
    const sym = document.getElementById('main-symbol-select').value || 'ALL';
    const r = await fetch('/api/data?symbol=' + encodeURIComponent(sym));
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    if (d.ok === false) {
      document.getElementById('tbody').innerHTML =
        '<tr><td colspan="11" class="empty">' + (d.error || 'Error loading data') + '</td></tr>';
      return;
    }
    renderAll(d);
  } catch(e) {
    document.getElementById('disc-banner').style.display = 'block';
    document.getElementById('sdot').className = 'dot err';
    document.getElementById('slabel').textContent = 'DISCONNECTED';
  }
}

document.getElementById('main-symbol-select').addEventListener('change', fetchData);

// ── log tail ──────────────────────────────────────────────────────────────────
let logPaused = false;
let lastLogCount = 0;
const logScroll = document.getElementById('log-scroll');

logScroll.addEventListener('scroll', () => {
  const atBottom = logScroll.scrollTop + logScroll.clientHeight >= logScroll.scrollHeight - 20;
  logPaused = !atBottom;
  document.getElementById('log-paused').style.display = logPaused ? 'inline-block' : 'none';
});

function logVisible() {
  return document.getElementById('view-logs').classList.contains('active');
}

function renderLog(lines) {
  const showInfo  = document.getElementById('log-info').checked;
  const showWarn  = document.getElementById('log-warn').checked;
  const showError = document.getElementById('log-error').checked;
  const filtered  = lines.filter(l => {
    const lv = (l.level || 'INFO').toUpperCase();
    if (lv === 'WARN'  && !showWarn)  return false;
    if (lv === 'ERROR' && !showError) return false;
    if (lv === 'INFO'  && !showInfo)  return false;
    return true;
  });
  if (filtered.length === lastLogCount && logScroll.innerHTML !== '') return;
  lastLogCount = filtered.length;
  logScroll.innerHTML = filtered.map(l => {
    const lv  = (l.level || 'INFO').toUpperCase();
    const msg = (l.msg || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    return '<div class="log-line">' +
      '<span class="log-ts">' + (l.ts || '        ') + '  </span>' +
      '<span class="log-lvl ' + lv + '">' + lv.padEnd(6) + '</span>' +
      '<span class="log-msg ' + lv + '">' + msg + '</span>' +
      '</div>';
  }).join('');
  if (!logPaused) logScroll.scrollTop = logScroll.scrollHeight;
}

async function fetchLog() {
  if (!logVisible()) return;
  try {
    const r = await fetch('/api/log');
    if (!r.ok) return;
    const d = await r.json();
    if (d.lines) renderLog(d.lines);
  } catch(_) {}
}

// Re-render when filters change
['log-info','log-warn','log-error'].forEach(id => {
  document.getElementById(id).addEventListener('change', () => { lastLogCount = -1; fetchLog(); });
});

// ── GEX ───────────────────────────────────────────────────────────────────────
let gexData = null;
let gexIvChart = null, gexOiChart = null, gexVolChart = null, gexMainChart = null;

function fGex(v) {
  if (v == null) return '—';
  const abs = Math.abs(v);
  const sign = v < 0 ? '-' : '';
  if (abs >= 1e9) return sign + '$' + (abs / 1e9).toFixed(2) + 'B';
  if (abs >= 1e6) return sign + '$' + (abs / 1e6).toFixed(2) + 'M';
  if (abs >= 1e3) return sign + '$' + (abs / 1e3).toFixed(1) + 'K';
  return sign + '$' + abs.toFixed(0);
}

function _vline(x, label, color) {
  return {
    id: 'vline_' + label,
    beforeDatasetsDraw(chart) {
      const {ctx, scales} = chart;
      if (!scales.x) return;
      const xPx = scales.x.getPixelForValue(x);
      if (xPx == null || isNaN(xPx)) return;
      const {top, bottom} = chart.chartArea;
      ctx.save();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(xPx, top); ctx.lineTo(xPx, bottom); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = color;
      ctx.font = '9px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(label, xPx, bottom + 12);
      ctx.restore();
    }
  };
}

function _hline(y, label, color) {
  return {
    id: 'hline_' + label,
    beforeDatasetsDraw(chart) {
      const {ctx, scales} = chart;
      if (!scales.y) return;
      const yPx = scales.y.getPixelForValue(y);
      if (yPx == null || isNaN(yPx)) return;
      const {left, right} = chart.chartArea;
      ctx.save();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(left, yPx); ctx.lineTo(right, yPx); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = color;
      ctx.font = '9px sans-serif';
      ctx.textAlign = 'left';
      ctx.fillText(label, right - 4, yPx - 3);
      ctx.restore();
    }
  };
}

// Trim a by-strike series to the contiguous band around its non-zero data, padded by
// `pad` strikes on each side — a horizontal strike ladder otherwise wastes most of its
// height on far-OTM strikes with no meaningful bar (the fetched window is wider than
// what's worth plotting).
function _trimToData(series, valueKeys, pad) {
  let lo = -1, hi = -1;
  for (let i = 0; i < series.length; i++) {
    const nonZero = valueKeys.some(k => Math.abs(series[i][k] || 0) > 0);
    if (nonZero) { if (lo === -1) lo = i; hi = i; }
  }
  if (lo === -1) return series;
  return series.slice(Math.max(0, lo - pad), Math.min(series.length, hi + pad + 1));
}

// Fixed-height containers squeeze bar thickness down as strike count grows — grow the
// container to fit instead, so each bar keeps a consistent, readable thickness regardless
// of how many strikes _trimToData left in view.
function _growChartHeight(canvasId, numRows, perRow, minH) {
  const canvas = document.getElementById(canvasId);
  const wrap = canvas && canvas.parentElement;
  if (!wrap) return;
  wrap.style.height = Math.max(minH, numRows * perRow) + 'px';
}

function _baseOpts(plugins) {
  return {
    responsive: true, maintainAspectRatio: false,
    animation: false,
    plugins: {
      legend: { display: false },
      tooltip: { mode: 'index', intersect: false, backgroundColor: '#1a1f2e',
                 titleColor: '#e6edf3', bodyColor: '#8b949e', borderColor: '#1e2430', borderWidth: 1 },
      ...(plugins || {})
    },
    scales: {
      x: { grid: { color: '#1a1f2a' }, ticks: { color: '#4a5568', font: { size: 9 }, maxRotation: 0 } },
      y: { grid: { color: '#1a1f2a' }, ticks: { color: '#4a5568', font: { size: 9 } } }
    }
  };
}

function renderIvChart(series, spot) {
  const labels = series.map(s => s.strike);
  const ds = [
    { label: 'Call IV', data: series.map(s => s.call_iv || null),
      borderColor: 'green', backgroundColor: 'rgba(0,128,0,0.1)',
      pointRadius: 4, pointHoverRadius: 6, borderWidth: 2, tension: 0, fill: false },
    { label: 'Put IV',  data: series.map(s => s.put_iv  || null),
      borderColor: 'red', backgroundColor: 'rgba(255,0,0,0.1)',
      pointRadius: 4, pointHoverRadius: 6, borderWidth: 2, tension: 0, fill: false },
  ];
  const opts = _baseOpts();
  opts.scales.x.title = { display: true, text: 'Strike Price', color: '#6b7280' };
  opts.scales.y.title = { display: true, text: 'Implied Volatility (%)', color: '#6b7280' };
  opts.scales.y.ticks.callback = v => v.toFixed(1) + '%';
  opts.plugins.tooltip.mode = 'index';
  opts.plugins.tooltip.callbacks = { label: ctx => ctx.dataset.label + ': ' + (ctx.parsed.y || 0).toFixed(2) + '%' };
  opts.plugins.vline = spot != null ? _vline(spot, '$' + spot.toFixed(2), '#f5a623') : {};
  if (gexIvChart) { gexIvChart.data.labels = labels; gexIvChart.data.datasets = ds; gexIvChart.update(); return; }
  gexIvChart = new Chart(document.getElementById('gex-iv-chart'), { type: 'line', data: { labels, datasets: ds }, options: opts });
}

function renderOiChart(series, spot) {
  series = _trimToData(series, ['call_oi', 'put_oi', 'call_vol', 'put_vol'], 3);
  const labels = series.map(s => s.strike);
  // Calls positive (right), puts negated (left) — mirrored horizontal bars. OI (dark) and
  // Volume (light) share the same origin and overlap rather than stack — Volume is drawn
  // second so it renders in front, appearing as a short highlighted segment near the base
  // of the (usually much larger) OI bar.
  const ds = [
    { label: 'Call OI',     data: series.map(s => s.call_oi),   backgroundColor: 'green' },
    { label: 'Put OI',      data: series.map(s => -s.put_oi),   backgroundColor: 'red' },
    { label: 'Call Volume', data: series.map(s => s.call_vol),  backgroundColor: 'lightgreen' },
    { label: 'Put Volume',  data: series.map(s => -s.put_vol),  backgroundColor: 'lightcoral' },
  ];
  const opts = _baseOpts();
  opts.indexAxis = 'y';
  opts.datasets = { bar: { minBarThickness: 14 } };
  opts.scales.y.title = { display: true, text: 'Strike', color: '#6b7280' };
  opts.scales.x.title = { display: true, text: 'Open Interest / Volume', color: '#6b7280' };
  // grouped:false makes same-category datasets overlap (full width, drawn in array order)
  // instead of Chart.js's default of splitting each dataset into its own thin sub-bar.
  opts.scales.y.grouped = false;
  opts.plugins.tooltip.callbacks = {
    label: ctx => (ctx.dataset.label || '') + ': ' + Math.abs(ctx.parsed.x).toLocaleString()
  };
  opts.plugins.hline = spot != null ? _hline(spot, '$' + spot.toFixed(2), '#f5a623') : {};
  _growChartHeight('gex-oi-chart', labels.length, 18, 220);
  if (gexOiChart) { gexOiChart.destroy(); gexOiChart = null; }
  gexOiChart = new Chart(document.getElementById('gex-oi-chart'),
    { type: 'bar', data: { labels, datasets: ds }, options: opts,
      plugins: spot != null ? [_hline(spot, '$' + spot.toFixed(2), '#f5a623')] : [] });
}

function renderVolChart(series, spot, mode) {
  series = _trimToData(series, ['call_vol', 'put_vol', 'total_vol'], 3);
  const labels = series.map(s => s.strike);
  let ds;
  if (mode === 'split') {
    // Calls positive (right), puts negated (left) — mirrored horizontal bars
    ds = [
      { label: 'Call Volume', data: series.map(s => s.call_vol),  backgroundColor: 'lightgreen' },
      { label: 'Put Volume',  data: series.map(s => -s.put_vol),  backgroundColor: 'lightcoral' },
    ];
  } else {
    ds = [{ label: 'Total Volume', data: series.map(s => s.total_vol), backgroundColor: 'purple' }];
  }
  const opts = _baseOpts();
  opts.indexAxis = 'y';
  opts.datasets = { bar: { minBarThickness: 14 } };
  opts.scales.y.title = { display: true, text: 'Strike', color: '#6b7280' };
  opts.scales.x.title = { display: true, text: 'Volume', color: '#6b7280' };
  if (mode === 'split') { opts.scales.x.stacked = true; opts.scales.y.stacked = true; }
  opts.plugins.tooltip.callbacks = { label: ctx => (ctx.dataset.label||'') + ': ' + Math.abs(ctx.parsed.x).toLocaleString() };
  _growChartHeight('gex-vol-chart', labels.length, 18, 260);
  if (gexVolChart) { gexVolChart.destroy(); gexVolChart = null; }
  gexVolChart = new Chart(document.getElementById('gex-vol-chart'),
    { type: 'bar', data: { labels, datasets: ds }, options: opts,
      plugins: spot != null ? [_hline(spot, '$' + spot.toFixed(2), '#f5a623')] : [] });
}

function renderGexMainChart(series, spot, zero, mode) {
  series = _trimToData(series, ['call_gex', 'put_gex', 'net_gex', 'abs_gex'], 3);
  const labels = series.map(s => s.strike);
  let ds, titleText, stacked = false;
  if (mode === 'split') {
    // Calls positive (right), puts already negative in data (left) — relative stacked bars
    ds = [
      { label: 'Call GEX', data: series.map(s => s.call_gex), backgroundColor: 'green' },
      { label: 'Put GEX',  data: series.map(s => s.put_gex),  backgroundColor: 'red' },
    ];
    titleText = 'GEX by Strike — Calls vs Puts';
    stacked = true;
  } else if (mode === 'abs') {
    ds = [{ label: '|Net GEX|', data: series.map(s => s.abs_gex), backgroundColor: 'blue' }];
    titleText = 'GEX by Strike — Absolute GEX';
  } else {
    // Net GEX: green where positive (call-heavy), red where negative (put-heavy)
    ds = [{ label: 'Net GEX',
            data: series.map(s => s.net_gex),
            backgroundColor: series.map(s => s.net_gex >= 0 ? 'green' : 'red') }];
    titleText = 'GEX by Strike — Net GEX (Green=Call Heavy, Red=Put Heavy)';
  }
  document.getElementById('gex-chart-title').textContent = titleText;
  const opts = _baseOpts();
  opts.indexAxis = 'y';
  opts.datasets = { bar: { minBarThickness: 14 } };
  opts.scales.y.title = { display: true, text: 'Strike Price', color: '#6b7280' };
  opts.scales.x.title = { display: true, text: 'Gamma Exposure ($)', color: '#6b7280' };
  if (stacked) { opts.scales.x.stacked = true; opts.scales.y.stacked = true; }
  opts.scales.x.ticks.callback = v => fGex(v);
  opts.plugins.tooltip.callbacks = {
    label: ctx => (ctx.dataset.label||'') + ': ' + fGex(ctx.parsed.x)
  };
  // Baseline height matches the sidebar (GEX View controls + Total GEX panel) so the chart
  // fills the available view height instead of sitting at a fixed 260px with empty space
  // below it; still grows taller than that when strike count needs more room.
  const sidebar = document.getElementById('gex-main-sidebar');
  const sidebarH = sidebar ? sidebar.offsetHeight : 260;
  _growChartHeight('gex-main-chart', labels.length, 18, sidebarH || 260);
  const hlinePlugins = [];
  if (spot != null) hlinePlugins.push(_hline(spot, '$' + spot.toFixed(2), 'orange'));
  if (zero != null) hlinePlugins.push(_hline(zero, 'Zero Γ: $' + zero.toFixed(2), 'purple'));
  opts.plugins.customHlines = { id: 'customHlines', beforeDatasetsDraw(chart) {
    hlinePlugins.forEach(p => p.beforeDatasetsDraw(chart));
  }};
  if (gexMainChart) { gexMainChart.destroy(); gexMainChart = null; }
  gexMainChart = new Chart(document.getElementById('gex-main-chart'),
    { type: 'bar', data: { labels, datasets: ds },
      options: opts, plugins: [opts.plugins.customHlines] });
}

function renderGexMetrics(totals) {
  const t = totals || {};
  document.getElementById('m-call-gex').textContent  = fGex(t.total_call_gex);
  const putEl = document.getElementById('m-put-gex');
  putEl.textContent = t.total_put_gex != null ? fGex(-t.total_put_gex) : '—';
  const netEl = document.getElementById('m-net-gex');
  netEl.textContent = fGex(t.net_gex);
  netEl.className = 'metric-val ' + (t.net_gex >= 0 ? 'pos' : 'neg');
  document.getElementById('m-max-strike').textContent = t.max_gex_strike != null ? '$' + t.max_gex_strike : '—';
  document.getElementById('m-call-wall').textContent = t.call_wall != null ? '$' + t.call_wall : '—';
  document.getElementById('m-put-wall').textContent = t.put_wall != null ? '$' + t.put_wall : '—';
  document.getElementById('m-zero-gamma').textContent = t.zero_gamma != null ? '$' + t.zero_gamma.toFixed(2) : '—';
}

function renderGex(d) {
  gexData = d;
  if (!d.ok) {
    document.getElementById('gex-iv-sub').textContent = d.error || 'No data';
    return;
  }
  const series = d.series || [];
  const spot   = d.underlying_price;
  const zero   = d.totals && d.totals.zero_gamma;
  const sym    = d.symbol || '';
  const exp    = d.expiration || '';
  document.getElementById('gex-iv-sub').textContent   = sym + ' Implied Volatility Skew — Exp: ' + exp;
  document.getElementById('gex-main-sub').textContent = sym + ' — Exp: ' + exp + (spot ? '  |  Spot: $' + spot.toFixed(2) : '');

  const gexMode = document.querySelector('input[name="gex_view"]:checked')?.value || 'net';
  const volMode = document.querySelector('input[name="vol_view"]:checked')?.value || 'total';

  renderIvChart(series, spot);
  renderOiChart(series, spot);
  renderVolChart(series, spot, volMode);
  renderGexMainChart(series, spot, zero, gexMode);
  renderGexMetrics(d.totals);
}

function _initGexSymbol() {
  // Sync dropdown to last-loaded symbol; otherwise leave SPX (the HTML default)
  if (!gexData) return;
  const sym = (gexData.symbol || '').toUpperCase();
  const sel = document.getElementById('gex-symbol-select');
  if ([...sel.options].some(o => o.value === sym)) {
    sel.value = sym;
  }
}

function gexSymbol() {
  const sel = document.getElementById('gex-symbol-select');
  return sel.value || (sel.options.length ? sel.options[0].value : '');
}

async function fetchGex() {
  const sym = gexSymbol();
  if (!sym) return;  // selector not populated yet — wait for the next auto-refresh tick
  const badge = document.getElementById('gex-source-badge');
  badge.textContent = 'Loading…';
  try {
    const r = await fetch('/api/gex?symbol=' + encodeURIComponent(sym));
    if (!r.ok) {
      badge.textContent = 'HTTP ' + r.status;
      badge.style.color = '#e8423a';
      return;
    }
    const d = await r.json();
    if (!d.ok) {
      // The server always answers with HTTP 200 even on internal failure
      // (e.g. no chain data cached yet for this symbol) — d.ok is the real
      // success signal. Without this check the badge was left stuck on
      // "Loading…" and renderGex(d) would run against a response with no
      // `series`, rendering blank/broken charts with no visible error.
      badge.textContent = 'error: ' + (d.error || 'unknown');
      badge.style.color = '#e8423a';
      return;
    }
    if (d.source === 'rest') {
      badge.textContent = '⚡ live REST fetch';
      badge.style.color = '#f5a623';
    } else if (d.source === 'stream_cache') {
      badge.textContent = '● stream cache';
      badge.style.color = '#00c896';
    } else {
      badge.textContent = '';
    }
    renderGex(d);
  } catch(_) { badge.textContent = 'error'; }
}

// GEX sub-tabs
document.querySelectorAll('.gex-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.gex-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.gex-tab-panel').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('gex-panel-' + tab.dataset.gexTab).classList.add('active');
    // resize charts that were rendered while their panel was hidden
    [gexMainChart, gexIvChart, gexOiChart, gexVolChart].forEach(c => { if (c) c.resize(); });
  });
});

// Symbol selector
document.getElementById('gex-symbol-select').addEventListener('change', fetchGex);

// GEX radio toggle listeners
document.querySelectorAll('input[name="gex_view"]').forEach(el =>
  el.addEventListener('change', () => { if (gexData) renderGex(gexData); }));
document.querySelectorAll('input[name="vol_view"]').forEach(el =>
  el.addEventListener('change', () => { if (gexData) renderGex(gexData); }));

// ── auto-refresh ──────────────────────────────────────────────────────────────
// fetchGex() no-ops until the symbol selector is populated (by fetchData()'s response),
// so chain the first GEX fetch after the first data fetch rather than firing in parallel.
fetchData().then(fetchGex);
setInterval(() => {
  cd--;
  document.getElementById('scountdown').textContent = 'Refresh in ' + cd + 's';
  if (cd <= 0) { cd = 30; fetchData(); fetchGex(); }
}, 1000);
setInterval(fetchLog, 10000);
setInterval(fetchGex, 15000);
</script>
</body>
</html>"""


# ── HTTP handler ──────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/data"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            sym = (qs.get("symbol") or [None])[0]
            try:
                result = _build_api_data(sym)
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            body = json.dumps(result, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/gex"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            sym = (qs.get("symbol") or [None])[0]
            try:
                result = _build_gex_data(sym)
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            body = json.dumps(result, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/log"):
            try:
                result = _build_log_data()
            except Exception as exc:
                result = {"ok": False, "error": str(exc), "lines": []}
            body = json.dumps(result, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_):
        pass  # suppress request logs


class _ThreadingServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="MEICAgent Dashboard")
    parser.add_argument("--port", type=int, default=5050)
    args = parser.parse_args()
    port = args.port

    # Check if already running
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    already = probe.connect_ex(("127.0.0.1", port)) == 0
    probe.close()
    if already:
        print(f"Dashboard already running at http://localhost:{port} — opening browser.")
        webbrowser.open(f"http://localhost:{port}")
        sys.exit(0)

    try:
        # Loopback-only: this API has no authentication, and serves trade P&L, credit
        # amounts, strikes, and AI reasoning text — binding to all interfaces would expose
        # it to the whole LAN (or the internet, if the port is forwarded).
        server = _ThreadingServer(("127.0.0.1", port), _Handler)
    except OSError as exc:
        print(f"ERROR: Cannot bind to port {port}: {exc}")
        print(f"Check what is using it: netstat -ano | findstr :{port}")
        sys.exit(1)

    url = f"http://localhost:{port}"
    print(f"MEICAgent Dashboard  ->  {url}")
    print("Press Ctrl+C to stop.")
    threading.Timer(0.5, webbrowser.open, args=[url]).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
