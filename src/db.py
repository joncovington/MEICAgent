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

_DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "meic_trades.db")
# MEIC_DB_PATH lets the paper-trading engine (src/paper.py) and its skills point every
# db.py subcommand at data/paper_trades.db instead, without duplicating this module.
_DB_PATH = os.environ.get("MEIC_DB_PATH") or _DEFAULT_DB_PATH


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
    dollar_multiplier         REAL DEFAULT 100,
    fill_confirmed_at         TEXT,
    risk_profile              TEXT,
    execution_mode            TEXT,
    iv_rank_source            TEXT,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ic_trades_date_status ON ic_trades(trade_date, status);
CREATE INDEX IF NOT EXISTS idx_ic_trades_symbol_status ON ic_trades(symbol, status);

CREATE TABLE IF NOT EXISTS ic_spread_legs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ic_order_id       TEXT NOT NULL REFERENCES ic_trades(ic_order_id),
    side              TEXT NOT NULL CHECK (side IN ('put', 'call')),
    status            TEXT NOT NULL DEFAULT 'open',
    exit_time         TEXT,
    exit_reason       TEXT,
    exit_price        REAL,
    pnl               REAL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE(ic_order_id, side)
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
    session_init_at     TEXT,
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
    symbol           TEXT,
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
        ("dollar_multiplier",       "REAL DEFAULT 100"),
        ("risk_profile",           "TEXT"),
        ("execution_mode",         "TEXT"),
        ("iv_rank_source",         "TEXT"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE ic_trades ADD COLUMN {col} {col_type}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ic_trades_profile_date "
        "ON ic_trades(risk_profile, trade_date, status)"
    )
    # Drop columns removed from the schema
    if "trend_signal" in existing:
        conn.execute("ALTER TABLE ic_trades DROP COLUMN trend_signal")
    existing_ds = {row[1] for row in conn.execute("PRAGMA table_info(daily_summary)")}
    for col, col_type in [("closing_nlv", "REAL"), ("session_init_at", "TEXT")]:
        if col not in existing_ds:
            conn.execute(f"ALTER TABLE daily_summary ADD COLUMN {col} {col_type}")
    existing_ll = {row[1] for row in conn.execute("PRAGMA table_info(loop_log)")}
    if "symbol" not in existing_ll:
        conn.execute("ALTER TABLE loop_log ADD COLUMN symbol TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_loop_log_symbol_date ON loop_log(symbol, loop_date)")
    conn.commit()
    conn.close()
    _out({"ok": True, "message": "Database initialized"})


# ---------------------------------------------------------------------------
# Read commands
# ---------------------------------------------------------------------------

def cmd_get_open_trades(args):
    # --date lets callers iterating a non-"real-today" trade_date (chiefly the paper-trading
    # replay engine, which walks historical trading days) query that day's open positions
    # instead of always the live system clock's date.
    today = getattr(args, "date", None) or _today_et()
    symbol = getattr(args, "symbol", None)
    conn = _connect()
    sql = "SELECT * FROM ic_trades WHERE status IN ('pending','open','partial','partial_entry') AND trade_date = ?"
    params: list = [today]
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol.upper())
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    _out({"ok": True, "open_trades": [dict(r) for r in rows]})


def cmd_get_today_count(args):
    today = _today_et()
    symbol = getattr(args, "symbol", None)
    conn = _connect()
    sql = "SELECT COUNT(*) AS n FROM ic_trades WHERE trade_date = ? AND status != 'cancelled'"
    params: list = [today]
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol.upper())
    row = conn.execute(sql, params).fetchone()
    conn.close()
    _out({"ok": True, "today_count": row["n"]})


def cmd_get_today_pnl(args):
    today = _today_et()
    symbol = getattr(args, "symbol", None)
    conn = _connect()
    sql = "SELECT COALESCE(SUM(pnl), 0) AS total FROM ic_trades WHERE trade_date = ?"
    params: list = [today]
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol.upper())
    row = conn.execute(sql, params).fetchone()
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
    wins = sum(1 for t in trades if t["status"] == "expired")
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


