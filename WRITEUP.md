# Bookbound — one-page write-up

**Alpaca AI Trading Agents Hackathon · paper account `PA3S4EFAEQLX`**

> **The book is the unit of risk, not the trade.**
> An autonomous options agent whose language model *cannot choose what it trades*.

---

## The AI logic

Most AI trading agents let a model pick the trade and wrap it in guardrails. We built
that, measured it, and it did not work: across 22 trials on real shortlists the model
returned NO_TRADE 22 times. Removing a single line from its prompt — *"Prefer NO_TRADE
when the shortlist offers no clear edge"* — took selection from 0/6 to 6/6, so the
refusal rate was measuring our prompt, not the trades. With a neutral prompt it then
agreed with the deterministic rank 1 in **five of six** trials, and 12 identical inputs
produced **8 distinct rationales**, two of which contradicted each other.

Two conclusions followed, and they shaped the whole system:

1. **Selection among pre-priced, pre-gated, pre-ranked structures is arithmetic.** A
   language model layered on top of a deterministic ranker adds variance, not signal.
2. **A model asked to critique will critique.** Ours was told it was "rewarded for
   catching bad trades" and to reject when "the reasoning is generic" — which grades
   rhetoric, not risk. It vetoed 6 of 6 valid candidates.

So the layer is inverted. **Deterministic code selects rank 1.** The model reviews, with
one narrow mandate: *name a specific hazard in the market context that code cannot read.*
It approves by default, and a veto must cite an enumerated hazard — `EARNINGS_OR_EVENT`,
`HEADLINE_RISK`, `REGIME_CONTRADICTION`, `DATA_SUSPECT` — with evidence, or it is
discarded and logged. This strictly **reduces** model authority: it can no longer decide
what is traded, only object, on the record, to a decision code already made.

It earns its place. In a live cycle it read a Waller headline, inferred an FOMC meeting
inside the 27-day holding window, and vetoed with the citation. In testing it caught a
fixture where two legs carried identical quotes but different deltas — impossible pricing
we had not noticed.

## The risk gates

Every gate is deterministic, unit-tested, and can only **veto or shrink** — never enlarge,
invent, or approve. A candidate is judged by what the **post-trade book** looks like:

```
admit ⟺ |Δ|,|ν|,|Γ| within budget ∧ Θ ≥ floor ∧ max_loss ≤ 1% equity
        ∧ Σ risk ≤ 5% equity ∧ positive expectancy ∧ execution drag ≤ cap
        ∧ concentration ∧ payoff shape ∧ ¬drawdown_halt ∧ book fully priced
```

Per-trade gating misses the failure that matters: five individually-compliant bull spreads
leave the book at +240 delta — a directional bet nobody authorised. `tests/test_risk.py`
proves it, admitting trades until a *portfolio* gate stops the run.

The system **fails closed** on unknowns: a held position it cannot price halts new entries;
null Greeks are a hard reject, never a zero; an exit without a verified quote-derived limit
is reported `UNPRICED` rather than sent unbounded.

## The Alpaca infrastructure

Zero third-party dependencies — Python standard library plus Alpaca's own tooling.

| Concern | Implementation |
|---|---|
| Execution | **Alpaca CLI**, `api POST /v2/orders`, `order_class: mleg` |
| Inspection | **Alpaca MCP server**, `ALPACA_TOOLSETS` **without `trading`** |
| Market data | Market Data REST, `indicative` feed, contract-master verified |
| Environment | Paper trading, `PA3S4EFAEQLX`, seeded $100,000 |

The MCP server is configured with **no trading toolset at all**. The model is not *trusted*
not to trade; it is *unable* to.

Building against the real platform produced findings we encoded as defences: the option
chain returns **expired contracts by default** (always filter `expiration_date_gte`, and
treat null Greeks as a hard reject); `limit_price` for MLEG is **signed** — positive debit,
negative credit — with **no server-side validation**, so a sign error is silent and fills
against you; vendor implied vols **violate put-call parity** by a uniform 1.61 vol points,
so the market's own valuation is taken from the quoted mid rather than modelled; and paper
injects **random partial fills ~10% of the time**, which on a two-leg spread leaves a naked
option, so every cycle reconciles actual legs against intended structure.

## Results

Trading live on the competition account: **5 defined-risk vertical spreads filled** across
SPY, QQQ and IWM over four expiries — every one balanced, no naked shorts, $2,687 at risk
against a $4,988 portfolio cap. One order filled **17¢ better than its limit**, because
limits are priced off the executable side rather than the mid.

**238 tests.** Every decision *and every refusal* is appended to `state/decisions.jsonl`
with the bound that failed and the numbers on both sides of it; each cycle also seals an
immutable, hash-addressed evidence bundle for replay.

## What we do not claim

Nothing here is backtested, and candidate selection is **not an alpha claim**. Three ranking
metrics were tried and discarded for measuring their own assumptions rather than the market —
reward/risk, delta-weighted EV, and a skew-contaminated variance premium. Ranking now uses
observed execution cost and payoff geometry only. Over this competition's handful of sessions
any P&L number is statistically noise; what we offer instead is a system whose every decision
is bounded, explainable, and reproducible from its own audit trail.

*Not investment advice. Paper trading only; results are hypothetical.*
