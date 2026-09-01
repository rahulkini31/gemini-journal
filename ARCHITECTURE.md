# Bookbound — architecture

> **The book is the unit of risk, not the trade.**

An autonomous options agent for the Alpaca AI Trading Agents Hackathon. Every design decision below
traces to a verified platform fact in `knowledge-base/` — nothing here rests on an assumption we
have not tested against the live account.

*(Name is provisional. `07-competitive-landscape.md` found three teams independently using
"Aegis" — distinctiveness is worth something.)*

## The thesis

Nearly two-thirds of the 41 competing submissions built the same thing: an LLM proposes a trade,
deterministic gates veto it, everything is logged. That pattern is correct and we implement it — but
it is **table stakes**, and as a pitch it is already taken many times over.

The gap those systems share is that they gate **one trade at a time**. A per-trade gate will happily
approve five separate defined-risk bull spreads and leave the book with a delta of +240 and a
concentrated short-vol position nobody authorised. Each trade passed. The portfolio is now a
directional bet.

**Bookbound gates the portfolio.** A candidate is evaluated by what the book looks like *after* it,
not by what the trade looks like alone. Aggregate delta, vega, theta and gamma each have a budget;
a trade is admissible only if the post-trade book stays inside every one of them.

Three secondary commitments, each targeting a gap the competitive teardown found unclaimed:

1. **Honest data.** The free feed is indicative — derived quotes, wide spreads, and expired contracts
   returned by default. We model that explicitly rather than pretending we have OPRA.
2. **Execution quality.** MLEG is limit-only outside market hours and fills partially ~10% of the
   time by design. A spread that half-fills is no longer defined-risk. We reconcile and repair.
3. **Refusals are the product.** Every rejection is logged with the numbers that caused it. The
   audit trail is designed to be read by a judge, not just retained.

## Pipeline

```
 /v2/clock ── closed? ──> log, sleep
     │ open
     ▼
 MARKET SNAPSHOT  (batch; SQLite; agents never call the API directly)
   underlying bars/quotes (IEX)   option chain (expiration_date_gte, strike-bounded)
     │
     ▼
 CANDIDATE CONSTRUCTION            [deterministic Python — no LLM]
   build verticals from the chain, price from real bid/ask, drop anything
   with null greeks, zero width, or a spread wider than the edge
     │  shortlist the LLM cannot write to
     ▼
 PROPOSER  (Claude)  ──picks one, or NO TRADE──┐
 ADVERSARY (Featherless / GLM-5.2)  ───────────┤  must BOTH agree
   sees the same shortlist, argues the bear case │  disagreement ⇒ NO TRADE
     │                                           │
     ▼                                           │
 PORTFOLIO RISK ENGINE             [deterministic — the differentiator]
   book greeks now  +  candidate greeks  =  book greeks after
   reject unless ALL of: |Δ| |ν| |Γ| within budget, Θ floor, per-trade
   max loss, concentration, buying power, drawdown halt
     │ admit
     ▼
 EXECUTION   (Alpaca CLI, mleg, client_order_id, limit priced off real quotes)
     │
     ▼
 RECONCILE   intended legs vs actual legs → repair or flatten on mismatch
     │
     ▼
 AUDIT       append-only JSONL: every decision AND every refusal, with inputs
```

## Why each layer exists

### Market snapshot — one interface, batched
Borrowed from Alpaca's own reference architecture (`08-reference-architecture.md`): agents query a
single SQLite table and never touch the API directly. Reproducible, cacheable, and it keeps us inside
the **200 req/min** Basic-plan ceiling.

Two verified defences baked in here:
- **`expiration_date_gte` is mandatory.** The chain endpoint returns *expired* contracts by default;
  we measured it returning Aug 31 contracts on Sep 1 (`10-verification-log.md`).
- **`greeks == null` is a hard reject, never a zero.** Expired contracts return null Greeks. Any code
  that coerces those to 0.0 will size a position off a delta of zero and trade confidently wrong.

### Candidate construction — deterministic, before any model runs
The LLM never invents a contract, a strike, or a size. It selects from a shortlist that
deterministic code built and priced. This is the "bounded AI" pattern, but pushed earlier: the model
cannot express an untradeable idea because untradeable ideas are not in its input.

### Two models that must agree
The Proposer argues for a trade; the Adversary, running a *different* model family on Featherless
(`zai-org/GLM-5.2`), argues against. **Disagreement resolves to NO TRADE** — the safe default.

This is a real use of the partner technology rather than a decorative call: the second model's
independence is the point, and a different model family is more likely to be independently wrong than
a second call to the same one. It also makes the $300 Featherless credit at 1st place, and partner
prize eligibility, a by-product of the design rather than a bolt-on (`06-partner-tech.md`).

### Portfolio risk engine — the differentiator
Deterministic, no model in the loop, unit-tested. It computes:

