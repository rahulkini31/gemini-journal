# 05 — Data Tiers, Feeds and Simulation Artefacts

> Sources: [About Market Data API](https://docs.alpaca.markets/us/docs/about-market-data-api) ·
> [Historical Option Data](https://docs.alpaca.markets/us/docs/historical-option-data) ·
> [Real-time Option Data](https://docs.alpaca.markets/us/docs/real-time-option-data) ·
> [Option snapshots ref](https://docs.alpaca.markets/us/reference/optionsnapshots) ·
> [Option chain ref](https://docs.alpaca.markets/us/reference/optionchain) ·
> [Paper Trading](https://docs.alpaca.markets/us/docs/paper-trading)

**This is the page that most constrains what strategies are actually buildable.** Read it before
designing signals.

## Plan tiers

| | **Basic (free)** | **Algo Trader Plus ($99/mo)** |
|---|---|---|
| Stock feed | **IEX only** (one exchange, ~2–3% of volume) | All US exchanges (SIP: CTA + UTP) |
| Recent-data restriction | **latest 15 minutes withheld** | none |
| Stock history | since 2016 | since 2016 |
| REST rate limit | **200 req/min** | 10,000 req/min |
| Stock WS subscriptions | **30 symbols** | unlimited |
| Options feed | **`indicative`** | `opra` |
| Options WS quote subs | 200 | 1,000 |
| Option history | since **Feb 2024** | since Feb 2024 |

Assume you are on **Basic**. The winning-team perk is *one month of Algo Trader Plus* — which tells
you the organisers expect essentially the whole field to be on the free tier.

## `indicative` vs `opra`

> **indicative** — *"a free derivative of the original OPRA feed: the quotes are not actual OPRA
> quotes, they're just indicative derivatives. The trades are also derivatives and they're delayed by
> 15 minutes."*
>
> **opra** — the consolidated BBO: highest bid and lowest offer across options markets. Subscription
> required.

The `feed` parameter defaults to `opra` **if you are subscribed, otherwise `indicative`.** You will
silently get `indicative`. Pass it explicitly so your logs record which feed produced a decision.

### ✅ The key enabling finding — VERIFIED LIVE

**Greeks and implied volatility are returned on the free indicative feed.**
Confirmed 2026-09-01 against the live account — see `10-verification-log.md` for the raw response.
*Live contracts only; expired contracts return nulls (see the trap below).*

Both `GET /v1beta1/options/snapshots?symbols=…` and the chain endpoint return, per contract:

```jsonc
{
  "latestQuote": { "bp": …, "ap": …, "bs": …, "as": … },
  "latestTrade": { … },
  "greeks": { "delta": …, "gamma": …, "theta": …, "vega": …, "rho": … },
  "impliedVolatility": …,          // Black-Scholes
  "dailyBar": { … }, "minuteBar": { … }, "prevDailyBar": { … }
}
```

Greeks and IV are **required fields** in the snapshot schema and Alpaca computes them itself.
Verified example: `SPY260902C00762000` → `delta 0.7541, gamma 0.0418, theta -1.2429, vega 0.1265,
rho 0.0157, IV 0.1877`. This
means **delta-selection, IV-rank, vega/theta budgeting and portfolio-Greek management are all viable
on a free account.** A large amount of apparently sophisticated strategy design is unlocked at zero
cost — and many teams will not realise it.

The honest caveat, which belongs in your write-up: those Greeks are derived from *indicative* quotes,
so they are approximations of approximations. Measured live, a near-ATM SPY call quoted **5.63 / 6.83
— a ~19% spread.** That is not a real market. Treat them as a **ranking signal, not a pricing
oracle**. Rank contracts by delta/IV; price the actual order from `latestQuote` bid/ask with a
conservative limit.

## Option chain endpoint

```
GET https://data.alpaca.markets/v1beta1/options/snapshots/{underlying_symbol}
```

| Param | Notes |
|---|---|
| `feed` | `opra` \| `indicative` |
| `type` | `call` \| `put` |
| `strike_price_gte` / `strike_price_lte` | **use these** — an unfiltered chain is enormous |
| `expiration_date` | exact, `YYYY-MM-DD` |
| `expiration_date_gte` / `_lte` | range |
| `root_symbol` | filter by root |
| `updated_since` | RFC-3339 or `YYYY-MM-DD` |
| `limit` | 1–1000, default 100 |
| `page_token` | pagination |

Snapshot-by-symbols caps at **100 contracts per request**.

### 🚨 Verified trap: the chain returns EXPIRED contracts by default

Confirmed live on 2026-09-01 (`10-verification-log.md`). An unfiltered SPY chain query returned
contracts that expired **the previous day**, with `greeks: null` and `impliedVolatility: null`.
The feed was fine — the contracts were dead.

**Always pass `expiration_date_gte`.** And treat `greeks == null` as a **hard reject**, never as a
zero — an agent that coerces null Greeks to 0 will size positions off a delta of zero and trade
confidently wrong.

At 200 req/min, filter server-side. Fetching full chains for a 20-symbol watchlist unfiltered will
throttle you inside a minute.

## Websockets

```
wss://stream.data.alpaca.markets/v1beta1/{indicative|opra}     # options
wss://stream.data.sandbox.alpaca.markets/v1beta1/{feed}        # sandbox
```

- **Options stream is msgpack-only.** JSON examples in the docs are for readability; the wire format
  is binary. Your client must decode msgpack.
- Channels: trades (`t`) and quotes (`q`) only. **No Greeks over the stream** — Greeks come from REST
  snapshots.
- **You cannot subscribe to `*` for option quotes** — "there are simply too many of them."
- Stocks: 30 symbols on Basic. Budget them.

For a 3.5-session run, **REST polling on a timer is very likely the right call.** Websockets add
msgpack decoding, reconnection logic and subscription accounting for a marginal latency gain that a
2-day-hold options strategy cannot use. Poll on a schedule; spend the saved time on the risk layer.

## Paper simulation artefacts

Paper does **not** simulate: market impact · order-queue position · price improvement · regulatory
fees · dividends · borrow fees · slippage · latency.

And it *does* inject:

- **"When orders are eligible to be filled, they will receive partial fills for a random size 10% of
  the time."** — deliberate, random partial fills.
- Non-marketable limit orders fill only when the NBBO reaches them.
- Orders can fill at sizes exceeding real available liquidity.
- No fill confirmation emails.

### Why this matters for a *multi-leg* agent
A random partial fill on a spread leaves you with a **broken, directionally exposed position** rather
than the defined-risk structure you designed. Your position monitor must reconcile *actual* legs
against *intended* structure every cycle, and repair or flatten on mismatch. Handling this correctly
is both a real safety property and a strong, concrete thing to demo on video.

## Practical budget at 200 req/min

| Activity | Cost |
|---|---|
| `/v2/clock` | 1 |
| Underlying snapshots, 20 symbols batched | 1 |
| Filtered chain per candidate (delta/expiry-bounded) | 1 per symbol |
| Position + order reconciliation | 2–3 |
| News | 1 |

A 20-symbol universe on a 5-minute cycle lands around 30 requests per cycle — comfortably inside
budget with headroom for retries. A 200-symbol universe does not fit. **Choose a small universe and
say why**; breadth is not a virtue here.

---

### Credit vs debit under a wide feed

Both directions cross the spread, so execution drag applies either way — selling at the bid costs
exactly as much as buying at the ask. Measured live, debit verticals showed drag of $1.39–$13.61 per
spread. What differs is the payoff shape, not the transaction cost.

### What this page implies for design

1. **Greeks + IV are free.** Build delta/IV-driven selection — it is the highest-sophistication,
   lowest-cost move available.
2. **Rank on Greeks, price on quotes.** Never send an order priced off an indicative mid.
3. **Small universe, server-side filters, REST polling.** 200 req/min is the real budget.
4. **Reconcile legs every cycle.** Random partial fills will break spreads; detecting and repairing
   that is a feature, not a chore.
5. **Filter chains by `expiration_date_gte` and reject null Greeks.** Verified failure mode, not a
   hypothetical.
6. **Disclose the feed in your write-up.** Judges from Alpaca's Trading API team will know exactly
   what `indicative` means. Acknowledging it reads as competence; ignoring it reads as naivety.
