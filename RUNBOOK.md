# Runbook

Operating Bookbound during the competition. Account `PA3S4EFAEQLX`.

## Start / stop

```bash
bash scripts/run-live.sh            # DRY RUN - decides and logs, places nothing
bash scripts/run-live.sh --live     # places orders
tail -f state/runner.log
bash scripts/stop-live.sh           # graceful: finishes the cycle, logs runner_stop
```

`INTERVAL=180 bash scripts/run-live.sh --live` overrides the 300s cycle.

**Always run a dry-run session first** and read `state/decisions.jsonl`. A dry run
exercises the entire pipeline including both models; only the order submission is
skipped.

## What the loop does each cycle

1. Check `/v2/clock`. If closed, sleep until the next open.
2. Snapshot the chain for the universe; drop expired and unpriceable contracts.
3. **Exits first** — profit target +50%, stop loss −60%, time stop at 3 DTE.
   Closing always reduces exposure, so exits are never blocked by entry gates.
4. Build and price candidate verticals deterministically.
5. Proposer picks one or NO TRADE; adversary must approve. Disagreement, model
   failure, or an off-shortlist answer all resolve to NO TRADE.
6. Portfolio risk engine gates on the post-trade book.
7. Submit via CLI `mleg` with a `client_order_id`.
8. Reconcile actual legs against intended structure.

## Automatic safety behaviour

| Trigger | Behaviour |
|---|---|
| Day P&L ≤ −3% | every entry refused for the rest of the session |
| 5 consecutive errors | runner halts and logs `runner_halt` |
| Any model failure | NO TRADE |
| Position we cannot price | risk engine fails closed |
| 14:30 UTC on 4 Sep | **pre-deadline flatten** — closes everything 30 min before submissions close |

## Manual controls

```bash
python -m bookbound status     # account, book greeks, position health
python -m bookbound scan       # candidates + gate decisions, no model, no orders
python -m bookbound cycle      # one cycle, dry run
python -m bookbound audit      # decision log summary
python -m bookbound flatten --live   # close every option position
python -m bookbound panic      # cancel all open orders
```

## If something looks wrong

**Naked short reported by reconciliation.** A partial fill broke a spread. Check
`python -m bookbound status`; close the orphan with `flatten --live` if it is not
being repaired.

**Nothing ever trades.** Expected when quotes are wide — debit verticals bought
across a wide indicative spread have negative expected value, and the agent is
built to refuse rather than pay for the privilege. Check the refusal reasons:

```bash
python -m bookbound audit
grep edge_erosion state/decisions.jsonl | tail -5
```

If refusals are dominated by `edge_erosion`, the honest options are to widen
`max_edge_erosion_pct` in `config.py` or to accept fewer trades. **Widening it
buys trades by paying more spread — that is a real cost, not a free knob.**
Spreads tighten materially once the session is underway; check during market
hours before changing anything.

**Runner died.** `state/runner.log` has the traceback and `state/decisions.jsonl`
has the last decision. Positions are untouched by a runner crash — restart it.

## Pre-submission checklist

- [ ] `python -m bookbound flatten --live` and `panic` if you want a flat book
- [ ] `python -m bookbound audit` — decision counts for the write-up
- [ ] Account ID `PA3S4EFAEQLX` on the submission form
- [ ] **Rotate API keys** before the repo goes public
- [ ] Confirm `.env` is not committed: `git ls-files | grep -c '^\.env$'` returns 0
