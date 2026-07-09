Start the full MEICAgent paper-trading session: streamer and paper-trading loop.

This is the paper-trading counterpart to `/meic-start` — it starts the same shared DXLink streamer (paper trading marks positions from real market quotes, exactly like live) but runs the isolated `/paper-loop` instead of the live trading loop. It never touches `data/meic_trades.db`, never submits a live order, and is not gated by `enable_live_trading` — see `docs/paper-trading.md` for the full design.

## Step 1 — DXLink Streamer

Check if the streamer is running:

```bash
python src/streamer.py --status
```

If `running` is `false`: start it as a hidden background process.

```bash
Start-Process python -ArgumentList 'src\streamer.py' -WorkingDirectory $PWD -WindowStyle Hidden
```

## Step 2 — Paper-trading dashboard

Invoke the `/paper-dashboard` skill to launch (or confirm already running) the paper-trading dashboard at http://localhost:5051, separate from the live dashboard's port 5050.

## Step 3 — Paper-trading loop (scheduled task)

Register the unattended paper loop as a Windows scheduled task and fire the first run immediately:

```bash
python src/paper_loop.py --install-task
```

This creates the `MEICAgent-PaperLoop` task, which runs `python src/paper_loop.py --once` every 2 minutes. Each run is a short-lived process that reliably completes, self-heals if one fails, no-ops outside market hours (it's time-gated), and persists across sessions — the robust way to run it unattended on Windows (a long-running detached daemon proved fragile against stray console events). Each `--once` runs the parallel-shadow engine across every configured symbol: marking/exiting open ICs (per-side stops, the settlement-aware force-close cascade with physical-settlement early close + friction, and cash-settled left-to-expire settlement — no profit target) and evaluating new entries per profile. All writes go to `data/paper_trades.db`; the live account and `data/meic_trades.db` are never touched.

(For a one-off manual iteration outside the task — e.g. a final force-close pass — run `python src/paper_loop.py --once`. On non-Windows hosts, run `python src/paper_loop.py` in a terminal or wire a cron job instead.)

Tell the user:
"Paper-trading session started — the paper loop is registered as a scheduled task running every 2 minutes across all four risk profiles (conservative/moderate/aggressive/very-aggressive), self-healing and time-gated to market hours. Writes go to data/paper_trades.db only; the live account and data/meic_trades.db are untouched. Dashboard: http://localhost:5051 (Paper Mode). Stop the session with `python src/paper_loop.py --uninstall-task`; run /paper-report for a performance summary."
