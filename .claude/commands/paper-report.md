Produce the weekly (or custom-range) paper-trading performance report, comparing all four risk profiles side by side. Covers both `execution_mode='paper'` (forward, live-quote) and `execution_mode='replay'` (historical SPX) trades in one combined view, since both write to the same `data/paper_trades.db` schema.

## 1. Gather the range summary

```bash
python src/db.py --db data/paper_trades.db get_range_summary --start <YYYY-MM-DD> --end <YYYY-MM-DD>
```

Default the range to the last 7 calendar days ending today (ET) unless the user specifies a different window (e.g. "since program start", "last month"). This returns `profiles: { "conservative": {...}, "moderate": {...}, "aggressive": {...}, "very-aggressive": {...} }`, each with `total_trades`, `win_count`/`loss_count`/`win_rate_pct`, `profit_factor`, `avg_win`/`avg_loss`, `expectancy_per_trade`, `max_consecutive_losses`, `max_drawdown`, `worst_day`, `net_pnl`, and a `daily_pnl` series (date, net_pnl, cumulative_pnl) per profile.

## 2. Compute the risk-adjusted suite

`get_range_summary` gives dollar P&L and drawdown; derive the ratio metrics from each profile's `daily_pnl` series against the **$100,000 virtual bankroll** convention:

- **Period return** for each day = `net_pnl / 100000`.
- **Sharpe** = mean(period returns) / stdev(period returns), annualized by `sqrt(252)` if daily granularity.
- **Sortino** = same, but the denominator uses only the downside deviation (stdev of negative returns only).
- **Calmar** = (annualized return) / (max_drawdown / 100000).
- **Recovery factor** = net_pnl / max_drawdown (skip if max_drawdown is 0).

Flag **Sharpe > 3 or profit_factor > 4.0** as a likely overfit/curve-fit warning rather than a stronger pass — note this explicitly in the report rather than presenting it as unqualified good news.

## 3. Render equity + underwater curves

For each profile, build a markdown sparkline (or a compact ASCII bar sequence) from its `daily_pnl.cumulative_pnl` series (equity = 100000 + cumulative_pnl) and a parallel underwater curve (running peak − current equity). No plotting dependency needed — this is a text report.

## 4. Graduation-gate checklist

For each profile, check all six pre-registered gates from `docs/paper-trading.md`:

| Gate | Threshold |
|---|---|
| Sample size | ≥ 30 filled ICs (20 = hard floor, flag if 20–29) |
| Expectancy | `expectancy_per_trade` > 0 |
| Win rate | `win_rate_pct` ≥ 65% |
| Profit factor | 1.3 ≤ `profit_factor` ≤ 4.0 |
| Max drawdown | `max_drawdown` ≤ $15,000 (15% of $100k) |
| Worst single day | `worst_day` ≥ −$5,000 (5% of $100k) |

Mark each gate pass/fail per profile. A profile is **eligible for live** only if all six pass. Apply the **20–40% live-discount view** alongside the raw numbers: show what net_pnl and drawdown would look like at a 30% haircut, since paper fills are frictionless relative to live (see docs/paper-trading.md's known-limitations section — stop-out P&L is the most optimistic element of the paper model).

## 5. Per-trade log excerpt

Include the last ~15 trades across all profiles (from `get_range_summary`'s underlying data or a direct `ic_trades` query) — symbol, profile, entry/exit time, strikes, credit, exit reason, exit price, net P&L — for auditability.

## 6. Write the report

Plain-English synthesis: which profile(s) are trending toward graduation and why, which are clearly lagging, and whether the sample is even large enough yet to say anything (be honest if it isn't — conservative in particular may accrue few trades on low-IV weeks, which is a valid, reportable outcome per CLAUDE.md's low-IV credit-floor discussion, not a bug).

Save to `logs/paper-week-<N>-<start>_<end>.md` (increment `<N>` from the highest existing `logs/paper-week-*.md` file, or use the date range alone if this is the first report). One combined report covering all four profiles — do not write separate per-profile files.
