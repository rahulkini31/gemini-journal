# 04 — Options Mechanics on Alpaca

> Sources: [Options Trading](https://docs.alpaca.markets/us/docs/options-trading) ·
> [Options Orders](https://docs.alpaca.markets/us/docs/options-orders) ·
> [Options Level 3 Trading](https://docs.alpaca.markets/us/docs/options-level-3-trading)
>
> **Options are mandatory in every hackathon strategy.** This page is the rulebook for what you are
> actually allowed to place.

## Approval levels

| Level | Unlocks | Requirement |
|---|---|---|
| 0 | Options disabled | — |
| 1 | **Covered calls**, **cash-secured puts** | own the underlying shares / cash; sufficient options buying power |
| 2 | Level 1 + **buy calls and puts** | sufficient options buying power |
| 3 | Level 2 + **call spreads and put spreads** | sufficient options buying power |

### On paper, this is free
> **"In the Paper environment, options trading capability will be enabled by default — there's
> nothing you need to do!"**

Confirm the level actually granted on your fresh account before designing around Level 3:
```bash
alpaca account config get          # look for options_trading_level
alpaca api GET /v2/account | jq '.options_trading_level, .options_buying_power'
```
Disable/adjust via Trading Dashboard → Account → Configure.

**Level 3 is the interesting one.** Defined-risk spreads are what let an autonomous agent trade
options without unbounded downside — which is exactly what you want when a language model is
anywhere near the decision.

## OCC contract symbols

```
AAPL  231201  C  00195000
└─┬─┘ └──┬──┘ │  └───┬───┘
  │      │    │      └─ strike × 1000, zero-padded to 8 digits → $195.00
  │      │    └─ C call / P put
  │      └─ expiry YYMMDD → 2023-12-01
  └─ underlying, space-padded to 6 chars in the strict OCC form
```
Parse and *generate* these with a tested helper. An off-by-one in the strike padding produces a
"contract not found" that looks like a data problem and is not.

## Single-leg orders

Same `POST /v2/orders` endpoint as equities and crypto, with extra validation:

| Rule | Value |
|---|---|
| `qty` | whole numbers only |
| `notional` | **must not be populated** |
| `time_in_force` | `day` or `gtc` only |
| `extended_hours` | `false` or omitted |
| `type` | `market`, `limit`, `stop`, `stop_limit` — **stop types single-leg only** |

**Buying power** = execution price × 100 × contracts.
- Buy 1 call at $5.10 → $510
- Buy 1 put at $1.04 → $104
- Cash-secured put, $175 strike → strike × 100 × contracts = $17,500

That last line is the one that quietly kills naive strategies: a single cash-secured put on a $175
stock consumes 17.5% of a $100,000 account. Position sizing must be computed from *notional
collateral*, not premium.

## Multi-leg (MLEG) orders — Level 3

```jsonc
{
  "order_class": "mleg",
  "qty": "1",
  "type": "limit",
  "limit_price": "0.60",
  "time_in_force": "day",
  "legs": [
    { "symbol": "AAPL250117C00190000", "ratio_qty": "1",
      "side": "buy",  "position_intent": "buy_to_open"  },
    { "symbol": "AAPL250117C00210000", "ratio_qty": "1",
      "side": "sell", "position_intent": "sell_to_open" }
  ]
}
```

### Leg fields
- `symbol` — OCC contract symbol
- `ratio_qty` — relative proportion. **The greatest common divisor across all legs must be 1.**
  A 2:2 condor is invalid; express it as 1:1. Verified — the API rejects it with a parseable message:
  `"leg ratio quantities should be relatively prime: GCD[2 2] = 2"`. Normalise ratios by their GCD
  before submitting.
- `side` — `buy` | `sell`
- `position_intent` — `buy_to_open` | `sell_to_open` | `buy_to_close` | `sell_to_close`

### Hard restrictions
- ❌ **"MLeg orders that include an equity leg are not supported."** No covered calls or collars as a
  single MLEG order. Legging in manually means the equity leg is unprotected in between.
- ❌ **"An MLeg order is accepted only if all its legs are covered within the same MLeg order."**
  No naked short legs.
- ✅ **Market MLEG orders ARE supported — but only during market hours.** Verified: submitting one
  pre-market returns `42210000: "options market orders are only allowed during market hours"`. That
  is a session restriction, not a structural one. Outside 13:30–20:00 UTC you must use limit orders.
- ⚠️ Limit + day means **an unfilled spread simply expires at the close.** Your agent must detect
  the non-fill and decide whether to re-price, not assume it is in the trade.

### Documented strategies
Long call spread · long put spread · **iron condor** · rolls (close existing, open new strikes/expiry).
Straddles, strangles, calendars and butterflies are referenced only in passing — constructible in
principle, but unverified in the docs.

**Verified live:** a four-leg 1:1:1:1 structure (long call spread + long put spread) was accepted with
per-leg `position_intent`. Four-leg construction works.

### Margin — the "universal spread rule"
1. Maintenance margin is computed from the **piecewise-linear payoff**: the theoretical maximum loss
   across all underlying prices, **ignoring premium paid or received**.
2. Multiple expirations are computed separately per expiration; the **largest** requirement applies.
3. **Cost basis = maintenance margin + (net premium × 100).**

Worked example from the docs: a call credit spread with a $1,000 margin requirement and $500 net
credit has a $500 total cost basis.

This is genuinely favourable for defined-risk selling: your capital at risk is the spread width minus
credit, not the naked-short requirement.

## Exercise, assignment and expiry

- **Automatic exercise** of contracts at least **$0.01 in the money** at expiry.
- Manual: `POST /v2/positions/{symbol_or_contract_id}/exercise`
- Do-not-exercise is available (`optiondonotexercise`).
- If the account lacks buying power to take on an ITM exercise, **Alpaca sells the position within
  one hour before expiry** — i.e. it liquidates for you, on its schedule, not yours.
- ⚠️ **On paper, non-trade activities (NTAs) sync at the start of the following day**, even though
  balances update instantly. Assignment and exercise effects will look wrong until the next day.

**For a 3.5-session hackathon this matters a lot.** Anything expiring inside the window can generate
assignment noise that appears in your P&L after judging, or not at all. Prefer expiries safely beyond
4 Sep, or close positions rather than letting them expire.

## Options data — see `05-data-constraints.md`

The short version, because it changes strategy design: the **free indicative feed still returns full
Greeks and Black-Scholes implied volatility** on the snapshot and chain endpoints. Quotes are
*derived*, not real OPRA, and trades are 15 minutes delayed.

---

### What this page implies for design

1. **Build on defined-risk verticals and iron condors.** Level 3 on paper is free, max loss is
   bounded by construction, and margin treatment is favourable.
2. **No equity legs in MLEG.** Any strategy needing stock + option must be legged, and legging risk
   must be handled explicitly or avoided.
3. **Price spreads with limit orders and handle non-fills as a first-class state.** Day TIF means an
   unfilled order silently disappears at the close.
4. **Size from collateral, not premium.** Cash-secured puts consume enormous buying power.
5. **Avoid expiries inside the judging window** unless you deliberately want assignment mechanics —
   paper NTAs lag a day and will muddy the record judges read.
6. `ratio_qty` GCD must be 1 — normalise ratios before submitting.
