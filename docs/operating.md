# Operating the Agent

## Pre-market session setup

Run `/meic-start` before 9:30 ET — it launches the dashboard and agent loop in sequence:

```
/meic-start
```

This launches the dashboard as a background process, opens the browser at `http://localhost:5050`, then starts the agent loop. The agent will not enter new trades before `entry_window_start` (default 10:00 ET) or after `entry_window_end` (default 14:30 ET). At end of day it force-closes non-cash-settled positions before the bell (`physical_settlement_force_close_time`/`force_close_time`) and leaves cash-settled positions to expire and settle in cash (`expiration_settlement_time`); either way, starting early is safe. On the first iteration of each trading day, the loop runs a **daily connection check** to verify the tastytrade broker session is live before any market assessment begins.

To start components individually instead:

**Dashboard** — opens the browser at `http://localhost:5050`, auto-refreshes every 30 s:
```
/dashboard
```

**Loop** — begins the MEIC agent iterations:
```
/loop
```

---

## Starting the loop

Launch Claude Code from the MEICAgent folder (run `claude` in this directory), then start the loop **before 9:30 ET**:

```
/loop
```

The agent runs every ~2-30 minutes depending on session and open positions (see the loop cadence table in `CLAUDE.md`). The loop's own time gate (Step 2) skips all market-hours checks outside 09:30–15:55 ET, on weekends, or on a NYSE holiday, so starting early or leaving it running after close is safe — it will not attempt to trade outside market hours. New entries are additionally blocked before `entry_window_start` (default 10:00 ET) to avoid open-bell volatility.

---

## Paper trading

Before committing real capital, run the parallel-shadow paper engine. It evaluates all four risk profiles (conservative / moderate / aggressive / very-aggressive) against the same live-quote snapshot per symbol, each on its own $100,000 virtual bankroll, and never touches the live account or `data/meic_trades.db`.

Start a full unattended paper session:

```
/paper-start
```

This starts the shared DXLink streamer, launches the paper dashboard at `http://localhost:5051` (badged "Paper Mode — Simulated"), and registers a Windows scheduled task (`MEICAgent-PaperLoop`) that runs `python src/paper_loop.py --once` every 2 minutes — headless, time-gated to market hours, self-healing, and persistent across sessions. At the 16:00 ET settlement pass it writes a deterministic end-of-day report to `logs/paper-eod-<date>.md`.

Manage the session directly:

```bash
python src/paper_loop.py --status          # task status + open-position count
python src/paper_loop.py --once            # run a single manual iteration
python src/paper_loop.py --eod-report      # regenerate today's paper EOD report (--date to backfill)
python src/paper_loop.py --uninstall-task  # stop the unattended session
```

For a multi-day, profile-by-profile performance write-up (equity curves, risk-adjusted metrics, graduation-gate checklist):

```
/paper-report
```

On non-Windows hosts, run `python src/paper_loop.py` in a terminal or wire a cron job to `--once`. See [paper-trading.md](paper-trading.md) for the engine design, fee model, historical-replay accelerator, and graduation criteria.

---

## Checking status during the day

```
/meic-status
```

Prints a live summary of open positions, today's P&L, and the last few loop actions without interrupting the running loop.

---

## Dashboard

A local browser dashboard provides a live view of the trading session and historical analytics.

```
/dashboard
```

Or start it directly:

```bash
python src/dashboard.py
```

Opens at `http://localhost:5050` and auto-refreshes every 30 seconds.

**Today view**
- Multi-period stats grid — Net P&L, total trades, wins, losses, and W/L ratio across today / this week / this month / this year / all-time (live trades only)
- Trades table — each IC with entry time, strikes, wing width, per-spread credits, per-spread stop status badges (e.g. `STOPPED 11:21`), and P&L

**History view**
- NLV trend chart — account value over all days where the EOD sequence has run
- Session win rate breakdown
- Exit reason breakdown
- Avg P&L by IV rank bucket
- All-time fee drag summary

The dashboard reads directly from `data/meic_trades.db` — no extra dependencies beyond what is already installed. Stop it by closing the terminal window it opened.

For paper trading, run the same dashboard against the paper database on a separate port so both can be open at once:

```
/paper-dashboard        # http://localhost:5051, badged "Paper Mode — Simulated"
```

Or directly: `python src/dashboard.py --mode paper` (drives both the `data/paper_trades.db` path and the 5051 port). In paper mode the Performance view can be filtered by risk profile as well as by symbol.

---

## Verifying chain and strike selection

Before the first live session, or after any tastytrade SDK update, run:

```
/check-chain
```

This calls `get_market_overview`, `get_option_chain`, and `get_strategies` against today's expiration (or the next trading day if the market is closed), then cross-checks that:
- Greeks and quotes are complete
- `get_strategies` used live greeks for strike selection (not a positional fallback)
- The selected strikes appear within the chain window
- Short strike deltas are within ±0.05 of `delta_target`

A **PASS** result means the chain and strike selection are ready. A **NEEDS ATTENTION** result identifies the specific failing check.

---

## End-of-day report

After 15:55 ET the agent automatically spawns the `/eod-report` skill, which:

1. Reads today's trades and loop log
2. Writes a plain English analysis of entry quality, stop management, and what worked or didn't
3. Saves the analysis to the `daily_summary` table

You can also trigger it manually at any time. `/eod-report` accepts a scope argument — `both` (default), `live`, or `paper` — and an optional `--date YYYY-MM-DD`:

```
/eod-report                       # both live and paper reports for today
/eod-report live                  # live report only
/eod-report paper --date 2026-07-08
```

The live report is a plain-English synthesis; the paper report is deterministic and code-generated (a per-profile metrics table, exits-by-reason breakdown, and per-symbol P&L across all four profiles).

---

## Logs

All loop actions are written to `logs/agent.log` as newline-delimited JSON via `python src/notify.py log_event --level <LEVEL>`. Each entry includes a timestamp, level (typically `INFO`, `WARN` for conflicts, or `CRITICAL` for escalated failures like a missed force-close on a non-cash-settled symbol), message, and optional structured data. Review `WARN`/`CRITICAL` entries after EOD to identify conflict patterns and refine agent behavior.

Every log file is size-capped with rotation (10 MB per file, 5 backups), so `agent.log`, `streamer.log`, `paper_loop.log`, and `dashboard.log` never grow without bound. The paper daemon logs to `logs/paper_loop.log` in a human-readable one-line-per-iteration format.

The easiest way to watch the log live is the **Logs tab** in the dashboard — the live dashboard (`http://localhost:5050`) tails `agent.log` and the paper dashboard (`http://localhost:5051`) tails `paper_loop.log`; both color-code WARN/ERROR entries and auto-refresh every 10 seconds.

To tail from the terminal instead:

```bash
Get-Content logs/agent.log -Wait -Tail 20   # PowerShell
tail -f logs/agent.log                       # bash
```
