# 09 — Open Questions

> **Q1, Q4, Q5 and Q7 were resolved against the live account on 2026-09-01 —
> see [`10-verification-log.md`](10-verification-log.md).** The rest stand open.

Everything here is **unverified**. Each item names the exact command or action that settles it.
Do not design around an unresolved item without checking it first.

## Blocking — resolve before writing execution code

### 1. ✅ RESOLVED — options level on a fresh paper account
**Level 3, granted by default.** (`options_trading_level: 3`, `options_approved_level: 3`.)
Docs say options are "enabled by default" on paper but do not state the *level*. Level 3 is what
unlocks spreads, and every defined-risk design depends on it.

```bash
alpaca api GET /v2/account | jq '{level: .options_trading_level, obp: .options_buying_power}'
# or
alpaca account config get
```
**If it is not 3:** Trading Dashboard → Account → Configure. If it cannot be raised on paper, the
whole strategy family changes — fall back to Level 2 long calls/puts (still defined-risk, still
options-compliant).

### 2. Does `alpaca api POST /v2/orders` actually place an MLEG order?
The CLI has no documented first-class multi-leg submit command. `03-agent-surfaces.md` proposes the
raw-passthrough route, which is how you satisfy the CLI requirement *and* trade spreads. **This is an
assumption, not a documented fact.**

```bash
echo '{"order_class":"mleg","qty":"1","type":"limit","limit_price":"0.05",
  "time_in_force":"day","legs":[
    {"symbol":"<near>","ratio_qty":"1","side":"buy","position_intent":"buy_to_open"},
    {"symbol":"<far>","ratio_qty":"1","side":"sell","position_intent":"sell_to_open"}]}' \
| alpaca api POST /v2/orders
```
Use an unfillable limit price so the test cannot accidentally open a position, then cancel.
**If it fails:** use `place_option_order` via MCP for execution and the CLI for data/monitoring —
still satisfies the rule.

### 3. Do MLEG orders accept `type: "market"`?
Every documented example is limit + day. If market MLEG works, entry logic simplifies enormously; if
not, non-fill handling is mandatory. Test with a 1-contract wide spread on a liquid underlying.

## Important — resolve before finalising strategy

### 4. ✅ PARTLY RESOLVED — indicative Greeks exist; staleness still unmeasured
**Greeks and IV confirmed present on the free feed for live contracts.** Expired contracts return nulls — always filter with `expiration_date_gte`. Freshness vs a real-time source is still unmeasured.
`05-data-constraints.md` establishes Greeks and IV *are* returned free. It does **not** establish they
are fresh enough to trade on. Compare `get_option_snapshot` output against a live chain from any
public source at the same moment, for a liquid name.

**Why it matters:** if IV is computed from 15-minute-delayed derived quotes, IV-rank entry signals
lag the market by 15 minutes. That may be fine for a 2-day hold and fatal for anything intraday.

### 5. ⚠️ RESOLVED NEGATIVE — index endpoint returns 404
`GET /v1beta1/indices/latest?symbols=VIX` → **HTTP 404**. Do not build VIX regime detection on it. Use `VIXY`/`UVXY` via stock endpoints, or realised vol from SPY bars.
Regime detection built on VIX is a common design. Index data may be a separate entitlement.
```bash
alpaca api GET '/v1beta1/indices/latest?symbols=VIX' ; echo "exit=$?"
```

### 6. Real `ratio_qty` behaviour for an iron condor
A condor is four legs. Confirm 1:1:1:1 is accepted and that the GCD rule behaves as documented for
four legs, not just two. Also confirm which `position_intent` each leg needs.

### 7. ✅ RESOLVED — Featherless redemption
The setup guide PDF is **image-based** — text extraction failed and Chrome's viewer would not render
for capture. Page 2 carries a QR code and the redemption instructions.

**Action: open <https://storage.googleapis.com/lablab-static-eu/share/Hackathon-Setup-Guide-ALPACA26.pdf>
and read page 2 yourself.** Credits are first-come, first-served — do this early.

## Worth asking in Discord

### 8. Are the judging criteria weighted?
The page lists five criteria with no weights. If P&L is 50%, the calculus differs sharply from an
even split across five. Ask in the lablab.ai Discord — a public answer helps everyone and costs
nothing.

### 9. Is P&L judged as absolute return, risk-adjusted, or drawdown-aware?
"Judges will consider the project's P&L and how effectively the strategy performs through its trading
activity" is deliberately loose. "How effectively the strategy performs" may reward process over
outcome — worth clarifying.

### 10. Does the account need to show activity across multiple sessions?
The account ID exists so judges "can identify your trading activity." Whether a single session's
trades suffice, or breadth of activity is expected, changes how early you must go live.

## Lower priority

### 11. `place_option_order` multi-leg parameter shape via MCP
Documented as supporting "single-leg or multi-leg strategies" but the parameter schema is not in the
docs page. Inspect the live tool schema from your MCP client.

### 12. Actual MCP tool count on the installed version
Docs enumerate 65; the README says 90+. List tools from your client and record the real number rather
than citing either figure.

### 13. Featherless concurrency limits
Docs reference "Concurrent Unit Limits" and `/account/concurrency/stream` without publishing numbers.
Query that endpoint once you have a key.

---

## Verification pass — run this first

```bash
alpaca doctor
alpaca api GET /v2/account | jq '{status,equity,buying_power,options_trading_level,options_buying_power,account_number}'
alpaca clock
alpaca data option chain --underlying-symbol SPY | head -50
```

Four commands settle items 1, 4 and 5 partially, confirm the CLI is authenticated against the right
account, and print the account number you owe the submission form.
