"""MEICAgent Dashboard — local HTTP server serving a trading dashboard."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import sys
import threading
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

_DB_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "meic_trades.db")
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "agent.log")


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


def _today_stats(conn: sqlite3.Connection, today: str) -> dict:
    r = _one(conn, """
        SELECT COALESCE(SUM(pnl), 0)                                    AS net_pnl,
               COUNT(*)                                                  AS total_trades,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)                AS wins,
               SUM(CASE WHEN pnl IS NOT NULL AND pnl <= 0 THEN 1 ELSE 0 END) AS losses
        FROM ic_trades
        WHERE trade_date = ?
          AND (is_paper = 0 OR is_paper IS NULL)
          AND status NOT IN ('cancelled', 'pending', 'partial_entry')
    """, (today,)) or {}
    result = {
        "net_pnl":      round(float(r.get("net_pnl") or 0), 2),
        "total_trades": int(r.get("total_trades") or 0),
        "wins":         int(r.get("wins") or 0),
        "losses":       int(r.get("losses") or 0),
    }
    result["wl_ratio"] = _wl_ratio(result["wins"], result["losses"])
    return result


def _historical_stats(conn: sqlite3.Connection, start: str, today: str) -> dict:
    r = _one(conn, """
        SELECT COALESCE(SUM(net_pnl), 0)                            AS net_pnl,
               COALESCE(SUM(entries_filled), 0)                     AS total_trades,
               COALESCE(SUM(win_count), 0)                          AS wins,
               COALESCE(SUM(entries_filled - win_count), 0)         AS losses
        FROM daily_summary
        WHERE summary_date >= ? AND summary_date < ?
    """, (start, today)) or {}
    return {
        "net_pnl":      float(r.get("net_pnl") or 0),
        "total_trades": int(r.get("total_trades") or 0),
        "wins":         int(r.get("wins") or 0),
        "losses":       int(r.get("losses") or 0),
    }


def _alltime_stats(conn: sqlite3.Connection, today: str) -> dict:
    r = _one(conn, """
        SELECT COALESCE(SUM(net_pnl), 0)                            AS net_pnl,
               COALESCE(SUM(entries_filled), 0)                     AS total_trades,
               COALESCE(SUM(win_count), 0)                          AS wins,
               COALESCE(SUM(entries_filled - win_count), 0)         AS losses
        FROM daily_summary
        WHERE summary_date < ?
    """, (today,)) or {}
    return {
        "net_pnl":      float(r.get("net_pnl") or 0),
        "total_trades": int(r.get("total_trades") or 0),
        "wins":         int(r.get("wins") or 0),
        "losses":       int(r.get("losses") or 0),
    }


def _merge(hist: dict, today: dict) -> dict:
    result = {
        "net_pnl":      round(hist.get("net_pnl", 0) + today.get("net_pnl", 0), 2),
        "total_trades": hist.get("total_trades", 0) + today.get("total_trades", 0),
        "wins":         hist.get("wins", 0) + today.get("wins", 0),
        "losses":       hist.get("losses", 0) + today.get("losses", 0),
    }
    result["wl_ratio"] = _wl_ratio(result["wins"], result["losses"])
    return result


# ── Per-spread status ─────────────────────────────────────────────────────────

def _spread_statuses(trade: dict) -> tuple[dict, dict]:
    status = (trade.get("status") or "").lower()
    exit_time = trade.get("exit_time") or ""
    time_str = ""
    if exit_time:
        s = str(exit_time).replace("T", " ")
        time_str = s[11:16] if len(s) >= 16 else ""

    def b(label: str, btype: str) -> dict:
        return {"label": label, "type": btype}

    monitoring = b("monitoring", "monitoring")
    expired    = b("expired",    "expired")
    pending    = b("pending",    "pending")
    cancelled  = b("cancelled",  "cancelled")
    force      = b("force closed", "force_closed")
    stopped    = b(f"STOPPED {time_str}".strip(), "stopped")

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
        ea = trade.get("exit_analysis")
        if ea:
            try:
                obj = json.loads(ea) if isinstance(ea, str) else ea
                which = (obj.get("stopped_spread") or "").lower()
                if which == "put":
                    remaining = monitoring if status == "partial" else expired
                    return stopped, remaining
                if which == "call":
                    remaining = monitoring if status == "partial" else expired
                    return remaining, stopped
            except (json.JSONDecodeError, AttributeError):
                pass
        return stopped, stopped
    return b(status, "unknown"), b(status, "unknown")


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

def _build_api_data() -> dict:
    if not os.path.exists(_DB_PATH):
        return {"ok": False, "error": "Database not found — run: python src/db.py init_db"}

    conn = _connect()
    today = _today()
    t_stats = _today_stats(conn, today)

    stats = {
        "today":    t_stats,
        "week":     _merge(_historical_stats(conn, _week_start(),  today), t_stats),
        "month":    _merge(_historical_stats(conn, _month_start(), today), t_stats),
        "year":     _merge(_historical_stats(conn, _year_start(),  today), t_stats),
        "all_time": _merge(_alltime_stats(conn, today),                    t_stats),
    }

    raw_trades = _rows(conn, """
        SELECT ic_order_id, entry_time, fill_confirmed_at,
               put_strike, call_strike, wing_width, net_credit, quantity,
               put_credit, call_credit, status, session_quality,
               iv_rank_at_entry, iv_skew_signal, price_action_signal,
               stop_trigger_current, stop_limit_current, stop_adjustment_count,
               exit_time, exit_price, exit_reason, pnl, fees,
               ai_entry_reasoning, exit_analysis
        FROM ic_trades
        WHERE trade_date = ?
          AND (is_paper = 0 OR is_paper IS NULL)
        ORDER BY entry_time
    """, (today,))

    trades = []
    for t in raw_trades:
        put_s, call_s = _spread_statuses(t)
        row = {k: v for k, v in t.items() if k != "exit_analysis"}
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

    by_session = _rows(conn, """
        SELECT session_quality,
               COUNT(*) AS total,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
               ROUND(AVG(pnl), 2) AS avg_pnl
        FROM ic_trades
        WHERE (is_paper = 0 OR is_paper IS NULL)
          AND status NOT IN ('cancelled','pending','partial_entry')
          AND session_quality IS NOT NULL
        GROUP BY session_quality
        ORDER BY total DESC
    """)

    by_exit = _rows(conn, """
        SELECT COALESCE(exit_reason, 'open') AS exit_reason, COUNT(*) AS count
        FROM ic_trades
        WHERE (is_paper = 0 OR is_paper IS NULL)
          AND status NOT IN ('cancelled','pending','partial_entry')
        GROUP BY exit_reason
        ORDER BY count DESC
    """)

    by_iv = _rows(conn, """
        SELECT
            CASE
                WHEN iv_rank_at_entry < 0.25 THEN '<25%'
                WHEN iv_rank_at_entry < 0.50 THEN '25-50%'
                WHEN iv_rank_at_entry < 0.75 THEN '50-75%'
                ELSE '>75%'
            END AS iv_bucket,
            COUNT(*) AS trades,
            ROUND(AVG(pnl), 2) AS avg_pnl,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins
        FROM ic_trades
        WHERE (is_paper = 0 OR is_paper IS NULL)
          AND pnl IS NOT NULL AND iv_rank_at_entry IS NOT NULL
          AND status NOT IN ('cancelled','pending','partial_entry')
        GROUP BY iv_bucket
        ORDER BY MIN(iv_rank_at_entry)
    """)

    fee_row = _one(conn, """
        SELECT COALESCE(SUM(net_credit * quantity), 0) AS gross_credit,
               COALESCE(SUM(fees), 0)                  AS total_fees,
               COALESCE(SUM(pnl), 0)                   AS net_pnl
        FROM ic_trades
        WHERE (is_paper = 0 OR is_paper IS NULL)
          AND status NOT IN ('cancelled','pending','partial_entry')
    """) or {}
    gross = float(fee_row.get("gross_credit") or 0)
    fees  = float(fee_row.get("total_fees") or 0)
    net   = float(fee_row.get("net_pnl") or 0)
    fee_summary = {
        "gross_credit":  round(gross, 2),
        "total_fees":    round(fees, 2),
        "net_pnl":       round(net, 2),
        "fee_drag_pct":  round(fees / gross * 100, 1) if gross > 0 else None,
    }

    conn.close()

    return {
        "ok":         True,
        "as_of":      _now_iso(),
        "today":      today,
        "stats":      stats,
        "trades":     trades,
        "last_loop":  last_loop,
        "nlv_series": nlv_series,
        "analytics": {
            "by_session":  by_session,
            "by_exit":     by_exit,
            "by_iv":       by_iv,
            "fee_summary": fee_summary,
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
              <td class="lbl">Wins</td>
              <td class="today-col" id="w-today"></td>
              <td id="w-week"></td><td id="w-month"></td>
              <td id="w-year"></td><td id="w-all"></td>
            </tr>
            <tr>
              <td class="lbl">Losses</td>
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
              <th>TIME</th><th>WIDTH</th><th>PUT STRIKE</th><th>CALL STRIKE</th>
              <th>PUT $</th><th>CALL $</th><th>NET CREDIT</th>
              <th>PUT STATUS</th><th>CALL STATUS</th>
              <th style="text-align:right">P&amp;L</th>
            </tr>
          </thead>
          <tbody id="tbody">
            <tr><td colspan="10" class="empty">Loading&hellip;</td></tr>
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
    tbody.innerHTML = '<tr><td colspan="10" class="empty">No trades today — agent is monitoring</td></tr>';
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
    const tip = (t.ai_entry_reasoning || '').replace(/"/g, '&quot;');
    return '<tr title="' + tip + '">' +
      '<td>' + fTime(t.entry_time) + '</td>' +
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
}

// ── render all ────────────────────────────────────────────────────────────────
function renderAll(d) {
  cache = d;
  document.getElementById('disc-banner').style.display = 'none';
  document.getElementById('as-of').textContent = d.as_of
    ? d.as_of.substring(0, 19).replace('T', ' ') + ' ET' : '';
  renderStats(d.stats || {});
  renderTrades(d.trades || []);
  renderStatus(d);
  if (document.getElementById('view-history').classList.contains('active')) renderHistory(d);
}

// ── fetch ─────────────────────────────────────────────────────────────────────
async function fetchData() {
  try {
    const r = await fetch('/api/data');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    if (d.ok === false) {
      document.getElementById('tbody').innerHTML =
        '<tr><td colspan="10" class="empty">' + (d.error || 'Error loading data') + '</td></tr>';
      return;
    }
    renderAll(d);
  } catch(e) {
    document.getElementById('disc-banner').style.display = 'block';
    document.getElementById('sdot').className = 'dot err';
    document.getElementById('slabel').textContent = 'DISCONNECTED';
  }
}

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

// ── auto-refresh ──────────────────────────────────────────────────────────────
fetchData();
setInterval(() => {
  cd--;
  document.getElementById('scountdown').textContent = 'Refresh in ' + cd + 's';
  if (cd <= 0) { cd = 30; fetchData(); }
}, 1000);
setInterval(fetchLog, 10000);
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
        elif self.path == "/api/data":
            try:
                result = _build_api_data()
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
        server = _ThreadingServer(("", port), _Handler)
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