def _range_stats_for_rows(rows: list[dict]) -> dict:
    """Compute financial stats for one already-filtered group of ic_trades rows.

    net pnl per trade = pnl - fees (matches get_eod_summary's net_pnl = gross_pnl - fees
    convention, applied per-trade so profit factor / avg win-loss are dollar-accurate).
    """
    total_trades = len(rows)
    gross_credit = sum((r["net_credit"] or 0) for r in rows)
    gross_pnl = sum((r["pnl"] or 0) for r in rows)
    fees = sum((r["fees"] or 0) for r in rows)

    resolved = [r for r in rows if r["pnl"] is not None]
    net_pnls = [(r["pnl"] or 0) - (r["fees"] or 0) for r in resolved]
    wins = [p for p in net_pnls if p > 0]
    losses = [p for p in net_pnls if p <= 0]
    win_count = len(wins)
    loss_count = len(losses)
    resolved_count = win_count + loss_count

    gross_win_total = sum(wins)
    gross_loss_total = abs(sum(losses))
    profit_factor = round(gross_win_total / gross_loss_total, 3) if gross_loss_total > 0 else None
    avg_win = round(gross_win_total / win_count, 2) if win_count else None
    avg_loss = round(sum(losses) / loss_count, 2) if loss_count else None
    win_rate_pct = round(win_count / resolved_count * 100, 1) if resolved_count else None
    net_pnl_total = sum(net_pnls)
    expectancy = round(net_pnl_total / resolved_count, 2) if resolved_count else None

    max_consecutive_losses = 0
    streak = 0
    for r in rows:
        if r["pnl"] is None:
            continue
        net = (r["pnl"] or 0) - (r["fees"] or 0)
        if net <= 0:
            streak += 1
            max_consecutive_losses = max(max_consecutive_losses, streak)
        else:
            streak = 0

    # Per-day net-pnl series with a running cumulative sum, for equity-curve /
    # drawdown / worst-day computation by the caller (report or dashboard).
    by_date: dict[str, float] = {}
    for r in rows:
        if r["pnl"] is None:
            continue
        by_date.setdefault(r["trade_date"], 0.0)
        by_date[r["trade_date"]] += (r["pnl"] or 0) - (r["fees"] or 0)
    daily_pnl = []
    running = 0.0
    peak = 0.0
    max_drawdown = 0.0
    worst_day = None
    for date in sorted(by_date):
        day_pnl = round(by_date[date], 2)
        running += day_pnl
        peak = max(peak, running)
        max_drawdown = max(max_drawdown, peak - running)
        worst_day = day_pnl if worst_day is None else min(worst_day, day_pnl)
        daily_pnl.append({"date": date, "net_pnl": day_pnl, "cumulative_pnl": round(running, 2)})

    return {
        "total_trades": total_trades,
        "gross_credit": round(gross_credit, 2),
        "gross_pnl": round(gross_pnl, 2),
        "fees": round(fees, 2),
        "net_pnl": round(net_pnl_total, 2),
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate_pct": win_rate_pct,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy_per_trade": expectancy,
        "max_consecutive_losses": max_consecutive_losses,
        "max_drawdown": round(max_drawdown, 2),
        "worst_day": worst_day,
        "daily_pnl": daily_pnl,
    }


def cmd_get_range_summary(args):
    """Multi-day / multi-week P&L, win-rate, and drawdown rollup — the aggregation that
    get_eod_summary/get_today_pnl don't provide (both are hardcoded to today). Used by the
    paper-trading weekly report and, optionally, live multi-day review. Groups results by
    risk_profile so the four parallel-shadow profiles (or "unassigned" for rows with no
    profile set, e.g. live trades) can be compared side by side from one call.
    """
    if not args.start or not args.end:
        _out({"ok": False, "error": "Both --start and --end are required (YYYY-MM-DD)"})
        return
    conn = _connect()
    where = ["trade_date >= ?", "trade_date <= ?", "status NOT IN ('cancelled','pending','partial_entry')"]
    params: list = [args.start, args.end]
    if args.symbol:
        where.append("symbol = ?")
        params.append(args.symbol.upper())
    if args.profile:
        where.append("risk_profile = ?")
        params.append(args.profile)
    rows = _rows_dicts(conn, where, params)
    conn.close()

    groups: dict[str, list[dict]] = {}
    for r in rows:
        key = r["risk_profile"] or "unassigned"
        groups.setdefault(key, []).append(r)

    profiles = {key: _range_stats_for_rows(group_rows) for key, group_rows in groups.items()}

    _out({
        "ok": True,
        "start": args.start,
        "end": args.end,
        "symbol": args.symbol.upper() if args.symbol else None,
        "profiles": profiles,
    })


