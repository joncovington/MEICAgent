# MEICAgent Project Map

Autonomous 0DTE options trading agent executing Multiple Entry Iron Condors (MEIC) concurrently on any combination of index or equity symbols (configured via the `symbols` list in `config.json`) via the tastytrade brokerage API. Each configured symbol must offer daily-expiring option chains — see the 0DTE expiration hard stop in `CLAUDE.md` Step 6. Every symbol shares one account-wide risk budget (buying power, `max_concurrent_ics`, `max_entries_per_day`); correlation exposure across symbols is not yet guarded.

---

## Directory Layout

```
MEICAgent/
├── src/                    Core Python modules (all runtime code lives here)
├── tests/                  Pytest suite (db, streamer cache, dashboard tests)
├── data/                   Runtime databases — gitignored except .gitkeep
│   ├── meic_trades.db      Trade ledger (ic_trades, daily_summary, loop_log, paper_marks)
│   └── stream_cache.db     DXLink event cache (quotes, greeks, OI, chain, status)
├── logs/                   Agent loop log, EOD reports — gitignored except .gitkeep
├── docs/                   Operator docs (setup, strategy, operating guides)
├── research/               Trading research notes (GEX, 0DTE strategies, covered calls)
├── .claude/commands/       Project slash commands (skills invoked during the agent loop)
├── .agents/skills/         agentmemory skill definitions (auto-installed, gitignored)
├── config.json             Active runtime configuration (see CLAUDE.md config table)
├── config.example.json     Annotated template
├── CLAUDE.md               Agent operating instructions and strategy reference (primary doc)
└── pyproject.toml          Python project metadata and test config
```

---

## Source Files (`src/`)

### `tt.py` — Tastytrade Tool Interface (1,361 lines)
The single entry point for all tastytrade operations. Run as `python src/tt.py <command>`.

**Lookup flow**: every read command checks `stream_cache.db` first (tier 1 — streamer cache), then the REST result cache (tier 2 — within 10s), then opens a live DXLink connection (tier 3). The HTTP server in `streamer.py` intercepts calls via `_try_streamer_http()` when the streamer daemon is running, so quote/greeks latency is effectively zero.

**Cache helpers**

| Function | Returns |
|---|---|
| `_cache_get_trade(symbol)` | Last trade price from `stream_trades` |
| `_cache_get_quotes(symbols)` | `{sym: {bid,ask,mid}}` from `stream_quotes` (≤10s) |
| `_cache_get_greeks(symbols)` | `{sym: {delta,gamma,theta,iv}}` from `stream_greeks` (≤10s) |
| `_cache_get_oi(symbols)` | `{sym: open_interest}` from `stream_oi` (no age filter — daily snapshot) |
| `_cache_get_chain(symbol, expiry)` | Serialized chain from `stream_chain` (≤30s) |

**Chain/quote helpers**

| Function | Purpose |
|---|---|
| `_fetch_chain(symbol)` | Fetch full option chain via tastytrade SDK |
| `_atm_window(options, n, center)` | Slice ±n strikes around center price |
| `_collect_greeks(syms, timeout)` | Live DXLink Greeks for missing symbols |
| `_collect_last_prices(syms, timeout)` | Live DXLink Trade events |
| `_collect_quotes(syms, timeout)` | Live DXLink Quote events |

**Commands**

| Command function | CLI name | Purpose |
|---|---|---|
| `cmd_get_connection_status` | `get_connection_status` | Verify OAuth session |
| `cmd_list_accounts` | `list_accounts` | Account numbers |
| `cmd_get_account_info` | `get_account_info` | Buying power, NLV |
| `cmd_get_positions` | `get_positions` | Open broker positions |
| `cmd_get_market_overview` | `get_market_overview` | IV rank, underlying price |
| `cmd_get_quote` | `get_quote` | Last price (cache → DXLink) |
| `cmd_get_option_chain` | `get_option_chain` | Chain with optional greeks/quotes |
| `cmd_get_strategies` | `get_strategies` | IC candidate with POP and credit |
| `cmd_get_gex` | `get_gex` | GEX profile (requires OI in cache) |
| `cmd_get_working_orders` | `get_working_orders` | Live/unfilled orders |
| `cmd_execute_trade` | `execute_trade` | Dry-run or live order |
| `cmd_adjust_order` | `adjust_order` | Replace a working order |
| `cmd_close_position` | `close_position` | Cancel working order by ID |
| `cmd_stream_status` | `stream_status` | Streamer daemon health |
| `cmd_stream_subscribe` | `stream_subscribe` | Warm cache for specific symbols |
| `cmd_secrets_status` | `secrets_status` | Credential presence check |
| `cmd_secrets_set` | `secrets_set` | Write credentials to OS keyring |

**GEX computation** (`_compute_gex`)
- Formula: `gamma × OI × 100 × spot² × 0.01` per strike
- Outputs: `net_gex`, `gex_positive`, `gamma_flip` (interpolated zero-crossing), `call_wall` (max call GEX strike), `put_wall` (max put GEX strike), `per_strike` breakdown

