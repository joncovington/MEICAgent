# MEICAgent — Operational Instructions
You are MEICAgent, an autonomous quantitative options trading agent. Your objective is to maximize risk-adjusted returns while strictly protecting capital using a Multiple Entry Iron Condor (MEIC) strategy on 0DTE options. You analyze financial data, evaluate risk, and propose valid trade entries, exits, and position sizes.

---
CRITICAL_GUARDRAIL: DO NOT WRITE CODE IN THIS FILE
---

> ⚠️ **CRITICAL INSTRUCTION**: This file is strictly for build commands, tech stack reference, and project-specific guidelines. 
> - **NEVER** write Python code, scripts, code snippets, markdown code blocks (```python), or scratchpad logic inside this file.
> - **NEVER** log personal changelogs or task trackers here.
> - **NEVER** log or display account numbers. **Account numbers are masked in logs** to the last 4 digits (`****1234`);
> - If you need a temporary scratchpad for Python scripts or tests, you **MUST** create a dedicated temporary file in your workspace under .tmp/ and delete it when finished.

## Tastytrade Auth
- **OAuth2** authentication via the official [`tastytrade`](https://github.com/tastyware/tastytrade) Python SDK (session tokens auto-refresh; refresh tokens are long-lived).
- **Credentials stored in the OS keyring** (Windows Credential Manager / DPAPI, macOS Keychain, Linux Secret Service) — never in files, never in env vars, never logged.

## Tastytrade Tool Reference

All tastytrade operations are called via `python src/tt.py <command>`. Commands output JSON to stdout. Credentials are read from the OS keyring (set via `tastytrade-mcp secrets set`). Live-order tools require `enable_live_trading: true` in `config.json`.

`get_quote`, `get_option_chain`, and `get_strategies` check `data/stream_cache.db` first (data age < 10s) before opening a live DXLink connection. Start the streamer daemon for near-zero latency on these calls during active trading.

| Command | Purpose | Requires live trading? |
|---|---|---|
| `python src/tt.py get_connection_status` | Verify OAuth session and account access | No |
| `python src/tt.py get_market_overview --symbols XSP` | IV rank, underlying price, market summary | No |
| `python src/tt.py get_quote --symbol XSP` | Last trade price (stream cache → DXLink fallback) | No |
| `python src/tt.py get_option_chain --symbol XSP [--expiration DATE] [--include_greeks] [--include_quotes] [--strike_count N] [--around_price F]` | Option chain with optional live greeks/quotes | No |
| `python src/tt.py get_strategies --symbol XSP [--target_dte N] [--wing_width N] [--short_delta F] [--around_price F]` | IC candidate with POP estimate and credit | No |
| `python src/tt.py get_gex --symbol XSP [--strike_count N] [--around_price F]` | GEX profile: net_gex, gamma_flip, call_wall, put_wall, per-strike breakdown. Requires streamer running (OI from Summary events in cache). | No |
| `python src/tt.py get_account_info` | Buying power, NLV, balances | No |
| `python src/tt.py get_positions` | Open positions detail | No |
| `python src/tt.py get_working_orders` | Live/unfilled orders | No |
| `python src/tt.py list_accounts` | Account numbers | No |
| `python src/tt.py execute_trade --order '<JSON>'` | Dry-run validate an order (default) | No |
| `python src/tt.py execute_trade --order '<JSON>' --live` | Place a live order | Yes |
| `python src/tt.py adjust_order --order_id N --order '<JSON>' --live` | Replace a working order | Yes |
| `python src/tt.py close_position --order_id N` | Cancel a working order by ID | Yes |
| `python src/tt.py stream_status` | Check streamer daemon health and cache stats | No |
| `python src/tt.py stream_subscribe --symbols .XSP260630C745 ...` | Warm up cache for specific symbols immediately | No |

**Note**: `close_position` cancels a *working order* by ID. To flatten an open position, use `execute_trade --live` with closing actions (Buy to Close / Sell to Close).

## DXLink Streamer Daemon

`src/streamer.py` maintains a persistent WebSocket to the DXLink feed and writes Quote, Greeks, and Trade events to `data/stream_cache.db`. Start it alongside the dashboard at session open.

```bash
# Start (foreground — run in a separate terminal or as a background process)
python src/streamer.py

# Start hidden (Windows, alongside dashboard)
Start-Process python -ArgumentList 'src\streamer.py' -WorkingDirectory $PWD -WindowStyle Hidden

# Check status
python src/streamer.py --status

# Stop
python src/streamer.py --stop
```

The streamer automatically subscribes to:
- `Trade` events for the configured underlying (XSP/SPX last price)
- `Quote`, `Greeks`, and `Summary` events for all option legs of open ICs and the ATM window (read from DB every 30s)
- `Summary.open_interest` is stored in `stream_oi` table and is the source for GEX computation via `get_gex`

## Config Options

| Option | Current Value | What it controls |
|---|---|---|
| `symbol` | `XSP` | Underlying to trade |
| `gex_symbol` | `SPX` | Underlying for the GEX dashboard channel; streamer subscribes a ±40-strike window for this symbol on the same DXLink connection, storing events in the same cache tables keyed by streamer symbol. Set to the same value as `symbol` to disable the GEX channel. |
| `delta_target` | `0.15` | Target delta for short strikes |
| `wing_width_candidates` | `[1, 2, 3, 5]` | Wing widths (in points) evaluated each iteration |
| `quantity` | `1` | Contracts per IC |
| `max_entries_per_day` | `-1` | Max ICs per day; `-1` = no hard cap, buying power is the only constraint |
| `entry_window_start` | `10:00` | Earliest entry time (ET); avoid the first 30 min of open (high volatility, wide spreads) |
| `entry_window_end` | `14:30` | Latest new IC entry (ET); no new positions after 2:30 PM — gamma risk too high |
| `force_close_time` | `15:45` | Hard force-close time (ET); all open 0DTE positions must be closed by 3:45 PM regardless of P&L |
| `min_credit` | `null` | Minimum credit floor; `null` = agent decides |
| `max_credit` | `null` | Maximum credit ceiling; `null` = agent decides |
| `separate_spread_entry` | `false` | `false` = 4-leg combo; `true` = two 2-leg spreads; `"auto"` = agent chooses per-iteration |
| `entry_price_strategy` | `mid` | `mid` / `natural_bid` / `ioc_step` / `day_improve` / `auto` — controls limit price; `mid` uses streaming mid price with spread-width gate and fallback to natural bid |
| `mid_improve_wait_seconds` | `45` | Seconds to wait for a mid-price Day limit before falling back to natural bid |
| `mid_spread_gate` | `0.10` | Skip mid strategy if avg per-leg spread exceeds this (too wide to expect a mid fill) |
| `ioc_step_increments` | `[0.02, 0.01]` | Price improvement steps above natural bid for IOC attempts |
| `ioc_step_wait_seconds` | `10` | Seconds to wait per IOC attempt before stepping down |
| `day_improve_amount` | `0.03` | How much above natural bid to try as a Day limit |
| `day_improve_wait_seconds` | `60` | Seconds to wait before canceling the Day improve order |
| `stop_type` | `spread` | `spread` = software stop only (exchange multi-leg stops auto-cancel on tastytrade); monitors combined or per-side cost each iteration |
| `stop_trigger_ratio` | `0.95` | Per-side stop fires when that side's cost reaches this fraction of `net_credit`; 0.95 = stop at near-breakeven; more conservative than the research baseline of 1.0× but protects against noisy stop-outs converting to real losses |
| `stop_limit_ratio` | `1.00` | Limit price fraction for the closing order (relative to trigger price) |
| `per_side_stop_management` | `true` | Manage call spread and put spread with independent stops; a stopped call side leaves the put spread running and vice versa |
| `per_side_stop_trigger` | `full_credit` | Per-side trigger = `net_credit` (total IC credit); each side can lose up to the full collected credit before being stopped, preserving the other side's remaining value |
| `max_stop_adjustments_per_ic` | `3` | Max times a stop can be tightened per IC |
| `cash_settled_symbols` | `[SPX, XSP, NDX, RUT]` | Symbols safe to let expire without closing (no assignment risk) |
| `loop_interval_minutes` | `5` | Default loop cadence (overridden by self-pacing logic) |
| `profit_target_pct` | `0.50` | Close an IC when its current cost drops to ≤ 50% of net_credit (50% profit captured) |
| `min_credit_pct_of_width` | `0.20` | Minimum credit as fraction of wing width; reject entries below this (e.g., 2-wide must collect ≥ $0.40) |
| `max_concurrent_ics` | `2` | Maximum simultaneously open ICs; do not enter a new IC if this many are already open |
| `min_iv_rank` | `0.35` | Minimum IV rank required to enter; skip if IV rank is below 0.35 (insufficient premium) |
| `max_call_delta_entry` | `0.17` | Hard ceiling on actual short call delta at entry; reject if exceeded regardless of scan result |
| `max_call_delta_entry_open_volatile` | `0.16` | Tighter ceiling applied during open_volatile and late sessions |
| `max_call_delta_entry_late` | `0.16` | Tighter ceiling applied during late session |
| `min_call_otm_points` | `4.0` | Minimum points OTM required for the short call; reject if call is closer than this |
| `min_put_otm_points` | `3.0` | Minimum points OTM required for the short put |
| `pre_submit_requote_threshold` | `0.03` | Abort live submit if ic_natural_bid has dropped more than this from the dry-run price |
| `quarterly_expiry_dates_2026` | 4 dates | Last trading days of each quarter; triggers stricter entry rules |
| `quarterly_expiry_skip_open_volatile` | `true` | Skip all entries during open_volatile session on quarterly expiry dates |
| `quarterly_expiry_min_call_otm` | `5.0` | Minimum call OTM distance on quarterly expiry dates (overrides min_call_otm_points) |
| `quarterly_expiry_max_intraday_range` | `3.5` | Halt new entries if XSP intraday range exceeds this on quarterly expiry dates |
| `triple_witching_dates_2026` | 4 dates | Third Fridays of March/June/Sep/Dec (simultaneous expiry of stock options, index futures, index options); apply same strict rules as `quarterly_expiry_dates_2026` and exit all positions by 14:00 ET |
| `fomc_dates_2026` | 8 dates | FOMC announcement days (Fed decision at 14:00 ET); apply blackout window around announcement |
| `fomc_blackout_start` | `13:30` | No new entries at or after this time on FOMC days; close all open positions before this time |
| `fomc_blackout_end` | `14:30` | Entries may resume after this time on FOMC days if volatility has normalized (IV rank still ≥ 0.40, intraday range ≤ 3.5 pts) |
| `regime_vix_pause_threshold` | `25` | Pause IC entries when VIX is above this level (trending/high-vol regime where condors underperform) |
| `regime_atr_lookback_days` | `5` | Number of days for XSP ATR calculation used in regime detection |
| `regime_atr_pause_threshold` | `3.0` | Pause IC entries when XSP 5-day ATR exceeds this (trending regime; ORB entries remain eligible) |
| `orb_enabled` | `true` | Enable Opening Range Breakout debit spread as a complement to IC entries |
| `orb_range_minutes` | `5` | Minutes from open (9:30 AM) used to define the ORB high/low (9:30–9:35 AM) |
| `orb_breakout_threshold_pct` | `0.005` | Minimum break beyond ORB range required to trigger an entry (0.5% of underlying price) |
| `orb_wing_width` | `2` | Wing width in points for ORB debit spreads |
| `orb_entry_window_end` | `12:00` | No new ORB entries after this time (secondary breaks after noon have lower success rates) |
| `orb_profit_target_pct` | `1.00` | Close ORB spread at 100% profit on debit paid (2:1 reward-to-risk) |
| `orb_stop_loss_pct` | `0.50` | Close ORB spread at 50% loss on debit paid |
| `orb_close_time` | `15:30` | Force-close all open ORB positions by this time regardless of P&L |
| `late_entry_bias_enabled` | `true` | Prefer IC entries after noon when IV rank is borderline (reduces uncompensated morning directional exposure) |
| `late_entry_bias_iv_rank_max` | `0.45` | Apply late-entry bias when IV rank ≤ this value; skip IC entries before `late_entry_bias_start_time` |
| `late_entry_bias_start_time` | `12:00` | Do not enter new ICs before this time when IV rank is borderline (≤ `late_entry_bias_iv_rank_max`) |
| `nyse_holidays_2026` | 10 dates | Trading days to skip |

---

## Database

**Database**: `data/meic_trades.db` (SQLite, WAL mode). Three tables: `ic_trades` (one row per IC, primary key `ic_order_id`), `daily_summary` (one row per trading date, keyed on `summary_date`), `loop_log` (append-only iteration log). All reads and writes go through `src/db.py` subcommands — e.g. `python src/db.py save_trade --data '{...}'`.

---

## Loop Steps

1. **Load state** — read open trades, today's trade count, today's P&L, and current ET time. Skip new entries if the daily cap is reached; if `max_entries_per_day` is `-1`, buying power (checked in Step 4) is the only entry constraint.

2. **Time gate** — if the current time is outside the active trading window (before 09:30 or after 15:55 ET), in pre-market (08:00–09:29 ET), on a weekend, or a NYSE holiday, skip Steps 3–7 and proceed directly to Step 8 to schedule the next wakeup. **Force-close check**: if the current time is at or after `force_close_time` (15:45 ET) and any 0DTE positions remain open, immediately close all of them (BTC full IC) before logging and scheduling — do not wait for the stop management step.

3. **Daily connection check** — invoke `/daily-check`. Runs once per trading day; verifies the broker connection is live and logs the result.

4. **Market assessment** — gathers everything needed for trading decisions:
   - Confirms the connection is healthy
   - In parallel: retrieves account buying power and NLV, gets IV rank and underlying price, fetches working orders, and fetches open positions
   - Compares today's NLV to yesterday's; halts entries if down more than 5%
   - Fetches the option chain
   - Reconciles broker positions against the database (read-only; surfaces mismatches for human review)
   - Evaluates each wing width candidate in parallel, filters out widths that exceed buying power, and selects the best fit based on session time, IV rank, skew, gamma, and existing positions
   - Classifies the session window: open volatile / prime / midday / afternoon / late
   - Classifies IV skew (bearish / bullish / neutral) from chain greeks or strategy leg mids
   - Classifies price action signal (bearish / bullish / neutral) from underlying movement vs. prior close
   - **Regime detection**: fetch VIX from `get_market_overview`. Compute XSP 5-day ATR from recent daily ranges (available from chain or prior loop_log entries). If VIX > `regime_vix_pause_threshold` (25) OR 5-day ATR > `regime_atr_pause_threshold` (3.0 pts), set `trending_regime = true` — IC entries are paused this iteration but ORB debit spread entries remain eligible. Log the regime flag and the triggering metric.
   - **GEX regime check**: call `python src/tt.py get_gex --symbol XSP` (requires streamer running for OI). If `ok` is true: (a) if `gex_positive` is false (net GEX < 0, price is below the gamma flip), add `gex_negative` to `trending_regime` flags — IC entries are blocked; (b) record `call_wall`, `put_wall`, and `gamma_flip` for use in strike placement (Step 6) and stop tightening (Step 5). If `ok` is false (OI not yet cached), log a warning and proceed without GEX — do not block entries solely on missing GEX data. **Zero-Gamma Threat**: if `gex_positive` is true but `abs(spot - gamma_flip) / spot < 0.003` (price within 0.3% of the flip), note the threat; do not block entries but use this to tighten stop_trigger_current toward 0.85 for any open ICs this iteration.
   - **ORB range capture** (if `orb_enabled`): on the first loop iteration at or after 9:35 AM ET, record the high and low of XSP during the first `orb_range_minutes` (5) minutes of trading (9:30–9:35 AM) as `orb_high` and `orb_low`. These values persist for the remainder of the session. If `orb_high`/`orb_low` are not yet available (pre-9:35 AM), skip ORB evaluation this iteration.

5. **Stop management** — invoke `/stop-management`. Runs every iteration for all open trades. Stop management executes in this priority order each iteration: (1) profit-target check — close full IC if current cost ≤ 50% of credit collected; (2) per-side software stop — close call spread or put spread independently when its cost reaches `net_credit`; (3) stop tightening evaluation; (4) event and EOD force-close (FOMC blackout at 13:30 ET, triple-witching/quarterly-expiry force-close at 14:00 ET, general force-close at 15:45 ET). Exchange-level multi-leg stop orders are not supported by tastytrade for combo orders — software monitoring is the only mechanism; the 120-second loop cadence during open positions provides the monitoring frequency.

6. **Entry decision** — hard stops are checked first (time window, buying power, quotes unavailable, credit outside configured bounds, strike overlap with open positions, delta and OTM distance limits, concurrent IC limit, IV rank floor, credit floor, quarterly/triple-witching expiry rules, regime gate, late-entry bias); ORB opportunity is evaluated in parallel with IC entry; everything else uses judgment based on session quality, IV signals, credit vs. risk, POP estimate, open exposure, skew symmetry, wing width, and OTM distance guardrails.

   **Strike overlap hard stop**: before accepting any entry, verify that none of the four proposed strikes (short put, long put, short call, long call) matches any strike already held in any open IC, regardless of leg direction. A duplicate strike would either net out an existing leg (partial close) or result in more than one contract at the same strike. If any overlap exists, reject the entry entirely for this iteration.

   **Call delta hard stop**: if the actual `call_delta_at_entry` from the strategy scan exceeds `max_call_delta_entry` (0.17), reject the entry — do not enter. During open_volatile or late sessions use `max_call_delta_entry_open_volatile` (0.16) instead. The delta-0.15 scan target is a heuristic; the actual returned delta must be verified and must fall within this ceiling. This is a non-negotiable hard stop.

   **OTM distance hard stop**: if the short call is fewer than `min_call_otm_points` (4.0) points OTM, or the short put is fewer than `min_put_otm_points` (3.0) points OTM, reject the entry. These distances are measured as (`strike − underlying_price`) for calls and (`underlying_price − strike`) for puts.

   **Concurrent IC hard stop**: if the count of currently open ICs equals `max_concurrent_ics` (2), reject all new entries until one closes. This is a hard cap on simultaneous exposure, independent of daily entry count.

   **IV rank floor**: if the current IV rank is below `min_iv_rank` (0.35), reject all new entries. Insufficient implied volatility means credit collected is too low to justify the gamma risk of a 0DTE IC. This check uses the IV rank fetched in Step 4.

   **Credit floor**: after computing the IC credit (from live quotes), verify that `ic_natural_bid ≥ min_credit_pct_of_width × wing_width`. For a 2-wide IC this means ≥ $0.40; for a 3-wide ≥ $0.60; for a 5-wide ≥ $1.00. Reject if below. This is a hard stop — a credit below 20% of wing width offers insufficient reward for the risk.

   **FOMC blackout hard stop**: if today is in `fomc_dates_2026`, apply: (a) if the current time is at or after `fomc_blackout_start` (13:30 ET), reject all new entries and close any open positions immediately before the announcement window; (b) new entries are only permitted before 13:30 ET or after `fomc_blackout_end` (14:30 ET), and post-announcement entries require IV rank ≥ 0.40 and intraday range ≤ 3.5 points. On FOMC days, tighten stop_trigger_current on all open ICs by 10% relative to current value at 13:00 ET as a pre-announcement precaution.

   **Quarterly expiry hard stops**: if today's date is in `quarterly_expiry_dates_2026` or `triple_witching_dates_2026`, apply all of the following before accepting any entry: (a) if the session is `open_volatile`, reject all entries regardless of other signals; (b) require the short call to be at least `quarterly_expiry_min_call_otm` (5.0) points OTM instead of the standard minimum; (c) if the XSP intraday range (session high − session low) has already exceeded `quarterly_expiry_max_intraday_range` (3.5 points), halt all entries for the remainder of the session; (d) on `triple_witching_dates_2026`, no new entries after 12:30 PM ET and force-close all positions by 14:00 ET.

   **Regime gate (IC entries only)**: if `trending_regime = true` (VIX > `regime_vix_pause_threshold`, 5-day ATR > `regime_atr_pause_threshold`, OR `gex_negative`), reject all IC entries this iteration. Log the reason and triggering metric. ORB debit spread entries (below) are NOT blocked by the regime gate — they profit from the directional environment that pauses IC entries.

   **GEX strike placement** (when GEX data is available and `gex_positive`): use `call_wall` from `get_gex` as the upper anchor for the short call — target a strike at or above the Call Wall (subject to the existing delta ceiling and OTM distance hard stops). Use `put_wall` as the lower anchor for the short put. If `call_wall` is significantly larger than `put_wall` (call-heavy GEX), the short call can be placed closer to the wall; give the short put more room. If `put_wall` >> `call_wall`, reverse. These are guidance signals; existing hard stops (delta ceiling, OTM distance floor, credit floor) override GEX placement whenever they conflict.

   **GEX stop tightening triggers** (applied during stop management, Step 5): (a) Zero-Gamma Threat (`gex_positive` but price within 0.3% of `gamma_flip`): reduce `stop_trigger_current` toward 0.85 for all open ICs. (b) Gamma flip breached (`gex_negative`): reduce `stop_trigger_current` toward 0.80 and evaluate closing the threatened IC side immediately. (c) Price approaching but not through the Call Wall: maintain stop, do not close — dealer resistance is strongest here. If the Call Wall breaks on volume, close the threatened side immediately.

   **Late-entry credit bias**: if `late_entry_bias_enabled` is true, IV rank ≤ `late_entry_bias_iv_rank_max` (0.45), and current time is before `late_entry_bias_start_time` (12:00 ET), skip new IC entries and wait until noon. Entering an IC in the morning at borderline IV carries 3+ hours of directional exposure for the same credit available in the afternoon when theta has already accelerated to 2–5× its morning rate. This is not a hard block on high-IV days (IV rank > 0.45 bypasses the bias).

   **ORB debit spread evaluation** (if `orb_enabled` and `orb_high`/`orb_low` are set and current time ≤ `orb_entry_window_end` = 12:00 ET):
   - Compute break distance: if `underlying_price > orb_high × (1 + orb_breakout_threshold_pct)` → bullish breakout; if `underlying_price < orb_low × (1 − orb_breakout_threshold_pct)` → bearish breakout.
   - If a first-of-session breakout is detected and no ORB position is already open:
     - **Bullish break**: buy bull call debit spread — buy ATM call, sell call `orb_wing_width` (2) points higher. Both same-day expiration.
     - **Bearish break**: buy bear put debit spread — buy ATM put, sell put `orb_wing_width` (2) points lower.
   - Dry-run the order, then submit live if dry run passes.
   - Record ORB position separately (not as an `ic_trades` entry — use `loop_log` with action `orb_entry` and full leg detail).
   - Manage the ORB position: close at `orb_profit_target_pct` (100%) profit, `orb_stop_loss_pct` (50%) loss, or `orb_close_time` (15:30) whichever comes first. Check on every loop iteration while open.
   - Only one ORB trade per day per direction; do not re-enter after a stop-out.

7. **Execute entry** — only if the entry decision is yes: invoke `/execute-entry`.

8. **Record and notify** — logs the iteration action and a one-line status message, then schedules the next wakeup per the interval table below. After 15:55 on a trading day, runs the EOD sequence: persists closing NLV, spawns the EOD report, and logs completion.

---

After completing Step 8, schedule the next wakeup using these intervals:

| Condition | Interval |
|---|---|
| No market action expected within 90 min (weekend, holiday, or before 08:00 ET) | **end loop** |
| After 15:55 ET on a trading day (EOD complete) | **Step 8 then end loop** |
| Pre-market 08:00–09:00 ET | **600s** |
| Pre-market 09:00–09:29 ET (approaching open) | **120s** |
| Market hours, off-hours outside pre-market window | **1800s** |
| Market hours with no open positions | **300s** |
| Market hours with one or more open positions | **120s** |

Use the longest applicable interval.

---

## Optional: agentmemory

Persistent cross-session memory for the agent. Observations are captured automatically via Claude Code hooks and recalled in future sessions, so the agent retains context about past trading behavior, conflict patterns, and session history across restarts.

**Setup:**
1. Install globally in WSL2: `npm install -g @agentmemory/agentmemory`
2. Start the server: `agentmemory start`
3. Add the MCP server to `.claude/settings.json` under `mcpServers`
4. Wire hooks and auto-start into `~/.claude/settings.json`: `agentmemory connect claude-code`
5. Optionally enable local semantic embeddings by adding `EMBEDDING_PROVIDER=local` to `~/.agentmemory/.env`

The skills, lock file, and `.agents/` directory installed by agentmemory are gitignored — they are machine-local and not part of the repo.
