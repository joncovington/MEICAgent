# Strategy Overview

MEICAgent runs a **Multiple Entry Iron Condor (MEIC)** strategy on 0DTE options. Rather than placing a single IC at the open, it evaluates market conditions on each loop iteration and enters additional ICs throughout the day when conditions are favorable.

---

## Iron Condor structure

Each IC consists of four legs:
- **Short put** + **long put** (put spread) — below the market
- **Short call** + **long call** (call spread) — above the market

Short strikes are targeted near `delta_target` (default 0.15) on each side. Wing width is selected dynamically per entry from `wing_width_candidates`.

---

## Wing width selection

The agent evaluates all widths in `wing_width_candidates` on each entry and picks the one that best fits current conditions:

- **Earlier in the session** — favor wider wings (more credit, more room)
- **Later in the session** or multiple ICs already open — favor narrower wings (lower max loss as gamma accelerates)
- **High IV rank** — wider wings are more defensible
- **Skewed market** — adjust width by side based on put/call IV skew
- **Elevated short-strike gamma** (above 0.07) — prefer narrower wings

Any width where `width × 100 > available buying power` is eliminated before comparison.

---

## Entry sessions

| Window | Label | Notes |
|---|---|---|
| 09:30–10:00 | `open_volatile` | Elevated volatility; weigh IV rank and skew carefully |
| 10:00–11:30 | `prime` | Preferred entry window |
| 11:30–13:00 | `midday` | Generally good conditions |
| 13:00–14:30 | `afternoon` | Less time remaining; weigh credit vs. time risk |
| 14:30–15:30 | `late` | Very limited time; weigh credit, open exposure, and IV carefully |
| After 15:30 | — | No new entries |

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

When a stop fills, the agent immediately evaluates the remaining spread and chooses the action that best maximizes net P&L:

1. **Close the full remaining spread** — eliminates all tail risk
2. **Buy back only the short leg** — removes directional exposure, leaves the long leg open at zero cost
3. **Leave the DAY stop working** — best when closing would add unnecessary fees with little risk benefit

The agent re-evaluates partial positions on every subsequent iteration until market close.

---

## EOD handling

After 15:00 ET, the agent reviews each open spread for unacceptable gamma risk and force-closes any spread where:
- The underlying is within 0.5% of the short strike with < 30 min remaining
- Short-strike gamma is above 0.10
- The spread value is accelerating faster than stops can track

**Cash-settled symbols** (SPX, XSP, NDX, RUT) — remaining open positions can be left to expire; cash settlement delivers intrinsic value automatically with no assignment risk.

**Non-cash-settled symbols** — all remaining open legs are closed before 15:45 ET.

---

## Conflict resolution

When signals conflict or inputs are ambiguous, the agent never halts. It applies a capital-protective default, logs a detailed plain English account of the conflict, and continues to the next step. Defaults:

| Scenario | Default |
|---|---|
| Uncertain whether to enter | Skip entry |
| Conflicting stop tightening signals | Leave current stop in place |
| Uncertain whether to force-close | Close it |
| Uncertain post-stop action | Leave the DAY stop working |
| MCP returns unexpected data | Take no trading action; log raw response |

All conflicts are logged as `WARN` in `logs/agent.log` for post-session review.
