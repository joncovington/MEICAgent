Verify that the tastytrade MCP option chain and delta-based strike selection are working for the relevant trading session, **for every symbol in `config.json`'s `symbols` list**. Tests today's expiration if before 16:00 ET on a trading day; otherwise tests the next trading day. Repeat Steps 2–4 below once per symbol; report one row per symbol in the final table plus an overall verdict.

---

## Step 1: Determine target expiration and symbol list

```bash
python -c "
import datetime, pytz, json
et = pytz.timezone('America/New_York')
now = datetime.datetime.now(et)
with open('config.json') as f:
    cfg = json.load(f)
year = str(now.year)
holidays = set(cfg.get('nyse_holidays_' + year, []))
def next_trading_day(d):
    d += datetime.timedelta(days=1)
    while d.weekday() >= 5 or str(d) in holidays:
        d += datetime.timedelta(days=1)
    return d
today = now.date()
today_is_trading = today.weekday() < 5 and str(today) not in holidays
if today_is_trading and now.hour < 16:
    target = today
    reason = 'today (market open or pre-cutoff)'
else:
    target = next_trading_day(today)
    reason = 'next trading day (market closed or after 16:00 ET)'
dte = (target - today).days
test_width = max(1, cfg['max_wing_width'] // 2)
symbols = cfg.get('symbols') or ([cfg['symbol']] if cfg.get('symbol') else ['XSP'])
print(json.dumps({'now_et': now.strftime('%H:%M %Z'), 'target_date': str(target), 'dte': dte, 'reason': reason, 'symbols': symbols, 'delta_target': cfg['delta_target'], 'test_wing_width': test_width}))
"
```

Record: `target_date`, `dte`, `symbols` (the full list to test), `delta_target`, `test_wing_width`, `reason`. Run Steps 2–4 once for each entry in `symbols`.

---

## Step 2: Market overview

Call `get_market_overview` with `symbols: [<symbol>]`.

Extract:
- `iv_rank` and `iv_percentile` (strings — convert with `float()`)
- `last` underlying price to use as `around_price` in Steps 3 and 4

**If `last` is absent or null** (common after hours): proceed without `around_price` and note the omission — the chain window will center on the median strike rather than ATM.

---

## Step 3: Option chain and strategies (run in parallel)

**Option chain:**
```json
{
  "symbol": "<symbol>",
  "expiration": "<target_date>",
  "include_greeks": true,
  "include_quotes": true,
  "strike_count": 20,
  "around_price": <last price, or omit if unavailable>
}
```

**Strategies:**
```json
{
  "symbol": "<symbol>",
  "target_dte": <dte>,
  "wing_width": <test_wing_width>,
  "short_delta": <delta_target>,
  "around_price": <last price, or omit if unavailable>
}
```

---

## Step 4: Verify and report

### Chain checks
1. `ok: true` — request succeeded
2. `greeks_complete` — live greeks received for all strikes in window
3. `greeks_received` — count
4. `quotes_complete` — live bid/ask quotes received for all strikes
5. `quotes_received` — count

### Strategy checks
6. `ok: true` — request succeeded
7. `greeks_used_for_strike_selection` — live greeks drove strike selection (not positional fallback)
8. `net_credit` — per-share credit; `null` or `0` is expected after hours on an already-expired expiration
9. `estimated_pop`

### Cross-checks
10. **Strikes in chain**: confirm all four leg strikes (`short_put`, `long_put`, `short_call`, `long_call`) from `get_strategies` appear in the chain response. If any are missing, note whether a larger `strike_count` would capture them.
11. **Delta accuracy**: from the chain, look up the delta for the short put and short call symbols returned by `get_strategies`. Both should be within ±0.05 of `delta_target` in absolute value. If the symbols are not in the chain window, note it.

### Output

Present as a two-section table **per symbol** (repeat both tables for each entry in `symbols`), followed by one overall verdict covering every symbol tested:

**Chain health — `<SYMBOL>`**
| Check | Result | Notes |
|---|---|---|
| Request ok | ✓ / ✗ | |
| greeks_complete | ✓ / ✗ | N strikes |
| quotes_complete | ✓ / ✗ | N strikes |
| around_price used | ✓ / — | last price or median fallback |

**Strike selection — `<SYMBOL>`**
| Check | Result | Notes |
|---|---|---|
| Request ok | ✓ / ✗ | |
| Greeks used for selection | ✓ / ✗ | live / positional fallback |
| All 4 strikes in chain | ✓ / ✗ / partial | list any missing legs |
| Short put delta | ~0.XX | pass if within ±0.05 of target |
| Short call delta | ~0.XX | pass if within ±0.05 of target |
| net_credit | $X.XX | null/0 = after-hours expiry (expected) |

End with a one-line verdict per symbol, then an overall summary:

- **PASS** — all checks green for this symbol; chain and strike selection are ready for the next session.
- **NEEDS ATTENTION** — flag the specific failing check(s) and what to investigate for this symbol.
- **Overall**: PASS only if every symbol in `symbols` passed; otherwise list which symbol(s) need attention.
