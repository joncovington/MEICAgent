Run one iteration of the MEICAgent parallel-shadow paper-trading loop.

As of the standalone runner, the paper loop is implemented in code (`src/paper_loop.py`), not agent orchestration — this keeps a single source of truth for the iteration logic. Prefer the unattended daemon (`/paper-start`) for a full session; use this skill for a **single manual iteration** (e.g. a one-off force-close pass, or a quick check).

## Run one iteration

```bash
python src/paper_loop.py --once
```

This runs, for every symbol in `config.json`'s `symbols`, one full pass: fetch the live underlying price + IV rank, the shared VIX / VIX1D (→ ratio, → VIX-banded short delta), and GEX; build wing-width candidates from `wing_widths_by_symbol`; then hand the snapshot to `paper.process_symbol`, which marks/exits every open paper IC across all four profiles (profit-target, per-side stops, and the settlement-aware force-close cascade — including the physically-settled early close + assignment/pin friction) and evaluates new entries per profile. All writes go to `data/paper_trades.db`; the live account and `data/meic_trades.db` are never touched.

Report the per-symbol, per-profile outcomes from the JSON it prints (fills, skip reasons, or exits).

## Unattended session

For a full session that self-paces on the market-hours cadence without per-iteration invocation, use `/paper-start` (which launches `src/paper_loop.py` as a hidden background daemon) — or start it directly:

```bash
python src/paper_loop.py --status   # check
Start-Process python -ArgumentList 'src/paper_loop.py' -WorkingDirectory $PWD -WindowStyle Hidden
python src/paper_loop.py --stop     # stop
```

Details of the metrics, gates, force-close cascade, and graduation criteria are in `docs/paper-trading.md`.
