# Risk Profiles

## Overview

A **risk profile** is a named preset of entry-gate thresholds that you select based on market conditions or your confidence level. Instead of manually editing a dozen keys in `config.json` every time you want to trade more (or less) aggressively, you switch profiles with one slash command: `/set-risk-profile <name>`.

Each profile bundles gate-threshold changes with offsetting position-sizing and stop-management adjustments, following a core principle: **every relaxed gate is paired with a compensating constraint** (fewer concurrent positions or tighter stops), so you're reallocating risk, not just adding it.

## Four Tiers: conservative → moderate → aggressive → very-aggressive

### Conservative (default)

**Today's settings — the baseline.**

| Gate | Value | Rationale |
|---|---|---|
| `min_iv_rank` | 0.30 | Skip if IV rank below 30%; safest premium edge |
| `min_credit_pct_of_width` | 0.15 | Require 15% of spread width as credit; widest cushion |
| `max_call_delta_entry` | 0.20 | Sell calls at ≤20 delta (furthest OTM) |
| `min_call_otm_pct` | 0.35% | Calls must be 0.35%+ above spot |
| `min_put_otm_pct` | 0.30% | Puts must be 0.30%+ below spot |
| `late_entry_bias_start_time` | 12:00 | Don't enter before noon on borderline-IV days |
| `regime_vix_pause_threshold` | 25 | Pause ICs when VIX > 25 |
| `regime_atr_pause_threshold_pct` | 0.015 | Pause ICs when 5-day ATR > 1.5% of underlying |
| `max_concurrent_ics` | 4 | Allow up to 4 simultaneous ICs |
| `stop_trigger_ratio` | 0.95 | Stop at 95% of credit received (loose stop) |
| `daily_ic_trade_target` | 2 | Target 2 ICs/day |

**Trade-off**: Fewest entries (~1–2/day on quiet days), highest per-trade safety margin. Best for: learning, uncertain markets, or after a losing streak.

---

### Moderate (Tier 1)

**Slightly lower bars on IV/credit floors; enter earlier in the day.**

| Gate | Change | Rationale |
|---|---|---|
| `min_iv_rank` | 0.30 → **0.22** | Accept lower IV — only lose ~5–10% edge vs conservative, but unlock 30–40% more entry candidates on flat-vol days |
| `min_credit_pct_of_width` | 0.15 → **0.12** | Accept thinner credit — 20% haircut, but gates that fell just short of conservative now qualify |
| `late_entry_bias_start_time` | 12:00 → **11:00** | Start entering at 11 AM instead of noon — capture an extra hour of morning premium (theta is still accelerating) |
| `stop_trigger_ratio` | 0.95 → **0.93** | Tighten stop slightly — 0.93 = stop at 93% of credit (2% tighter) to offset lower entry bars |
| `daily_ic_trade_target` | 2 → **3** | Target 3 ICs/day (expect 20–50% more entries vs conservative) |
| Other gates | (unchanged) | VIX/ATR/delta/OTM thresholds stay the same |

**Trade-off**: ~1–2 additional entries/week on normal weeks, slightly thinner per-trade credit margin but matched by tighter stop management. **Start here if conservative is leaving money on the table.**

---

### Aggressive (Tier 2)

**Tier 1 + delta and OTM distance relaxed. Offset with fewer concurrent positions and tighter stop.**

| Gate | Change | Rationale |
|---|---|---|
| `min_iv_rank` | 0.30 → **0.20** | Accept even lower IV — 33% lower than conservative |
| `min_credit_pct_of_width` | 0.15 → **0.10** | Accept tight credit — 33% haircut from conservative, matched by tighter stop and fewer concurrent ICs |
| `max_call_delta_entry` | 0.20 → **0.22** | Accept calls 10% closer to ATM — higher gamma, but tighter OTM buffers gone so stops must absorb more |
| `min_call_otm_pct` | 0.35% → **0.30%** | Calls only 0.30% OTM instead of 0.35% — much tighter |
| `min_put_otm_pct` | 0.30% → **0.25%** | Puts only 0.25% OTM instead of 0.30% — much tighter |
| `late_entry_bias_start_time` | 12:00 → **11:00** | Same as moderate |
| `max_concurrent_ics` | 4 → **3** | **Offset #1**: cap at 3 simultaneous ICs instead of 4 (25% fewer positions) to limit total gamma exposure when each strike is closer to money |
| `stop_trigger_ratio` | 0.95 → **0.90** | **Offset #2**: stop at 90% of credit (5% tighter than conservative) — increased stop cost paired with reduced position count keeps total risk budget similar |
| `daily_ic_trade_target` | 2 → **4** | Target 4 ICs/day (expect 50–100% more entries vs conservative) |
| Regime gates | (unchanged) | VIX/ATR pause thresholds unchanged — still skip in volatile regimes |

**Trade-off**: ~2–3 additional entries/week on normal weeks; each one trades tighter strikes (higher per-trade risk), but fewer concurrent positions and tighter stops cap total exposure. Requires disciplined stop management and comfort with smaller win/loss swings. **Use when you want 2–3× more activity and accept tighter daily P&L ranges.**

---

### Very-Aggressive (Tier 3)

**Tier 2 + regime-gate thresholds relaxed. Trade through higher-VIX/choppier conditions. Tightest stops, smallest position cap.**