---

### `streamer.py` — DXLink Streamer Daemon (1,267 lines)
Persistent background process maintaining a WebSocket to the DXLink feed. Writes Quote, Greeks, Trade, and Summary (OI) events to `stream_cache.db`. Also runs an embedded HTTP server for low-latency command routing.

**Startup**: `python src/streamer.py` (foreground) or `Start-Process python -ArgumentList 'src\streamer.py' -WindowStyle Hidden` (background).

**Subscription management**

| Function | Purpose |
|---|---|
| `_resolve_subscriptions(underlying)` | Builds `{Trade,Quote,Greeks,Summary: [syms]}` from open trades |
| `_apply_subscriptions(streamer, state, subs, ...)` | Diffs current vs desired subscriptions, calls subscribe/unsubscribe |
| `_poll_subscriptions(...)` | Re-runs resolve+apply every 30s |
| `_atm_refresher(...)` | Fetches DTE-0 chain, maintains ±15-strike ATM window for Quote/Greeks/Summary |
| `_gex_refresher(...)` | Maintains ±20-strike window for the GEX symbol (SPX by default) |

**Event listeners** (each an async task in `TaskGroup`)

| Coroutine | Writes to |
|---|---|
| `_listen_trade` | `stream_trades` |
| `_listen_quote` | `stream_quotes` |
| `_listen_greeks` | `stream_greeks` |
| `_listen_summary` | `stream_oi` (open interest only) |

**HTTP API** (`_ApiHandler`)
Runs on port 5001 (default). Accepts `POST /api` with `{"command": "...", "args": {...}}`. Routes supported commands (`_CMD_DEFAULTS`) to `tt.py` functions via `_rest_loop` (a dedicated asyncio event loop in a background thread). Enables `tt.py`'s `_try_streamer_http()` shortcut for near-zero latency reads.

**State** (`_State`)
Tracks subscribed symbols per event type, ATM window center and symbols, GEX channel center and symbols, reconnect count, last event timestamp.

---

### `db.py` — Trade Database (527 lines)
CLI interface to `data/meic_trades.db`. Run as `python src/db.py <command>`.

**Schema**

| Table | Key | Purpose |
|---|---|---|
| `ic_trades` | `ic_order_id` | One row per IC: entry, stops, exit, P&L, reasoning |
| `daily_summary` | `summary_date` | Per-day aggregate stats and EOD NLV |
| `loop_log` | `id` (serial) | Append-only iteration log |
| `paper_marks` | `id` (serial) | Intraday paper-trade mark-to-market |

**Commands**

| Function | CLI name |
|---|---|
| `cmd_init_db` | `init_db` |
| `cmd_get_open_trades` | `get_open_trades` |
| `cmd_get_today_count` | `get_today_count` |
| `cmd_get_today_pnl` | `get_today_pnl` |
| `cmd_get_eod_summary` | `get_eod_summary` |
| `cmd_save_trade` | `save_trade` |
| `cmd_update_trade` | `update_trade` |
| `cmd_record_stop_adjustment` | `record_stop_adjustment` |
| `cmd_log_loop_action` | `log_loop_action` |
| `cmd_get_session_init` | `get_session_init` |
| `cmd_set_session_init` | `set_session_init` |
| `cmd_save_daily_summary` | `save_daily_summary` |

---

### `dashboard.py` — Intraday Dashboard (1,778 lines)
Flask web app at `http://localhost:5050`. Provides live market monitoring and GEX visualization. Opens a browser tab on startup.

**Tabs / panels**
- Account summary (NLV, buying power, open P&L)
- Open positions with real-time greeks
- Option chain viewer
- IV skew chart
- GEX analysis: net GEX bar chart, call wall / put wall / gamma flip markers, OI heatmap, volume profile

**Data flow**: reads `stream_cache.db` for low-latency data; falls back to REST via `tt.py` HTTP calls for chain data not yet in cache.

---

### `session.py` — Session Management (35 lines)
Singleton wrapper around `tastytrade.ProductionSession`. `get_session()` authenticates via OS keyring credentials (read by `credentials.py`) and caches the session for reuse. `reset_session()` forces re-authentication.

---

### `credentials.py` — Keyring Wrapper (65 lines)
Reads and writes tastytrade credentials (`username`, `password`) to the OS keyring (Windows Credential Manager / DPAPI). Never touches files or environment variables.

| Function | Purpose |
|---|---|
| `get_secret(key)` | Read one credential |
| `set_secret(key, val)` | Write one credential |
| `secrets_present()` | True if both username and password are set |
| `missing_secrets()` | List of missing credential keys |
| `secrets_status()` | Dict summary for CLI output |

---

### `notify.py` — Logging (67 lines)
Appends structured log events to `logs/agent.log`. Called as `python src/notify.py log_event --level INFO --message "..."`.

---

## Databases

