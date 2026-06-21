"""SQLite CLI helper for MEICAgent. All commands print JSON to stdout."""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

try:
    import pytz
    _ET = pytz.timezone("America/New_York")
    def _now_et():
        return datetime.now(_ET)
    def _today_et():
        return _now_et().strftime("%Y-%m-%d")
except ImportError:
    def _now_et():
        return datetime.now(timezone.utc)
    def _today_et():
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "meic_trades.db")


def _connect():
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _out(data):
    print(json.dumps(data, default=str))


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS ic_trades (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date                TEXT NOT NULL,
    entry_time                TEXT,
    expiration                TEXT,
    symbol                    TEXT NOT NULL,
    put_strike                REAL,
    call_strike               REAL,
    wing_width                REAL,
    put_symbol                TEXT,
    call_symbol               TEXT,
    long_put_symbol           TEXT,
    long_call_symbol          TEXT,
    put_credit                REAL,
    call_credit               REAL,
    net_credit                REAL,
    quantity                  INTEGER DEFAULT 1,
    put_delta_at_entry        REAL,
    call_delta_at_entry       REAL,
    long_put_delta_at_entry   REAL,
    long_call_delta_at_entry  REAL,
    underlying_price_entry    REAL,
    iv_rank_at_entry          REAL,
    iv_pct_at_entry           REAL,
    session_quality           TEXT,
    iv_skew_signal            TEXT,
    price_action_signal       TEXT,
    ai_entry_reasoning        TEXT,
    ic_order_id                  TEXT UNIQUE NOT NULL,
    put_spread_entry_order_id    TEXT,
    call_spread_entry_order_id   TEXT,
    put_stop_order_id            TEXT,
    call_stop_order_id           TEXT,
    stop_trigger_original     REAL,
    stop_limit_original       REAL,
    stop_trigger_current      REAL,
    stop_limit_current        REAL,
    stop_adjustment_count     INTEGER DEFAULT 0,
    stop_adjustment_history   TEXT DEFAULT '[]',
    status                    TEXT DEFAULT 'pending',
    exit_time                 TEXT,
    exit_price                REAL,
    exit_reason               TEXT,
    exit_analysis             TEXT,
    put_stop_cost             REAL,
    call_stop_cost            REAL,
    pnl                       REAL,
    fees                      REAL,
    fill_confirmed_at         TEXT,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_summary (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    summary_date        TEXT UNIQUE NOT NULL,
    symbol              TEXT,
    total_entries       INTEGER DEFAULT 0,
    entries_filled      INTEGER DEFAULT 0,
    entries_stopped     INTEGER DEFAULT 0,
    entries_expired     INTEGER DEFAULT 0,
    entries_cancelled   INTEGER DEFAULT 0,
    gross_credit        REAL DEFAULT 0,
    gross_pnl           REAL DEFAULT 0,
    fees                REAL DEFAULT 0,
    net_pnl             REAL DEFAULT 0,
    closing_nlv         REAL,
    win_count           INTEGER DEFAULT 0,
    win_rate_pct        REAL,
    avg_iv_rank         REAL,
    sessions_entered    TEXT DEFAULT '[]',
    ai_day_summary      TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS loop_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    loop_time        TEXT NOT NULL,
    loop_date        TEXT NOT NULL,
    action           TEXT,
    reasoning        TEXT,
    open_trades_n    INTEGER DEFAULT 0,
    today_count      INTEGER DEFAULT 0,
    today_pnl        REAL DEFAULT 0,
    iv_rank          REAL,
    underlying_price REAL,
    session_quality  TEXT,
    mcp_errors       TEXT DEFAULT '[]',
    duration_ms      INTEGER,
    created_at       TEXT NOT NULL
);
"""


def cmd_init_db(_args):
    conn = _connect()
    for statement in _DDL.split(";"):
        stmt = statement.strip()
        if stmt:
            conn.execute(stmt)
    # Migrations: add columns that may be absent in databases created before this version
    existing = {row[1] for row in conn.execute("PRAGMA table_info(ic_trades)")}
    for col, col_type in [
        ("long_put_delta_at_entry",  "REAL"),
        ("long_call_delta_at_entry", "REAL"),
        ("iv_skew_signal",              "TEXT"),
        ("price_action_signal",        "TEXT"),
        ("put_spread_entry_order_id",  "TEXT"),
        ("call_spread_entry_order_id", "TEXT"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE ic_trades ADD COLUMN {col} {col_type}")
    # Drop columns removed from the schema
    if "trend_signal" in existing:
        conn.execute("ALTER TABLE ic_trades DROP COLUMN trend_signal")
    existing_ds = {row[1] for row in conn.execute("PRAGMA table_info(daily_summary)")}
    for col, col_type in [("closing_nlv", "REAL")]:
        if col not in existing_ds:
            conn.execute(f"ALTER TABLE daily_summary ADD COLUMN {col} {col_type}")
    conn.commit()
    conn.close()
    _out({"ok": True, "message": "Database initialized"})


# ---------------------------------------------------------------------------
# Read commands
# ---------------------------------------------------------------------------

def cmd_get_open_trades(_args):
    today = _today_et()
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM ic_trades WHERE status IN ('pending','open','partial','partial_entry') AND trade_date = ?",
        (today,)
    ).fetchall()
    conn.close()
    _out({"ok": True, "open_trades": [dict(r) for r in rows]})


def cmd_get_today_count(_args):
    today = _today_et()
    conn = _connect()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM ic_trades WHERE trade_date = ? AND status != 'cancelled'",
        (today,)
    ).fetchone()
    conn.close()
    _out({"ok": True, "today_count": row["n"]})


def cmd_get_today_pnl(_args):
    today = _today_et()
    conn = _connect()
    row = conn.execute(
        "SELECT COALESCE(SUM(pnl), 0) AS total FROM ic_trades WHERE trade_date = ?",
        (today,)
    ).fetchone()
    conn.close()
    _out({"ok": True, "today_pnl": round(float(row["total"]), 2)})


def cmd_get_eod_summary(_args):
    today = _today_et()
    conn = _connect()
    trades = conn.execute(
        "SELECT * FROM ic_trades WHERE trade_date = ?", (today,)
    ).fetchall()
    trades = [dict(r) for r in trades]

    total = len(trades)
    filled = sum(1 for t in trades if t["status"] not in ("pending", "cancelled"))
    stopped = sum(1 for t in trades if t["status"] == "stopped")
    expired = sum(1 for t in trades if t["status"] == "expired")
    cancelled = sum(1 for t in trades if t["status"] == "cancelled")
    gross_credit = sum((t["net_credit"] or 0) for t in trades)
    gross_pnl = sum((t["pnl"] or 0) for t in trades)
    fees = sum((t["fees"] or 0) for t in trades)
    net_pnl = gross_pnl - fees
    wins = sum(1 for t in trades if (t["pnl"] or 0) > 0)
    win_rate = round(wins / filled * 100, 1) if filled else None
    iv_values = [t["iv_rank_at_entry"] for t in trades if t["iv_rank_at_entry"] is not None]
    avg_iv = round(sum(iv_values) / len(iv_values), 1) if iv_values else None
    sessions = list({t["session_quality"] for t in trades if t["session_quality"]})

    loop_rows = conn.execute(
        "SELECT * FROM loop_log WHERE loop_date = ? ORDER BY loop_time DESC LIMIT 20",
        (today,)
    ).fetchall()
    summary_row = conn.execute(
        "SELECT ai_day_summary, closing_nlv FROM daily_summary WHERE summary_date = ?", (today,)
    ).fetchone()
    conn.close()

    _out({
        "ok": True,
        "date": today,
        "total_entries": total,
        "entries_filled": filled,
        "entries_stopped": stopped,
        "entries_expired": expired,
        "entries_cancelled": cancelled,
        "gross_credit": round(gross_credit, 2),
        "gross_pnl": round(gross_pnl, 2),
        "fees": round(fees, 2),
        "net_pnl": round(net_pnl, 2),
        "win_count": wins,
        "win_rate_pct": win_rate,
        "avg_iv_rank": avg_iv,
        "sessions_entered": sessions,
        "ai_day_summary": summary_row["ai_day_summary"] if summary_row else None,
        "closing_nlv": float(summary_row["closing_nlv"]) if summary_row and summary_row["closing_nlv"] else None,
        "trades": trades,
        "loop_log": [dict(r) for r in loop_rows],
    })


# ---------------------------------------------------------------------------
# Write commands
# ---------------------------------------------------------------------------

def cmd_save_trade(args):
    data = json.loads(args.data)
    now = str(_now_et())
    data.setdefault("trade_date", _today_et())
    data.setdefault("created_at", now)
    data["updated_at"] = now
    if "stop_adjustment_history" in data and not isinstance(data["stop_adjustment_history"], str):
        data["stop_adjustment_history"] = json.dumps(data["stop_adjustment_history"])

    cols = list(data.keys())
    placeholders = ", ".join(["?" for _ in cols])
    updates = ", ".join([f"{c} = excluded.{c}" for c in cols if c not in ("ic_order_id", "created_at")])
    sql = (
        f"INSERT INTO ic_trades ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(ic_order_id) DO UPDATE SET {updates}"
    )
    conn = _connect()
    conn.execute(sql, [data[c] for c in cols])
    conn.commit()
    rowid = conn.execute(
        "SELECT id FROM ic_trades WHERE ic_order_id = ?", (data["ic_order_id"],)
    ).fetchone()["id"]
    conn.close()
    _out({"ok": True, "id": rowid})


def cmd_update_trade(args):
    now = str(_now_et())
    fields = {}
    for attr in ("status", "exit_price", "exit_time", "exit_reason", "exit_analysis",
                 "put_stop_order_id", "call_stop_order_id",
                 "put_spread_entry_order_id", "call_spread_entry_order_id",
                 "stop_trigger_current", "stop_limit_current",
                 "pnl", "fees", "fill_confirmed_at"):
        val = getattr(args, attr, None)
        if val is not None:
            fields[attr] = val
    if not fields:
        _out({"ok": False, "error": "No fields to update"})
        return
    fields["updated_at"] = now
    set_clause = ", ".join([f"{k} = ?" for k in fields])
    sql = f"UPDATE ic_trades SET {set_clause} WHERE ic_order_id = ?"
    conn = _connect()
    cur = conn.execute(sql, list(fields.values()) + [args.ic_order_id])
    conn.commit()
    conn.close()
    _out({"ok": True, "rows_updated": cur.rowcount})


def cmd_record_stop_adjustment(args):
    now = str(_now_et())
    conn = _connect()
    row = conn.execute(
        "SELECT stop_adjustment_history, stop_adjustment_count FROM ic_trades WHERE ic_order_id = ?",
        (args.ic_order_id,)
    ).fetchone()
    if not row:
        conn.close()
        _out({"ok": False, "error": f"Trade {args.ic_order_id} not found"})
        return
    history = json.loads(row["stop_adjustment_history"] or "[]")
    history.append({
        "time": now,
        "new_trigger": args.new_trigger,
        "new_limit": args.new_limit,
        "reason": args.reason,
    })
    new_count = (row["stop_adjustment_count"] or 0) + 1
    conn.execute(
        """UPDATE ic_trades
           SET stop_trigger_current = ?,
               stop_limit_current = ?,
               stop_adjustment_count = ?,
               stop_adjustment_history = ?,
               updated_at = ?
           WHERE ic_order_id = ?""",
        (args.new_trigger, args.new_limit, new_count, json.dumps(history), now, args.ic_order_id)
    )
    conn.commit()
    conn.close()
    _out({"ok": True, "stop_adjustment_count": new_count})


def cmd_log_loop_action(args):
    now_et = _now_et()
    now_str = str(now_et)
    today = now_et.strftime("%Y-%m-%d")
    ctx = {}
    if args.market_context and args.market_context != "{}":
        try:
            ctx = json.loads(args.market_context)
        except json.JSONDecodeError:
            pass
    # Flat args override JSON context when provided
    if args.iv_rank is not None:
        ctx["iv_rank"] = args.iv_rank
    if args.session_quality is not None:
        ctx["session_quality"] = args.session_quality
    if args.underlying_price is not None:
        ctx["underlying_price"] = args.underlying_price
    if args.open_trades is not None:
        ctx["open_trades"] = args.open_trades
    if args.today_count is not None:
        ctx["today_count"] = args.today_count
    if args.today_pnl is not None:
        ctx["today_pnl"] = args.today_pnl
    conn = _connect()
    conn.execute(
        """INSERT INTO loop_log
           (loop_time, loop_date, action, reasoning,
            open_trades_n, today_count, today_pnl,
            iv_rank, underlying_price, session_quality,
            mcp_errors, duration_ms, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            now_str, today,
            args.action, args.reasoning,
            ctx.get("open_trades", 0),
            ctx.get("today_count", 0),
            ctx.get("today_pnl", 0),
            ctx.get("iv_rank"),
            ctx.get("underlying_price"),
            ctx.get("session_quality"),
            json.dumps(ctx.get("mcp_errors", [])),
            ctx.get("duration_ms"),
            now_str,
        )
    )
    conn.commit()
    conn.close()
    _out({"ok": True})


