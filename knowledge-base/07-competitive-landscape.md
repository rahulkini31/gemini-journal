# 07 — Competitive Landscape

> Source: the public submissions list on
> <https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon>, retrieved 2026-09-01.
> ~41 projects were publicly listed at that time. Descriptions below are paraphrased from each
> team's own one-line summary.

**This is the most actionable page in the knowledge base.** The field has already shown its hand, and
it converged hard on one idea.

## The dominant pattern — stated plainly

> **"The LLM proposes, deterministic code disposes."**

Roughly **two-thirds of the field** independently arrived at the same architecture:

1. A language model generates a trade thesis or picks from a shortlist.
2. **Deterministic, non-LLM code** validates it against hard limits.
3. The gates can **veto or shrink** a trade, never enlarge or invent one.
4. Every decision — *including every refusal* — is logged for audit.
5. Structures are **defined-risk options spreads** so max loss is bounded by construction.

Teams saying essentially this in their own words: **Vetoed** ("an agent most useful when it says no…
the AI is the least-trusted component… the model may only pick from a shortlist it cannot write"),
**Aegis** ("re-derives or re-observes every claim a model makes… refuses anything it cannot check"),
**Visheshak** ("an LLM provides judgment but deterministic code holds veto power over every dollar"),
**Horizon Blackline** ("the LLM proposes, deterministic risk gates authorize, every decision is
hash-chained"), **AEGIS-Q** ("bounded AI selects a pre-validated spread — or abstains"), **Debatte**
("no model places orders"), **EdgeStack** ("a public graveyard of rejected ideas… journals every
trade and every refusal"), **BABIL** ("fail-closed execution, and a kill switch"), **AegisAlpha /
Returnee** ("7 zero-LLM deterministic mathematical risk guardrails"), **Pin Desk** ("twelve
deterministic gates can veto the LLM; it can only ever shrink a trade").

### What this means for you

The risk-gate framing is **table stakes, not a differentiator.** If your pitch is "an AI trading
agent with deterministic risk gates and an audit log," you have described at least fifteen other
submissions, several of which executed it well. You still need to *build* it — an ungated LLM
touching orders is genuinely unsafe and judges will mark it down — but you cannot *lead* with it.

## Clusters

### 1 · Governance / auditability (≈15 projects) — **saturated**
Vetoed · Aegis · Horizon Blackline · Visheshak · BABIL · AEGIS-Q · EdgeStack · AegisAlpha (×2) ·
TradeMind · Vermilion · SPY Sentinel · Pin Desk · Autonomous Unified Risk & Alpha Agent

Notable executions: **Horizon Blackline** hash-chains its decision log (tamper-evidence, not just
logging). **Vega** claims "48 of 48 claims it publishes reproduce from one credential-free command" —
reproducibility as the pitch. **EdgeStack** publishes its *rejected* ideas.

### 2 · Multi-agent debate (≈6) — **crowded**
**TradeCouncil** (Bull/Bear debate → CIO picks CALL/PUT/NO TRADE, Featherless) · **Debatte** (five
analysts with *separated data views*) · **OptionFlow Sentinel** (5-agent LangGraph) · **AlphaSwarm
Sovereign** (adversarial dialectic + chart vision + 1,000-path Monte Carlo) · **AegisAlpha/Zenith**
(parallel Bull/Bear) · **ORACLE** (adversarial risk analysis, ToT Monte Carlo)

The bull/bear-plus-arbiter pattern is the single most reused multi-agent topology in the field.
Debatte's twist — giving each analyst a *different slice of the data* so disagreement is
informational rather than stylistic — is the most interesting variation and is worth understanding
before you design your own.

### 3 · Premium selling / volatility harvesting (≈6) — **crowded but principled**
**IV Rank Premium Harvester** (IV screen → credit spreads / iron condors) · **VRP Engine** (variance
risk premium, API+MCP+CLI) · **ThetaTrap** (Qwen + official MCP, earnings risk) · **Pin Desk** (dealer
gamma / pinning) · **Strike Sentry** (cash-secured puts) · **Options Sniper** (bull call spreads)

Economically the most defensible family — selling overpriced vol is a real edge — and it fits the
free-tier Greeks/IV availability (`05-data-constraints.md`). But over 3.5 sessions, theta collection
is tiny and one adverse move dominates.

### 4 · News / sentiment driven (≈4)
**NewsFlow Trader** (LLM scores headlines, Next.js dashboard) · **Crowd Excess** (attention and price
outrunning objective news → contrarian) · **SOGNO** (technical + news sentiment, entirely via the
official CLI) · **Strike Sentry**

### 5 · Research / strategy discovery loops (≈3) — **comparatively open**
**Futarchists Options** (strategies as *genetic sequences*, evolved through validated research) ·
**Odysseus** (agent discovers hypotheses → generates C# StockSharp strategies → backtests → validates
finalists on unseen history) · **a continual learning agent** (PX5000 vol forecasts + continual
stock-selection memory)

Only three teams built a closed loop where the system *improves its own strategy*. This is the
thinnest cluster relative to how impressive it is.

### 6 · Portfolio-level / hedging (≈3) — **comparatively open**
**VibeHedge** (xLSTM forecasting + FinRL-X gates + protective hedges) · **ORACLE** (Greek hedging) ·
**Vega** (long-gamma convexity — explicitly *buying* premium while everyone else sells)

**Vega is the contrarian trade of the hackathon**: while the field sells premium, it buys convexity
and cannot lose more than the premium paid. Over a 3.5-session window with event risk, that asymmetry
is defensible.

### 7 · Weak or non-compliant (≈4)
**AI Stock Trading Agent** (generic buy/sell/hold ML signals, no options) · **Cloudrise** ("they see
the market and tell you the result" — AI21 tag only, **no Alpaca tag**) · **KRAKN.AI** (crypto + US
stocks; options unclear) · **Tissue Regeneration & Genetic Factor Navigator** (tissue-engineering
parameters mapped to biotech options — creative, but the thesis link is a stretch)

**The effective competitive field is therefore closer to ~35 than 41.** Several entries will fail the
hard options requirement outright (`01-hackathon-brief.md`).

## Where the field is thin — genuine openings

| Opening | Why it is open | Why it is credible |
|---|---|---|
| **Portfolio-level Greek management** | Almost everyone gates *per trade*. Only ORACLE and VibeHedge manage exposure *across* positions. | Free Greeks make aggregate delta/vega/theta budgeting computable at zero cost. |
| **Honest modelling of feed + fill distortion** | Nobody's summary mentions the indicative feed or paper's random partial fills. | Two Alpaca platform people are judging. Showing you know `indicative` ≠ OPRA, and that spreads break on partial fills, is instant credibility. |
| **Closed-loop strategy evolution** | Only Futarchists and Odysseus. | Backtest-in-the-loop is impressive on video and independent of live P&L luck. |
| **Execution quality as a first-class concern** | Everyone focuses on *what* to trade; almost nobody on *getting filled*. | MLEG is limit+day — non-fills are a real, demonstrable failure mode. |
| **Making the 3.5-session P&L problem explicit** | The whole field competes on a P&L number that is statistically meaningless. | Naming this and optimising for *bounded, explainable* results is a genuinely differentiated posture. |

## Calibration notes

- **Quality bar is high.** Several submissions cite concrete metrics — AdventurousCat claims a
  "1.5 µs deterministic risk guardrail with a 55.4% audited win rate and 1.73 profit factor."
  Vague claims will not stand out.
- **Naming matters.** The field is full of Aegis/AegisAlpha/AEGIS-Q collisions — three teams
  independently chose near-identical names. Pick something distinct.
- **Claude + Alpaca MCP is the default stack** — safe, not differentiating (`06-partner-tech.md`).
- **Presentation quality is visibly high** — every entry has a video. Budget real time for it.

## The strategic read

Four of five judging criteria are artefact-based and fully under your control. P&L is ~3.5 sessions
of noise. The field has saturated the "safe AI trading agent" narrative.

**The opening is not a better strategy — it is a better-evidenced one.** Something that is
demonstrably correct under the platform's actual constraints (indicative Greeks, partial fills,
limit-only MLEG, 200 req/min), manages risk at the *portfolio* level, and can prove every claim it
makes, competes on the axes that are actually winnable.

---

### What this page implies for design

1. **Build the risk gates — but do not lead with them.** They are the price of entry.
2. **Differentiate on portfolio-level Greeks, execution quality, or a closed research loop** — the
   three thin clusters.
3. **Name the platform's real distortions out loud.** It is free credibility with these judges.
4. **Pick a distinctive name.** "Aegis" is taken three times over.
5. **Treat P&L as a bounded risk**, and make the write-up and video carry the weight.
