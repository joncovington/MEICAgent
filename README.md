# MEICAgent

An autonomous options trading agent running the **Multiple Entry Iron Condor (MEIC)** strategy on 0DTE index options. Rather than a traditional rules-only trading-bot framework, the agent itself runs the decision loop every few minutes during market hours, reading live market data, checking a stack of risk gates, and deciding whether to enter, hold, or close positions. It talks to tastytrade directly via their official Python SDK (OAuth2, no middleman broker API). Live trading is gated behind an explicit config flag and defaults to dry-run.

## Features

- **Multi-symbol, one shared risk budget** — trades multiple underlyings (e.g. XSP + SPX) concurrently in a single loop pass, sharing one account-wide buying-power/position-count budget rather than per-symbol silos. Correlation risk across symbols is not yet guarded — avoid configuring highly correlated symbols (e.g. SPX and XSP) together until that safeguard exists.
- **No hardcoded contract logic** — all contract-specific parameters (instrument type, dollar multiplier, leg symbols) are read directly from the live strategy scan, so adding a new symbol needs no code changes, only a config entry
- **Live DXLink streaming daemon** — persistent WebSocket connection maintaining a rolling near-the-money option window per symbol (quotes, greeks, open interest, trade volume), so entry decisions and GEX calculations run off sub-second cached data instead of cold REST calls
- **Per-symbol GEX (Gamma Exposure) engine** — computes net GEX, gamma flip, call wall, and put wall live from real open interest and greeks, both from open-interest positioning and from actual traded volume
- **Adaptive per-side stop management** — call and put spreads managed independently; a stopped side doesn't force-close the untouched side
- **Opening Range Breakout (ORB) sub-strategy** — a directional debit-spread complement to the core IC strategy, capturing the 9:30–9:35 ET range and trading breakouts
- **Fee-aware credit floors** — rejects entries where estimated fees would eat most/all of the collected premium, using each symbol's own historical fee data once enough trades exist
- **Full audit trail** — every loop iteration, entry, rejection reason, and stop adjustment is logged with reasoning, plus an automatically-written end-of-day narrative report

## Simplified entry gate logic

All of the following must pass — any one failure blocks the trade:

1. **Time window** — no entries before 10:00 ET or after 14:30 ET; force-close everything by 15:45 ET regardless of P&L
2. **IV rank floor** — skip if IV rank is too low (insufficient premium to justify gamma risk)
3. **Late-entry bias** — on borderline-IV days, wait until noon rather than accept thin morning credit for the same directional exposure
4. **Strike selection** — target a specific short-strike delta, then apply hard floors on top: minimum distance (%) from spot for both the short call and short put, and a ceiling on the actual call delta regardless of what the delta-target scan picked
5. **Credit floors** — three independent checks: credit as a % of spread width, a flat per-symbol dollar minimum, and a fee-adjusted floor (must clear estimated fees by a real margin)
6. **GEX regime gate** — no new iron condors when the symbol is in a *negative* gamma regime (dealers short gamma = trending/volatile conditions where mean-reversion strategies like MEIC underperform); ORB entries are exempt since they want that regime
7. **Account-wide caps** — max concurrent condors and max daily entries are shared across every traded symbol, not per-symbol
8. **Event calendars** — hard blackouts/tighter rules around FOMC announcements, quarterly expiry, and triple witching

## Dashboard

Local web dashboard (auto-refreshing) with:

- **Performance view** — P&L by day/week/month/all-time, win rate, session-quality and IV-rank breakdowns, fee-drag tracking, full trade history — filterable by symbol or account-wide
- **GEX view** — horizontal by-strike gamma exposure profile (matches the classic dealer-positioning chart style), with call wall / put wall / zero-gamma reference lines, plus a live spot-price trail tracing the day's price action directly across the profile; toggle between OI-based and volume-based GEX side by side
- **IV Skew & Volume tabs** — call/put IV curve and open-interest/volume-by-strike, same live data
- **Live log tail** — streaming agent log with level filtering, so you can watch the reasoning in real time

Everything runs locally against your own tastytrade account — no cloud dependency for trade execution.

The agent runs as a recurring `/loop` — on each iteration (~every 5 minutes during market hours) it reads persisted state, assesses market conditions, makes automated entry and stop decisions, executes trades, and logs a plain English account of everything it did and why. Trades **any combination of index or equity symbols**, configured via `symbols` (a list) in `config.json` — equity index options (XSP, SPX, NDX, RUT) and CME futures options (/MES, /ES, /MNQ, /NQ) are all supported. Every symbol must list daily-expiring (0DTE) option chains; the agent hard-stops any entry where the fetched chain's nearest expiration isn't today. Symbols that don't settle in cash at expiration (most equities) should be left out of `cash_settled_symbols` so a missed force-close is escalated as an assignment-risk failure rather than routine cleanup.

---

## Documentation

- [Setup](docs/setup.md) — installation, configuration, database init, going live
- [Operating](docs/operating.md) — starting the loop, status, dashboard, EOD report, logs
- [Strategy](docs/strategy.md) — MEIC structure, wing width selection, stops, post-stop evaluation, EOD handling

---

## Quick start

```bash
# 1. Install dependencies
pip install pytz pytest pytest-asyncio

# 2. Configure
cp config.example.json config.json   # then edit config.json

# 3. Initialize the database
python src/db.py init_db

# 4. Start the session (before 9:30 ET)
/meic-start
```

See [docs/setup.md](docs/setup.md) for the full setup walkthrough.

---

## Project structure

```
MEICAgent/
├── CLAUDE.md                        # Agent operational brain (loaded every loop iteration)
├── config.example.json              # Config template — copy to config.json
├── src/
│   ├── tt.py                        # tastytrade CLI — get_quote, get_strategies, execute_trade, etc.
│   ├── streamer.py                  # Persistent DXLink streaming daemon (live quotes/greeks/OI/volume)
│   ├── session.py                   # OAuth2 session management
│   ├── credentials.py               # OS-keyring credential storage
│   ├── db.py                        # SQLite CLI helper
│   ├── notify.py                    # Structured log CLI helper
│   └── dashboard.py                 # Local browser dashboard (port 5050)
├── docs/
│   ├── setup.md                     # Installation and configuration
│   ├── operating.md                 # Running and monitoring the agent
│   └── strategy.md                  # MEIC strategy details
├── .claude/
│   ├── settings.json                # Permissions and MCP environment overrides
│   └── commands/
│       ├── meic-start.md            # /meic-start skill — launch full session
│       ├── setup.md                 # /setup skill — credentials and initial config
│       ├── daily-check.md           # Daily broker-connection check (Step 3 of the loop)
│       ├── execute-entry.md         # Entry execution (Step 7 of the loop)
│       ├── stop-management.md       # Per-side stop management (Step 5 of the loop)
│       ├── dashboard.md             # /dashboard skill
│       ├── eod-report.md            # /eod-report skill
│       ├── meic-status.md           # /meic-status skill
│       └── check-chain.md           # /check-chain skill — verify chain and strike selection
├── data/                            # Created at first run (gitignored)
│   ├── meic_trades.db               # Trade history, loop log, daily summaries
│   └── stream_cache.db              # Live streamer cache (quotes/greeks/OI/volume/GEX history)
└── logs/                            # Created at first run (gitignored)
    ├── agent.log                    # Agent session log
    ├── streamer.log                 # Streamer daemon log
    ├── dashboard.log                # Dashboard server log
    └── eod-<date>.md                # Daily end-of-day report, one per trading day
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
