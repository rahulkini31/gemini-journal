# 08 — Alpaca's Own Multi-Agent Reference Architecture

> Source: <https://alpaca.markets/learn/building-a-multi-agent-ai-trading-system-on-alpaca>
> Linked directly from the hackathon page under "AI & AGENT DEVELOPMENT".

**Read this as the baseline the organisers themselves published.** It is the shape they consider
good. It is also *not* a valid hackathon submission as-is — see the gaps at the bottom.

## The pipeline

```
06:30 ET batch
   Alpaca StockHistoricalDataClient  (all ~500 S&P 500 tickers, ONE batch request)
 + Finnhub    (fundamentals, insider activity, analyst ratings, earnings)
 + yfinance   (VIX term structure, yield curve, sector ETF returns)
 + FRED       (credit spreads, Fed Funds, inflation breakevens)
            │
            ▼
   SQLite  market_snapshot  table   ← single consolidated interface
            │
            ▼
   5 research agents, parallel + ISOLATED (async)
   Momentum · Macro · StatArb · Contrarian · Exotic
            │  structured proposals
            ▼
   Critic agent  →  validates against investment_memo.yaml
            │
            ▼
   Human gate   (APPROVE / REJECT / REVISE)
            │
            ▼
   Risk Guard   (deterministic Python, ZERO LLM)
            │
            ▼
   Alpaca TradingClient  →  order + OCO bracket
            │
            ▼
   Position monitor, every 15 min — rebuilds missing brackets
```

## The five research agents

| Agent | Lens | Inputs |
|---|---|---|
| **Momentum** | breakouts, relative strength | price action, volume, RSI |
| **Macro** | sector rotation, factor plays | FRED, yield curve, VIX |
| **StatArb** | pairs, spread dislocations | rolling correlation, dislocation scores |
| **Contrarian** | oversold bounces, crowded unwinds | sentiment, insider activity |
| **Exotic** | calendar effects, earnings binaries | earnings calendar, volume patterns |

Plus **Critic** (validates structural compliance with governance rules — *not* trade quality) and
**Risk Guard** (deterministic position limits, no LLM).

**Agents are isolated.** They have "no visibility into each other's output before a structured
proposal is submitted." This is a deliberate choice against the debate pattern: independence
preserves signal diversity, where debate can collapse agents onto a shared view.

## Proposal schema

Every agent returns the same fixed structure:

```yaml
ticker:            AAPL
direction:         long | short
thesis:            "…"
entry_conditions:  …
exits:
  take_profit_pct: …
  stop_loss_pct:   …
  time_stop_days:  …
macro_alignment:   …
confidence_score:  …
position_sizing_pct: …
```

A **fixed schema is what makes deterministic gating possible at all.** Free-text model output cannot
be mechanically validated; a typed proposal can.

## Governance — `investment_memo.yaml`

- S&P 500 universe only (no ETFs, no crypto)
- Leverage cap **1.0x**
- Max single position **10%**
- Holding period **2–28 days**
- Required proposal fields

**Reported outcome: 82 proposals, 26 approved — a 32% approval rate.** A system that rejects two out
of three of its own ideas is the intended behaviour, not a malfunction.

## Risk Guard — deterministic, unit-tested, no model

```python
# execution/risk_guard.py (simplified, from the article)
def check_position_limits(proposal, portfolio_value, current_positions):
    position_pct = proposal['position_sizing_pct']
    if position_pct > 10.0:
        return False, "Exceeds max single position (10%)"
    sector = get_sector(proposal['ticker'])
    sector_exposure = sum(
        p['sizing_pct'] for p in current_positions
        if get_sector(p['ticker']) == sector
    )
    if sector_exposure + position_pct > 30.0:
        return False, "Exceeds max sector concentration (30%)"
    return True, "OK"
```

Hard limits: **10%** single position · **30%** sector concentration · **1.0x** leverage ·
drawdown halts at **5% daily / 10% weekly / 15% total**.

> "Risk checks run as deterministic code, unit-tested, with no model in the loop."

## Execution — entry plus OCO bracket

```python
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

entry = trade_client.submit_order(MarketOrderRequest(
    symbol=ticker, qty=shares, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
))

bracket = trade_client.submit_order(MarketOrderRequest(
    symbol=ticker, qty=shares, side=OrderSide.SELL,
    time_in_force=TimeInForce.GTC, order_class=OrderClass.OCO,
    take_profit=TakeProfitRequest(limit_price=take_profit_price),
    stop_loss=StopLossRequest(stop_price=stop_loss_price),
))
```

## Engineering decisions worth stealing

1. **One batch data request, not 500.** "Individual requests for the 500 tickers hit rate limits
   before the data finishes loading. One batch request solves this." Directly relevant to the
   200 req/min free-tier budget (`05-data-constraints.md`).
2. **Single SQLite interface.** "Agents query one interface and never call data sources directly."
   Reproducible, cacheable, testable, and it decouples agent logic from API quirks.
3. **Market orders first, deliberately.** "Adding limit order complexity may introduce entry timing
   noise before there is enough signal data to justify it." Ship the simple execution path first.
4. **Position monitor rebuilds missing brackets** every 15 minutes — pure price checks, no LLM. An
   unprotected position is treated as a bug to be repaired automatically.
5. **Specialisation over generalisation** — five narrow agents beat one general one.

## ⚠️ Two gaps that make this invalid as a submission

| Gap | Hackathon requirement | Fix |
|---|---|---|
| **Equities only** — no options anywhere in the pipeline | *All strategies must incorporate options trading* | Replace the execution layer with MLEG defined-risk spreads (`04-options-mechanics.md`); replace the equity OCO bracket with spread-level exits |
| **Human approval gate** — "process integrity requires a human decision point" | *Autonomous* AI trading agents | Replace the human gate with a second deterministic authority, or an adversarial model whose veto is binding. Do **not** simply delete the gate |

Note that the article defends the human gate on *accountability* grounds, not capability grounds:
"Not because the system cannot execute autonomously." Removing it for the hackathon is legitimate —
but the accountability it provided has to be replaced by something, or you have simply removed a
safety layer. That replacement is a good thing to be able to point at in the write-up.

Also note: this architecture uses **custom async Python, not LangGraph or CrewAI.** The article names
no orchestration framework. A framework is not required to look serious.

---

### What this page implies for design

1. **A fixed, typed proposal schema is the keystone.** Everything downstream — critic, gates, audit
   log — depends on it. Design it first.
2. **Steal the batch-fetch + single-SQLite-interface pattern** — it solves the rate-limit problem and
   makes runs reproducible.
3. **Isolated specialists, not debate**, is the organisers' published preference — and it is the
   *less* crowded choice in this field (`07-competitive-landscape.md`).
4. **Port the risk limits to options:** per-spread max loss, aggregate delta/vega, concentration,
   drawdown halts.
5. **The position monitor is the highest-value non-obvious component** — and with paper's random
   partial fills, leg reconciliation matters even more here than bracket rebuilding did there.
