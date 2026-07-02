# Strategy Overview

MEICAgent runs a **Multiple Entry Iron Condor (MEIC)** strategy on 0DTE options. Rather than placing a single IC at the open, it evaluates market conditions on each loop iteration and enters additional ICs throughout the day when conditions are favorable.

---

## Iron Condor structure

Each IC consists of four legs:
- **Short put** + **long put** (put spread) — below the market
- **Short call** + **long call** (call spread) — above the market

Short strikes begin from `delta_target` (default 0.18) as a starting suggestion. The agent then applies OTM distance guardrails (see below) and may move strikes farther OTM if the guardrail requires it. Wing width is decided dynamically per entry, bounded by `max_wing_width` rather than picked from a fixed list.

---

## Wing width selection

The agent isn't restricted to a fixed enumerated list — it decides the wing width per entry, choosing any reasonable value up to `max_wing_width`, and picks the one that best fits current conditions:

- **Earlier in the session** — favor wider wings (more credit, more room)
- **Later in the session** or multiple ICs already open — favor narrower wings (lower max loss as gamma accelerates)
- **High IV rank** — wider wings are more defensible
- **Skewed market** — adjust width by side based on put/call IV skew
- **Elevated short-strike gamma** (above 0.07) — prefer narrower wings

Any width where `width × dollar_multiplier > available buying power` is eliminated before comparison. `dollar_multiplier` is returned by `get_strategies` and reflects the contract's point value (100 for equity options; 5 for /MES, 50 for /ES, 2 for /MNQ, 20 for /NQ).

---

## Entry sessions

| Window | Label | Notes |
|---|---|---|
| 09:30–10:00 | `open_volatile` | Elevated volatility; entries blocked until `entry_window_start` (default 10:00, configurable in `config.json`); weigh IV rank and skew carefully |
| 10:00–11:30 | `prime` | Preferred entry window |
| 11:30–13:00 | `midday` | Generally good conditions |
| 13:00–14:30 | `afternoon` | Less time remaining; weigh credit vs. time risk |
| 14:30–15:30 | `late` | Very limited time; weigh credit, open exposure, and IV carefully |
| After 15:30 | — | No new entries |

---

## Short strike placement and OTM guardrails

`delta_target` is a suggestion, not a hard requirement. After the delta-based strikes are returned, the agent computes the actual OTM distance for each short strike and checks it against context-aware minimums:

| Condition | Minimum call OTM | Minimum put OTM |
|---|---|---|
| Bearish IV skew | 4.5 pts | 4.0 pts |
| Bullish IV skew | 4.0 pts | 4.5 pts |
| `open_volatile` session | 4.0 pts | 4.0 pts |
| Default | 3.5 pts | 3.5 pts |

When multiple conditions apply, the largest applicable minimum wins per side. If a delta-targeted strike is closer than its floor, the agent finds the nearest chain strike at or beyond the minimum distance and uses that instead — accepting a lower delta in exchange for more OTM buffer. The adjustment is noted in `ai_entry_reasoning`.

---

## Entry price strategy

By default the agent submits at the **natural bid** — the price buyers are actively bidding for the IC combo — which is always within the CBOE Complex Order Book's acceptable price range. Submitting at mid is consistently rejected ("Complex_order_outside_acceptable_price_range") because the DXLink quote snapshot lags the exchange's real-time NBBO.

Two optional strategies can attempt to capture price improvement above the natural bid. Set `entry_price_strategy` in `config.json`:

| Value | Behavior |
|---|---|
| `"natural_bid"` | Always submit at natural bid. Default and safest. |
| `"ioc_step"` | Try natural bid + each increment in `ioc_step_increments` as IOC orders, then fall back to natural bid as a Day order. |
| `"day_improve"` | Submit a Day limit at natural bid + `day_improve_amount`, wait `day_improve_wait_seconds`, cancel-replace at natural bid if unfilled. |
| `"auto"` | Agent chooses per-iteration: `ioc_step` in volatile/late sessions, `day_improve` in calm midday, `natural_bid` when time is short or credit is thin. |

**Note**: Whether prices even slightly above natural bid pass the CBOE COB check is empirically unknown. An `ioc_step` attempt that would normally just sit unfilled may instead be rejected outright — the agent treats this the same as a miss and steps to the next increment. A few live sessions with `ioc_step` enabled will confirm whether improvement is achievable.

---

## Stop management