| Gate | Change | Rationale |
|---|---|---|
| Tier 1 + 2 gates | (all carry forward) | All relaxations from moderate + aggressive stay in place |
| `min_iv_rank` | 0.20 → **0.15** | Accept IV rank down to 15% — skipping the market's quietest conditions |
| `regime_vix_pause_threshold` | 25 → **30** | **Trade when VIX 25–30** (normally paused for IC) — dealer short-gamma conditions, normally avoid; now accepted with extreme position discipline |
| `regime_atr_pause_threshold_pct` | 0.015 → **0.020** | **Trade when 5-day ATR 1.5–2.0% of underlying** (normally paused) — trending/volatile markets where mean-reversion edge weakens, now accepted |
| `max_call_delta_entry` | 0.22 → **0.24** | Accept calls 20% closer to ATM than conservative |
| `min_call_otm_pct` | 0.30% → **0.25%** | Calls only 0.25% OTM |
| `min_put_otm_pct` | 0.25% → **0.20%** | Puts only 0.20% OTM |
| `late_entry_bias_start_time` | 11:00 → **10:00** | Start entering at 10 AM (market open) — no bias gate; accept directional exposure in the first hour |
| `max_concurrent_ics` | 3 → **2** | **Offset #1**: cap at 2 simultaneous ICs (50% fewer than conservative) — regime relaxation requires extreme position discipline |
| `stop_trigger_ratio` | 0.90 → **0.85** | **Offset #2**: stop at 85% of credit (10% tighter than conservative) — each stop is deeper, accepted risk is extreme |
| `daily_ic_trade_target` | 4 → **5** | Target 5 ICs/day (expect 150%+ more entries vs conservative on high-activity days) |

**Trade-off**: Maximum activity (~3–5 additional ICs/week vs conservative on normal weeks); each trade is **riskiest** (closest-to-money strikes, highest gamma, widest daily swings); offsetting with smallest position count and tightest stops. **Only for experienced traders who can emotionally handle stops 10% deeper per position, or who deliberately want to test unfamiliar regimes. Not recommended for first month of operation.**

---

## Relaxation Principle: Gates First, Offsets Second

The four tiers follow a deliberate sequence. **Do not skip ahead** — the intermediate tiers exist because relaxing gates in isolation creates risk, while the offsets (position caps, tighter stops) only work if they compound properly.

### Recommended progression

1. **Start at conservative** if you're new, uncertain, or just traded out of a drawdown.
2. **Move to moderate** after 2–4 weeks when you've observed: entry rejection reasons in your logs, which 2–3 gates block most entries, and whether your per-trade win rate stays 60%+ on your entries.
3. **Escalate to aggressive** only if: moderate's ~3 ICs/day felt sustainable, most days closed green (ICs expiring/settling for a profit rather than getting stopped), and your largest losses didn't exceed 2% of account equity. Aggressive **requires active stop management** — do not set it and forget it.
4. **Reach very-aggressive only deliberately**, after running aggressive for 2+ weeks. This tier is for live-testing high-VIX/ATR tactics, not a normal mode. Plan a short experiment (1 week) with explicit drawdown limits before committing.

### What each relaxation costs

| Relaxation | Tier | Cost | Offset |
|---|---|---|---|
| IV-rank floor (0.30→0.22) | Moderate | ~5–10% edge loss per trade | Accept thinner credit margin; monitor win rate |
| Credit floor (0.15→0.12) | Moderate | ~20% net premium loss per trade if fee-heavy | Pair with tighter stops |
| Late-bias start (12:00→11:00) | Moderate | +1 hour directional exposure | Stop management picks up the slack |
| Delta relaxation (0.20→0.22) | Aggressive | +2–3% gamma per position | Cap concurrent positions to 3 |
| OTM relaxation (0.35%→0.30%) | Aggressive | Strikes 17% closer to money = 2–3× higher pin/assignment risk at expiration | Pair with 5% tighter stops (0.95→0.90) |
| Regime gate (VIX 25→30) | Very-Aggressive | Trade 30–50% of days you currently skip due to elevated vol | Reduce position cap to 2 and stop at 85% (10% tighter) |
| ATR gate (30→40 pts) | Very-Aggressive | Trade trending markets with half the normal mean-reversion edge | Same tight offsets as VIX gate |

---

## How Profiles Actually Work: Config Mutation

When you run `/set-risk-profile moderate`:

1. The command reads `config.risk.json` and extracts the `moderate` profile object.
2. It backs up your current `config.json` → `config.json.bak` (so you can revert if needed).
3. It overwrites the matching keys in `config.json` with the moderate profile's values.
4. It updates `config.risk.json`'s `active_profile` field to track which one is active (for logging/auditing).
5. It prints a before/after table showing every key that changed, plus the profile's rationale.
6. The next loop iteration reads the updated `config.json` and picks up the new values — **no restart needed**, but the change is not retroactive to in-flight positions.

**Important**: Profile switches happen *between* loop iterations. If you're in the middle of a scan when you switch, the new config takes effect on the next 5-minute tick, not immediately. This is intentional — it prevents gates from flipping mid-trade.

---

## Switching Back

`/set-risk-profile conservative` restores today's baseline. Since the `conservative` profile contains every key at its exact current value, switching back is a complete reset.

If you've manually edited `config.json` outside of profile switches (e.g., tuned `stop_trigger_ratio` from 0.95 to 0.92), switching profiles **overwrites only the keys in that profile** — unspecified keys stay as you edited them. So manual edits can survive profile switches if you're careful.

---

## Which Profile to Use: Decision Tree

- **Conservative**: First week, learning phase, or post-loss recovery
- **Moderate**: Steady state; conservative rejected 40%+ of candidate trades last week
- **Aggressive**: 2+ weeks on moderate, win rate 60%+, largest loss < 2% account equity
- **Very-Aggressive**: Deliberate 1-week experiment testing high-VIX/ATR tactics; not a permanent mode

**Default recommendation**: Run **moderate** for most traders most of the time. It captures ~50% more entries than conservative without the execution complexity of aggressive/very-aggressive.

