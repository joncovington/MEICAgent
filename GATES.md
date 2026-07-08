# MEICAgent Entry Gates & Constraints

Complete reference of all gates, blocks, and constraints that control IC and ORB entry decisions.

## Sequential Gate Flow

Each gate runs in order; an entry is rejected immediately upon hitting the first gate that blocks it.

---

## Time-Based Gates

### 1. Time Window Gate (Global)
- **Trigger**: Before 09:30 ET or after 15:55 ET, on weekends, or on NYSE holidays
- **Effect**: Skip Steps 3–7 entirely; jump to Step 8 (next wakeup scheduling)
- **Config**: None (hard-coded trading hours)
- **IC Impact**: Blocks all entries
- **ORB Impact**: Blocks all entries

### 2. Force-Close Gate (Global)
- **Trigger**: At or after `force_close_time` (15:45 ET by default)
- **Effect**: Close all 0DTE positions immediately (BTC full IC)
- **Config**: `force_close_time` (15:45 ET)
- **Escalation**: For physically-settled symbols not in `cash_settled_symbols`, log at CRITICAL if close fails; for cash-settled, routine retry
- **IC Impact**: Forces close, does not allow entries
- **ORB Impact**: Forces close

### 3. Entry Window Gate (IC only)
- **Trigger**: Outside `entry_window_start` (10:00 ET) to `entry_window_end` (14:30 ET)
- **Effect**: Reject IC entries
- **Config**: `entry_window_start`, `entry_window_end`
- **IC Impact**: Blocks entries (hard stop)
- **ORB Impact**: Not blocked; ORB has its own window

---

## Regime Gates (Pause IC, Allow ORB)

### 4. VIX Regime Gate (Global, Account-Wide)
- **Trigger**: VIX > `regime_vix_pause_threshold` (25)
- **Effect**: Pause IC entries for ALL symbols this iteration
- **Config**: `regime_vix_pause_threshold` (25)
- **Rationale**: High VIX = elevated skew, tail-risk hedging, unfavorable for short premium
- **IC Impact**: Blocks all IC entries
- **ORB Impact**: NOT blocked (ORB profits from directional environment)

### 5. VIX1D Ratio Gate (Global, Account-Wide)
- **Trigger**: VIX1D/VIX > `regime_vix1d_ratio_pause_threshold` (1.30)
- **Effect**: Pause IC entries for ALL symbols
- **Config**: `regime_vix1d_ratio_pause_threshold` (1.30)
- **Rationale**: Event day signal (Fed, CPI, etc.); same-day vol > 30-day vol by 30%+ = structural uncertainty
- **Status**: Documented trader convention, not independently backtested for this strategy
- **IC Impact**: Blocks all IC entries
- **ORB Impact**: NOT blocked

### 6. ATR Regime Gate (Symbol-Specific)
- **Trigger**: 5-day ATR > `regime_atr_pause_threshold` (30.0 pts)
- **Effect**: Pause IC entries for THIS symbol only
- **Config**: `regime_atr_pause_threshold` (30.0 pts), `regime_atr_lookback_days` (5)
- **Rationale**: Elevated realized volatility = trending environment, unfavorable for static IC width
- **IC Impact**: Blocks this symbol's IC entries (other symbols unaffected)
- **ORB Impact**: NOT blocked