Each IC gets two **DAY stop-limit orders** — one for the put spread, one for the call spread. Both are sized as a fraction of the full IC net credit:

```
stop_trigger = net_credit × stop_trigger_ratio   (default 0.90)
stop_limit   = net_credit × stop_limit_ratio     (default 0.95)
```

At these levels, closing the stopped spread costs approximately the full IC credit, leaving the other spread to expire worthless at near break-even.

Stops are tightened (never loosened) by AI judgment as conditions change. Triggers for tightening include:

| Condition | Reference trigger |
|---|---|
| After 14:00 ET, entered before 11:00 (aged position) | 85% of credit |
| IV rank rose > 15 pts since entry | 80% of credit |
| Underlying moved > 0.3% against a short strike | 82% of credit |
| Short-strike gamma > 0.08 | 80% of credit |
| < 90 min to expiry AND spread value < 50% of credit | 75% of credit |

---

## Post-stop evaluation

When a stop fills, the agent evaluates the remaining spread **in the same iteration** — it does not defer to the next loop. It chooses the action that best maximizes net P&L:

1. **Close the full remaining spread** — eliminates all tail risk; best when the spread can be bought cheaply
2. **Buy back only the short leg** — removes directional exposure, leaves the long leg open at zero cost
3. **Hold and monitor** — spread still has meaningful value; closing would add unnecessary fees with little risk benefit. Status is set to `partial` so the agent re-evaluates on every subsequent iteration.

The agent re-evaluates all partial positions (and stopped positions with remaining open legs) on every subsequent iteration until market close.

---

## EOD handling

After 15:00 ET, the agent reviews each open spread for unacceptable gamma risk and force-closes any spread where:
- The underlying is within 0.5% of the short strike with < 30 min remaining
- Short-strike gamma is above 0.10
- The spread value is accelerating faster than stops can track

All remaining open legs are force-closed before 15:45 ET regardless of symbol — the agent never intentionally holds a position to expiration. What differs by symbol is how a *missed* force-close is handled: for cash-settled symbols (default: SPX, XSP, NDX, RUT — configurable via `cash_settled_symbols`), a miss still settles in cash automatically, so it's routine remediation. For non-cash-settled symbols (individual equities, and futures options like /MES, /ES, /NQ, /MNQ), a miss risks physical assignment and is escalated as a critical failure with an immediate marketable-limit retry.

---

## Futures options support

The agent supports futures options on CME equity index contracts:

| Symbol | Product | `dollar_multiplier` | Strike interval |
|---|---|---|---|
| /MES | Micro E-mini S&P 500 | $5/pt | 5 pts |
| /ES | E-mini S&P 500 | $50/pt | 5 pts |
| /MNQ | Micro E-mini NASDAQ-100 | $2/pt | 10 pts |
| /NQ | E-mini NASDAQ-100 | $20/pt | 10 pts |

When the symbol being processed in the per-symbol loop starts with `/`, the agent calls `get_quote` (not `get_market_overview`) to obtain the underlying price for that symbol. IV rank is unavailable for futures — the agent treats it as neutral (0.5) for that symbol and relies on premium quality and delta targeting. All four legs carry `instrument_type: "Future Option"` from the `get_strategies` response; stop orders use the same `instrument_type` so no hardcoding is needed. A `symbols` list can freely mix futures and index/equity symbols — this check applies per symbol, independently.

`dollar_multiplier` is returned by `get_strategies` and is used for:
- **Buying power check**: `wing_width × dollar_multiplier` = max loss per spread
- **Position sizing**: `net_credit_per_contract` is already in dollars (no manual scaling needed)
- **P&L accounting**: the DB stores `dollar_multiplier` per trade for correct unrealized/realized P&L math

---

## Conflict resolution

When signals conflict or inputs are ambiguous, the agent never halts. It applies a capital-protective default, logs a detailed plain English account of the conflict, and continues to the next step. Defaults:

| Scenario | Default |
|---|---|
| Uncertain whether to enter | Skip entry |
| Conflicting stop tightening signals | Leave current stop in place |
| Uncertain whether to force-close | Close it |
| Uncertain post-stop action | Leave the DAY stop working |
| MCP returns retryable error | Skip iteration (INFO log); retry automatically next wakeup |
| MCP returns non-retryable error | Take no trading action; log WARN with raw response |

All conflicts are logged as `WARN` in `logs/agent.log` for post-session review.
