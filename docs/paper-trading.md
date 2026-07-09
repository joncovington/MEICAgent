# Paper-Trading Multi-Profile Evaluation

Capital-free evaluation of the four risk profiles (conservative / moderate / aggressive /
very-aggressive) on identical market conditions, before graduating one to live trading via
`/set-risk-profile`. This document is the operating reference; `.claude/commands/paper-loop.md`
and `.claude/commands/paper-report.md` are the operational skills.

## Why a custom engine, not the tastytrade sandbox

tastytrade has no true paper-trading mode; its dev sandbox mis-selects strikes for this
strategy's delta-targeted scan and intermittently 502s on market-overview calls (see project
memory `project_sandbox_get_strategies_issue`). `tt.py execute_trade`'s dry-run path only
validates an order broker-side (`place_order(dry_run=True)`) — no fill, no price, no P&L,
nothing persisted. So a synthetic-fill engine (`src/paper.py`) was built instead, reusing the
exact entry/stop math from `execute-entry.md`/`stop-management.md` against **real, live market
quotes**, and only stubbing the two broker-mutating calls (submit, close).

## Design

- **Parallel shadow.** Each iteration fetches one market snapshot per symbol (chain, quotes,
  VIX, GEX) and evaluates **all four profiles independently against it** — they're virtual
  sub-accounts with no shared capital constraint, so all four can hold positions on the same
  day. This removes market-regime confounding between profiles and needs far fewer calendar
  weeks than testing profiles sequentially.
- **Deterministic, not agent-judged.** `src/paper.py`'s gate evaluator and fill/exit engine
  encode a fixed policy: wing width = widest candidate ≤ `max_wing_width` that clears the
  profile's fee-aware credit floor; entry price = `ic_natural_bid`; no discretionary skips.
  This means paper results measure the **profile parameters**, reproducibly — not the live
  agent's session-by-session judgment, which sits on top of a graduated profile afterward.
- **Isolated storage.** All paper (and replay) trades live in `data/paper_trades.db`, written
  via `python src/db.py --db data/paper_trades.db <command>` — the same schema and commands as
  the live DB, with a `risk_profile` and `execution_mode` (`paper` | `replay`) column added.
  The live loop and `data/meic_trades.db` are never touched by this system.
