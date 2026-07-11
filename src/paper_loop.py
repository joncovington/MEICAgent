"""Standalone paper-trading loop daemon.

Runs the parallel-shadow paper engine (src/paper.py) unattended across every configured
symbol on the live market-hours cadence - the code counterpart to the agent-orchestrated
.claude/commands/paper-loop.md, so a paper session runs in the background like the streamer
instead of needing a per-iteration agent invocation. All writes go to data/paper_trades.db;
the live account and data/meic_trades.db are never touched.

Each iteration, for every symbol in config.json's `symbols`:
  - fetches the live underlying price + IV rank, the shared VIX / VIX1D (→ ratio), and GEX,
  - builds wing-width candidates (from `wing_widths_by_symbol`) at the VIX-banded short delta,
  - hands the snapshot to paper.process_symbol, which marks/exits open ICs across all four
    profiles (including the physically-settled early force-close + friction) and evaluates
    new entries per profile.

CLI:
  python src/paper_loop.py            # run the loop in the foreground
  python src/paper_loop.py --once     # run a single iteration and exit (for testing)
  python src/paper_loop.py --status   # print daemon status
  python src/paper_loop.py --stop     # stop a running daemon

Launch hidden in the background (like the streamer):
  Start-Process python -ArgumentList 'src/paper_loop.py' -WorkingDirectory $PWD -WindowStyle Hidden
"""

import argparse
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    import pytz
    _ET = pytz.timezone("America/New_York")
    def _now_et():
        return datetime.now(_ET)
except ImportError:
    def _now_et():
        return datetime.now(UTC)

_ROOT = Path(__file__).resolve().parent.parent
_PID_FILE = _ROOT / "data" / "paper_loop.pid"
_LOCK_FILE = _ROOT / "data" / "paper_loop.once.lock"
_LOG_FILE = _ROOT / "logs" / "paper_loop.log"
_TASK_NAME = "MEICAgent-PaperLoop"
_PAPER_DB = str(_ROOT / "data" / "paper_trades.db")
_CONFIG_PATH = _ROOT / "config.json"
_TT = [sys.executable, str(_ROOT / "src" / "tt.py")]
_DB = [sys.executable, str(_ROOT / "src" / "db.py"), "--db", _PAPER_DB]

sys.path.insert(0, str(_ROOT / "src"))
from cherrypick.core import calendar as _cal  # noqa: E402  (shared NYSE trading-day calendar)

import paper  # noqa: E402  (also bootstraps src/_core onto sys.path for cherrypick.core)

logger = logging.getLogger("paper_loop")
_stop = False


def _pythonw():
    """The windowless interpreter (pythonw.exe) next to sys.executable, so the scheduled task's
    --once runs don't flash a console window every tick. Falls back to sys.executable."""
    cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return cand if os.path.exists(cand) else sys.executable


