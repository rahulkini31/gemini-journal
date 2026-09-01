# 10 — Verification Log (live account, 2026-09-01)

Run against the actual competition paper account. This supersedes the corresponding entries in
`09-open-questions.md`.

## Competition account — confirmed compliant

| Field | Value |
|---|---|
| `account_number` | **PA3S4EFAEQLX** ← this is the ID the submission form wants |
| `status` | `ACTIVE` |
| `equity` / `cash` | **$100,000** ✅ meets the hackathon balance rule |
| `buying_power` | $400,000 (4× intraday multiplier — margin account) |
| `options_trading_level` | **3** ✅ |
| `options_approved_level` | **3** |
| `options_buying_power` | $100,000 |
| `crypto_status` | `ACTIVE` |
| `created_at` | **2026-09-01T05:09:14Z** ✅ fresh, created for this hackathon |

All four account-related hackathon rules are satisfied. **Level 3 confirmed** — defined-risk verticals
and iron condors are available.

## ✅ Q1 RESOLVED — Level 3 is granted by default on paper
`options_trading_level: 3` with no request or approval step. The `04-options-mechanics.md` design
assumptions hold.

## ✅ Q4 RESOLVED — Greeks and IV **are** free… with a trap

Confirmed on the **indicative** feed, no subscription:

```
SPY260902C00762000
  greeks = {delta 0.7541, gamma 0.0418, theta -1.2429, vega 0.1265, rho 0.0157}
  impliedVolatility = 0.1877
  bid/ask = 5.63 / 6.83
```

### 🚨 The trap: the chain endpoint returns EXPIRED contracts by default

The first query returned contracts expiring **2026-08-31 — the previous day** — with
`greeks: null` and `impliedVolatility: null`. Nothing was wrong with the feed; the contracts were
dead.

```jsonc
// SPY260831C00761000 — expired, keys present but no greeks
{ "keys": ["dailyBar","latestQuote","latestTrade","minuteBar","prevDailyBar"] }

// SPY260902C00762000 — live, greeks + impliedVolatility present
{ "keys": ["dailyBar","greeks","impliedVolatility","latestQuote","latestTrade","minuteBar","prevDailyBar"] }
```

**Always pass `expiration_date_gte`.** An agent that takes the first N contracts off an unfiltered
chain will silently receive expired contracts with null Greeks — and any null-handling that
"defaults to zero" will produce confidently wrong trades.

```bash
# correct
.../v1beta1/options/snapshots/SPY?feed=indicative&type=call \
  &strike_price_gte=760&strike_price_lte=775 \
  &expiration_date_gte=2026-09-02&expiration_date_lte=2026-09-30&limit=4
```

**Treat `greeks == null` as a hard reject, never as a zero.** This is a genuine, demonstrable
correctness property — and exactly the "honest handling of the platform" opening from
`07-competitive-landscape.md`.

### Indicative spreads are wide
`5.63 / 6.83` on a near-ATM SPY call is a **~19% spread**. That is the indicative feed being derived,
not a real market. Reinforces the rule from `05-data-constraints.md`: **rank on Greeks, price
conservatively, and never assume you fill at mid.**

## ✅ OPRA confirmed unavailable
```
feed=opra → {"message": "OPRA agreement is not signed"}
```
A clean, catchable error. Do not silently fall back — log which feed produced each decision.

## ⚠️ Q5 PARTIAL — index endpoint not available
`GET /v1beta1/indices/latest?symbols=VIX` → **HTTP 404**. Either the path is wrong or index data is
not entitled on Basic. **Do not design VIX-based regime detection around this endpoint** without
finding the correct path first. Workaround: use `VIXY`/`UVXY` ETFs via the stock endpoints, or derive
a regime signal from realised vol on SPY bars.

## ✅ Q7 RESOLVED — Featherless setup guide read
The PDF's markdown conversion is now at `knowledge-base/Hackathon-Setup-Guide-ALPACA26.md`.
See `06-partner-tech.md` for the redemption flow.

## ✅ Featherless credentials verified

| Check | Result |
|---|---|
| `GET /v1/models` | HTTP 200, **21,905 models** |
| `POST /v1/chat/completions` with `zai-org/GLM-5.2` | responded correctly |
| Usage accounting | `{prompt_tokens: 20, completion_tokens: 21, cached_tokens: 0}` |

Note the key format is **`rc_…`**, not the `fw-…` shown in the setup guide — the guide is out of date
on that detail. `cached_tokens` in the usage block implies prompt caching is supported, which is worth
exploiting if agent prompts share a long stable prefix.

## Market state at verification
`is_open: false`, next open **2026-09-01T09:30:00-04:00** (13:30 UTC). Verification ran at 02:08 ET,
pre-market. **No trading time has been lost.**

## ✅ Alpaca CLI installed and authenticated

`brew install alpacahq/tap/cli` → **v0.0.14**. Authenticates against the competition account purely
from `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` in the environment — no `alpaca profile login` needed,
which makes it trivially usable from a cron job or a container.

```
$ alpaca account get --jq '{acct:.account_number, lvl:.options_trading_level, eq:.equity}'
{ "acct": "PA3S4EFAEQLX", "eq": "100000", "lvl": 3 }
```

Read-only commands confirmed working: `alpaca account get`, `alpaca clock`, `alpaca order list`,
`alpaca position list`, `alpaca data option chain` (returned 100 snapshots).

⚠️ `alpaca option contracts --underlying-symbol SPY` returned `{"code": 0, …}` rather than a contract
list — the response shape differs from the docs. Prefer `alpaca data option chain` for discovery, or
inspect this command's real output shape before depending on it.

**This is the half of Q2 that could be answered without trading: the CLI works, authenticates, and
reads.** Whether its raw `api POST` passthrough accepts an MLEG payload is still open.

## Account state after verification — clean

```
open orders : 0
positions   : 0
equity      : $100,000
```

No test orders were placed. Nothing pollutes the judged trading history.

## Still unresolved — require placing a test order

The three MLEG questions all need a live order submission, which the sandbox blocked. They are the
**highest-priority** remaining unknowns because the entire "satisfy the CLI requirement while trading
spreads" strategy in `03-agent-surfaces.md` rests on Q2.

- **Q2** — does `alpaca api POST /v2/orders` accept an `order_class: "mleg"` payload?
- **Q3** — do MLEG orders accept `type: "market"`, or is limit genuinely the only option?
- **Q6** — four-leg iron condor `ratio_qty` / `position_intent` behaviour

Prepared test payloads (unfillable limits, cancel immediately afterwards) are in
`scripts/verify-mleg.sh`.

- **Q8–Q10** — judging weights and P&L interpretation (Discord questions)
