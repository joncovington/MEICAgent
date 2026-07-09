Run one iteration of the MEICAgent parallel-shadow paper-trading loop.

As of the standalone runner, the paper loop is implemented in code (`src/paper_loop.py`), not agent orchestration — this keeps a single source of truth for the iteration logic. Prefer the unattended daemon (`/paper-start`) for a full session; use this skill for a **single manual iteration** (e.g. a one-off force-close pass, or a quick check).

## Run one iteration

```bash
python src/paper_loop.py --once
```

This runs, for every symbol in `config.json`'s `symbols`, one full pass: fetch the live underlying price + IV rank, the shared VIX / VIX1D (→ ratio, → VIX-banded short delta), and GEX; build wing-width candidates from `wing_widths_by_symbol`; then hand the snapshot to `paper.process_symbol`, which marks/exits every open paper IC across all four profiles (per-side stops, the settlement-aware force-close cascade with the physically-settled early close + assignment/pin friction, and cash-settled left-to-expire settlement — no profit target) and evaluates new entries per profile. All writes go to `data/paper_trades.db`; the live account and `data/meic_trades.db` are never touched.

Report the per-symbol, per-profile outcomes from the JSON it prints (fills, skip reasons, or exits).

## Unattended session

For a full session that runs on its own without per-iteration invocation, use `/paper-start`, which registers a Windows scheduled task running `--once` every 2 minutes (robust, self-healing, persists across sessions, time-gated to market hours). Manage it directly with:

```bash
python src/paper_loop.py --install-task    # register + fire the first run (recommended)
python src/paper_loop.py --status          # daemon/task status + open-position count
python src/paper_loop.py --uninstall-task  # stop the unattended session
python src/paper_loop.py --eod-report       # write logs/paper-eod-<date>.md now (--date to backfill)
```

The daemon also writes that deterministic end-of-day report automatically, once, at the 16:00 settlement pass — a per-profile metrics table (trades, win rate, net P&L, expectancy, profit factor, max drawdown), an exits-by-reason breakdown, and per-symbol P&L. It's code-generated (no agent), distinct from the agent-synthesized `/paper-report`.

On non-Windows hosts, run `python src/paper_loop.py` in a terminal (or wire a cron job to `--once`). A long-running detached daemon (`--start`) also exists but is less robust on Windows than the scheduled task.

Details of the metrics, gates, force-close cascade, and graduation criteria are in `docs/paper-trading.md`.
