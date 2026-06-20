# MEICAgent

An AI-driven Multiple Entry Iron Condor (MEIC) trading agent for 0DTE options, powered by [Claude Code](https://claude.ai/code) and the tastytrade brokerage API.

The agent runs as a Claude Code `/loop` — on each iteration (~every 5 minutes during market hours) it reads persisted state, assesses market conditions, makes AI-driven entry and stop decisions, executes trades, and logs a plain English account of everything it did and why.

---

## Requirements

- **Python 3.11+**
- **Claude Code** — [claude.ai/code](https://claude.ai/code)
- **tastytrade-mcp** — [github.com/joncovington/tastytrade-mcp](https://github.com/joncovington/tastytrade-mcp)
- **tastytrade account** — live account or developer sandbox (tastytrade does not offer paper trading; the developer sandbox is a separate environment for testing without real capital)
- **SendGrid account** *(optional)* — free tier is sufficient; required only if `email.enabled` is set to `true` in `config.json`

---

## Setup

### 1. Install tastytrade-mcp

Follow the instructions at [github.com/joncovington/tastytrade-mcp](https://github.com/joncovington/tastytrade-mcp) to install and configure the MCP server. Verify it is available on your PATH:

```bash
tastytrade-mcp --help
```

### 2. Clone this repo

```bash
git clone https://github.com/joncovington/MEICAgent.git
cd MEICAgent
```

### 3. Install Python dependencies

```bash
pip install keyring pytz pytest pytest-asyncio
pip install sendgrid  # optional — only needed if email.enabled = true
```

### 4. Configure the agent

Copy the example config and edit it with your settings:

```bash
cp config.example.json config.json
```

Key fields to update in `config.json`:

| Field | Description |
|---|---|
| `symbol` | Underlying to trade (e.g. `XSP`, `SPX`, `NDX`) |
| `delta_target` | Short strike delta target (default `0.15`) |
| `wing_width_candidates` | Wing widths to evaluate per entry (agent picks the best) |
| `quantity` | Number of contracts per IC leg |
| `max_entries_per_day` | Hard cap on entries (`-1` = no cap, rely on AI + buying power) |
| `sandbox` | `true` to use the tastytrade developer sandbox, `false` for your live account |
| `email.enabled` | `true` to send alerts via SendGrid (optional — logs are always written to `logs/agent.log` regardless) |
| `email.from` | Verified SendGrid sender address |
| `email.to` | Address to receive alerts |

### 5. Store your SendGrid API key *(optional)*

Skip this step if you are not using email alerts (`email.enabled: false` in `config.json`).

The agent resolves the API key in this order:
1. **OS keyring** (preferred) — uses your platform's native secret store
2. **Environment variable** fallback — `MEICAGENT_SENDGRID_KEY`

| Platform | Keyring backend |
|---|---|
| Windows | Windows Credential Manager |
| macOS | Keychain |
| Linux desktop | Secret Service (gnome-keyring / kwallet) |
| Linux headless / server | No keyring available — use the env var fallback |

**To store via keyring** (Windows, macOS, Linux desktop):

```bash
python -c "import keyring; keyring.set_password('meicagent', 'sendgrid_api_key', 'YOUR_SENDGRID_KEY')"
```

To verify:

```bash
python -c "import keyring; print(keyring.get_password('meicagent', 'sendgrid_api_key'))"
```

**To use the environment variable instead** (Linux headless, Docker, CI):

```bash
export MEICAGENT_SENDGRID_KEY=YOUR_SENDGRID_KEY
```

Add this to your shell profile or container environment so it persists across sessions.

### 6. Initialize the database

```bash
python db.py init_db
```

This creates `data/meic_trades.db` (SQLite, WAL mode). Safe to run multiple times.

### 7. Configure the MCP server

The Claude Code MCP connection is pre-configured in `.claude/settings.json`. By default it runs in sandbox mode with live trading disabled:

```json
{
  "TASTYTRADE_SANDBOX": "true",
  "ENABLE_LIVE_TRADING": "false"
}
```

To go live, set both to `"true"` and update `"sandbox": false` in `config.json`.

---

## Running the tests

The test suite uses `MockMCP` — a plain Python stub that returns realistic response dicts. No tastytrade connection or credentials required.

```bash
pytest
```

For a human-readable end-to-end report:

```bash
python tests/test_mock_run.py
```

Three scenarios are covered: `midday_normal` (3 open ICs, all stops working), `stop_filled` (one stop order disappears — IC stopped out), and `bp_rejected` (dry-run pre-flight returns buying-power rejection).

---

## Running the agent

Open the MEICAgent folder in VS Code with the Claude Code extension (or run `claude` from this directory), then start the loop **before 9:30 ET**:

```
/loop
```

The agent runs every ~5 minutes. The tastytrade MCP gates all market-hours checks, so starting early or leaving it running after close is safe — it will not attempt to trade outside market hours.

---

## Checking status during the day

```
/meic-status
```

Prints a live summary of open positions, today's P&L, and the last few loop actions without interrupting the running loop.

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

## Project structure

```
MEICAgent/
├── CLAUDE.md                        # Agent operational brain (loaded every loop iteration)
├── config.example.json              # Config template — copy to config.json
├── db.py                            # SQLite CLI helper
├── notify.py                        # SendGrid email + structured log CLI helper
├── .claude/
│   ├── settings.json                # MCP server wiring for Claude Code
│   └── commands/
│       ├── eod-report.md            # /eod-report skill
│       └── meic-status.md           # /meic-status skill
├── data/                            # Created at first run (gitignored)
│   └── meic_trades.db
└── logs/                            # Created at first run (gitignored)
    └── agent.log
```

---

## Strategy overview

The agent places multiple Iron Condors throughout the 0DTE session, adapting to market conditions on each entry:

- **Wing width** — evaluated dynamically per entry across `wing_width_candidates`; wider early in the day for more credit, narrower late or when multiple ICs are open
- **Stops** — DAY stop-limit orders sized to ~break-even on the full IC credit; tightened by AI judgment as the day progresses
- **Post-stop** — the remaining spread is re-evaluated every iteration (close, hold, or buy back just the short leg)
- **EOD** — cash-settled symbols (SPX, XSP, NDX, RUT) can expire naturally; non-cash-settled positions are closed by 15:45 ET
- **Conflict resolution** — when signals are ambiguous the agent takes the capital-protective default and logs a detailed plain English account for review

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
