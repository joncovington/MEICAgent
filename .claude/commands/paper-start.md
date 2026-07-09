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

## Step 3 — Paper-trading loop daemon

Check if the paper loop daemon is already running:

```bash
python src/paper_loop.py --status
```

If `running` is `false`, start it as a hidden background process (same pattern as the streamer). `src/paper_loop.py` runs the parallel-shadow engine unattended across every configured symbol on the live market-hours cadence (120s with open positions, 300s idle), so no per-iteration agent invocation is needed:

```bash
Start-Process python -ArgumentList 'src/paper_loop.py' -WorkingDirectory $PWD -WindowStyle Hidden
```

(For a one-off manual iteration instead of the daemon — e.g. to force a final force-close pass — run `python src/paper_loop.py --once`.)

Tell the user:
"Paper-trading session started — the paper loop daemon is running unattended across all four risk profiles (conservative/moderate/aggressive/very-aggressive), self-pacing on the market-hours cadence. Writes go to data/paper_trades.db only; the live account and data/meic_trades.db are untouched. Dashboard: http://localhost:5051 (Paper Mode). Stop with `python src/paper_loop.py --stop`; run /paper-report for a performance summary."