def _rows_dicts(conn: sqlite3.Connection, where: list[str], params: list) -> list[dict]:
    sql = (
        "SELECT trade_date, risk_profile, symbol, pnl, fees, net_credit, status "
        f"FROM ic_trades WHERE {' AND '.join(where)} ORDER BY trade_date, id"
    )
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _ic_open_fee_fallback(symbol: str):
    """Computed per-symbol IC open-fee from the shared tastytrade schedule (cherrypit.fees), replacing
    MEIC's hand-maintained fee_estimate_fallback_per_contract constants. Returns None if the core
    submodule isn't present."""
    core = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_core")
    if os.path.isdir(core) and core not in sys.path:
        sys.path.insert(0, core)
    try:
        from cherrypit.fees import ic_open_fee
    except Exception:
        return None
    return ic_open_fee(symbol)


def cmd_get_fee_estimate(args):
    """Estimate $/contract fee drag for a symbol from recent closed trades.

    Used by the fee-adjusted credit floor (Step 6): a fixed pct-of-width
    credit floor can pass a trade whose entire credit gets consumed by fees
    on symbols/wing-widths with high fee-to-premium ratios (e.g. XSP 2026-06-30:
    $4.00 gross credit, $4.96 fees, net -$0.97). Sample size is reported so
    the caller can fall back to the computed `fallback_per_contract` (from the
    shared fee schedule) when the sample is thin.
    """
    symbol = (args.symbol or "").upper()
    lookback = args.lookback or 20
    conn = _connect()
    rows = conn.execute(
        "SELECT fees, quantity FROM ic_trades "
        "WHERE symbol = ? AND status NOT IN ('pending', 'cancelled', 'partial_entry') "
        "AND fees IS NOT NULL AND quantity IS NOT NULL AND quantity > 0 "
        "ORDER BY id DESC LIMIT ?",
        (symbol, lookback),
    ).fetchall()
    conn.close()

    sample_size = len(rows)
    total_fees = sum((r["fees"] or 0) for r in rows)
    total_contracts = sum((r["quantity"] or 0) for r in rows)
    avg_fee_per_contract = round(total_fees / total_contracts, 2) if total_contracts else None

    _out({
        "ok": True,
        "symbol": symbol,
        "sample_size": sample_size,
        "avg_fee_per_contract": avg_fee_per_contract,
        "fallback_per_contract": _ic_open_fee_fallback(symbol),
        "total_fees": round(total_fees, 2),
        "total_contracts": total_contracts,
    })


def cmd_get_step_timing(args):
    """Summarize logged step latency (see `timing_stop_management` / `timing_entry_evaluation`
    rows written by log_loop_action) so entry-evaluation vs stop-management wall-clock cost can
    actually be compared, rather than inferred from the loop_log row timestamps (which cluster
    within milliseconds of each other since rows are written back-to-back at logging time, not
    spread across the work each step does).
    """
    conn = _connect()
    where = ["duration_ms IS NOT NULL"]
    params: list = []
    if args.action:
        where.append("action = ?")
        params.append(args.action)
    if args.symbol:
        where.append("symbol = ?")
        params.append(args.symbol.upper())
    if args.lookback_days:
        where.append("loop_date >= date('now', ?)")
        params.append(f"-{args.lookback_days} days")
    rows = conn.execute(
        f"SELECT action, symbol, duration_ms FROM loop_log WHERE {' AND '.join(where)} "
        "ORDER BY id DESC",
        params,
    ).fetchall()
    conn.close()

    by_action: dict[str, list[int]] = {}
    for r in rows:
        by_action.setdefault(r["action"], []).append(r["duration_ms"])

    summary = {}
    for action, durations in by_action.items():
        summary[action] = {
            "sample_size": len(durations),
            "avg_ms": round(sum(durations) / len(durations), 1),
            "min_ms": min(durations),
            "max_ms": max(durations),
        }

    _out({"ok": True, "sample_size": len(rows), "by_action": summary})


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
    """Read-modify-write on stop_adjustment_history/count for one ic_order_id.

    Wrapped in BEGIN IMMEDIATE so the SELECT and UPDATE are atomic against a
    concurrent call for the same trade — without this, two overlapping calls
    could both read the same history, and the second write would silently
    drop the first adjustment.
    """
    now = str(_now_et())
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT stop_adjustment_history, stop_adjustment_count FROM ic_trades WHERE ic_order_id = ?",
            (args.ic_order_id,)
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
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
    finally:
        conn.close()
    _out({"ok": True, "stop_adjustment_count": new_count})


