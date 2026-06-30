Produce the end-of-day analysis report for the MEICAgent MEIC trading session.

1. Gather today's trade data:
```bash
python src/db.py get_eod_summary
```

2. Write a detailed plain English analysis covering all of the following. Be honest, specific, and write for the account owner who will read this after market close — not a data dump, a genuine synthesis:

- **Day overview**: number of entries, net P&L, win/loss breakdown, IV rank range across the session
- **Entry quality**: for each IC, was the entry timing good in hindsight? Did the session quality and trend signal match the outcome?
- **Stop management**: were stops triggered? Did any tightening occur? Did it help or hurt P&L? Were stops too tight or too loose given how the day played out?
- **Post-stop actions**: for any stopped spread, what happened to the remaining position? Did the action taken serve well or poorly?
- **Partial positions**: if any, did holding them generate additional P&L or not? What drove the outcome?
- **What worked**: patterns or decisions that contributed positively to P&L
- **What didn't work**: mistakes, missed entries, or exits that cost P&L — and why
- **Recommendations**: 2–3 specific, actionable observations for improving future sessions

3. Save the analysis to the database and write the report file:
```bash
python src/db.py save_daily_summary --date="<YYYY-MM-DD>" --summary="<your full analysis>"
```

Then write the analysis to `logs/eod-<YYYY-MM-DD>-<symbol>.md` (e.g. `logs/eod-2026-06-29-XSP.md`). The file should contain only the analysis text — no raw log output or DB data dumps.
