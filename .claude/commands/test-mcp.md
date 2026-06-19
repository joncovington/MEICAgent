Run a full end-to-end test of the MEICAgent loop against the `tastytrade-mock` MCP server and produce a plain English report of findings.

**All MCP tool calls in this skill must use the `tastytrade-mock` server.**

---

## Phase 1 — Connection

Call `get_connection_status`.

Check:
- Did the call succeed?
- Is `mock_mode: true`? If not, stop immediately — you are connected to the wrong server.

---

## Phase 2 — Account & Market State

Call in parallel: `get_account_info`, `get_positions`, `get_working_orders`, `get_market_overview` (symbol from config.json).

Then call `get_option_chain` (symbol from config.json, filter dte == 0).

Record:
- Derivative buying power and net liquidating value
- Number of open position legs (expect 12 for 3 ICs × 4 legs)
- Number of working orders (expect 3 stop orders)
- IV rank, IV percentile, underlying price
- ATM strike and put/call skew derived from the chain

---

## Phase 3 — Strategy Candidates

Call `get_strategies` in parallel for each width in `config.wing_width_candidates` using `strategy: "iron_condor"`, `target_dte: 0`, and `short_delta` from config.

For each candidate record: put strike, call strike, net credit, estimated POP.

Apply the wing width selection logic from CLAUDE.md Step 3e and identify which width you would choose given the mock scenario, and why.

---

## Phase 4 — Stop Management Simulation

Map each working order to its IC using the position and order data from Phase 2. For each of the 3 ICs:
- Identify which stop order belongs to it (put spread stop vs. call spread stop)
- Confirm the stop is still working (not filled)
- Evaluate whether stop tightening would be warranted given the fixture's timestamp and market state, applying the judgment criteria from CLAUDE.md Step 4d
- State clearly: tighten or hold, and why

---

## Phase 5 — Entry Decision Simulation

Using the account state and market data from Phase 2–3, work through the CLAUDE.md Step 5 entry decision:
- Are any hard stops triggered? (max entries, time gate, buying power)
- What does AI judgment say about a 4th IC entry given: session quality, IV rank, trend signal, existing open positions, best wing width candidate?
- State the decision and full reasoning as you would write it in `ai_entry_reasoning`

---

## Phase 6 — Order Execution Pre-flight

If Phase 5 concluded an entry would be appropriate, call `execute_trade` with `dry_run=true` using the best strategy candidate from Phase 3. Record the full response — particularly `ok`, `buying_power`, and any `problems`.

If Phase 5 concluded no entry, call `execute_trade` with `dry_run=true` anyway using the best candidate — this validates the pre-flight path independently of the entry decision.

Do **not** call `execute_trade` with `dry_run=false` at any point in this skill.

---

## Phase 7 — Report

Write a plain English test report covering all of the following. Be specific — use actual values from the fixture, not generalities.

**1. Connection**
Did the mock server connect cleanly? Was `mock_mode` confirmed?

**2. State reading accuracy**
Did the fixture data load as expected? Note actual counts of legs and working orders vs. what the fixture should contain. Flag any discrepancy.

**3. Option chain quality**
Was the chain populated with 0DTE strikes? Were greeks (delta, theta, IV) present? Was ATM derivable? Describe the put/call skew observed.

**4. Strategy candidates**
List each wing width evaluated, its credit and POP. Identify the selected width and explain the reasoning.

**5. Stop management**
For each of the 3 ICs: confirm stop mapping, tightening decision, and reasoning. Note if any stop appeared ambiguous or unmappable.

**6. Entry decision**
State the decision and reasoning. Note which hard stops (if any) blocked entry and which judgment factors were most influential.

**7. Pre-flight result**
State the dry-run outcome. If `ok=true`: confirm buying power and legs were accepted. If `ok=false`: quote the rejection reason verbatim.

**8. Overall verdict**
One paragraph: is the agent ready to run against a live/sandbox tastytrade session, or are there gaps to address first? List any tool calls that returned unexpected structure or missing fields — these are the things most likely to break in production.