def cmd_save_daily_summary(args):
    now = str(_now_et())
    date = args.date or _today_et()
    if not args.summary and args.closing_nlv is None:
        _out({"ok": False, "error": "Provide --summary and/or --closing_nlv"})
        return
    conn = _connect()
    conn.execute(
        """INSERT INTO daily_summary (summary_date, ai_day_summary, closing_nlv, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(summary_date) DO UPDATE SET
             ai_day_summary = COALESCE(excluded.ai_day_summary, ai_day_summary),
             closing_nlv = COALESCE(excluded.closing_nlv, closing_nlv),
             updated_at = excluded.updated_at""",
        (date, args.summary, args.closing_nlv, now, now)
    )
    conn.commit()
    conn.close()
    _out({"ok": True, "date": date})


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="MEICAgent DB helper")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init_db")
    sub.add_parser("get_open_trades")
    sub.add_parser("get_today_count")
    sub.add_parser("get_today_pnl")
    sub.add_parser("get_eod_summary")

    p_save = sub.add_parser("save_trade")
    p_save.add_argument("--data", required=True)

    p_upd = sub.add_parser("update_trade")
    p_upd.add_argument("--ic_order_id", required=True)
    for f in ("status", "exit_price", "exit_time", "exit_reason", "exit_analysis",
              "put_stop_order_id", "call_stop_order_id",
              "put_spread_entry_order_id", "call_spread_entry_order_id",
              "stop_trigger_current", "stop_limit_current",
              "pnl", "fees", "fill_confirmed_at"):
        p_upd.add_argument(f"--{f}", default=None)

    p_adj = sub.add_parser("record_stop_adjustment")
    p_adj.add_argument("--ic_order_id", required=True)
    p_adj.add_argument("--new_trigger", required=True, type=float)
    p_adj.add_argument("--new_limit", required=True, type=float)
    p_adj.add_argument("--reason", required=True)

    p_dsum = sub.add_parser("save_daily_summary")
    p_dsum.add_argument("--date", default=None)
    p_dsum.add_argument("--summary", default=None)
    p_dsum.add_argument("--closing_nlv", default=None, type=float)

    p_log = sub.add_parser("log_loop_action")
    p_log.add_argument("--action", required=True)
    p_log.add_argument("--reasoning", default="")
    p_log.add_argument("--market_context", default="{}")
    p_log.add_argument("--iv_rank", default=None, type=float)
    p_log.add_argument("--session_quality", default=None)
    p_log.add_argument("--underlying_price", default=None, type=float)
    p_log.add_argument("--open_trades", default=None, type=int)
    p_log.add_argument("--today_count", default=None, type=int)
    p_log.add_argument("--today_pnl", default=None, type=float)

    args = parser.parse_args()

    dispatch = {
        "init_db": cmd_init_db,
        "get_open_trades": cmd_get_open_trades,
        "get_today_count": cmd_get_today_count,
        "get_today_pnl": cmd_get_today_pnl,
        "get_eod_summary": cmd_get_eod_summary,
        "save_trade": cmd_save_trade,
        "update_trade": cmd_update_trade,
        "save_daily_summary": cmd_save_daily_summary,
        "record_stop_adjustment": cmd_record_stop_adjustment,
        "log_loop_action": cmd_log_loop_action,
    }

    fn = dispatch.get(args.command)
    if fn is None:
        parser.print_help()
        sys.exit(1)
    fn(args)


if __name__ == "__main__":
    main()
