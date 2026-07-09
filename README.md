# MEICAgent

An autonomous options trading agent running the **Multiple Entry Iron Condor (MEIC)** strategy on 0DTE index options. Rather than a traditional rules-only trading-bot framework, the agent itself runs the decision loop every few minutes during market hours, reading live market data, checking a stack of risk gates, and deciding whether to enter, hold, or close positions. It runs inside **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** (Anthropic's CLI coding assistant), which executes the operating instructions in `CLAUDE.md` and the skills in `.claude/commands/`. It talks to tastytrade directly via their official Python SDK (OAuth2, no middleman broker API). Live trading is gated behind an explicit config flag and defaults to dry-run.

**New in this release:** a full **paper-trading system** that shadow-trades all four risk profiles against live quotes with zero capital, a dedicated paper dashboard, an unattended self-healing daemon, corrected MEIC exit rules (cash-settled positions are now left to expire, not force-closed), and automated end-of-day reports. See [What's new](#whats-new).

---

## Quick start

**Prerequisites:** Python 3.11+, a tastytrade account, and [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — Anthropic's CLI coding assistant, which runs the agent's decision loop and every `/`-command below. Install it with `npm install -g @anthropic-ai/claude-code`, then launch it from the project folder with `claude`.

> Two kinds of commands appear in this guide: plain `python …` commands run in a normal terminal, and `/`-prefixed commands (like `/meic-start`) are Claude Code skills you type at the `claude` prompt. The skills just orchestrate the same underlying `python src/*.py` calls.

**New to this? First, open a terminal and get the code.** You'll need [Git](https://git-scm.com/downloads) installed. Open your terminal:
- **Windows** — the Git installer includes "Git Bash"; use it for every command here.
- **macOS** — open **Terminal** (Applications → Utilities), or install Git via `xcode-select --install`.
- **Linux** — open your terminal; install Git with your package manager (e.g. `sudo apt install git`).

Then download this project and move into its folder:

```bash
# 1. Download ("clone") the project, then move into the folder it creates
git clone https://github.com/joncovington/MEICAgent.git
cd MEICAgent
```

Every command below is run from inside that `MEICAgent` folder. On macOS/Linux, if `python`/`pip` aren't found, use `python3`/`pip3` instead.

```bash
# 2. Install dependencies (tastytrade, keyring, pytz, flask from pyproject.toml)
pip install -e .
pip install pytest pytest-asyncio     # optional — only needed to run the test suite

# 3. Initialize the database
python src/db.py init_db

# 4. Launch Claude Code, then run the guided credential + config setup:
claude
```
```
/setup                                # inside Claude Code — stores credentials, creates config
```

(Prefer to configure by hand instead of `/setup`? Copy `config.example.json` to `config.json` and store credentials with `python src/tt.py secrets_set`.)

Then, inside Claude Code, pick a track:

**Paper trading (recommended first)** — no capital, no live orders, runs all four risk profiles side by side:

```
/paper-start
```

Launches the market-data streamer, the paper dashboard at `http://localhost:5051`, and (on Windows) registers a self-healing scheduled task that evaluates every configured symbol every 2 minutes during market hours. On **macOS/Linux**, the scheduled task isn't available — instead keep the loop running in a terminal with `python src/paper_loop.py`, or wire a cron job to `python src/paper_loop.py --once` every 2 minutes.

**Live / dry-run trading** — the real agent loop (defaults to dry-run until `enable_live_trading: true`):

```
/meic-start
```

Launches the streamer, the live dashboard at `http://localhost:5050`, and the agent loop.

See [docs/setup.md](docs/setup.md) for the full walkthrough and [docs/paper-trading.md](docs/paper-trading.md) for the paper-trading design and graduation criteria.

---

## What's new

- **Parallel-shadow paper trading** — every trading day, all four risk profiles (conservative / moderate / aggressive / very-aggressive) are evaluated deterministically against the *same* live-quote snapshot per symbol, each with its own $100,000 virtual bankroll. No capital, no live orders, apples-to-apples profile comparison. Optional SPX historical-replay mode front-loads samples from past days that actually paid. See [docs/paper-trading.md](docs/paper-trading.md).
- **Corrected MEIC exit rules** — iron condors have exactly three exits: a per-side software stop, a time-based force-close **before the bell for non-cash-settled symbols only** (QQQ/IWM/equities — avoids physical assignment), and **left-to-expire cash settlement for cash-settled symbols** (SPX/XSP). There is **no profit-target exit** — that was removed as it isn't part of MEIC. Event days (FOMC, triple-witching, quarterly) still force-close everything as risk overrides.
- **Two dashboards, one codebase** — the same dashboard runs against the live account (port 5050) or the paper account (port 5051, visibly badged "Paper Mode"), so you can watch both at once without confusion.
- **Realistic fee modeling** — the paper engine charges tastytrade's exact broad-based-index-options fee schedule per leg (commission, clearing, ORF, per-symbol exchange fee, TAF on sells), so simulated P&L reflects real cost drag.
- **Unattended, self-healing daemon** — the paper loop runs as a Windows scheduled task firing a short-lived process every 2 minutes: headless, time-gated to market hours, and persistent across sessions. It writes a deterministic end-of-day report automatically at the settlement pass.
- **Automated end-of-day reports** — `/eod-report` reproduces the live report, the paper report, or both; the paper daemon also emits its report unattended each day. Bounded log rotation keeps every log file from growing without limit.

---

## Features

- **Multi-symbol, one shared risk budget** — trades multiple underlyings (e.g. SPX + XSP + QQQ + IWM) concurrently in a single loop pass, sharing one account-wide buying-power/position-count budget rather than per-symbol silos. Correlation risk across symbols is not yet guarded — avoid configuring highly correlated symbols (e.g. SPX and XSP) together until that safeguard exists.
- **No hardcoded contract logic** — all contract-specific parameters (instrument type, dollar multiplier, leg symbols) are read directly from the live strategy scan, so adding a new symbol needs no code changes, only a config entry.
- **Settlement-aware exits** — cash-settled index options are left to expire and settled in cash; physically-settled symbols are force-closed before the bell to avoid assignment, with a missed close on a non-cash symbol escalated as an assignment-risk failure rather than routine cleanup.
- **Live DXLink streaming daemon** — persistent WebSocket connection maintaining a rolling near-the-money option window per symbol (quotes, greeks, open interest, trade volume), so entry decisions and GEX calculations run off sub-second cached data instead of cold REST calls.
- **Per-symbol GEX (Gamma Exposure) engine** — computes net GEX, gamma flip, call wall, and put wall live from real open interest and greeks, both from open-interest positioning and from actual traded volume.
- **Adaptive per-side stop management** — call and put spreads managed independently; a stopped side doesn't force-close the untouched side.
- **Opening Range Breakout (ORB) sub-strategy** — a directional debit-spread complement to the core IC strategy, capturing the 9:30–9:35 ET range and trading breakouts. ORB keeps its own profit target and stop, distinct from the iron-condor exit rules.
- **Fee-aware credit floors** — rejects entries where estimated fees would eat most/all of the collected premium, using each symbol's own historical fee data once enough trades exist.
- **Full audit trail** — every loop iteration, entry, rejection reason, and stop adjustment is logged with reasoning, plus an automatically-written end-of-day narrative report.

## Simplified entry gate logic

All of the following must pass — any one failure blocks the trade:

1. **Time window** — no entries before 10:00 ET or after 14:30 ET. At end of day, non-cash-settled positions are force-closed before the bell; cash-settled positions are left to expire and settle in cash. Event days force-close everything.
2. **IV rank floor** — skip if IV rank is too low (insufficient premium to justify gamma risk).
3. **Late-entry bias** — on borderline-IV days, wait until noon rather than accept thin morning credit for the same directional exposure.
4. **Strike selection** — target a VIX-banded short-strike delta, then apply hard floors on top: minimum distance (%) from spot for both the short call and short put, and a ceiling on the actual call delta regardless of what the delta-target scan picked.
5. **Credit floors** — two independent checks: credit as a % of spread width, and a fee-adjusted floor (the credit must clear estimated fees by a real, width-aware margin). Both are width- and IV-aware, so narrow low-credit setups where fees would consume the premium are rejected.
6. **GEX regime gate** — no new iron condors when the symbol is in a *negative* gamma regime (dealers short gamma = trending/volatile conditions where mean-reversion strategies like MEIC underperform); ORB entries are exempt since they want that regime.
7. **Account-wide caps** — max concurrent condors and max daily entries are shared across every traded symbol, not per-symbol.
8. **Event calendars** — hard blackouts/tighter rules around FOMC announcements, quarterly expiry, and triple witching.

## Risk Profiles

Switch entry-gate thresholds with a single command instead of hand-editing `config.json`. A **risk profile** bundles IV-rank floors, credit minimums, delta limits, stop triggers, and position caps — each preset offsets its gate relaxations with compensating constraints (fewer concurrent ICs, tighter stops) so you're reallocating risk, not just adding it.

| Profile | What it does | Trade-off |
|---|---|---|
| **conservative** (default) | Strict IV-rank (≥30%) and credit floors, wide OTM buffers, latest entry time (12:00 PM) | Fewest trades (~1–2/day), highest per-trade safety margin |
| **moderate** | Slightly relax IV-rank (≥22%) and credit floors, enter earlier (11:00 AM) | ~1 more trade/day, thinner credit cushion but offset by tighter 93% stop |
| **aggressive** | Tier 1 + accept closer-to-money strikes (delta 0.22, OTM tighter); cap 3 concurrent ICs instead of 4 | ~2–3 more trades/week, each one riskier but position cap and 90% stop limit total exposure |
| **very-aggressive** | Tier 2 + trade through higher-VIX (≤30) and trending (ATR ≤40) conditions; cap 2 concurrent ICs, stop at 85% | Most trades (~3–5 more/week on active weeks), each with high gamma/pin risk; only for deliberate short experiments |

Use `/set-risk-profile <name>` to switch (backed up automatically, takes effect on next loop). The paper-trading system runs **all four profiles at once** so you can compare them on identical market days before committing to one. Start at **moderate** after 2–4 weeks if conservative rejects 40%+ of entries. See [docs/risk-profiles.md](docs/risk-profiles.md) for the full rationale, decision tree, and when to escalate.

## Paper trading

Before risking capital, run the parallel-shadow paper engine to build a performance record:

```
/paper-start                              # streamer + paper dashboard + unattended daemon
/paper-report                             # weekly (or custom-range) profile comparison
python src/paper_loop.py --eod-report     # deterministic end-of-day report on demand
python src/paper_loop.py --status         # daemon/task status + open-position count
python src/paper_loop.py --uninstall-task # stop the unattended session
```

Every 2 minutes during market hours, the engine takes one live-quote snapshot per symbol and runs all four risk profiles against it deterministically — synthetic fills at natural bid, each profile on its own $100,000 virtual bankroll, tastytrade's exact fee schedule applied per leg. Writes go only to `data/paper_trades.db`; the live account and `data/meic_trades.db` are never touched, and no live order is ever submitted (paper mode is not gated by `enable_live_trading`).

A pre-registered **graduation gate** (≥30 filled ICs, positive expectancy, ≥65% win rate, profit factor 1.3–4.0, bounded drawdown and worst day) decides when a profile has earned live capital. See [docs/paper-trading.md](docs/paper-trading.md) for the full design, the SPX historical-replay accelerator, and the known limitations of a frictionless paper model.

## Dashboard

The same local web dashboard (auto-refreshing) runs in two modes:

- **Live mode** (`/dashboard`, port 5050) — your real tastytrade account
- **Paper mode** (`/paper-dashboard`, port 5051) — the paper account, visibly badged "Paper Mode — Simulated" so it can never be mistaken for real data

Both can run at once. Views:

- **Performance view** — P&L by day/week/month/all-time, equity and underwater curves, win-rate/profit-factor/expectancy trends, and risk-adjusted tiles (Sharpe/Sortino/Calmar/recovery factor); filterable by symbol, and by risk profile in paper mode
- **Today view** — live open positions with per-spread credits and per-side stop badges, plus a multi-period stats grid
- **GEX view** — horizontal by-strike gamma exposure profile (classic dealer-positioning chart style) with call wall / put wall / zero-gamma reference lines and a live spot-price trail; toggle OI-based vs volume-based GEX side by side
- **IV Skew & Volume tabs** — call/put IV curve and open-interest/volume-by-strike from the same live data
- **Live log tail** — streaming agent log with level filtering, so you can watch the reasoning in real time

Everything runs locally against your own tastytrade account — no cloud dependency for trade execution.

---

## Documentation

- [Setup](docs/setup.md) — installation, configuration, database init, going live
- [Operating](docs/operating.md) — starting the loop, status, dashboard, EOD report, logs
- [Strategy](docs/strategy.md) — MEIC structure, wing width selection, stops, exit rules, EOD settlement handling
- [Paper trading](docs/paper-trading.md) — the parallel-shadow engine, fee model, historical replay, graduation gate, known limitations
- [Risk Profiles](docs/risk-profiles.md) — trade-off tiers for entry-gate thresholds, when to switch, full rationale

---

## Project structure

```
MEICAgent/
├── CLAUDE.md                        # Agent operational brain (loaded every loop iteration)
├── config.example.json              # Config template — copy to config.json
├── config.risk.json                 # Risk-profile presets (conservative → very-aggressive)
├── src/
│   ├── tt.py                        # tastytrade CLI — get_quote, get_strategies, execute_trade, etc.
│   ├── streamer.py                  # Persistent DXLink streaming daemon (live quotes/greeks/OI/volume)
│   ├── session.py                   # OAuth2 session management
│   ├── credentials.py               # OS-keyring credential storage
│   ├── db.py                        # SQLite CLI helper (live + paper databases)
│   ├── notify.py                    # Structured log CLI helper
│   ├── paper.py                     # Deterministic parallel-shadow paper engine (all 4 profiles)
│   ├── paper_loop.py                # Unattended paper daemon / scheduled-task runner + EOD report
│   ├── paper_replay.py              # SPX historical-replay mode (0DTESPX data)
│   └── dashboard.py                 # Local browser dashboard (--mode live|paper)
├── docs/
│   ├── setup.md                     # Installation and configuration
│   ├── operating.md                 # Running and monitoring the agent
│   ├── strategy.md                  # MEIC strategy details and exit rules
│   ├── paper-trading.md             # Paper-trading engine, fee model, graduation gate
│   └── risk-profiles.md             # Entry-gate threshold presets and when to use each
├── .claude/
│   ├── settings.json                # Permissions and MCP environment overrides
│   └── commands/
│       ├── meic-start.md            # /meic-start — launch full live session
│       ├── paper-start.md           # /paper-start — launch full paper session
│       ├── setup.md                 # /setup — credentials and initial config
│       ├── set-risk-profile.md      # /set-risk-profile — switch entry-gate preset
│       ├── daily-check.md           # Daily broker-connection check (Step 3 of the loop)
│       ├── execute-entry.md         # Entry execution (Step 7 of the loop)
│       ├── stop-management.md       # Per-side stop management (Step 5 of the loop)
│       ├── dashboard.md             # /dashboard — live dashboard (port 5050)
│       ├── paper-dashboard.md       # /paper-dashboard — paper dashboard (port 5051)
│       ├── paper-loop.md            # /paper-loop — one paper iteration
│       ├── eod-report.md            # /eod-report — live and/or paper EOD report
│       ├── paper-report.md          # /paper-report — multi-day profile comparison
│       ├── meic-status.md           # /meic-status — quick session status
│       └── check-chain.md           # /check-chain — verify chain and strike selection
├── data/                            # Created at first run (gitignored)
│   ├── meic_trades.db               # Live trade history, loop log, daily summaries
│   ├── paper_trades.db              # Paper trade history (all four profiles)
│   └── stream_cache.db              # Live streamer cache (quotes/greeks/OI/volume/GEX history)
└── logs/                            # Created at first run (gitignored; all rotated)
    ├── agent.log                    # Agent session log
    ├── streamer.log                 # Streamer daemon log
    ├── paper_loop.log               # Paper daemon log
    ├── dashboard.log                # Dashboard server log
    ├── eod-<date>.md                # Daily live end-of-day report
    └── paper-eod-<date>.md          # Daily paper end-of-day report
```

---

## License

MIT — see [LICENSE](LICENSE) for full terms.

---

## Disclaimer

This software is provided for **educational and informational purposes only**. It is not financial advice, investment advice, trading advice, or any other type of advice.

- The authors and contributors are not registered investment advisors, broker-dealers, or financial planners.
- Nothing in this repository constitutes a recommendation to buy, sell, or hold any security or financial instrument.
- Options trading involves substantial risk of loss and is not appropriate for all investors. 0DTE options carry extreme risk due to rapid time decay and gamma exposure.
- Past performance of any strategy — simulated or live — does not guarantee future results.
- You are solely responsible for all trading decisions and any resulting gains or losses.
- Always consult a qualified financial professional before trading with real capital.

**Use this software at your own risk. The authors accept no liability for any financial losses incurred through its use.**
