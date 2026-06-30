# Setup

## Requirements

- **Python 3.11+**
- **Claude Code** — [claude.ai/code](https://claude.ai/code)
- **tastytrade-mcp** — [github.com/joncovington/tastytrade-mcp](https://github.com/joncovington/tastytrade-mcp)
- **tastytrade account** — live account or developer sandbox (tastytrade does not offer paper trading; the developer sandbox is a separate environment for testing without real capital)

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
pip install pytz pytest pytest-asyncio
```

### 4. Configure the agent

Copy the example config and edit it with your settings:

```bash
cp config.example.json config.json
```

Key fields to update in `config.json`:

| Field | Description |
|---|---|
| `symbol` | Underlying to trade. Equity: `XSP`, `SPX`, `NDX`, `RUT`. Futures: `/MES`, `/ES`, `/MNQ`, `/NQ` |
| `delta_target` | Short strike delta target (default `0.15`) |
| `wing_width_candidates` | Wing widths to evaluate per entry (agent picks the best fit) |
| `quantity` | Number of contracts per IC leg |
| `max_entries_per_day` | Hard cap on entries (`-1` = no cap, rely on AI + buying power) |
| `entry_window_start` | Earliest time to enter new ICs in HH:MM ET (default `"09:45"`) |
| `separate_spread_entry` | Order structure: `false` = 4-leg combo (default), `true` = separate 2-leg spreads, `"auto"` = agent decides per-iteration based on IV rank, session, and open IC count |
| `entry_price_strategy` | Limit price strategy: `"natural_bid"` = always submit at natural bid (default, safest), `"ioc_step"` = try IOC orders above natural bid then fall back, `"day_improve"` = Day limit above natural bid with cancel-replace, `"auto"` = agent decides per-iteration |
| `ioc_step_increments` | Amounts above natural bid to attempt as IOC orders (e.g. `[0.02, 0.01]`). Used when `entry_price_strategy` is `"ioc_step"` or `"auto"` selects it |
| `ioc_step_wait_seconds` | Seconds to wait per IOC attempt before stepping to the next increment (default `10`) |
| `day_improve_amount` | Amount above natural bid to submit as a Day limit when using `"day_improve"` (default `0.03`) |
| `day_improve_wait_seconds` | Seconds to wait before canceling the Day improve order and resubmitting at natural bid (default `60`) |

### 5. Initialize the database

```bash
python src/db.py init_db
```

This creates `data/meic_trades.db` (SQLite, WAL mode). Safe to run multiple times.

### 6. Configure the MCP server

The tastytrade MCP server is defined in `.mcp.json` at the project root and enabled via `.claude/settings.local.json`. By default live trading is disabled and a buying-power buffer is applied:

```json
{
  "ENABLE_LIVE_TRADING": "false",
  "FORCE_DRY_RUN": "true",
  "BUYING_POWER_BUFFER_PCT": "25",
  "ACCOUNT_DEPLOY_LIMIT_PCT": "80"
}
```

These environment variables are read by the `tastytrade-mcp` process at startup. To go live, set `ENABLE_LIVE_TRADING` to `"true"` and remove `FORCE_DRY_RUN` (or set it to `"false"`) in `.mcp.json`, then restart the Claude Code session so the MCP server picks up the new env.

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