def cmd_record_leg_exit(args):
    now = str(_now_et())
    conn = _connect()
    row = conn.execute(
        "SELECT id FROM ic_trades WHERE ic_order_id = ?", (args.ic_order_id,)
    ).fetchone()
    if not row:
        conn.close()
        _out({"ok": False, "error": f"Trade {args.ic_order_id} not found"})
        return
    conn.execute(
        """INSERT INTO ic_spread_legs
               (ic_order_id, side, status, exit_time, exit_reason, exit_price, pnl, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(ic_order_id, side) DO UPDATE SET
               status = excluded.status,
               exit_time = excluded.exit_time,
               exit_reason = excluded.exit_reason,
               exit_price = excluded.exit_price,
               pnl = excluded.pnl,
               updated_at = excluded.updated_at""",
        (args.ic_order_id, args.side, args.status, args.exit_time,
         args.exit_reason, args.exit_price, args.pnl, now, now)
    )
    conn.commit()
    conn.close()
    _out({"ok": True})


def cmd_get_spread_legs(args):
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM ic_spread_legs WHERE ic_order_id = ?", (args.ic_order_id,)
    ).fetchall()
    conn.close()
    _out({"ok": True, "legs": [dict(r) for r in rows]})


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
    if args.duration_ms is not None:
        ctx["duration_ms"] = args.duration_ms
    conn = _connect()
    conn.execute(
        """INSERT INTO loop_log
           (loop_time, loop_date, symbol, action, reasoning,
            open_trades_n, today_count, today_pnl,
            iv_rank, underlying_price, session_quality,
            mcp_errors, duration_ms, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            now_str, today, args.symbol,
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


def cmd_get_session_init(_args):
    today = _today_et()
    conn = _connect()
    row = conn.execute(
        "SELECT session_init_at FROM daily_summary WHERE summary_date = ?", (today,)
    ).fetchone()
    conn.close()
    already_run = bool(row and row["session_init_at"])
    _out({"already_run": already_run})


def cmd_set_session_init(_args):
    now = str(_now_et())
    today = _today_et()
    conn = _connect()
    conn.execute(
        """INSERT INTO daily_summary (summary_date, session_init_at, created_at, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(summary_date) DO UPDATE SET
             session_init_at = excluded.session_init_at,
             updated_at = excluded.updated_at""",
        (today, now, now, now)
    )
    conn.commit()
    conn.close()
    _out({"ok": True, "session_init_at": now})


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
    global _DB_PATH
    parser = argparse.ArgumentParser(description="MEICAgent DB helper")
    parser.add_argument("--db", default=None,
                         help="Override the database path (defaults to MEIC_DB_PATH env var, "
                              "then data/meic_trades.db). Used by the paper-trading engine to "
                              "point at data/paper_trades.db.")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init_db")
    p_open = sub.add_parser("get_open_trades")
    p_open.add_argument("--symbol", default=None, help="Filter to one symbol; omit for all symbols")
    p_open.add_argument("--date", default=None,
                         help="Override trade_date to query (YYYY-MM-DD); defaults to the real "
                              "system today. Used by paper-trading replay to query a historical day.")
    p_cnt = sub.add_parser("get_today_count")
    p_cnt.add_argument("--symbol", default=None, help="Filter to one symbol; omit for all symbols")
    p_pnl = sub.add_parser("get_today_pnl")
    p_pnl.add_argument("--symbol", default=None, help="Filter to one symbol; omit for all symbols")
    sub.add_parser("get_eod_summary")
    sub.add_parser("get_session_init")
    sub.add_parser("set_session_init")

    p_range = sub.add_parser("get_range_summary")
    p_range.add_argument("--start", required=True, help="Inclusive start date, YYYY-MM-DD")
    p_range.add_argument("--end", required=True, help="Inclusive end date, YYYY-MM-DD")
    p_range.add_argument("--profile", default=None,
                          help="Filter to one risk_profile; omit to group by every profile present")
    p_range.add_argument("--symbol", default=None, help="Filter to one symbol; omit for all symbols")

    p_fee = sub.add_parser("get_fee_estimate")
    p_fee.add_argument("--symbol", required=True)
    p_fee.add_argument("--lookback", default=20, type=int)

    p_timing = sub.add_parser("get_step_timing")
    p_timing.add_argument("--action", default=None,
                           help="Filter to one action, e.g. timing_stop_management or timing_entry_evaluation")
    p_timing.add_argument("--symbol", default=None)
    p_timing.add_argument("--lookback_days", default=None, type=int)

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

    p_leg = sub.add_parser("record_leg_exit")
    p_leg.add_argument("--ic_order_id", required=True)
    p_leg.add_argument("--side", required=True, choices=["put", "call"])
    p_leg.add_argument("--status", required=True)
    p_leg.add_argument("--exit_time", default=None)
    p_leg.add_argument("--exit_reason", default=None)
    p_leg.add_argument("--exit_price", default=None, type=float)
    p_leg.add_argument("--pnl", default=None, type=float)

    p_getlegs = sub.add_parser("get_spread_legs")
    p_getlegs.add_argument("--ic_order_id", required=True)

    p_dsum = sub.add_parser("save_daily_summary")
    p_dsum.add_argument("--date", default=None)
    p_dsum.add_argument("--summary", default=None)
    p_dsum.add_argument("--closing_nlv", default=None, type=float)

    p_log = sub.add_parser("log_loop_action")
    p_log.add_argument("--symbol", default=None,
                        help="Symbol this log row is for; omit for an iteration-level summary row spanning all symbols")
    p_log.add_argument("--action", required=True)
    p_log.add_argument("--reasoning", default="")
    p_log.add_argument("--market_context", default="{}")
    p_log.add_argument("--iv_rank", default=None, type=float)
    p_log.add_argument("--session_quality", default=None)
    p_log.add_argument("--underlying_price", default=None, type=float)
    p_log.add_argument("--open_trades", default=None, type=int)
    p_log.add_argument("--today_count", default=None, type=int)
    p_log.add_argument("--today_pnl", default=None, type=float)
    p_log.add_argument("--duration_ms", default=None, type=int,
                        help="Elapsed wall-clock milliseconds for the step this row represents (e.g. stop management or one symbol's entry evaluation)")

    args = parser.parse_args()

    if args.db:
        _DB_PATH = args.db
    elif "MEIC_DB_PATH" in os.environ:
        _DB_PATH = os.environ["MEIC_DB_PATH"]

    dispatch = {
        "init_db": cmd_init_db,
        "get_open_trades": cmd_get_open_trades,
        "get_today_count": cmd_get_today_count,
        "get_today_pnl": cmd_get_today_pnl,
        "get_eod_summary": cmd_get_eod_summary,
        "get_session_init": cmd_get_session_init,
        "set_session_init": cmd_set_session_init,
        "get_range_summary": cmd_get_range_summary,
        "get_fee_estimate": cmd_get_fee_estimate,
        "get_step_timing": cmd_get_step_timing,
        "save_trade": cmd_save_trade,
        "update_trade": cmd_update_trade,
        "save_daily_summary": cmd_save_daily_summary,
        "record_stop_adjustment": cmd_record_stop_adjustment,
        "log_loop_action": cmd_log_loop_action,
        "record_leg_exit": cmd_record_leg_exit,
        "get_spread_legs": cmd_get_spread_legs,
    }

    fn = dispatch.get(args.command)
    if fn is None:
        parser.print_help()
        sys.exit(1)
    fn(args)


if __name__ == "__main__":
    main()
