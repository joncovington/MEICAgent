# MEICAgent

An AI-driven Multiple Entry Iron Condor (MEIC) strategy trading agent for 0DTE options, powered by [Claude Code](https://claude.ai/code) and the tastytrade brokerage API.

The agent runs as a Claude Code `/loop` — on each iteration (~every 5 minutes during market hours) it reads persisted state, assesses market conditions, makes AI-driven entry and stop decisions, executes trades, and logs a plain English account of everything it did and why.

---

## Documentation

- [Setup](docs/setup.md) — installation, configuration, SendGrid, database init, going live
- [Operating](docs/operating.md) — starting the loop, status, dashboard, EOD report, logs
- [Strategy](docs/strategy.md) — MEIC structure, wing width selection, stops, post-stop evaluation, EOD handling

---

## Quick start

```bash
# 1. Install dependencies
pip install keyring pytz pytest pytest-asyncio

# 2. Configure
cp config.example.json config.json   # then edit config.json

# 3. Initialize the database
python db.py init_db

# 4. Open in Claude Code and start the loop (before 9:30 ET)
/loop
```

See [docs/setup.md](docs/setup.md) for the full setup walkthrough.

---

## Project structure

```
MEICAgent/
├── CLAUDE.md                        # Agent operational brain (loaded every loop iteration)
├── config.example.json              # Config template — copy to config.json
├── db.py                            # SQLite CLI helper
├── notify.py                        # SendGrid email + structured log CLI helper
├── dashboard.py                     # Local browser dashboard (port 5050)
├── docs/
│   ├── setup.md                     # Installation and configuration
│   ├── operating.md                 # Running and monitoring the agent
│   └── strategy.md                  # MEIC strategy details
├── .claude/
│   ├── settings.json                # MCP server wiring for Claude Code
│   └── commands/
│       ├── dashboard.md             # /dashboard skill
│       ├── eod-report.md            # /eod-report skill
│       └── meic-status.md           # /meic-status skill
├── data/                            # Created at first run (gitignored)
│   └── meic_trades.db
└── logs/                            # Created at first run (gitignored)
    └── agent.log
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
