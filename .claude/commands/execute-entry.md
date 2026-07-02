Execute a new MEICAgent iron condor entry. Only invoke when the entry decision (Step 6) is yes.

Requires: strategy strikes, wing width, and quotes already evaluated this iteration.

1. **Strike overlap check** — fetch open trades and collect all strikes currently held across all open ICs. Confirm that none of the four proposed strikes (short put, long put, short call, long call) appears in that set. A matching strike would either net out an existing leg (partial close) or result in more than one contract at the same strike — both are disallowed. If any overlap is found, abort immediately without submitting an order.

2. **Price check** — get the current underlying price:
```bash
python src/tt.py get_quote --symbol <symbol>
```
If the underlying has moved more than 0.5 points since the `get_strategies` call, re-fetch strikes:
```bash
python src/tt.py get_strategies --symbol <symbol> --short_delta <delta_target> --wing_width <width> --around_price <last>
```

3. **Fetch live quotes** — get the current bid/ask/mid for all four legs:
```bash
python src/tt.py get_option_chain --symbol <symbol> --expiration <date> --include_quotes --around_price <last>
```
From the response, extract for each of the four legs: `bid`, `ask`, `mid`. Compute:
- `ic_mid` = short_put_mid + short_call_mid − long_put_mid − long_call_mid
- `ic_natural_bid` = short_put_bid + short_call_bid − long_put_ask − long_call_ask
- `avg_spread_per_leg` = mean of (ask − bid) across all four legs

Abort if `ic_mid` ≤ 0 or `ic_natural_bid` ≤ 0.

4. **Apply price strategy** — based on `entry_price_strategy` config:

   **`mid` (default)**:
   Attempt a Day limit at `ic_mid` if conditions are favorable; fall back to `ic_natural_bid` otherwise.

   *Gate — skip mid attempt and go straight to natural_bid if any of:*
   - `avg_spread_per_leg` > `mid_spread_gate` (default 0.10) — spread too wide for a mid fill
   - Session is `open_volatile` (9:45–10:15, elevated gamma) — mid orders stale too fast
   - Session is `late` (after 14:45) — time pressure outweighs credit improvement
   - `ic_mid − ic_natural_bid` < 0.03 — spread so tight the improvement isn't worth the wait

   *If gate passes:*
   - Submit a Day limit at `ic_mid` (round to nearest $0.01). Go to Step 5 (dry run), then Step 6 (submit).
   - After submitting, wait `mid_improve_wait_seconds` (default 45s) polling working orders.
   - If filled within the wait: proceed to Step 7 (save trade).
   - If unfilled: cancel the working order, then resubmit at `ic_natural_bid` as a Day limit. Proceed to Step 5 and Step 6 for the fallback order.

   *If gate fails:* use `ic_natural_bid` directly as a Day limit.

   **`natural_bid`**: submit the order at `ic_natural_bid` as a Day limit.

   **`ioc_step`**: try IOC orders at `ic_natural_bid` + each increment in `ioc_step_increments`, waiting `ioc_step_wait_seconds` per attempt. If none fill, fall back to `ic_natural_bid` as a Day limit.

   **`day_improve`**: submit a Day limit at `ic_natural_bid` + `day_improve_amount`. Wait `day_improve_wait_seconds`. If unfilled, cancel and resubmit at `ic_natural_bid`.

   **`auto`**: choose strategy based on session window, spread width, and IV rank:
   - `open_volatile` or `late`: use `natural_bid`
   - `prime` or `midday`, `avg_spread_per_leg` ≤ 0.07: use `mid`
   - `prime` or `midday`, `avg_spread_per_leg` > 0.07: use `day_improve`
   - `afternoon`: use `day_improve` if IV rank > 0.40, else `natural_bid`

5. **Dry run** — submit a dry-run order at the chosen limit price and check for errors, buying-power warnings, or rejections. Abort without submitting if the dry run fails.
```bash
python src/tt.py execute_trade --order '<JSON order spec>'
```
(Default is dry run; omit `--live` to validate only.)

5a. **Pre-submit requote** — immediately before adding `--live`, re-fetch the current bid/ask for all four legs:
```bash
python src/tt.py get_option_chain --symbol <symbol> --expiration <date> --include_quotes --around_price <last>
```
Recompute `ic_natural_bid` from the fresh quotes. If either of these conditions holds, **abort** the live submission and re-evaluate next iteration:
- `ic_natural_bid` ≤ 0 (credit has flipped to a debit — would trigger Spread Checker rejection)
- `ic_natural_bid` < `planned_limit_price` − `pre_submit_requote_threshold` (price has dropped more than $0.03 from the dry-run price)

Do not widen the limit to compensate — abort cleanly. Log the abort reason. This check catches underlying price drift between the Step 3 quote fetch and actual submission.

6. **Submit the order** — add `--live` to submit the real order. Based on `separate_spread_entry` config:
   - `false`: one 4-leg combo order.
   - `true`: two separate 2-leg spread orders (dry run each before submitting each).
   - `auto`: use separate spreads if IV rank > 0.35, session is late or open_volatile, or 2+ ICs already open; otherwise use combo.

7. **Save the trade** — record the new IC in the database:
```bash
python src/db.py save_trade --data '<JSON with all entry fields>'
```
Required fields: `ic_order_id`, `symbol`, `put_strike`, `call_strike`, `wing_width`, `put_credit`, `call_credit`, `net_credit`, `quantity`, `put_delta_at_entry`, `call_delta_at_entry`, `long_put_delta_at_entry`, `long_call_delta_at_entry`, `underlying_price_entry`, `iv_rank_at_entry`, `session_quality`, `iv_skew_signal`, `price_action_signal`, `ai_entry_reasoning`, `stop_trigger_original`, `stop_limit_original`, `stop_trigger_current`, `stop_limit_current`.

`stop_trigger_original` and `stop_trigger_current` both start equal to config's `stop_trigger_ratio` (0.95); `stop_limit_original` and `stop_limit_current` both start equal to config's `stop_limit_ratio` (1.02). The `_current` fields are the ones stop-management reads and may tighten over time (Step 6 of stop-management); `_original` is kept for reference/audit.

Record `entry_price_strategy_used` and `entry_limit_price` so the EOD report can track fill rates per strategy.

8. **Log the entry**:
```bash
python src/notify.py log_event --level INFO --message "Entry: <symbol> IC <put_strike>/<call_strike> credit <net_credit> (strategy: <strategy_used>)"
```