### `data/meic_trades.db`

**`ic_trades`** — one row per IC entry
Key fields: `ic_order_id`, `symbol`, `put_strike`, `call_strike`, `wing_width`, `net_credit`, `status` (`pending` / `open` / `stopped` / `expired` / `cancelled`), `stop_trigger_current`, `stop_adjustment_count`, `exit_price`, `pnl`, `ai_entry_reasoning`

**`daily_summary`** — one row per trading date
Key fields: `total_entries`, `entries_filled`, `gross_pnl`, `net_pnl`, `closing_nlv`, `win_rate_pct`, `avg_iv_rank`, `ai_day_summary`

**`loop_log`** — append-only iteration record
Key fields: `action`, `reasoning`, `open_trades_n`, `today_pnl`, `iv_rank`, `underlying_price`, `session_quality`, `duration_ms`

### `data/stream_cache.db`

| Table | Key | Content | Age policy |
|---|---|---|---|
| `stream_quotes` | `symbol` | bid/ask/mid | ≤10s |
| `stream_greeks` | `symbol` | delta/gamma/theta/iv | ≤10s |
| `stream_trades` | `symbol` | last price | no filter |
| `stream_oi` | `symbol` | open_interest | no filter (daily snapshot) |
| `stream_chain` | `(symbol, expiry)` | full chain JSON | ≤30s |
| `stream_status` | `id=1` | pid, connected_since, subscribed counts | live |
| `stream_rest_cache` | `key` | REST API response JSON | ≤10s |

---

## Agent Loop (CLAUDE.md)

The agent loop is driven by `/loop` (Claude Code self-pacing) following 8 steps each iteration:

```
1. Load state          → open trades, today count, P&L, current ET time
2. Time gate           → skip if outside 09:30–15:55 ET, holiday, weekend
3. Daily check         → verify broker connection (once per day)
4. Market assessment   → buying power, IV rank, chain, GEX regime, regime detection, ORB range
5. Stop management     → profit targets, per-side stops, stop tightening, FOMC/EOD force-close
6. Entry decision      → all hard stops checked; IC entry or ORB debit spread
7. Execute entry       → dry run → live order → save trade
8. Record & notify     → log iteration, schedule next wakeup
```

**Loop cadence**

| Condition | Interval |
|---|---|
| Pre-market 08:00–09:00 ET | 600s |
| Pre-market 09:00–09:29 ET | 120s |
| Market hours, no positions | 300s |
| Market hours, positions open | 120s |
| Off-hours / weekend / holiday | end loop |

---

## Slash Commands (`.claude/commands/`)

| Command | Purpose |
|---|---|
| `/meic-start` | Start streamer + agent loop |
| `/meic-status` | Display current session status |
| `/daily-check` | Verify broker connection (loop Step 3) |
| `/stop-management` | Run stop management (loop Step 5) |
| `/execute-entry` | Execute IC entry (loop Step 7) |
| `/eod-report` | Generate end-of-day report |
| `/dashboard` | Launch dashboard at localhost:5050 |
| `/setup` | First-time credential and config setup |
| `/check-chain` | Verify option chain and delta selection |

---

## Tests (`tests/`)

| File | What it tests |
|---|---|
| `test_db.py` | db.py multi-symbol support: loop_log.symbol, --symbol filters, migrations |
| `test_streamer_cache.py` | SQLite cache read/write correctness, multi-symbol subscription isolation |
| `test_dashboard.py` | Dashboard route responses, symbol-filtered stats |
| `bench_streamer.py` | Streamer throughput benchmark |

Run: `pytest tests/` (no credentials, no live connection required).

---

## Key Design Decisions

- **No MCP**: tastytrade operations go direct via the Python SDK (`tastytrade` library), not through an MCP server. The streamer HTTP API (`port 5001`) provides the low-latency routing layer.
- **Credential security**: OS keyring only (Windows Credential Manager / DPAPI). No `.env`, no files, no environment variables.
- **OI source**: DXLink `Summary` events only. The REST option chain API and Greeks events do not carry open interest. OI is cached in `stream_oi` with no age filter (exchange end-of-day snapshot).
- **GEX signal**: computed in `tt.py` from `stream_oi` + `stream_greeks`. Blocks IC entries when net GEX is negative (below gamma flip). Used to anchor short strikes near call wall / put wall.
- **Software stops only**: tastytrade does not support exchange-level multi-leg stop orders for combo ICs. All stop monitoring is software-based in the 120s loop cadence.
- **Force-close, not expiration**: all 0DTE positions are force-closed by `force_close_time` (15:45 ET) regardless of symbol — the agent never intentionally holds through expiration. `cash_settled_symbols` (default: XSP, SPX, NDX, RUT) instead controls how urgently a *missed* force-close is escalated: a symbol outside this list carries physical assignment risk if a close fails, so that failure is treated as critical rather than routine. Any equity or physically-settled symbol traded via `symbol` should be left out of `cash_settled_symbols`.
