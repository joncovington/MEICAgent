# Setup

## Requirements

- **Python 3.11+**
- **Claude Code** — [claude.ai/code](https://claude.ai/code)
- **tastytrade account** — live account or developer sandbox (tastytrade does not offer paper trading; the developer sandbox is a separate environment for testing without real capital)

There is no separate MCP server to install — `src/tt.py` talks to tastytrade directly via the official Python SDK (OAuth2).

---

## Installation

### 1. Clone this repo

```bash
git clone https://github.com/joncovington/MEICAgent.git
cd MEICAgent
```

### 2. Install Python dependencies

```bash
pip install pytz pytest pytest-asyncio
```

### 3. Set tastytrade credentials

Store OAuth2 credentials (`client_secret`, `refresh_token`) in the OS keyring — never in files or environment variables:

```bash
python src/tt.py secrets_set
```

This prompts for each credential with hidden input and preserves existing values if you press Enter without typing. `account_number` is optional; the SDK discovers accounts automatically if omitted. See `/setup` for a guided walkthrough, including connection verification.

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
| `entry_window_start` | Earliest time to enter new ICs in HH:MM ET (default `"10:00"`) |
| `separate_spread_entry` | Order structure: `false` = 4-leg combo (default), `true` = separate 2-leg spreads, `"auto"` = agent decides per-iteration based on IV rank, session, and open IC count |
| `entry_price_strategy` | Limit price strategy: `"mid"` = streaming mid price with a spread-width gate and fallback to natural bid, `"natural_bid"` = always submit at natural bid (safest), `"ioc_step"` = try IOC orders above natural bid then fall back, `"day_improve"` = Day limit above natural bid with cancel-replace, `"auto"` (default) = agent decides per-iteration by session/spread-width/IV rank |
| `ioc_step_increments` | Amounts above natural bid to attempt as IOC orders (e.g. `[0.02, 0.01]`). Used when `entry_price_strategy` is `"ioc_step"` or `"auto"` selects it |
| `ioc_step_wait_seconds` | Seconds to wait per IOC attempt before stepping to the next increment (default `10`) |
| `day_improve_amount` | Amount above natural bid to submit as a Day limit when using `"day_improve"` (default `0.03`) |
| `day_improve_wait_seconds` | Seconds to wait before canceling the Day improve order and resubmitting at natural bid (default `60`) |

### 5. Initialize the database

```bash
python src/db.py init_db
```

This creates `data/meic_trades.db` (SQLite, WAL mode). Safe to run multiple times.

### 6. Start the streamer daemon (recommended)

```bash
python src/streamer.py
```

Maintains a persistent DXLink WebSocket so quote/greeks/chain reads are served from cache instead of a cold connection each time. `/meic-start` launches this automatically alongside the dashboard and loop.

### 7. Enable live trading (when ready)

Tastytrade operations go direct via the Python SDK in `tt.py` — there is no MCP server involved. Live order submission is gated by a single `config.json` key, checked by `tt.py`'s `_live_trading_enabled()` before any `execute_trade`/`adjust_order`/`close_position` call:

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
