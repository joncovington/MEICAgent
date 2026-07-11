"""SPX historical 0DTE replay for the paper-trading engine.

Feeds src/paper.py's deterministic process_symbol() from historical SPX option-chain
snapshots (bid/ask/delta) via the 0DTESPX.com API (https://api.0dtespx.com), so the four
risk profiles can reach statistical significance in days rather than weeks, and the
dashboard's timeframe charts are backfilled immediately. SPX only — see
docs/paper-trading.md for the XSP/NDX/RUT forward-paper-only rationale.

Known data limitation (confirmed against the live API spec): 0DTESPX provides bid/ask and
unsigned delta per strike, but no gamma/theta/vega/IV. Consequences, both handled here:
  - GEX regime gate cannot run in replay (needs gamma + open interest) — always skipped,
    flagged via snapshot["gex"] = {"ok": False, "reason": "replay_no_gamma_data"}.
  - min_iv_rank has no native IV field — replay derives a VIX-percentile proxy from
    GET /market-data/historical/{date}?series=VIX and tags every replay trade with
    iv_rank_source="vix_proxy" (see _iv_rank_proxy) so it stays distinguishable from
    forward-paper trades that use the real IV rank.

Rate limits (leaky bucket, ~0.116 credits/sec drain, market-data 0-150 credits/request)
make a per-second full-day pull infeasible, so replay marks are taken at the same 120s
cadence as the live loop (~195 marks/session) via the time-range snapshot endpoint, and
each day's fetched snapshots are cached locally under data/replay_cache/ so re-running a
day never re-hits the API.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import keyring
import keyring.errors

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paper  # noqa: E402

_API_BASE = "https://api.0dtespx.com"
_SERVICE_NAME = "meicagent"
_TOKEN_KEY = "0dtespx:bearer_token"
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "replay_cache")
_LOOP_INTERVAL_SECONDS = 120  # matches the live loop's in-position cadence
_WING_WIDTHS = [2, 5, 10]      # candidate widths scanned each mark, widest-first in paper.py


class ReplayError(RuntimeError):
    pass


def _token() -> str:
    try:
        tok = keyring.get_password(_SERVICE_NAME, _TOKEN_KEY)
    except keyring.errors.KeyringError as exc:
        raise ReplayError(f"Keyring read failed: {exc}") from exc
    if not tok:
        raise ReplayError(
            "No 0DTESPX bearer token stored. Run: python src/paper_replay.py set_token --token <token>"
        )
    return tok


def _api_get(path: str, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(f"{_API_BASE}{path}", headers={"Authorization": _token()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ReplayError(
            f"0DTESPX API {path} -> HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}") from exc
    except urllib.error.URLError as exc:
        raise ReplayError(f"0DTESPX API {path} unreachable: {exc.reason}") from exc


def _cache_path(date: str) -> str:
    return os.path.join(_CACHE_DIR, f"{date}.json")


def _load_cached_day(date: str):
    path = _cache_path(date)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


def _save_cached_day(date: str, marks: list) -> None:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    with open(_cache_path(date), "w") as f:
        json.dump(marks, f)


def _session_times(date: str) -> tuple:
    """Regular session 09:30-16:00 ET, expressed as naive datetimes for arithmetic;
    only used to build the mark schedule, not compared against real wall-clock time."""
    day = datetime.fromisoformat(date)
    return day.replace(hour=9, minute=30), day.replace(hour=16, minute=0)


def fetch_day(date: str, force: bool = False) -> list:
    """Fetch (or load from cache) one historical SPX day as a list of 120s-cadence marks,
    each an option-chain-snapshot-endpoint response plus its timestamp."""
    if not force:
        cached = _load_cached_day(date)
        if cached is not None:
            return cached

    start, end = _session_times(date)
    marks = []
    cursor = start
    while cursor <= end:
        next_cursor = min(cursor + timedelta(seconds=_LOOP_INTERVAL_SECONDS), end)
        path = (f"/market-data/option-chain-snapshots/"
                f"{cursor.strftime('%Y-%m-%dT%H:%M:%S')}Z/{next_cursor.strftime('%Y-%m-%dT%H:%M:%S')}Z")
        chunk = _api_get(path)
        marks.append({"time_et": cursor.strftime("%H:%M"), "data": chunk})
        cursor = next_cursor
        time.sleep(1.0)  # stay well under the leaky-bucket drain rate

    _save_cached_day(date, marks)
    return marks


def _iv_rank_proxy(vix_series: list, lookback: int = 252) -> dict:
    """VIX-percentile-over-lookback proxy for iv_rank, keyed by date. Approximate: VIX is
    SPX 30-day IV, not a strike-specific IV-rank percentile, hence iv_rank_source='vix_proxy'
    on every replay trade so this is auditable and distinguishable from forward-paper rows
    that carry the real, symbol-specific IV rank."""
    values = [(row["date"], row["value"]) for row in vix_series if row.get("value") is not None]
    proxy = {}
    for i, (date, value) in enumerate(values):
        window = [v for _, v in values[max(0, i - lookback):i + 1]]
        if len(window) < 2:
            proxy[date] = {"vix": value, "iv_rank": None}
            continue
        lo, hi = min(window), max(window)
        rank = (value - lo) / (hi - lo) if hi > lo else 0.5
        proxy[date] = {"vix": value, "iv_rank": round(rank, 3)}
    return proxy


def _select_candidate(strikes: list, underlying: float, wing_width: float, target_delta: float, is_call: bool):
    """Nearest-by-delta short strike + its wing, from 0DTESPX's per-strike bid/ask/delta
    rows. Mirrors tt.py's _closest_by_delta/_select_*_spread logic but keyed off delta only
    (0DTESPX provides unsigned delta, no gamma/theta/vega/IV)."""
    pool = [s for s in strikes if (s["strike"] > underlying) == is_call]
    if not pool:
        return None
    short = min(pool, key=lambda s: abs(abs(s.get("delta") or 0) - target_delta))
    wing_strike = short["strike"] + wing_width if is_call else short["strike"] - wing_width
    long_candidates = [s for s in strikes if abs(s["strike"] - wing_strike) < 0.01]
    long_ = long_candidates[0] if long_candidates else None
    if long_ is None:
        return None
    return short, long_


def build_snapshot(date: str, mark: dict, iv_proxy_for_day: dict, target_delta: float = 0.16) -> dict:
    """Build the same snapshot shape src/paper.py.process_symbol() expects from live data,
    sourced from one 0DTESPX chain mark instead of tt.py."""
    raw = mark["data"]
    underlying = raw.get("underlying_price") or raw.get("spot")
    strikes_raw = raw.get("strikes") or raw.get("chain") or []
    strikes = [
        {"strike": float(s["strike"]), "streamer_symbol": f"SPX-{s['strike']}-{s.get('type', '')}",
         "delta": s.get("delta"), "bid": s.get("bid"), "ask": s.get("ask")}
        for s in strikes_raw
    ]
    calls = [s for s in strikes if str(s["streamer_symbol"]).endswith("C") or s.get("type") == "C"]
    puts = [s for s in strikes if str(s["streamer_symbol"]).endswith("P") or s.get("type") == "P"]

    candidates = []
    for width in _WING_WIDTHS:
        call_pair = _select_candidate(calls, underlying, width, target_delta, is_call=True)
        put_pair = _select_candidate(puts, underlying, width, target_delta, is_call=False)
        if not call_pair or not put_pair:
            continue
        short_call, long_call = call_pair
        short_put, long_put = put_pair
        if any(s.get("bid") is None or s.get("ask") is None for s in (short_call, long_call, short_put, long_put)):
            continue
        candidates.append({
            "wing_width": width,
            "short_put": short_put, "long_put": long_put,
            "short_call": short_call, "long_call": long_call,
        })

    leg_quotes = {s["streamer_symbol"]: {
        "bid": s["bid"], "ask": s["ask"],
        "mid": round((s["bid"] + s["ask"]) / 2, 4) if s.get("bid") is not None and s.get("ask") is not None else None,
    } for s in strikes if s.get("bid") is not None}

    ivp = iv_proxy_for_day.get(date, {"vix": None, "iv_rank": None})

    return {
        "symbol": "SPX",
        "date": date,
        "now_et": mark["time_et"],
        "expiration": date,
        "dte": 0,
        "underlying_price": underlying,
        "iv_rank": ivp["iv_rank"],
        "iv_rank_source": "vix_proxy",
        "vix": ivp["vix"],
        "vix1d_ratio": None,   # not derivable from 0DTESPX; regime gate skips this trigger
        "atr_5day": None,      # not derivable from a single day's snapshot; regime gate skips
        "session_quality": _session_quality(mark["time_et"]),
        "gex": {"ok": False, "reason": "replay_no_gamma_data"},
        "candidates": candidates,
        "leg_quotes": leg_quotes,
    }


def _session_quality(time_et: str) -> str:
    h, m = (int(x) for x in time_et.split(":"))
    minutes = h * 60 + m
    if minutes < 10 * 60 + 15:
        return "open_volatile"
    if minutes < 12 * 60:
        return "prime"
    if minutes < 14 * 60:
        return "midday"
    if minutes < 14 * 60 + 45:
        return "afternoon"
    return "late"


def run_day(date: str, db_path: str, profiles_filter=None, force_fetch: bool = False) -> dict:
    marks = fetch_day(date, force=force_fetch)
    vix_series = _api_get(f"/market-data/historical/{date}?series=VIX").get("series", [])
    iv_proxy = _iv_rank_proxy(vix_series)

    per_mark_results = []
    for mark in marks:
        snapshot = build_snapshot(date, mark, iv_proxy)
        if not snapshot["underlying_price"]:
            continue
        result = paper.process_symbol(snapshot, db_path, execution_mode="replay",
                                       profiles_filter=profiles_filter)
        per_mark_results.append({"time_et": mark["time_et"], "result": result})

    return {"ok": True, "date": date, "marks_processed": len(per_mark_results), "results": per_mark_results}


def main():
    parser = argparse.ArgumentParser(description="SPX 0DTE historical replay for the paper-trading engine")
    parser.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                       "..", "data", "paper_trades.db"))
    sub = parser.add_subparsers(dest="command")

    p_tok = sub.add_parser("set_token", help="Store the 0DTESPX bearer token in the OS keyring")
    p_tok.add_argument("--token", required=True)

    sub.add_parser("sessions", help="List available historical trading days")

    p_run = sub.add_parser("run", help="Replay one historical SPX trading day")
    p_run.add_argument("--date", required=True, help="YYYY-MM-DD")
    p_run.add_argument("--profiles", default=None, help="Comma-separated subset; omit for all four")
    p_run.add_argument("--force_fetch", action="store_true", help="Bypass the local cache")

    args = parser.parse_args()

    if args.command == "set_token":
        try:
            keyring.set_password(_SERVICE_NAME, _TOKEN_KEY, args.token)
        except keyring.errors.KeyringError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            sys.exit(1)
        print(json.dumps({"ok": True}))
    elif args.command == "sessions":
        try:
            print(json.dumps(_api_get("/market-data/sessions")))
        except ReplayError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            sys.exit(1)
    elif args.command == "run":
        profiles_filter = args.profiles.split(",") if args.profiles else None
        try:
            result = run_day(args.date, args.db, profiles_filter, args.force_fetch)
        except ReplayError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            sys.exit(1)
        print(json.dumps(result, default=str))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
