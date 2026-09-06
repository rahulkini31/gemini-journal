# Bookbound shortlist hardening task list

This checklist converts the September 2 shortlist audit into executable behavior.
Every item is developed red-green-refactor: add a Given/When/Then test that fails,
implement the behavior, then run the focused and complete test suites.

## P0 — data and lifecycle correctness

- [ ] Fetch the newest adjusted daily bars and keep them chronological.
- [ ] Carry one explicit `as_of` timestamp and New York exchange date through DTE calculations.
- [ ] Preserve option quote timestamps, sizes, venues, and conditions; reject stale or asynchronous legs.
- [ ] Fail closed when spot, forecast volatility, or forward inputs required by a model are missing.
- [ ] Evaluate profit targets, stops, and time exits on a complete structure, never one leg.
- [ ] Reconcile after exits and halt or flatten an unbalanced/naked book before considering entries.
- [ ] Include working orders in reserved risk and prevent duplicate submissions.

## P1 — candidate quality and portfolio safety

- [ ] Generate several validated delta anchors and widths per underlying/expiry.
- [ ] Apply hard feasibility and portfolio gates before any candidate-count truncation.
- [ ] Remove PoP from the deterministic quality score until a physical distribution is calibrated.
- [ ] Pareto-prune comparable structures and select a diverse top K by underlying, expiry, direction, and anchor.
- [ ] Reconstruct remaining maximum loss from complete option payoffs rather than leg cost bases.
- [ ] Add gross and per-underlying dollar Greeks plus deterministic spot/volatility stress scenarios.
- [ ] Refresh and reprice the chosen legs immediately before submission, then rerun every gate.
- [ ] Use stable order identity and cancel/replace stale working orders instead of accumulating DAY orders.

## P2 — event risk, auditability, and validation

- [ ] Annotate scheduled macro events and ex-dividend dates; gate unsafe short-option assignment setups.
- [ ] Expose timestamps, raw quotes, sizes, breakeven, Greeks, model inputs, and uncertainty to the LLM.
- [ ] Capture a complete immutable cycle bundle: market data, context, book, working orders, settings, and `as_of`.
- [ ] Replay without network or wall-clock dependencies.
- [ ] Evaluate the actual entry/exit policy walk-forward, including fills, slippage, fees, CVaR, and calibration.
- [ ] Update architecture/runbook claims so documentation matches executable behavior.

## Release gate

- [ ] All behavior scenarios and legacy regression tests pass.
- [ ] A replay is byte-stable under a frozen `as_of`.
- [ ] No stale quote, unknown book, reconciliation breach, or duplicate working order can reach submission.
- [ ] Remaining model assumptions are labeled as scenarios rather than guaranteed expectancy.