def _emit(obj):
    """Write JSON to stdout when there is one. Under pythonw.exe (the headless task runs)
    sys.stdout is None, so a plain print() would crash the run after its work is done."""
    try:
        sys.stdout.write(json.dumps(obj, default=str) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def _setup_logging(console: bool = True):
    _LOG_FILE.parent.mkdir(exist_ok=True)
    # Rotate so the log can't grow without bound (10 MB x 5 backups).
    handlers = [RotatingFileHandler(_LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")]
    # A detached, hidden-window process (Start-Process -WindowStyle Hidden) can have an
    # invalid stdout; writing to it via a StreamHandler risks killing the daemon. Only attach
    # the console handler for interactive/--once runs where stdout is real.
    if console and sys.stdout is not None and sys.stdout.isatty():
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


def _handle_signal(signum, frame):
    global _stop
    _stop = True
    logger.info("Received signal %s - stopping after this iteration.", signum)


# ---------------------------------------------------------------------------
# tt.py / db.py helpers
# ---------------------------------------------------------------------------

# Spawning children from a detached, hidden Windows process is fragile unless the child
# gets no inherited console/stdin: give every subprocess an explicit null stdin and the
# CREATE_NO_WINDOW flag so it can't attach to (or block on) the parent's hidden console.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _subrun(cmd, timeout=90):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)


def _run_json(cmd, timeout=90):
    try:
        r = _subrun(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"ok": False, "error": (r.stdout[-200:] + r.stderr[-200:]).strip()}


def _load_config():
    with open(_CONFIG_PATH) as f:
        return json.load(f)


def _symbols(cfg):
    if cfg.get("symbols"):
        return [str(s).strip().upper() for s in cfg["symbols"] if str(s).strip()]
    if cfg.get("symbol"):
        return [str(cfg["symbol"]).strip().upper()]
    return ["XSP"]


# ---------------------------------------------------------------------------
# Market-data → snapshot
# ---------------------------------------------------------------------------

def _delta_target(cfg, vix):
    if vix is None:
        return cfg.get("delta_target", 0.18)
    if vix <= cfg.get("vix_band_low_max", 18):
        return cfg.get("delta_target_vix_low", 0.16)
    if vix <= cfg.get("vix_band_elevated_max", 25):
        return cfg.get("delta_target_vix_elevated", 0.14)
    if vix <= cfg.get("vix_band_high_max", 35):
        return cfg.get("delta_target_vix_high", 0.12)
    return cfg.get("delta_target_vix_crisis", 0.10)


def _session_quality(now):
    mins = now.hour * 60 + now.minute
    if mins < 10 * 60 + 15:
        return "open_volatile"
    if mins < 12 * 60:
        return "prime"
    if mins < 14 * 60:
        return "midday"
    if mins < 14 * 60 + 45:
        return "afternoon"
    return "late"


def _fetch_vix():
    d = _run_json(_TT + ["get_market_overview", "--symbols", "VIX"])
    if not d.get("ok"):
        return None
    for m in d.get("metrics", []):
        for k in ("close", "last", "mark"):
            if m.get(k) is not None:
                try:
                    return float(m[k])
                except (TypeError, ValueError):
                    pass
    return None


def _fetch_overview(symbol):
    """Return (underlying_price, iv_rank) or (None, None)."""
    d = _run_json(_TT + ["get_market_overview", "--symbols", symbol])
    if not d.get("ok"):
        return None, None
    for m in d.get("metrics", []):
        if str(m.get("symbol", "")).upper() != symbol:
            continue
        price = None
        for k in ("close", "last", "mark"):
            if m.get(k) is not None:
                try:
                    price = float(m[k])
                    break
                except (TypeError, ValueError):
                    pass
        ivr = m.get("implied_volatility_index_rank")
        ivr = float(ivr) if ivr is not None else None
        return price, ivr
    return None, None


def _build_candidates(symbol, last, widths, delta_target, today):
    """Fetch the 0DTE chain and build wing-width candidates + leg_quotes. Mirrors the
    strike-selection the live loop/paper-loop.md describe: nearest-delta short strikes,
    wings at each configured width. A wide strike window (80) also covers near-money legs
    of already-open ICs so they mark this iteration."""
    chain = _run_json(_TT + ["get_option_chain", "--symbol", symbol, "--expiration", today,
                             "--include_quotes", "--include_greeks",
                             "--around_price", str(last), "--strike_count", "80"])
    if not chain.get("ok"):
        return [], {}, chain.get("error", "chain fetch failed")
    entries = []
    for _exp, rows in chain.get("chain", {}).items():
        entries = rows
        break
    calls, puts = [], []
    for e in entries:
        strike = e.get("strike_price") or e.get("strike")
        if strike is None or e.get("bid") is None or e.get("ask") is None:
            continue
        ot = str(e.get("option_type", "")).lower()
        rec = {"strike": float(strike), "streamer_symbol": e.get("streamer_symbol"),
               "delta": e.get("delta"), "bid": e.get("bid"), "ask": e.get("ask")}
        if "c" in ot and "p" not in ot:
            calls.append(rec)
        elif "p" in ot:
            puts.append(rec)
    leg_quotes = {r["streamer_symbol"]: {"bid": r["bid"], "ask": r["ask"],
                                         "mid": round((r["bid"] + r["ask"]) / 2, 4)}
                  for r in calls + puts if r["streamer_symbol"]}
    if not calls or not puts:
        return [], leg_quotes, "no call/put quotes"

    def nearest(pool, target):
        c = [x for x in pool if x["delta"] is not None]
        return min(c, key=lambda x: abs(abs(x["delta"]) - target)) if c else None

    short_call = nearest([c for c in calls if c["strike"] > last], delta_target)
    short_put = nearest([p for p in puts if p["strike"] < last], delta_target)
    if not short_call or not short_put:
        return [], leg_quotes, "no short strike near delta"
    by_call = {c["strike"]: c for c in calls}
    by_put = {p["strike"]: p for p in puts}
    candidates = []
    for w in widths:
        lc = by_call.get(short_call["strike"] + w)
        lp = by_put.get(short_put["strike"] - w)
        if lc and lp:
            candidates.append({"wing_width": w, "short_put": short_put, "long_put": lp,
                               "short_call": short_call, "long_call": lc})
    return candidates, leg_quotes, None


def _open_count():
    d = _run_json(_DB + ["get_open_trades"])
    return len(d.get("open_trades", [])) if d.get("ok") else 0


# ---------------------------------------------------------------------------
# Iteration
# ---------------------------------------------------------------------------

_PROF_ABBR = {"conservative": "cons", "moderate": "mod", "aggressive": "agg", "very-aggressive": "vagg"}


def _fmt_symbol(sym, info):
    """One readable clause per symbol. Collapses the common case where all four profiles did
    the same thing (e.g. all skipped for the same reason) into 'all <reason>'; otherwise lists
    each profile's outcome. Fills/exits already read as 'FILL $x' / 'EXIT <action>'."""
    if info.get("error"):
        return f"{sym}: ERROR {info['error']}"
    ivr = info.get("ivr")
    ivr_s = f"{ivr:.2f}" if isinstance(ivr, (int, float)) else "-"
    outc = info.get("outcomes") or {}
    ordered = [outc.get(p) for p in paper.ALL_PROFILE_NAMES]
    if not any(ordered):
        return f"{sym}(ivr {ivr_s}): -"
    if len(set(ordered)) == 1 and ordered[0] is not None:
        return f"{sym}(ivr {ivr_s}): all {ordered[0]}"
    parts = " ".join(f"{_PROF_ABBR.get(p, p)}:{outc.get(p, '-')}" for p in paper.ALL_PROFILE_NAMES)
    return f"{sym}(ivr {ivr_s}): {parts}"


def _format_iteration(now_et, vix, vix1d_ratio, delta_target, summary):
    """A compact, human-readable one-line iteration summary for the log and the dashboard."""
    vix_s = f"{vix:.1f}" if isinstance(vix, (int, float)) else "-"
    ratio_s = f"{vix1d_ratio:.2f}" if isinstance(vix1d_ratio, (int, float)) else "-"
    header = f"[{now_et} ET] VIX {vix_s}  1D-ratio {ratio_s}  delta {delta_target}"
    body = "   ".join(_fmt_symbol(s, summary[s]) for s in summary)
    return f"{header}   {body}"


# ---------------------------------------------------------------------------
# Deterministic end-of-day report
# ---------------------------------------------------------------------------

def _eod_report_path(day):
    return _LOG_FILE.parent / f"paper-eod-{day}.md"


def _money(x):
    return f"-${abs(x):,.2f}" if x is not None and x < 0 else f"${x:,.2f}" if x is not None else "-"


def _write_eod_report(day):
    """Write a deterministic end-of-day paper report for `day` to logs/paper-eod-<day>.md.
    Code-generated (no agent) so it runs unattended from the daemon's settlement pass. Uses
    db.py get_range_summary for the tested per-profile metrics, plus a direct read for the
    exit-reason breakdown and per-symbol P&L. Returns the path written."""
    summ = _run_json(_DB + ["get_range_summary", "--start", day, "--end", day])
    profiles = summ.get("profiles", {}) if summ.get("ok") else {}

    exits, by_symbol = {}, {}
    entries = open_n = 0
    net_total = 0.0
    try:
        con = sqlite3.connect(_PAPER_DB)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT symbol, status, exit_reason, pnl, fees FROM ic_trades WHERE trade_date=?",
            (day,)).fetchall()
        con.close()
    except sqlite3.Error:
        rows = []
    for r in rows:
        st = r["status"]
        if st == "cancelled":
            continue
        entries += 1
        net = (r["pnl"] or 0) - (r["fees"] or 0)
        net_total += net
        by_symbol[r["symbol"]] = by_symbol.get(r["symbol"], 0.0) + net
        if st in ("open", "partial", "pending", "partial_entry"):
            open_n += 1
            reason = "(still open)"
        else:
            reason = r["exit_reason"] or st
        exits[reason] = exits.get(reason, 0) + 1

    L = [f"# Paper Trading - EOD Report {day}", ""]
    L.append("_Deterministic parallel-shadow engine. MEIC has no profit target - exits are "
             "per-side stops, non-cash-settled time force-close, or cash-settled "
             "expiration-settlement._")
    L.append("")
    L.append("## Account-wide (all profiles)")
    L.append(f"- Entries filled: **{entries}**")
    L.append(f"- Net P&L (net of fees): **{_money(round(net_total, 2))}**")
    L.append(f"- Still open at report time: {open_n}")
    if by_symbol:
        L.append("- By symbol: " + ", ".join(f"{s} {_money(round(v, 2))}" for s, v in sorted(by_symbol.items())))
    L.append("")

    L.append("## Per profile")
    L.append("| Profile | Trades | Wins | Losses | Win % | Net P&L | Expectancy/IC | Profit Factor | Max DD |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for name in ("conservative", "moderate", "aggressive", "very-aggressive"):
        s = profiles.get(name)
        if not s:
            L.append(f"| {name} | 0 | - | - | - | $0.00 | - | - | - |")
            continue
        wr = f"{s['win_rate_pct']:.0f}%" if s.get("win_rate_pct") is not None else "-"
        pf = f"{s['profit_factor']:.2f}" if s.get("profit_factor") is not None else "-"
        exp = _money(s.get("expectancy_per_trade")) if s.get("expectancy_per_trade") is not None else "-"
        L.append(f"| {name} | {s.get('total_trades', 0)} | {s.get('win_count', 0)} | "
                 f"{s.get('loss_count', 0)} | {wr} | {_money(s.get('net_pnl'))} | {exp} | {pf} | "
                 f"{_money(s.get('max_drawdown'))} |")
    L.append("")

    L.append("## Exits by reason")
    if exits:
        L.append("| Reason | Count |")
        L.append("|---|---|")
        for reason, cnt in sorted(exits.items(), key=lambda kv: -kv[1]):
            L.append(f"| {reason} | {cnt} |")
    else:
        L.append("_No entries today - flat session._")
    L.append("")
    L.append(f"_Generated {_now_et().strftime('%Y-%m-%d %H:%M:%S %Z')} · paper DB only; live account untouched._")

    path = _eod_report_path(day)
    path.write_text("\n".join(L), encoding="utf-8")
    return path


def run_iteration(cfg, force=False):
    now = _now_et()
    if not force and not _is_trading_time(now, cfg):
        logger.info("outside trading window (%s ET) - skipping.", now.strftime("%H:%M"))
        return {"skipped": "outside_trading_window"}
    today = now.strftime("%Y-%m-%d")
    now_et = now.strftime("%H:%M")
    vix = _fetch_vix()
    vix1d = _run_json(_TT + ["get_vix1d"]).get("last")
    vix1d_ratio = round(vix1d / vix, 3) if (vix1d and vix) else None
    delta_target = _delta_target(cfg, vix)
    widths_by_sym = cfg.get("wing_widths_by_symbol", {})
    session = _session_quality(now)

    summary = {}
    for symbol in _symbols(cfg):
        price, ivr = _fetch_overview(symbol)
        if price is None:
            summary[symbol] = {"error": "no price"}
            continue
        widths = widths_by_sym.get(symbol, widths_by_sym.get("DEFAULT", [2, 5, 10]))
        candidates, leg_quotes, cand_err = _build_candidates(symbol, price, widths, delta_target, today)
        gex = _run_json(_TT + ["get_gex", "--symbol", symbol])
        snapshot = {
            "symbol": symbol, "date": today, "now_et": now_et, "expiration": today, "dte": 0,
            "underlying_price": price, "iv_rank": ivr, "iv_rank_source": "native",
            "vix": vix, "vix1d_ratio": vix1d_ratio,
            "atr_5day": None,  # no historical-OHLC source wired in; ATR pct gate stays inactive
            "session_quality": session,
            "gex": gex if gex.get("ok") else {"ok": False},
            "candidates": candidates, "leg_quotes": leg_quotes,
        }
        result = paper.process_symbol(snapshot, _PAPER_DB, "paper")
        # Per-profile outcome for the log - fills and exits made to stand out from skips.
        outcomes = {}
        for prof, actions in result.get("results", {}).items():
            for a in actions:
                if a.get("entry") == "filled":
                    outcomes[prof] = f"FILL ${a.get('net_credit')}"
                elif a.get("entry") == "skipped":
                    outcomes.setdefault(prof, a.get("reason"))
                elif "decision" in a:
                    act = a["decision"].get("action")
                    if act and act != "hold":
                        outcomes[prof] = f"EXIT {act}"
        summary[symbol] = {"ivr": ivr, "widths": [c["wing_width"] for c in candidates],
                           "outcomes": outcomes, "cand_err": cand_err}

    reason = _format_iteration(now_et, vix, vix1d_ratio, delta_target, summary)
    try:
        _subrun(_DB + ["log_loop_action", "--action", "paper_iteration", "--reasoning", reason[:900]])
    except Exception:
        pass
    logger.info("%s", reason)

    # Once-per-day EOD report, written on the first pass at/after the settlement time (16:00),
    # i.e. after positions have settled. The file-exists guard makes it fire exactly once even
    # though the daemon keeps ticking through the 16:00–16:05 settlement window.
    sett = cfg.get("expiration_settlement_time", "16:00")
    if (now.hour * 60 + now.minute) >= paper._time_to_minutes(sett) and not _eod_report_path(today).exists():
        try:
            p = _write_eod_report(today)
            logger.info("wrote EOD report: %s", p)
        except Exception as exc:
            logger.warning("EOD report failed: %s", exc)
    return summary


# ---------------------------------------------------------------------------
# Cadence / time gate
# ---------------------------------------------------------------------------

def _is_trading_time(now, cfg):
    # Weekend + NYSE-holiday gate via the shared calendar (cfg kept for signature compatibility).
    if not _cal.is_trading_day(now.date()):
        return False
    mins = now.hour * 60 + now.minute
    # Runs 09:30 through 16:05 - the extra 5 min past the 16:00 close lets the settlement pass
    # fire so cash-settled positions left to expire get settled at the close (paper.settlement_
    # active fires at expiration_settlement_time, default 16:00). Entries are independently
    # blocked after entry_window_end (14:30), so the extension only affects marking/settlement.
    return 9 * 60 + 30 <= mins < 16 * 60 + 5


def _sleep_seconds(now, cfg, open_positions):
    if not _is_trading_time(now, cfg):
        return 600  # idle outside market hours; daemon stays up until --stop
    return 120 if open_positions > 0 else 300


# ---------------------------------------------------------------------------
# Daemon control (mirrors src/streamer.py)
# ---------------------------------------------------------------------------

def _running_pid():
    if not _PID_FILE.exists():
        return None
    try:
        pid = int(_PID_FILE.read_text().strip())
    except ValueError:
        _PID_FILE.unlink(missing_ok=True)
        return None
    alive = False
    try:
        import psutil
        alive = psutil.pid_exists(pid)
    except ImportError:
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


def _task_installed():
    if os.name != "nt":
        return False
    r = subprocess.run(["schtasks", "/Query", "/TN", _TASK_NAME], capture_output=True, text=True)
    return r.returncode == 0


def _cmd_status():
    pid = _running_pid()
    info = {
        "daemon_running": pid is not None,   # a long-running --start daemon (if used)
        "pid": pid,
        "scheduled_task": _task_installed(),  # the recommended --install-task automation
        "open_positions": _open_count(),
    }
    _emit(info)


def _cmd_stop():
    pid = _running_pid()
    if pid is None:
        _emit({"ok": False, "error": "not running"})
        return
    try:
        os.kill(pid, signal.SIGTERM)
        _emit({"ok": True, "signal": "SIGTERM", "pid": pid})
    except Exception as exc:
        _emit({"ok": False, "error": str(exc)})


def _spawn_detached():
    """Launch a fully detached background copy of the daemon and return. On Windows this uses
    DETACHED_PROCESS (no console at all) + a new process group, so the daemon can't be stopped
    by stray CTRL_C/CTRL_CLOSE console events from whatever shell launched it - the failure
    mode that made -WindowStyle Hidden / Task Scheduler launches exit within seconds. Stop it
    explicitly with --stop."""
    existing = _running_pid()
    if existing is not None:
        _emit({"ok": False, "error": f"Paper loop already running (pid {existing})."})
        return
    kwargs = {}
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--_run"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, cwd=str(_ROOT), **kwargs)
    time.sleep(2)
    pid = _running_pid()
    _emit({"ok": pid is not None, "pid": pid,
                      "detail": "daemon spawned" if pid else "spawn did not register a PID yet"})


def _loop():
    _PID_FILE.parent.mkdir(exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, _handle_signal)
    if os.name == "nt":
        # A detached daemon can still receive stray CTRL_C/CTRL_BREAK console events (e.g. when
        # the launching shell closes) that would otherwise trip the graceful _stop and exit the
        # loop within seconds. Ignore them; --stop hard-terminates via TerminateProcess instead.
        for _sig in (signal.SIGINT, getattr(signal, "SIGBREAK", None)):
            if _sig is not None:
                try:
                    signal.signal(_sig, signal.SIG_IGN)
                except Exception:
                    pass
    else:
        try:
            signal.signal(signal.SIGINT, _handle_signal)
        except Exception:
            pass
    logger.info("Paper loop daemon started (pid %d).", os.getpid())
    try:
        while not _stop:
            cfg = _load_config()
            now = _now_et()
            if _is_trading_time(now, cfg):
                try:
                    run_iteration(cfg)
                except Exception as exc:
                    logger.exception("iteration error: %s", exc)
            else:
                logger.info("outside trading window (%s ET) - idling.", now.strftime("%H:%M"))
            delay = _sleep_seconds(_now_et(), cfg, _open_count())
            # Sleep in short slices so a SIGTERM is honored promptly.
            for _ in range(delay):
                if _stop:
                    break
                time.sleep(1)
    finally:
        _PID_FILE.unlink(missing_ok=True)
        logger.info("Paper loop daemon stopped.")


# ---------------------------------------------------------------------------
# --once concurrency lock (the scheduled task fires every 2 min; a slow iteration
# must not overlap the next and double-process the paper DB)
# ---------------------------------------------------------------------------

def _acquire_once_lock():
    try:
        fd = os.open(str(_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:  # steal a stale lock (a prior --once that died mid-run)
            if time.time() - os.path.getmtime(_LOCK_FILE) > 180:
                os.unlink(_LOCK_FILE)
                return _acquire_once_lock()
        except OSError:
            pass
        return False


def _release_once_lock():
    try:
        os.unlink(_LOCK_FILE)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Scheduled-task automation (the robust unattended launcher on Windows)
# ---------------------------------------------------------------------------
# A long-running detached daemon proved fragile on Windows (stray console events / abrupt
# death of a child-spawning process). Instead the automation registers a Task Scheduler job
# that runs `--once` every 2 minutes - each run is a short-lived process that reliably
# completes, self-heals if one fails, no-ops outside market hours (time-gated), and persists
# across sessions independent of any launching shell.

def _install_task():
    if os.name != "nt":
        _emit({"ok": False, "error": "scheduled-task install is Windows-only; on "
                          "other OSes run `python src/paper_loop.py` in a terminal or via cron"})
        return
    # pythonw.exe = no console window, so the every-2-min --once run is truly headless.
    tr = f'"{_pythonw()}" "{os.path.abspath(__file__)}" --once'
    r = subprocess.run(["schtasks", "/Create", "/TN", _TASK_NAME, "/TR", tr,
                        "/SC", "MINUTE", "/MO", "2", "/F", "/IT"],
                       capture_output=True, text=True)
    ok = r.returncode == 0
    # Fire one run immediately so positions are managed without waiting for the first tick.
    if ok:
        subprocess.run(["schtasks", "/Run", "/TN", _TASK_NAME], capture_output=True, text=True)
    _emit({"ok": ok, "task": _TASK_NAME, "cadence": "every 2 min",
                      "detail": (r.stdout or r.stderr).strip()})


def _uninstall_task():
    if os.name != "nt":
        _emit({"ok": False, "error": "Windows-only"})
        return
    subprocess.run(["schtasks", "/End", "/TN", _TASK_NAME], capture_output=True, text=True)
    r = subprocess.run(["schtasks", "/Delete", "/TN", _TASK_NAME, "/F"], capture_output=True, text=True)
    _emit({"ok": r.returncode == 0, "detail": (r.stdout or r.stderr).strip()})


def main():
    parser = argparse.ArgumentParser(description="MEICAgent paper-trading loop daemon")
    parser.add_argument("--install-task", action="store_true",
                        help="Register a Windows scheduled task that runs --once every 2 min "
                             "(the recommended unattended launcher) and fire one run now")
    parser.add_argument("--uninstall-task", action="store_true",
                        help="Remove the scheduled task (stops the unattended paper session)")
    parser.add_argument("--start", action="store_true",
                        help="Spawn a long-running detached daemon in the background (alternative "
                             "to the scheduled task; less robust on Windows)")
    parser.add_argument("--status", action="store_true", help="Print daemon/task status and exit")
    parser.add_argument("--stop", action="store_true", help="Stop a running --start daemon")
    parser.add_argument("--once", action="store_true", help="Run a single iteration and exit")
    parser.add_argument("--force", action="store_true",
                        help="With --once, run even outside market hours (for testing)")
    parser.add_argument("--eod-report", action="store_true",
                        help="Write the deterministic end-of-day paper report now (regenerates)")
    parser.add_argument("--date", default=None, help="With --eod-report, the day (YYYY-MM-DD); default today")
    parser.add_argument("--_run", action="store_true", help=argparse.SUPPRESS)  # internal: the detached child
    args = parser.parse_args()

    if args.status:
        _cmd_status()
        return
    if args.eod_report:
        day = args.date or _now_et().strftime("%Y-%m-%d")
        path = _write_eod_report(day)
        _emit({"ok": True, "report": str(path)})
        return
    if args.stop:
        _cmd_stop()
        return
    if args.install_task:
        _install_task()
        return
    if args.uninstall_task:
        _uninstall_task()
        return
    if args.start:
        _spawn_detached()
        return

    if args.once:
        _setup_logging(console=True)
        if not _acquire_once_lock():
            _emit({"ok": True, "skipped": "another --once is already running"})
            return
        try:
            summary = run_iteration(_load_config(), force=args.force)
        finally:
            _release_once_lock()
        _emit({"ok": True, "summary": summary})
        return

    # Bare invocation (or the internal --_run child): run the loop in this process. Foreground
    # in a terminal, or the detached child spawned by --start.
    existing = _running_pid()
    if existing is not None:
        _emit({"ok": False, "error": f"Paper loop already running (pid {existing}). "
                                                 f"Run 'python src/paper_loop.py --stop' first."})
        raise SystemExit(1)
    _setup_logging(console=False)  # daemon: file-only, no fragile stdout dependency
    _loop()


if __name__ == "__main__":
    main()
