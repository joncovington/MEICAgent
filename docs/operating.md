# Operating the Agent

## Pre-market session setup

Run `/meic-start` before 9:30 ET — it launches the watchdog, dashboard, and agent loop in sequence:

```
/meic-start
```

This launches the watchdog and dashboard as background processes, opens the browser at `http://localhost:5050`, then starts the agent loop. The agent will not enter new trades before `entry_window_start` (default 09:45 ET) or after 15:30 ET, so starting early is safe. On the first iteration of each trading day, the loop runs a **daily connection check** (Step 2.5) to verify the tastytrade MCP is live before any market assessment begins.

To start components individually instead:

**Watchdog** — alerts via email and Windows toast if the loop stops running:
```
/watchdog
```

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

Open the MEICAgent folder in VS Code with the Claude Code extension (or run `claude` from this directory), then start the loop **before 9:30 ET**:

```
/loop
```

The agent runs every ~5 minutes. The tastytrade MCP gates all market-hours checks, so starting early or leaving it running after close is safe — it will not attempt to trade outside market hours. New entries are additionally blocked before `entry_window_start` (default 09:45 ET) to avoid open-bell volatility.

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

**Paper view**
- Same stats grid and trades table as Today, but filtered to paper trades (`is_paper = 1`)
- Includes slippage column showing simulated entry cost vs. mid-market credit

**History view**
- NLV trend chart — account value over all days where the EOD sequence has run
- Session win rate breakdown
- Exit reason breakdown
- Avg P&L by IV rank bucket
- All-time fee drag summary

The dashboard reads directly from `data/meic_trades.db` — no extra dependencies beyond what is already installed. Stop it by closing the terminal window it opened.

---

## Verifying chain and strike selection

Before the first live session, or after any tastytrade-mcp update, run:

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
4. Sends an EOD email via SendGrid

You can also trigger it manually at any time:

```
/eod-report
```

---

## Logs

All loop actions are written to `logs/agent.log` as newline-delimited JSON. Each entry includes a timestamp, level (`INFO` or `WARN`), message, and optional structured data. Review `WARN` entries after EOD to identify conflict patterns and refine agent behavior.

The easiest way to watch the log live is the **Logs tab** in the dashboard (`http://localhost:5050`) — it tails the last 200 lines, color-codes WARN/ERROR entries, and auto-refreshes every 10 seconds.

To tail from the terminal instead:

```bash
Get-Content logs/agent.log -Wait -Tail 20   # PowerShell
tail -f logs/agent.log                       # bash
```
