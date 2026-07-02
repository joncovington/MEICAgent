Run the MEICAgent stop management step. Executes every loop iteration for all open trades **across every configured symbol in one pass** (not scoped to one symbol) — each trade record already carries its own `symbol`, so every rule below that references "the symbol" means that specific trade's symbol, not a single configured default.

Requires: open trades list and current market data (underlying price, option chain) already gathered this iteration.

1. **Detect and chase unfilled stop-close orders** — tastytrade doesn't support exchange-level multi-leg stop orders for combos, so `put_stop_order_id`/`call_stop_order_id` on an open or partial IC identify a *software-triggered closing order* placed in a prior iteration (Step 5 below), not a resting exchange stop. For each open/partial IC with one of these order IDs set, check working orders:
   - **Order no longer working (filled)**: the side is closed. Confirm the fill price from the order/transaction history, update `put_stop_cost`/`call_stop_cost` with the actual fill, and record the leg now: `python src/db.py record_leg_exit --ic_order_id "<id>" --side put|call --status stopped --exit_time "<iso>" --exit_reason "<reason>" --exit_price <actual_fill_price> --pnl <leg_pnl>`. Clear the stored order ID.
   - **Order still working (unfilled)**: the quote moved away since it was placed. Cancel it (`python src/tt.py close_position --order_id <id>`), recompute the closing price from fresh quotes using the same formula as Step 5 (`(short_ask − long_bid) × stop_limit_current`), resubmit a new Day limit, and update the stored order ID to the new order. Do **not** call `record_leg_exit` yet — that only happens once a fill is confirmed, so P&L isn't recorded against an order that never actually executed.

2. **Cancel stale pending entries** — any IC in `pending` or `partial_entry` status with `entry_time` more than 10 minutes ago:
   - Cancel the working order: `python src/tt.py close_position --order_id <id>`
   - If partially filled (one spread filled, one not), close the filled spread immediately to remove unhedged exposure.
   - Update status to `cancelled`.

3. **Confirm fills** — for any IC in `pending` status, check working orders. If the order is no longer listed, confirm the fill:
   - Update status to `open`, set `fill_confirmed_at`.
   - `put_stop_order_id`/`call_stop_order_id` stay unset at this point — tastytrade doesn't support exchange-level combo stops, so these fields are only populated later, in Step 5, when a software stop actually places a closing order.

4. **Profit-target check** — for each `open` IC, before checking stops:
   - Fetch the current bid/ask/mid for all four legs from the option chain.
   - Compute `ic_current_cost_mid` = short_put_mid + short_call_mid − long_put_mid − long_call_mid (total spread value to close).
   - If `ic_current_cost_mid ≤ profit_target_pct × net_credit` (default: 0.50 × net_credit), the position has captured 50%+ of the collected credit. Close the full IC immediately at `ic_natural_bid` as a Day limit. Update status to `closed_profit_target`, record `exit_reason = "profit_target_50pct"`.
   - Record both legs: `python src/db.py record_leg_exit --ic_order_id "<id>" --side put --status expired --exit_time "<iso>" --exit_reason "profit_target_50pct" --exit_price <put_fill> --pnl <put_leg_pnl>` and the same for `--side call`.

5. **Monitor software stops (per-side)** — for each `open` IC, manage the call spread and put spread independently:

   **Per-side cost computation:**
   - `call_spread_cost_mid` = short_call_mid − long_call_mid (positive = cost to close the short call spread)
   - `put_spread_cost_mid` = short_put_mid − long_put_mid (positive = cost to close the short put spread, note: put mids are positive)

   **Per-side stop trigger:** each side's stop fires when its cost equals or exceeds `net_credit` (the total IC credit). This means a single bad side can cost up to the full collected premium before being stopped — protecting capital at net breakeven while allowing the other side to continue decaying.

   - If `call_spread_cost_mid ≥ stop_trigger_current × net_credit` and no `call_stop_order_id` is already working (i.e. this side isn't already being chased per Step 1):
     - Close the call spread only (BTC short call + STC long call) at `(short_call_ask − long_call_bid) × stop_limit_current` as a Day limit. The `stop_limit_current` cushion (default 1.02, config `stop_limit_ratio`) prices the limit slightly past the marketable crossing price so it fills promptly even if the quote ticks against you before the order reaches the exchange.
     - Update `call_stop_cost` and `exit_reason` for the call side. Set IC status to `partial` (put spread still open). Record the new order's ID into `call_stop_order_id` (`python src/db.py update_trade --ic_order_id "<id>" --call_stop_order_id <order_id>`).
     - Do not call `record_leg_exit` yet — Step 1 confirms the fill and records the leg next iteration (or immediately if you can confirm the fill within this same iteration).
     - The put spread continues to run independently; re-evaluate it on every subsequent iteration.
   - If `put_spread_cost_mid ≥ stop_trigger_current × net_credit` and no `put_stop_order_id` is already working:
     - Close the put spread only (BTC short put + STC long put) at `(short_put_ask − long_put_bid) × stop_limit_current` as a Day limit, same cushion rationale as above.
     - Update `put_stop_cost` and `exit_reason` for the put side. Set IC status to `partial`. Record the new order's ID into `put_stop_order_id`.
     - Do not call `record_leg_exit` yet — same as above, Step 1 confirms and records the fill.
     - The call spread continues to run independently.
   - If both sides have triggered in the same iteration (uncommon), close the full IC in one combo order, record its ID into both `put_stop_order_id` and `call_stop_order_id`, and let Step 1 confirm and record both legs next iteration.

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
   - Mark as `expired` only if the position is flat and that trade's own `symbol` is in `cash_settled_symbols` (no exchange close needed — cash settlement handles it, but only if fully OTM). If the trade's symbol is not in `cash_settled_symbols`, it must be closed, never left to expire — see the assignment-risk escalation in Step 2 of the main loop.
   - For any side not already recorded via `record_leg_exit` this trade (i.e. still `open`), record it now with `--status expired|force_closed` matching the outcome, plus `--exit_time`, `--exit_reason`, `--exit_price`, and `--pnl` for that leg.

8. **Re-evaluate partial and post-stop positions** — apply the same close / buy-back-short / hold framework to any `partial` status IC on every iteration. When the remaining side finally closes (expires OTM or gets stopped), record its leg via `record_leg_exit` — the other side's row was already written when it stopped in step 5.
