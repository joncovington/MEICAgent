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
| `symbols` | List of underlyings to trade concurrently, e.g. `["XSP", "SPX"]` — any index or equity symbols tastytrade supports, such as equity index options (`XSP`, `SPX`, `NDX`, `RUT`) or futures options (`/MES`, `/ES`, `/MNQ`, `/NQ`). Every symbol must list daily-expiring (0DTE) option chains; most single-name equities don't. Non-cash-settled symbols should be left out of `cash_settled_symbols` so a missed force-close is escalated as an assignment-risk failure. All symbols share one account-wide risk budget (buying power, `max_concurrent_ics`, `max_entries_per_day`) — there's no per-symbol cap, and correlated symbols aren't exposure-limited against each other yet. The single-symbol `symbol` key is still accepted as a deprecated alias for `["symbol"]` if `symbols` is omitted. |
| `delta_target` | Short strike delta target (default `0.18`) |
| `max_wing_width` | Upper bound (points) on spread width; the agent decides the actual wing width per entry rather than picking from a fixed list |
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

### 6. Enable live trading (when ready)

Tastytrade operations go direct via the Python SDK in `tt.py` — there is no MCP server involved (`.mcp.json` at the project root only configures the unrelated `agentmemory` server, if you've set that up). Live order submission is gated by a single `config.json` key, checked by `tt.py`'s `_live_trading_enabled()` before any `execute_trade`/`adjust_order`/`close_position` call:

```json
{
  "enable_live_trading": false
}
```

By default this is `false`, so `execute_trade`/`adjust_order` calls without `--live` always dry-run regardless of this setting, and calls with `--live` are rejected outright. To go live, set `enable_live_trading` to `true` in `config.json` — no restart of the Claude Code session is required, since `tt.py` reads `config.json` fresh on every invocation.

---

## Running the tests

The test suite operates on temp SQLite databases and stubbed cache data — no tastytrade connection or credentials required.

```bash
pytest
```
