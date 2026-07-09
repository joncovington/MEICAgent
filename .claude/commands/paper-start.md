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

## Step 3 — Paper-trading loop

Invoke the `/paper-loop` skill directly (it is self-scheduling — unlike the live loop, its instructions are not wrapped inside `/loop`; `paper-loop.md`'s own Step 5 arms its next wakeup with the `/paper-loop` prompt).

Tell the user:
"Paper-trading session started — the loop will self-pace each iteration across all four risk profiles (conservative/moderate/aggressive/very-aggressive). Writes go to data/paper_trades.db only; the live account and data/meic_trades.db are untouched. Dashboard: http://localhost:5051 (Paper Mode). Run /paper-report for a performance summary."
