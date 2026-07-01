Run the MEICAgent stop management step. Executes every loop iteration for all open trades.

Requires: open trades list and current market data (underlying price, option chain) already gathered this iteration.

1. **Detect filled stops** — for each open IC, check working orders. If a stop order has disappeared (filled), the spread is stopped out:
   - Evaluate the remaining spread: close it, buy back the short leg, or hold and monitor based on time remaining, credit remaining, and gamma risk.
   - Update trade status in the database accordingly.

2. **Cancel stale pending entries** — any IC in `pending` or `partial_entry` status with `entry_time` more than 10 minutes ago:
   - Cancel the working order: `python src/tt.py close_position --order_id <id>`
   - If partially filled (one spread filled, one not), close the filled spread immediately to remove unhedged exposure.
   - Update status to `cancelled`.

3. **Confirm fills** — for any IC in `pending` status, check working orders. If the order is no longer listed, confirm the fill:
   - Update status to `open`, set `fill_confirmed_at`.
   - Record `put_stop_order_id` / `call_stop_order_id` if stop orders were placed.

4. **Profit-target check** — for each `open` IC, before checking stops:
   - Fetch the current bid/ask/mid for all four legs from the option chain.
   - Compute `ic_current_cost_mid` = short_put_mid + short_call_mid − long_put_mid − long_call_mid (total spread value to close).
   - If `ic_current_cost_mid ≤ profit_target_pct × net_credit` (default: 0.50 × net_credit), the position has captured 50%+ of the collected credit. Close the full IC immediately at `ic_natural_bid` as a Day limit. Update status to `closed_profit_target`, record `exit_reason = "profit_target_50pct"`.

5. **Monitor software stops (per-side)** — for each `open` IC, manage the call spread and put spread independently:

   **Per-side cost computation:**
   - `call_spread_cost_mid` = short_call_mid − long_call_mid (positive = cost to close the short call spread)
   - `put_spread_cost_mid` = short_put_mid − long_put_mid (positive = cost to close the short put spread, note: put mids are positive)

   **Per-side stop trigger:** each side's stop fires when its cost equals or exceeds `net_credit` (the total IC credit). This means a single bad side can cost up to the full collected premium before being stopped — protecting capital at net breakeven while allowing the other side to continue decaying.

   - If `call_spread_cost_mid ≥ stop_trigger_current × net_credit`:
     - Close the call spread only (BTC short call + STO long call) at `short_call_bid − long_call_ask` as a Day limit.
     - Update `call_stop_cost` and `exit_reason` for the call side. Set IC status to `partial` (put spread still open).
     - The put spread continues to run independently; re-evaluate it on every subsequent iteration.
   - If `put_spread_cost_mid ≥ stop_trigger_current × net_credit`:
     - Close the put spread only (BTC short put + STO long put) at `short_put_bid − long_put_ask` as a Day limit.
     - Update `put_stop_cost` and `exit_reason` for the put side. Set IC status to `partial`.
     - The call spread continues to run independently.
   - If both sides have triggered in the same iteration (uncommon), close the full IC in one combo order.

   **Legacy full-IC stop (fallback):** if per-side cost data is unavailable (stale chain, streaming failure), fall back to the combined cost: `ic_current_cost_mid ≥ stop_trigger_current × net_credit` → close full IC.

6. **Evaluate stop tightening** — for each open IC where `stop_adjustment_count < max_stop_adjustments_per_ic`, consider tightening `stop_trigger_current` if any of:
   - Significant time has elapsed since entry (e.g., 2+ hours, most theta already decayed)
   - IV has moved materially (IV rank dropped ≥ 0.10 since entry — remaining premium is thin)
   - Underlying has moved against a short strike (within 2 points)
   - Gamma level is elevated (short delta > 0.25 on either side)
   - Expiry is approaching (within 90 minutes of close, tighten to 0.80 × net_credit)
   - **FOMC pre-announcement (today in `fomc_dates_2026`, current time 13:00–13:30 ET):** tighten stop_trigger_current by 10% on all open ICs as a pre-announcement precaution.
   - Record any adjustment via:
```bash
python src/db.py record_stop_adjustment --ic_order_id "<id>" --new_trigger <val> --new_limit <val> --reason "<reason>"
```

7. **EOD and event force-close** — forced close rules in priority order:
   - **FOMC blackout** (today in `fomc_dates_2026`, current time ≥ `fomc_blackout_start` = 13:30 ET): close all open ICs immediately before the announcement window.
   - **Triple witching / quarterly expiry force-close** (today in `triple_witching_dates_2026` or `quarterly_expiry_dates_2026`, current time ≥ 14:00 ET): close all open ICs.
   - **General force-close** (current time ≥ `force_close_time` = 15:45 ET): close all remaining open ICs regardless of P&L.
   - Mark as `expired` only if the position is flat and the symbol is in `cash_settled_symbols` (no exchange close needed — cash settlement handles it, but only if fully OTM).

7. **Re-evaluate partial and post-stop positions** — apply the same close / buy-back-short / hold framework to any `partial` status IC on every iteration.