### 7. GEX Negative Gate (Symbol-Specific)
- **Trigger**: Net GEX < 0 (price below gamma flip); dealers short gamma
- **Effect**: Pause IC entries for THIS symbol
- **Config**: None (computed from streamer's GEX data)
- **Rationale**: Negative gamma = dealer amplification of moves, unstable for short premium
- **IC Impact**: Blocks this symbol's IC entries
- **ORB Impact**: NOT blocked
- **Fallback**: If GEX data unavailable, proceed without GEX (do not block on missing data)

### 8. Zero-Gamma Threat (Symbol-Specific, Non-Blocking)
- **Trigger**: Price within 0.3% of gamma flip level; close to regime boundary
- **Effect**: Do NOT block; instead tighten `stop_trigger_current` toward 0.85 for any open ICs on this symbol
- **Config**: None (threshold 0.3% hard-coded)
- **Rationale**: Imminent regime flip = stop management risk
- **IC Impact**: Does not block, but tightens stops
- **ORB Impact**: Not applicable

---

## Expiration & Cycle Gates

### 9. 0DTE Expiration Gate (Hard Stop)
- **Trigger**: `dte != 0` (expiration is not today)
- **Effect**: Reject entry immediately
- **Config**: None (hard-coded requirement)
- **Rationale**: Strategy assumes same-day theta/gamma; multi-day spreads break stop management, force-close timing, credit floors
- **IC Impact**: Blocks entry with reason `no_0dte_expiration`
- **ORB Impact**: Blocks entry

### 10. Quarterly Expiry Gate
- **Trigger**: On OPEX (quarterly expiration) day AND `quarterly_expiry_skip_open_volatile` is true AND session is volatile (e.g., open_volatile flag set)
- **Effect**: Skip IC entries this symbol; apply tighter `quarterly_expiry_min_call_otm_pct` (0.67%) for allowed entries
- **Config**: `quarterly_expiry_skip_open_volatile`, `quarterly_expiry_min_call_otm_pct` (0.67%), `quarterly_expiry_dates_2026`
- **Rationale**: OPEX = extreme gamma concentration, pinning risk, wide spreads, low liquidity
- **IC Impact**: Blocks or restricts entries on OPEX
- **ORB Impact**: Not explicitly blocked, but liquidity may be poor

---

## Capacity & Resource Gates

### 11. Buying Power Gate (Global, Hard Stop)
- **Trigger**: Insufficient cash to collateralize new IC
- **Effect**: Reject entry
- **Config**: None (checked against account in real-time)
- **Rationale**: Binding account constraint; cannot be overridden
- **IC Impact**: Hard block
- **ORB Impact**: Hard block

### 12. Max Concurrent ICs Gate (Global, Hard Stop)
- **Trigger**: Already have `max_concurrent_ics` (4) open ICs
- **Effect**: Reject new IC entries
- **Config**: `max_concurrent_ics` (4)
- **Rationale**: Risk management; limits simultaneous directional exposure and stop management load
- **IC Impact**: Hard block
- **ORB Impact**: May also block if position count includes ORB

### 13. Daily IC Trade Target (Guidance, Soft)
- **Trigger**: Approaching `daily_ic_trade_target` (2)
- **Effect**: Do NOT hard-block; instead require higher conviction for additional entries
- **Config**: `daily_ic_trade_target` (2)
- **Rationale**: Heuristic guidance on daily activity; buying power is the binding constraint
- **IC Impact**: Soft guidance (higher selectivity, not a hard stop)
- **ORB Impact**: Not applicable

---

## Strike Quality & Risk Gates

### 14. Delta Gate (Call, Hard Stop)
- **Trigger**: Proposed call delta > `max_call_delta_entry` / `_open_volatile` / `_late`
  - Normal session: `max_call_delta_entry` (0.20 delta)
  - Volatile open: `max_call_delta_entry_open_volatile` (0.19)
  - Late session (after `late_entry_bias_start_time`): `max_call_delta_entry_late` (0.19)
- **Effect**: Reject entry
- **Config**: `max_call_delta_entry`, `max_call_delta_entry_open_volatile`, `max_call_delta_entry_late`
- **Rationale**: Higher delta = further ITM, tighter stops, higher early-close risk
- **IC Impact**: Hard block
- **ORB Impact**: Not applicable (ORB uses different delta logic)

### 15. OTM Distance Gates (Hard Stop)
- **Call OTM Gate**: Call strike < `min_call_otm_pct` (0.35%) above spot
- **Put OTM Gate**: Put strike < `min_put_otm_pct` (0.30%) below spot
- **Effect**: Reject entry
- **Config**: `min_call_otm_pct`, `min_put_otm_pct`
- **Rationale**: Too-close strikes = immediate stop risk or assignment risk
- **Quarterly OPEX Override**: On OPEX day, apply `quarterly_expiry_min_call_otm_pct` (0.67%) if `quarterly_expiry_skip_open_volatile` is true
- **IC Impact**: Hard block
- **ORB Impact**: Not applicable

### 16. Strike Overlap Gate (Hard Stop)
- **Trigger**: Proposed IC legs overlap with already-open positions on this symbol
- **Effect**: Reject entry
- **Config**: None (checked against `get_open_positions` for this symbol)
- **Rationale**: Prevents accidental double-stacking, simplifies stop management
- **IC Impact**: Hard block
- **ORB Impact**: Checked against existing ORB position

---

## Credit & Fee Gates

### 17. IV Rank Floor Gate (Hard Stop)
- **Trigger**: IV rank < `min_iv_rank` (0.30)
- **Effect**: Reject entry
- **Config**: `min_iv_rank` (0.30)
- **Rationale**: Low IV = poor credit relative to risk; entry edge is weak
- **IC Impact**: Hard block
- **ORB Impact**: Not explicitly checked

### 18. Gross Credit Floor Gate (Pct-of-Width, Hard Stop)
- **Trigger**: `ic_credit < min_credit_pct_of_width (0.15) × wing_width`
- **Effect**: Reject entry
- **Config**: `min_credit_pct_of_width` (0.15 or 10% in low-IV mode)
- **Rationale**: Insufficient credit relative to risk width
- **Low-IV Override**: If IV rank ≤ `low_iv_credit_floor_iv_rank_max` (0.35), apply `low_iv_min_credit_pct_of_width` (0.10)
- **IC Impact**: Hard block
- **ORB Impact**: Not applicable

### 19. Fee-Adjusted Credit Floor Gate (Hard Stop)
- **Trigger**: `(ic_credit × dollar_multiplier) − est_fee_per_contract < applicable_floor`
- **Effect**: Reject entry
- **Config**: `fee_estimate_lookback_trades`, `fee_estimate_min_sample_size`, `fee_estimate_fallback_per_contract`
- **Rationale**: Credit must clear estimated trading costs (opening); closing fees are typically waived on 0DTE expiration
- **Estimation Logic**: 
  - Call `python src/db.py get_fee_estimate --symbol <sym>` to retrieve historical average fee
  - If `sample_size < fee_estimate_min_sample_size`, fall back to `fee_estimate_fallback_per_contract[symbol]`
- **Example**: SPX: $0.45–$0.75 net credit (after fees) qualifies under this gate
- **IC Impact**: Hard block (separate from gross floor check; entry must clear both)
- **ORB Impact**: Not applicable

### 20. Spread Width Gate (Soft)
- **Trigger**: Avg per-leg bid-ask spread > `mid_spread_gate` (0.10 pts)
- **Effect**: Skip mid-price strategy; fall back to natural bid entry
- **Config**: `mid_spread_gate` (0.10)
- **Rationale**: Too-wide spreads = unfillable at streaming mid
- **IC Impact**: Soft gate; changes entry strategy rather than blocking
- **ORB Impact**: Not applicable

---

## IV & Bias Gates

### 21. Late-Entry Bias Gate (Soft, Time-Based)
- **Trigger**: 
  - `late_entry_bias_enabled` is true
  - IV rank ≤ `late_entry_bias_iv_rank_max` (0.45)
  - Current time < `late_entry_bias_start_time` (12:00 ET)
- **Effect**: Skip IC entries until noon; ORB unaffected
- **Config**: `late_entry_bias_enabled`, `late_entry_bias_iv_rank_max`, `late_entry_bias_start_time`
- **Rationale**: Morning entries at borderline IV carry 3+ hours of directional exposure; afternoon entries capture theta acceleration (2–5× morning rate)
- **High-IV Bypass**: If IV rank > `late_entry_bias_iv_rank_max` (0.45), do NOT skip; enter anytime
- **IC Impact**: Soft block (skips until noon; not a hard rejection)
- **ORB Impact**: Not blocked

---

## ORB-Specific Gates (ORB-Only)

### 22. ORB Enabled Gate
- **Trigger**: `orb_enabled` is false
- **Effect**: Skip ORB evaluation entirely
- **Config**: `orb_enabled` (true/false)
- **IC Impact**: Not applicable
- **ORB Impact**: Blocks all ORB entries

### 23. ORB Entry Window Gate
- **Trigger**: After `orb_entry_window_end` (12:00 ET)
- **Effect**: Reject ORB entries
- **Config**: `orb_entry_window_end` (12:00 ET)
- **Rationale**: ORB must be caught early; late-day setups lack the full 3+ hour trend window
- **IC Impact**: Not applicable
- **ORB Impact**: Hard block

### 24. ORB Breakout Detection Gate
- **Trigger**: Price move from 5-min open range < `orb_breakout_threshold_pct` (0.5%)
- **Effect**: No breakout detected; skip entry
- **Config**: `orb_range_minutes` (5), `orb_breakout_threshold_pct` (0.5%)
- **IC Impact**: Not applicable
- **ORB Impact**: Soft gate (no signal to trade)

### 25. ORB Already Open Gate
- **Trigger**: ORB position already filled this session
- **Effect**: Skip new ORB entries; log `action: "orb_already_open"`
- **Config**: None (state-based check)
- **IC Impact**: Not applicable
- **ORB Impact**: Soft block (one ORB per day max)

### 26. ORB Direction Exhausted Gate
- **Trigger**: ORB profit target already hit (`pnl ≥ orb_profit_target_pct` × wing_width)
- **Effect**: Close ORB position; skip re-entries until next day
- **Config**: `orb_profit_target_pct` (1.00 = 100%)
- **IC Impact**: Not applicable
- **ORB Impact**: Soft block (closes at target, no re-entries)

---

## Data Availability Gates

### 27. Quotes Unavailable Gate (Hard Stop)
- **Trigger**: Missing real-time chain snapshot, greeks, or live quotes for this symbol
- **Effect**: Reject IC entry for this symbol; log reason
- **Config**: None (data availability check)
- **IC Impact**: Hard block
- **ORB Impact**: Hard block (cannot assess risk without quotes)

### 28. GEX Data Unavailable Gate (Soft)
- **Trigger**: `get_gex` returns `ok=false` (OI not yet cached for this symbol)
- **Effect**: Do NOT block; proceed without GEX; log warning
- **Config**: None (streamer data availability)
- **Rationale**: GEX is useful but not required; entry proceeds blind to regime if streamer lags
- **IC Impact**: Soft gate (warning logged, entry allowed)
- **ORB Impact**: Not checked for ORB

### 29. VIX1D Unavailable Gate (Soft)
- **Trigger**: `get_vix1d` fails or returns no price
- **Effect**: Skip VIX1D ratio check; proceed with VIX/ATR/GEX only
- **Config**: None (data availability)
- **Rationale**: VIX1D is a secondary signal
- **IC Impact**: Soft gate (proceeds without it)
- **ORB Impact**: Not checked for ORB

---

## Summary: Gate Priorities

### Hard Blocks (Absolute, Cannot be Overridden)
1. Time window (before 09:30, after 15:55, weekends, holidays)
2. Buying power insufficient
3. 0DTE expiration gate
4. Quotes unavailable
5. Delta too high
6. OTM distance insufficient
7. Strike overlap
8. Both credit floors (gross + fee-adjusted)

### Regime/Soft Blocks (Block Entries, Allow Other Activity)
- VIX pause (all symbols)
- VIX1D pause (all symbols)
- ATR pause (symbol-specific)
- GEX negative (symbol-specific)

### Soft Guidance (Do Not Block; Adjust Behavior)
- Daily IC trade target (increases selectivity)
- Late-entry bias (skips until noon, not a hard rejection)
- Zero-gamma threat (tightens stops, not a block)
- IV rank floor (rejects but is philosophy-based, not mechanical)

### ORB-Specific (Bypass Regime Gates, Subject to Own Rules)
- ORB enabled
- ORB entry window
- ORB already open
- ORB direction exhausted

---

## Config Reference

| Gate | Config Key(s) | Default | Notes |
|------|---------------|---------|-------|
| Time window | Hard-coded | 09:30–15:55 ET | No config |
| Force close | `force_close_time` | 15:45 ET | All 0DTE closed |
| Entry window (IC) | `entry_window_start`, `entry_window_end` | 10:00–14:30 ET | — |
| VIX pause | `regime_vix_pause_threshold` | 25 | Account-wide |
| VIX1D ratio | `regime_vix1d_ratio_pause_threshold` | 1.30 | Account-wide |
| ATR pause | `regime_atr_pause_threshold`, `regime_atr_lookback_days` | 30.0 pts, 5 days | Symbol-specific |
| Max concurrent ICs | `max_concurrent_ics` | 4 | Hard stop |
| Daily IC target | `daily_ic_trade_target` | 2 | Soft guidance |
| Delta (call) | `max_call_delta_entry`, `_open_volatile`, `_late` | 0.20, 0.19, 0.19 | Hard stop |
| OTM (call, put) | `min_call_otm_pct`, `min_put_otm_pct` | 0.35%, 0.30% | Hard stop |
| OPEX OTM override | `quarterly_expiry_min_call_otm_pct`, `quarterly_expiry_skip_open_volatile` | 0.67%, true | Tighter on OPEX |
| IV rank floor | `min_iv_rank` | 0.30 | Hard stop |
| Credit floor (gross) | `min_credit_pct_of_width`, `low_iv_min_credit_pct_of_width`, `low_iv_credit_floor_iv_rank_max` | 0.15, 0.10, 0.35 | Hard stop |
| Fee floor | `fee_estimate_lookback_trades`, `fee_estimate_min_sample_size`, `fee_estimate_fallback_per_contract` | 20 trades, 5 min, symbol-specific | Hard stop |
| Spread width (soft) | `mid_spread_gate` | 0.10 | Changes strategy |
| Late-entry bias | `late_entry_bias_enabled`, `late_entry_bias_iv_rank_max`, `late_entry_bias_start_time` | true, 0.45, 12:00 ET | Soft block |
| ORB enabled | `orb_enabled` | true | Entire feature |
| ORB window | `orb_entry_window_end` | 12:00 ET | Hard stop |
| ORB range | `orb_range_minutes`, `orb_breakout_threshold_pct` | 5 min, 0.5% | Soft gate |
| ORB profit target | `orb_profit_target_pct` | 1.00 (100%) | Close at target |

