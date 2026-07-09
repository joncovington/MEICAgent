Produce the end-of-day report(s) for MEICAgent. Reproduces the **live** report, the **paper** report, or both.

## Scope

Read the argument to this command:
- **`both`** (default when no argument is given) — produce both the live and the paper EOD report.
- **`live`** — only the live agent-synthesized report (Section A).
- **`paper`** — only the deterministic paper report (Section B).

An optional `--date YYYY-MM-DD` selects the day; default is today (ET). Examples: `/eod-report`, `/eod-report paper`, `/eod-report live --date 2026-07-08`.

At the end, tell the user which file(s) were written.

---

## Section A — Live EOD report (when scope is `live` or `both`)

One combined report covering **every symbol traded today** — `get_eod_summary` returns all of today's trades across every configured symbol in one call, each with its own `symbol` field.

1. Gather today's live trade data (all symbols):
```bash
python src/db.py get_eod_summary
```

2. Write a detailed plain-English analysis for the account owner — a genuine synthesis, not a data dump. Where trading spanned multiple symbols, call out per-symbol differences explicitly ("XSP performed well while SPX had two stop-outs"):
- **Day overview**: entries, net P&L, win/loss, IV rank range — broken out by symbol if more than one traded
- **Entry quality**: was each IC's timing good in hindsight? Did session quality and trend signal match the outcome?
- **Exits**: which of the MEIC exits fired — per-side stops, non-cash-settled time force-close, or cash-settled expiration-settlement? (There is **no profit-target** exit.) Were stops too tight/loose? Any stop tightening — did it help or hurt?
- **Post-stop / partial positions**: for any stopped side, what happened to the remaining side, and did it serve well?
- **What worked / what didn't**: patterns and decisions that helped or cost P&L, and why
- **Recommendations**: 2–3 specific, actionable observations

   If `get_eod_summary` shows **0 entries** (e.g. a paper-only day with no live trading, or a flat session), write a short honest flat-session note instead of a full synthesis — do not invent activity.

3. Save and write the file:
```bash
python src/db.py save_daily_summary --date="<YYYY-MM-DD>" --summary="<your full analysis>"
```
Then write the analysis to `logs/eod-<YYYY-MM-DD>.md` — one file per day, analysis text only (no raw DB dumps).

---

## Section B — Paper EOD report (when scope is `paper` or `both`)

The paper report is **deterministic and code-generated** (no synthesis needed) — just run the generator, which reads `data/paper_trades.db` and writes the file:

```bash
python src/paper_loop.py --eod-report [--date <YYYY-MM-DD>]
```

It writes `logs/paper-eod-<date>.md` — a per-profile metrics table (trades, win rate, net P&L, expectancy, profit factor, max drawdown), an exits-by-reason breakdown, and per-symbol P&L across all four risk profiles. Report the path it prints; optionally show the user the file contents. (This is distinct from `/paper-report`, which is the agent-synthesized multi-day write-up.)

Note: the paper loop daemon already writes this file automatically at the 16:00 settlement pass; running it here just regenerates it on demand (or backfills a past `--date`).
