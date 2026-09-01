# 02 — The Alpaca Platform

> Docs root: <https://docs.alpaca.markets/us/docs/getting-started>
> Machine-readable index of *every* doc page: <https://docs.alpaca.markets/us/llms.txt>
> Any doc URL + `.md` returns clean markdown. Use this — it is far cheaper than scraping HTML.

## What Alpaca is

A **programmable brokerage**. You supply an API key; your application places real orders on US
stocks, ETFs, options and crypto. Alpaca supplies the brokerage infrastructure, the market data, and
the regulatory wrapper. You supply the application.

Two legal entities matter for disclosures:
- **Alpaca Securities LLC** (dba "Alpaca Clearing") — member FINRA/SIPC — securities.
- **Alpaca Crypto LLC** — FinCEN-registered MSB (NMLS #2160858), *not* SIPC/FINRA — crypto.

## The three API products

| Product | Purpose | Relevant here? |
|---|---|---|
| **Trading API** | You trade your own account programmatically. | ✅ This is the hackathon surface. |
| **Market Data API** | Historical + real-time bars, quotes, trades, news, corporate actions, Greeks. | ✅ Required for any strategy. |
| **Broker API** | Full-stack brokerage-as-a-service; you open and trade accounts *on behalf of others*. | ❌ Out of scope — different auth, different endpoints. Ignore the `broker-*` skills and docs. |

## Environments

| | Paper | Live |
|---|---|---|
| Base URL | `https://paper-api.alpaca.markets` | `https://api.alpaca.markets` |
| Money | Simulated | Real |
| Credentials | Separate key pair | Separate key pair |
| Options | **Enabled by default** (see `04-options-mechanics.md`) | Requires approval flow |
| Market data | Identical real data, subject to your plan tier | Same |

Data endpoints live on `https://data.alpaca.markets` regardless of environment.

**Auth headers** (all REST calls):
```
APCA-API-KEY-ID:     <key id>
APCA-API-SECRET-KEY: <secret>
```
Every response carries an `X-Request-ID` header. Log it — Alpaca support asks for it first.

## Paper accounts — the mechanics the hackathon depends on

- Anyone worldwide can open a paper-only account with just an email. No deposit, no card.
- **New paper accounts start at $100,000** by default. This is exactly the balance the hackathon
  requires — so a freshly created account satisfies the balance rule with no extra step.
- Changing the balance is **not** an in-place edit. The current dashboard replaces the old "reset"
  flow: click your paper account number → **"Open New Paper Account"** → set the starting balance.
- **This means one action satisfies two hackathon rules at once:** opening a new paper account gives
  you both the required *fresh, dedicated* account and the required *$100,000* balance.
- **Your paper account number appears in the upper-left of the dashboard.** That is the value the
  submission form wants. Capture it the moment you create the account.
- Generate a **new API key pair per account.** Keys are account-scoped.

### Do this early, not last
Judges evaluate P&L from the trading activity on the submitted account ID. An account created on the
final morning has almost no history to judge. Create it before the first session you intend to trade.

## Paper simulation fidelity — what it does *not* model

These distort any P&L number and are worth stating openly in your write-up (see
`05-data-constraints.md` for the full list and the strategic reading):

- No market impact, no order-queue position, no price improvement.
- No regulatory fees, no dividends, no borrow fees.
- No slippage or latency modelling.
- Non-marketable limit orders fill only when the NBBO reaches them.
- Orders can fill at sizes exceeding the real available liquidity.
- **When eligible to fill, orders receive a random partial fill roughly 10% of the time.** Your
  execution logic must handle partial fills or it will desynchronise from reality.

## Order lifecycle & fills

`POST /v2/orders` submits; the `trade_updates` websocket streams the lifecycle
(`new` → `partial_fill` → `fill` / `canceled` / `rejected` / `expired`).

Websocket streaming doc: <https://docs.alpaca.markets/us/docs/websocket-streaming.md>

Two idempotency tools you should use from the start:
- **`client_order_id`** — supply your own UUID; re-submitting the same one will not double-place.
  Retrieve by it with `GET /v2/orders:by_client_order_id`.
- **`--dry-run`** on the CLI previews an order without sending it.

An autonomous agent that retries on network errors *without* a client order id will eventually double
its position. This is the single most common self-inflicted wound in this class of project.

## Key endpoint groups (Trading API)

| Group | Path | Notes |
|---|---|---|
| Account | `/v2/account` | equity, buying power, `options_trading_level` |
| Account config | `/v2/account/configurations` | trading blocks, margin settings |
| Orders | `/v2/orders` | includes `order_class: mleg` for spreads |
| Positions | `/v2/positions` | close full/partial; `/exercise` for options |
| Assets | `/v2/assets` | tradability, shortability, fractionability |
| Option contracts | `/v2/options/contracts` | strike/expiry discovery |
| Clock & calendar | `/v2/clock`, `/v2/calendar` | **check `is_open` before every trading cycle** |
| Portfolio history | `/v2/account/portfolio/history` | the series judges will effectively be reading |
| Activities | `/v2/account/activities` | fills, dividends, option NTAs |
| Watchlists | `/v2/watchlists` | |

`GET /v2/clock` is the cheapest correctness win in the whole system: an agent that tries to trade at
03:00 UTC produces a log full of rejections and looks broken on video.

---

### What this page implies for design

1. Open the competition paper account **now**, record the account ID, generate its keys, and point
   the agent at it — history compounds.
2. Treat `client_order_id` as mandatory, not optional.
3. Gate every cycle on `/v2/clock`.
4. Build partial-fill handling in from the start; the simulator injects them deliberately.
5. Pull docs as `.md` — the `llms.txt` index plus `.md` suffix makes the entire documentation set
   cheap to read programmatically.
