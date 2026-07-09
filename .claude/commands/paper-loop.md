Run one iteration of the MEICAgent parallel-shadow paper-trading loop. Self-paced like `/loop`: run this now, then schedule the next wakeup per the interval table at the end.

This is a **separate, isolated loop** from the live trading loop in CLAUDE.md — it never touches `data/meic_trades.db`, never calls `execute_trade --live`, and is not subject to the CRITICAL_GUARDRAIL restrictions on the live loop (there is no broker order in this path to guard). All writes go to `data/paper_trades.db` via `python src/db.py --db data/paper_trades.db <command>`.

**Purpose**: generate capital-free, apples-to-apples performance data for all four risk profiles (conservative/moderate/aggressive/very-aggressive) simultaneously, on identical market days, so they can be objectively compared and one graduated to live. See `docs/paper-trading.md` for the full design and the graduation gate.

## 1. Time gate

Skip straight to Step 5 (schedule) if: before 09:30 ET, after 16:00 ET, a weekend, or a NYSE holiday (`nyse_holidays_2026` in `config.json`). No pre-market pass is needed for paper trading — nothing opens before the cash session starts.

## 2. Fetch one shared market snapshot per symbol (live quotes)

For **each symbol in `config.json`'s `symbols`** (SPX, XSP, NDX, RUT — same universe as the live loop):

1. Get the underlying price and IV rank: `python src/tt.py get_market_overview --symbols <SYM>`.
2. Get VIX and VIX1D once per iteration (shared across symbols, same as the live loop's Step 4a): `python src/tt.py get_vix1d`. Compute `vix1d_ratio = vix1d / vix`.
3. Get this symbol's 5-day ATR (from prior loop history or the chain, same source the live loop uses).
4. Get GEX: `python src/tt.py get_gex --symbol <SYM>`. If `ok` is false, set `snapshot["gex"] = {"ok": false}` and continue — do not block on missing GEX.
5. **Fetch multiple wing-width candidates** — this is the key difference from the live loop's single-scan approach, since the deterministic engine needs to pick the widest candidate that clears each profile's own credit floor. Use this symbol's entry from `wing_widths_by_symbol` in `config.json` (falling back to its `DEFAULT` list) — e.g. `[5, 10]` for SPX/QQQ, `[1, 2, 5]` for XSP, `[2, 5]` for IWM. These are set per instrument because wing width is dollar-denominated risk (10 points is ~0.13% of SPX but ~3.4% of IWM). For each width in the list, call:
   ```bash
   python src/tt.py get_strategies --symbol <SYM> --short_delta <vix_banded_delta_target> --wing_width <width> --around_price <last>
   ```
   using the same VIX-banded delta target selection as CLAUDE.md Step 4b (`delta_target_vix_low/_elevated/_high/_crisis`). Collect each width's four legs (`short_put`, `long_put`, `short_call`, `long_call`) with `strike`, `streamer_symbol`, `delta`, and live `bid`/`ask` (add `--include_quotes` or fetch via `get_option_chain --include_quotes` for the four specific strikes).
6. Warm the streamer cache for any paper legs not already subscribed (the streamer only auto-subscribes legs read from `data/meic_trades.db`, not the paper DB): `python src/tt.py stream_subscribe --symbols <streamer_symbols...>` for all legs across all fetched candidates.
7. Build `leg_quotes`: for every already-open paper IC on this symbol (any profile), fetch current bid/ask/mid for its four legs via `get_option_chain --include_quotes`, keyed by `streamer_symbol`.
8. Classify `session_quality` (open_volatile/prime/midday/afternoon/late) exactly as CLAUDE.md Step 4b does.

Assemble one snapshot dict per symbol:
```json
{
  "symbol": "XSP", "date": "<today ET, YYYY-MM-DD>", "now_et": "<HH:MM>",
  "expiration": "<today>", "dte": 0,
  "underlying_price": <float>, "iv_rank": <float>, "iv_rank_source": "native",
  "vix": <float>, "vix1d_ratio": <float>, "atr_5day": <float>,
  "session_quality": "<...>", "gex": {"ok": true, "gex_positive": true, ...},
  "candidates": [ {"wing_width": 2, "short_put": {...}, "long_put": {...}, "short_call": {...}, "long_call": {...}}, ... ],
  "leg_quotes": { "<streamer_symbol>": {"bid":..,"ask":..,"mid":..}, ... }
}
```

## 3. Run the deterministic engine

For each symbol, hand its snapshot to the paper engine — this single call marks/exits every open paper IC on that symbol across all four profiles, then evaluates new entries for each profile independently against the same snapshot:

```bash
python src/paper.py --db data/paper_trades.db process_symbol --snapshot '<snapshot JSON>' --execution_mode paper
```

The engine is a pure deterministic function (see `src/paper.py`) — it applies the fixed policy (widest wing width clearing the fee-aware credit floor, `natural_bid` entry, no discretionary skips) so results are reproducible and reflect the profile parameters, not agent judgment. Do not override its entry/exit decisions.

## 4. Log the iteration

For each symbol, log a paper-scoped loop row so `/paper-report` and the dashboard have an audit trail:
```bash
python src/db.py --db data/paper_trades.db log_loop_action --symbol <SYM> --action paper_iteration --reasoning "<one-line summary of entries/exits across all 4 profiles>"
```

## 5. Schedule the next wakeup

Use the same interval table as the live loop (CLAUDE.md, end of Loop Steps), since paper trading tracks the same market hours:

| Condition | Interval |
|---|---|
| No market action expected within 90 min (weekend, holiday, before 08:00 ET) | end loop |
| After 16:00 ET on a trading day | end loop |
| Market hours, any open paper positions across any profile | 120s |
| Market hours, no open paper positions | 300s |

Prompt for the next wakeup: `/paper-loop`.
