# Bookbound

**An autonomous options agent that gates the portfolio, not the trade.**

Built for the [Alpaca AI Trading Agents Hackathon](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon).
Paper account `PA3S4EFAEQLX`.

## The idea

Most AI trading agents gate one trade at a time: *is this trade within its risk limit?*
That check will happily approve five separate defined-risk bull spreads and leave the
book with a delta of +240 — a directional bet nobody authorised. Every trade passed. The
portfolio is the problem.

Bookbound evaluates a candidate by **what the book looks like after it**. Aggregate
delta, vega, gamma and theta each carry a budget, and a trade is admissible only if the
post-trade book stays inside all of them.

```
book_after = book_now ⊕ candidate
admit ⟺ |Δ|,|ν|,|Γ| within budget ∧ Θ ≥ floor ∧ max_loss ≤ cap
        ∧ Σ risk ≤ portfolio cap ∧ execution drag ≤ cap ∧ ¬drawdown_halt
```

Gates may **veto or shrink**. They can never enlarge, invent, or approve something the
strategy layer did not propose.

## Architecture

Full design in [`ARCHITECTURE.md`](ARCHITECTURE.md); the research it rests on is in
[`knowledge-base/`](knowledge-base/).

```
clock → market snapshot → EXITS FIRST  [deterministic]
      → candidate construction         [deterministic]
      → proposer + adversary           [two models must agree]
      → portfolio risk engine          [deterministic]
      → CLI mleg execution → reconciliation → audit
```

Exits run before entries every cycle — profit target +50%, stop loss −60%, time
stop at 3 DTE — because closing a position always reduces exposure and should
never wait behind an entry decision. See [`RUNBOOK.md`](RUNBOOK.md) for operating it.

The models never invent a contract, choose a size, or touch an order. They rank a
shortlist that deterministic code built, priced, ranked, and pre-filtered against the
risk budget. **Disagreement between the two models resolves to NO TRADE**, and so does
any model failure.

They are given real evidence to reason over — 20-day price action, realised volatility,
range position, and current headlines — because a model asked for a directional thesis
without directional data can only produce a mechanical one, and the adversary correctly
rejects those.

## Quick start

Zero third-party dependencies — Python 3.10+ standard library and the Alpaca CLI.

```bash
brew install alpacahq/tap/cli
cp .env.example .env      # fill in your keys
export PYTHONPATH=src

python -m bookbound status   # account, book greeks, position health
python -m bookbound scan     # build + gate candidates. No model, no orders.
python -m bookbound cycle    # one full cycle, DRY RUN
python -m bookbound run      # the live loop, DRY RUN
python -m bookbound audit    # decision log
python -m bookbound flatten --live   # close every position
python -m bookbound panic    # cancel everything
python -m unittest discover -s tests

# unattended
bash scripts/run-live.sh --live     # detached; tail -f state/runner.log
bash scripts/stop-live.sh           # graceful stop
```

## What makes it different

**Both spread directions.** Debit verticals (bull call / bear put) and credit verticals (bear call /
bull put), selected by risk-adjusted expected value. Alpaca's `mleg` `limit_price` is signed —
positive debit, negative credit — with **no server-side validation**, so a sign error is silent and
fills against you. Credits are stored negative throughout and a dedicated test class guards the
boundary.

**Portfolio-level Greek budgeting.** The differentiator. `tests/test_risk.py` contains the
proof: twenty individually-compliant spreads, refused by a *portfolio* gate before the
book runs away.

**Honest data handling.** Alpaca's free feed is `indicative` — derived quotes, trades
delayed 15 minutes. Verified consequences, encoded as defences:

| Reality | Defence |
|---|---|
| The chain returns **expired contracts by default** | `expiration_date_gte` always sent |
| Expired contracts return `greeks: null` | null greeks are a **hard reject**, never a zero |
| Quotes run wide (measured 5.63/6.83, ~19%) | quote-width filter; limits priced off real bid/ask, never mid |
| Crossing the spread costs expected value | **execution drag** is computed and gated |
| `limit_price` sign is unvalidated server-side | credits stored negative end-to-end; `TestCreditSignConvention` guards it |
| Held positions we cannot price | risk engine **fails closed** |

**Execution quality.** Paper injects random partial fills ~10% of the time. On a two-leg
spread that leaves a *naked* option, not a smaller spread — the defined-risk structure the
risk engine approved no longer exists. Every cycle reconciles actual legs against intended
structure and flags naked shorts.

**Refusals are the product.** Every decision *and every refusal* is appended to
`state/decisions.jsonl` with the bound that failed and the numbers on both sides of it.

## Safety

- MCP server runs with **no `trading` toolset** — the model has no order tool to call.
- Every order carries a caller-generated `client_order_id`, so a retry cannot double a position.
- Any model error, malformed response, or off-shortlist answer resolves to NO TRADE.
- Daily drawdown halt at −3%.
- `python -m bookbound panic` cancels everything.

## Operating it

The loop runs unattended on a 5-minute cycle, sleeping until the next open when
the market is closed, and **flattens the book 30 minutes before the submission
deadline**. It halts after 5 consecutive errors, stops cleanly on SIGINT within a
second, and records `runner_stop` on the way out.

Full procedures, failure modes and the pre-submission checklist:
[`RUNBOOK.md`](RUNBOOK.md).

## Status

Deterministic layers are unit-tested and run against live data. The pipeline has been
exercised end-to-end against the live paper account, including the two-model decision.

Not investment advice. Paper trading only; results are hypothetical.
