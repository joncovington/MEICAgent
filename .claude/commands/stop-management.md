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

4. **Monitor software stops** — for each `open` IC in `spread` mode:
   - Fetch the current spread cost from the option chain.
   - If cost ≥ `stop_trigger_current × net_credit`, execute the closing order (full IC) and update status to `stopped`.

5. **Evaluate stop tightening** — for each open IC where `stop_adjustment_count < max_stop_adjustments_per_ic`, consider tightening if any of:
   - Significant time has elapsed since entry
   - IV has moved materially
   - Underlying has moved against a short strike
   - Gamma level is elevated
   - Expiry is approaching
   - Record any adjustment via:
```bash
python src/db.py record_stop_adjustment --ic_order_id "<id>" --new_trigger <val> --new_limit <val> --reason "<reason>"
```

6. **EOD force-close (after 15:00 ET)** — for each open IC:
   - Force-close any spread with excessive gamma risk or where the full IC is at a net debit.
   - Mark remaining open ICs as `expired` at EOD if the symbol is in `cash_settled_symbols`.

7. **Re-evaluate partial and post-stop positions** — apply the same close / buy-back-short / hold framework to any `partial` status IC on every iteration.