```
book_after = book_now ⊕ candidate
admit  ⟺  |Δ(book_after)| ≤ Δ_budget
      ∧   |ν(book_after)| ≤ ν_budget
      ∧   |Γ(book_after)| ≤ Γ_budget
      ∧    Θ(book_after) ≥ Θ_floor
      ∧   max_loss(candidate) ≤ per_trade_cap
      ∧   Σ max_loss ≤ portfolio_cap
      ∧   concentration(underlying) ≤ cap
      ∧   ¬drawdown_halt
```

Gates may **veto or shrink**, never enlarge or invent. Every rejection returns the specific bound
that failed and the numbers on both sides of it.

### Execution — priced off real quotes, never mid
Verified facts driving this layer (`10-verification-log.md`):
- `alpaca api POST /v2/orders` accepts `order_class: mleg` — the CLI path works.
- Market MLEG orders are rejected **outside** market hours; limit is required there.
- `ratio_qty` must be relatively prime; the API enforces GCD = 1.
- The CLI writes error JSON to **stderr** — capture `2>&1` or error handling sees an empty string.
- Indicative spreads are wide: a near-ATM SPY call quoted 5.63 / 6.83, ~19%. **Never assume a mid
  fill.** Limits are priced conservatively and non-fills are an expected state, not an error.

Every order carries a caller-generated `client_order_id`, so a retry after a network error cannot
double the position.

### Reconciliation — partial fills break defined risk
Paper injects a random partial fill ~10% of the time. On a two-leg spread that leaves a *naked long
or short option*, not the bounded structure that was approved. Each cycle compares actual legs to
intended structure and repairs or flattens on mismatch. This is a genuine safety property and the
most concrete thing to show on video.

## Risk budget (initial)

Deliberately conservative. With ~3.5 judged sessions, P&L is statistically noise
(`07-competitive-landscape.md`); the goal is a bounded, explainable equity curve, not a large number.

| Bound | Value | Rationale |
|---|---|---|
| Max loss per trade | 1.0% of equity ($1,000) | survives many consecutive losses |
| Total open risk | 5% of equity | worst case is legible |
| Book |Δ| | ≤ 150 | ~$150 P&L per $1 SPY move |
| Book |ν| | ≤ 400 | caps the vol bet |
| Book |Γ| | ≤ 50 | caps convexity |
| Book Θ floor | ≥ −250/day | caps bleed |
| Concentration | ≤ 2 positions per underlying | |
| Daily drawdown halt | −3% | stop and log |
| Expiry window | 7–45 DTE | avoids assignment inside the judging window |

Expiries inside the judging window are avoided on purpose: paper syncs option non-trade activities
**the next day**, so assignment effects would land after judging or not at all
(`04-options-mechanics.md`).

## Stack

**Zero third-party dependencies.** Python 3.14 standard library, plus the Alpaca CLI (which the rules
require anyway) and REST over `urllib`. Featherless is OpenAI-compatible, so it needs no SDK either.

This is a deliberate choice: the whole system reproduces from a clone and two environment variables,
with no dependency resolution to go wrong on a judge's machine.

| Concern | Choice |
|---|---|
| Market data | Alpaca Market Data REST (`urllib`) |
| Execution | **Alpaca CLI** `api POST /v2/orders`, `order_class: mleg` |
| Inspection | **Alpaca MCP server**, `ALPACA_TOOLSETS` read-only — no `trading` toolset |
| Reasoning | Claude (proposer) + Featherless GLM-5.2 (adversary) |
| State | SQLite |
| Audit | append-only JSONL |

The MCP server carries **no trading tools at all**. The model is not trusted not to trade; it is made
*unable* to. That is a structural guarantee, not a promise (`03-agent-surfaces.md`).

## Layout

```
src/bookbound/
  config.py       env, universe, risk budget
  http.py         retrying JSON client over urllib
  market.py       clock, snapshots, chain (expiry filter + null-greek reject)
  book.py         positions → aggregate portfolio greeks
  structures.py   build + price vertical spreads from a chain
  risk.py         deterministic gates, portfolio-first
  llm.py          Featherless / OpenAI-compatible client
  agents.py       proposer + adversary, must agree
  execute.py      CLI mleg submission, client_order_id
  reconcile.py    intended vs actual legs
  audit.py        append-only decision log
  cycle.py        one full trading cycle
tests/            unit tests for the deterministic layers
scripts/          verification utilities
```

## What is deliberately NOT here

- **No backtest-driven strategy discovery.** Only two or three teams attempted it and doing it badly
  in three days is worse than not doing it.
- **No websockets.** msgpack-only, 30-symbol cap, and a 2-day-hold strategy cannot use the latency.
  REST polling on a timer is the right call (`05-data-constraints.md`).
- **No crypto.** Options are mandatory in every strategy and Alpaca has no crypto options.
- **No human approval gate.** The hackathon requires autonomy. The accountability the human gate
  provided is replaced by the two-model agreement plus the deterministic portfolio engine — not
  simply deleted.