- **$100,000 virtual bankroll per profile.** Anchors each profile's equity/drawdown curve
  (and the dashboard's Performance view) on a common baseline so the four are visually and
  numerically comparable.

## Fee model

Paper P&L is net of the **exact tastytrade fee schedule** (`src/paper._tt_fees`), not a flat
per-contract average:

- **Open** (all 4 legs): $1.00 commission + $0.10 clearing + $0.02 ORF + the per-symbol
  exchange proprietary index fee (SPX $0.60, XSP $0.00 under 10 contracts/leg, NDX $0.25,
  RUT $0.18), per contract, plus $0.00329 FINRA TAF on the 2 sell legs (short put + short call).
- **Active close** (stop / profit-target / force-close): no commission, but clearing + ORF +
  exchange fee + TAF still apply on the legs being closed. A per-side stop closes 2 legs; a
  full-IC close closes 4.
- **Expired OTM:** no fees at all — no closing transaction occurs.

This means a stopped IC correctly pays open+close costs and an expired IC pays open-only,
which is what makes narrow-width/low-credit setups (e.g. XSP) show realistic — sometimes
negative — net P&L instead of an optimistic flat-average estimate.

## SPX historical replay

`src/paper_replay.py` feeds the same deterministic engine from historical SPX 0DTE chains via
[0DTESPX.com](https://www.0dtespx.com/)'s API (`api.0dtespx.com`), so statistical significance
is reached in days instead of weeks and the dashboard's timeframe charts are backfilled
immediately. **SPX only** — the API doesn't cover XSP/NDX/RUT; those stay forward-paper-only
until a paid provider (ORATS 1-minute, or Databento OPRA) is justified.

**Setup:** `python src/paper_replay.py set_token --token <bearer token>` stores the token in
the OS keyring (same `keyring` mechanism as tastytrade credentials, distinct key). Register at
0DTESPX.com first.

**Known data limitation:** 0DTESPX provides bid/ask and **unsigned delta only** — no
gamma/theta/vega/IV. Two consequences, both handled explicitly rather than silently:

1. **GEX regime gate cannot run in replay** (needs gamma + open interest). Every replay
   snapshot sets `gex.ok = false`, so the gate never fires — replay entries are never blocked
   by GEX, which forward-paper entries can be. This is a known asymmetry between the two modes.
2. **`min_iv_rank` has no native IV field.** Replay derives a **VIX-percentile proxy**
   (`src/paper_replay._iv_rank_proxy`) from `GET /market-data/historical/{date}?series=VIX`
   instead — the binding gate stays meaningful, but it's an approximation (VIX is SPX 30-day
   IV, not a strike-specific IV-rank percentile). Every replay trade is tagged
   `iv_rank_source = "vix_proxy"` so it's distinguishable from forward-paper's real,
   symbol-specific IV rank in any report or dashboard breakdown.

**Rate limits:** 0DTESPX uses a leaky-bucket limiter (10,000 credits, ~0.116 credits/sec
drain; market-data calls cost 0–150 credits each). A per-second full-day pull is infeasible,
so replay marks are taken at the same 120-second cadence as the live loop's in-position
polling (~195 marks/session), fetched via the time-range snapshot endpoint and cached locally
under `data/replay_cache/<date>.json` so re-running a day never re-hits the API.

**Licensing:** 0DTESPX's `/llms.txt` states the platform is "free for registered users" but
does not spell out terms for driving an external engine from its API. **Confirm this is
permitted under their terms before relying on replay at scale.**

## Physical-settlement exit handling (QQQ/IWM and other non-cash-settled symbols)

SPX/XSP are cash-settled European-style — a missed close just settles to cash. QQQ/IWM (any
symbol **not** in `cash_settled_symbols`) are physically-settled American-style: a short leg
left open into expiration risks assignment into shares, and a strike pinned at-the-money at
the bell has an ambiguous ITM/OTM outcome. So paper mirrors the live loop's settlement-aware
exit behavior rather than treating every symbol identically:

- **Earlier force-close.** Non-cash-settled positions force-close at
  `physical_settlement_force_close_time` (default 15:30 ET), ~15 min ahead of the general
  `force_close_time` (15:45), staying clear of the illiquid, pin-risk-heavy final minutes.
  (`force_close_active` in `src/paper.py`.)
- **Full event force-close cascade.** Paper now also honors the FOMC-blackout (13:30 ET) and
  triple-witching/quarterly-expiry (14:00 ET) force-closes, which the earlier build omitted —
  these apply to all symbols, cash-settled or not.
- **Modeled assignment/pin friction.** When a physically-settled position force-closes, paper
  adds `physical_settlement_exit_friction` (per spread) to the cost-to-close, plus a
  `pin_risk_penalty_pct_of_width` penalty when a short strike is within `pin_risk_threshold_pct`
  of spot. This makes QQQ/IWM paper P&L strictly worse than an equivalent cash-settled close,
  so paper doesn't overstate their safety. The friction figures are deliberately conservative
  and config-tunable — calibrate them against a tiny-live run (same philosophy as the 20–40%
  graduation discount). Cash-settled symbols pay no friction (clean settlement). Every such
  close is recorded with a specific `exit_reason` (`force_close_physical_settlement`,
  `force_close_fomc`, `force_close_expiry_event`, or `force_close_eod`) for auditability.

**Live vs. paper:** live trading pays *real* fills, so the friction figures don't apply there —
live instead flattens physical positions early (same `physical_settlement_force_close_time`) and
escalates a failed close to `CRITICAL` (see CLAUDE.md Step 2 / stop-management.md Step 7).

## Metrics & graduation gate

Virtual bankroll: **$100,000 per profile**. A profile is eligible for live only when **all six**
hold, net of fees, over the test window:

| Gate | Threshold | Rationale |
|---|---|---|
| Sample size | ≥ 30 filled ICs (20 = hard floor, flagged) | <30 can't separate edge from luck (~20% false-positive rate at n=20) |
| Expectancy | Avg net P&L/IC > 0, bootstrap lower-90% > 0 | Positive edge, not a small-sample fluke |
| Win rate | ≥ 65% | 0DTE ICs are high-probability structures; read together with avg-win/avg-loss |
| Profit factor | 1.3 ≤ PF ≤ 4.0 | Negative skew — PF > 4.0 is flagged as curve-fit, not a stronger pass |
| Max drawdown | ≤ $15,000 (15% of $100k) | Caps path risk, not just endpoint P&L |
| Worst single day | ≥ −$5,000 (5% of $100k) | Screens the tail a high win-rate can mask |

**Reported, not gating:** Sharpe, Sortino, Calmar, recovery factor (Sharpe > 3 flagged as
likely overfit), avg-win/avg-loss, max consecutive losses, realized-vs-unrealized split.

## Known limitations

- **No exit-side slippage modeled.** Entries fill conservatively at `ic_natural_bid`; exits
  (stop, profit-target, force-close) fill at the computed marketable crossing price with no
  additional slippage. This is the deliberate base-build scope decision — friction is instead
  absorbed by a **20–40% haircut applied to paper P&L when judging graduation**, and expect
  **live drawdown ~1.5–2× the paper figure**.
- **Stop-out P&L is the single most optimistic element of the paper model.** Real 0DTE stop
  fills can slip badly on fast moves (documented cases of a stop trigger blowing several
  dollars past its limit); the 120-second loop cadence also samples stops later than a true
  intrabar trigger would. The live-discount view exists specifically to cover this.
- **No partial-fill / queue-position modeling.** Synthetic fills are all-or-nothing at the
  computed price — the single biggest realism gap versus Tier-1 broker simulators (which model
  volume-weighted slippage and land within ~8–12% of live). Acceptable here because natural-bid
  entry and the graduation discount already bias pessimistic.
- **Deterministic ≠ the live agent.** The engine measures profile *parameters* with judgment
  held constant; the live agent's session-by-session discretion sits on top of a graduated
  profile, so paper P&L is a mechanical baseline, not a live forecast.
- **Replay's GEX and VIX1D-ratio gates never fire** (data unavailable) — replay entry
  frequency for those specific triggers will run slightly higher than forward-paper's. This is
  a known, documented asymmetry, not a bug.
- **Physical-settlement risk is modeled only at force-close, not intraday.** The friction/pin
  model (above) captures the assignment/pin cost of *closing* a QQQ/IWM position at the bell,
  but does **not** simulate American-style *early* assignment during the session (e.g. a short
  call going deep ITM near an ex-dividend date and being assigned at 11am). The live loop's
  chosen scope is "earlier force-close + guardrails," not proactive early-assignment
  management, so this tail is documented rather than modeled — treat QQQ/IWM paper results as
  still slightly optimistic on assignment risk, on top of the general 20–40% discount.

## Multi-week cadence

Minimum 4 weeks of forward paper trading (~20 trading days), extended until the conservative
profile reaches ≥30 filled ICs or a 6–8 week cap — shortened materially by SPX replay, which
can front-load samples for SPX specifically. No mid-test parameter edits — changing thresholds
invalidates the sample. At window end, any profile clearing all six gates is eligible; switch
the live account via `/set-risk-profile`, keep `quantity=1` initially, and consider a small
tiny-live calibration run targeting **stop fills specifically**, since that's the most
optimistic part of the paper model.
