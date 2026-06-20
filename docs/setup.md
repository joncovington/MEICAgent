# Setup

## Requirements

- **Python 3.11+**
- **Claude Code** — [claude.ai/code](https://claude.ai/code)
- **tastytrade-mcp** — [github.com/joncovington/tastytrade-mcp](https://github.com/joncovington/tastytrade-mcp)
- **tastytrade account** — live account or developer sandbox (tastytrade does not offer paper trading; the developer sandbox is a separate environment for testing without real capital)
- **SendGrid account** *(optional)* — free tier is sufficient; required only if `email.enabled` is set to `true` in `config.json`

---

## Installation

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
