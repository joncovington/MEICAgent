"""Live latency benchmark for the DXLink streamer HTTP API.

Requires: streamer running (`python src/streamer.py` in another terminal).
Usage:    python tests/bench_streamer.py [--wait-warmup] [--symbol XSP]

Prints per-command latency and the `source` field so you can verify
which tier (stream_cache / rest_cache / live) each command used.

Exit code 1 if the streamer is not reachable.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_TT = [sys.executable, str(_ROOT / "src" / "tt.py")]
_STREAMER_PORT = 7699

# Latency thresholds (ms). Tests fail if any command exceeds its limit.
# These are generous to avoid false failures on slow CI machines.
_THRESHOLDS = {
    "stream_status":       500,
    "get_quote":           500,   # Tier 1: stream_cache
    "get_option_chain":    500,   # Tier 1: stream_cache
    "get_strategies_w1":   500,   # Tier 1: stream_cache
    "get_strategies_w2":   500,   # Tier 1: stream_cache
    "get_account_info":    500,   # Tier 2: rest_cache (after first poll)
    "get_positions":       500,   # Tier 2: rest_cache
    "get_working_orders":  500,   # Tier 2: rest_cache
    "get_market_overview": 500,   # Tier 2: rest_cache
    "execute_trade_dry":  3000,   # Tier 3: live REST
}


def _check_streamer() -> bool:
    try:
        body = json.dumps({"command": "stream_status", "args": {}}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{_STREAMER_PORT}/api",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=3)
        return True
    except Exception:
        return False


def _run(cmd: list[str]) -> tuple[float, dict]:
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    try:
        out = json.loads(proc.stdout)
    except Exception:
        out = {"ok": False, "raw": proc.stdout[:200], "stderr": proc.stderr[:200]}
    return elapsed_ms, out


def _fmt(ms: float, threshold: int) -> str:
    flag = "  " if ms <= threshold else "!!"
    return f"{flag} {ms:6.0f}ms"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="XSP")
    parser.add_argument(
        "--wait-warmup", action="store_true",
        help="Wait up to 20s for the REST poller first cycle to complete"
    )
    parser.add_argument(
        "--no-thresholds", action="store_true",
        help="Print results without failing on slow commands"
    )
    args = parser.parse_args()
    sym = args.symbol

    if not _check_streamer():
        print("ERROR: Streamer not reachable on port", _STREAMER_PORT)
        print("Start it with:  python src/streamer.py")
        sys.exit(1)

    print(f"Streamer reachable. Symbol: {sym}")

    if args.wait_warmup:
        print("Waiting up to 20s for REST poller first cycle…", flush=True)
        deadline = time.time() + 20
        warmed = False
        while time.time() < deadline:
            _, out = _run(_TT + ["get_account_info"])
            if out.get("source") == "rest_cache":
                warmed = True
                break
            time.sleep(1)
        print("REST cache warm." if warmed else "REST cache not warm yet — continuing anyway.")

    results: list[tuple[str, float, dict]] = []

    def bench(label: str, *cmd_args: str) -> dict:
        ms, out = _run(_TT + list(cmd_args))
        results.append((label, ms, out))
        src = out.get("source", "—")
        ok = "ok" if out.get("ok") else "ERR"
        print(f"  {_fmt(ms, _THRESHOLDS.get(label, 9999))}  [{ok}] [{src}]  {label}")
        return out

    print("\n── Tier 1: stream cache (quotes / chain / greeks) ───────────────")
    bench("stream_status",  "stream_status")
    bench("get_quote",      "get_quote",        "--symbol", sym)
    bench("get_option_chain", "get_option_chain", "--symbol", sym,
          "--include_quotes", "--include_greeks", "--strike_count", "10")
    bench("get_strategies_w1", "get_strategies",  "--symbol", sym,
          "--wing_width", "1", "--short_delta", "0.15")
    bench("get_strategies_w2", "get_strategies",  "--symbol", sym,
          "--wing_width", "2", "--short_delta", "0.15")

    print("\n── Tier 2: REST cache (polled every 15s) ────────────────────────")
    bench("get_account_info",   "get_account_info")
    bench("get_positions",      "get_positions")
    bench("get_working_orders", "get_working_orders")
    bench("get_market_overview","get_market_overview", "--symbols", sym)

    print("\n── Tier 3: live REST (dedicated REST loop) ──────────────────────")
    dry_order = json.dumps({
        "order_type": "Limit",
        "time_in_force": "Day",
        "legs": [
            {"symbol": f"{sym}  260630P00585000", "action": "Sell to Open",  "quantity": 1},
            {"symbol": f"{sym}  260630P00580000", "action": "Buy to Open",   "quantity": 1},
            {"symbol": f"{sym}  260630C00600000", "action": "Sell to Open",  "quantity": 1},
            {"symbol": f"{sym}  260630C00605000", "action": "Buy to Open",   "quantity": 1},
        ],
        "price": 0.50,
    })
    bench("execute_trade_dry", "execute_trade", "--order", dry_order)

    # ── Summary ─────────────────────────────────────────────────────────────
    print("\n── Summary ──────────────────────────────────────────────────────")
    failures = []
    for label, ms, out in results:
        threshold = _THRESHOLDS.get(label, 9999)
        status = "PASS" if ms <= threshold else "FAIL"
        if not out.get("ok"):
            status = "ERR "
        if status != "PASS":
            failures.append(label)
        print(f"  {status}  {ms:6.0f}ms / {threshold}ms  {label}")

    if failures and not args.no_thresholds:
        print(f"\n{len(failures)} command(s) exceeded threshold: {', '.join(failures)}")
        sys.exit(1)
    else:
        print("\nAll commands within threshold." if not failures else "\nDone (thresholds ignored).")


if __name__ == "__main__":
    main()
