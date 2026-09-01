# Alpaca AI Trading Agents Hackathon — Knowledge Base

Research snapshot compiled **2026-09-01** for the
[Alpaca AI Trading Agents Hackathon](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon)
(28 Aug – 4 Sep 2026, $6,300 pool).

This is a **decision instrument, not an encyclopedia.** Every page ends with a
*"What this page implies for design"* section. If you read nothing else, read those.

---

## ⏰ The clock

**Submissions close 4 Sep 2026, 15:00 UTC (20:30 IST).**
As of writing that leaves ~3 days and **≈3.5 US market sessions**: 1, 2 and 3 Sep in full, plus
90 minutes on 4 Sep before the cutoff. No market holiday intervenes.

## 🚧 Three things that disqualify you

1. Not autonomous.
2. Doesn't use the **MCP server or the CLI** (the SDK alone does not count).
3. **Doesn't trade options.** Every strategy must incorporate options.

Plus: the submission must run on a **brand-new paper account** seeded at **$100,000**, and you must
submit that **account ID**. Reused accounts are ineligible.

---

## Read in this order

| # | Page | Read it for |
|---|---|---|
| 1 | [`01-hackathon-brief.md`](01-hackathon-brief.md) | Rules, deadline, prizes, judging, submission checklist |
| 2 | [`07-competitive-landscape.md`](07-competitive-landscape.md) | **What 41 other teams already built, and where the gaps are** |
| 3 | [`05-data-constraints.md`](05-data-constraints.md) | **What data you actually have on a free account** |
| 4 | [`04-options-mechanics.md`](04-options-mechanics.md) | What orders you can actually place |
| 5 | [`03-agent-surfaces.md`](03-agent-surfaces.md) | MCP vs CLI vs SDK vs Skills — the decision matrix |
| 6 | [`08-reference-architecture.md`](08-reference-architecture.md) | Alpaca's own published multi-agent design |
| 7 | [`02-alpaca-platform.md`](02-alpaca-platform.md) | Accounts, environments, order lifecycle |
| 8 | [`06-partner-tech.md`](06-partner-tech.md) | Featherless credits + what the field is using |
| 9 | [`09-open-questions.md`](09-open-questions.md) | **What is still unverified, and the command that settles each** |
| 10 | [`10-verification-log.md`](10-verification-log.md) | **Live account state + what testing actually proved** |

Supporting: [`03a-mcp-tool-inventory.md`](03a-mcp-tool-inventory.md) ·
[`Hackathon-Setup-Guide-ALPACA26.md`](Hackathon-Setup-Guide-ALPACA26.md) ·
[`knowledge-graph.md`](knowledge-graph.md) · [`graph.json`](graph.json) ·
[`sources.md`](sources.md)

---

## The five findings that matter most

### 1 · Greeks and implied volatility are free — ✅ verified live
The option snapshot and chain endpoints return a full `greeks` object **and** Black-Scholes
`impliedVolatility` on the **indicative** (free) feed. Delta-selection, IV-rank and portfolio-Greek
budgeting are all viable without a $99/mo subscription. Many teams will not realise this.

**But the chain returns expired contracts by default**, and those come back with `greeks: null`.
Always filter with `expiration_date_gte`, and treat a null Greek as a hard reject rather than a zero.
→ `05-data-constraints.md`, `10-verification-log.md`

### 2 · The field converged on one architecture — so it can't be your pitch
Roughly two-thirds of the 41 submissions independently built *"the LLM proposes, deterministic code
disposes"*: bounded shortlists, non-LLM risk gates that can veto but never enlarge, full audit logs,
defined-risk spreads. **Build it — an ungated LLM near orders is genuinely unsafe — but don't lead
with it.** → `07-competitive-landscape.md`

### 3 · P&L is statistically meaningless here, and that's good news
≈3.5 sessions cannot produce a significant return. Four of the five judging criteria are
artefact-based and fully under your control. Bound the downside, make the write-up and video carry
the weight. → `01-hackathon-brief.md`

### 4 · The judges are Alpaca platform engineers
The Trading API team lead and the Chief Brokerage Officer are judging. They will know instantly
whether you understand `indicative` vs OPRA, MLEG's limit-and-day constraint, and paper's random
partial fills. **Naming the platform's real distortions is free credibility** — and nobody else's
submission summary mentions them. → `07-competitive-landscape.md`

### 5 · One action satisfies two rules
Opening a **new** paper account from the dashboard gives you both the required fresh account *and*
the required $100,000 balance — new accounts default to exactly that. Do it early: judges read the
trading history on the account ID you submit, and history only accumulates while you're live.
→ `02-alpaca-platform.md`

---

## Where the field is thin

| Opening | Why it's open |
|---|---|
| **Portfolio-level Greek management** | Everyone gates per-trade; almost nobody manages aggregate delta/vega across positions — and free Greeks make it computable |
| **Honest feed + fill modelling** | No submission summary mentions indicative quotes or random partial fills |
| **Execution quality / non-fill handling** | MLEG is limit+day, so unfilled spreads silently vanish at the close. Everyone studies *what* to trade; nobody studies *getting filled* |
| **Closed-loop strategy evolution** | Only two or three teams built a system that improves its own strategy |

---

## Account status — verified 2026-09-01

| | |
|---|---|
| Competition account | **PA3S4EFAEQLX** ← the ID the submission form wants |
| Equity | $100,000 ✅ |
| Options level | **3** ✅ — spreads and condors available |
| Created | 2026-09-01, fresh for this hackathon ✅ |
| Featherless | key valid, GLM-5.2 responding ✅ |

All four account-related eligibility rules are satisfied. Full detail in
[`10-verification-log.md`](10-verification-log.md).

**Q1–Q7 are now all resolved against the live account.** The CLI's raw passthrough *does* place MLEG
orders, four-leg structures work, and market MLEG orders are allowed inside market hours. The
architecture in `03-agent-surfaces.md` is verified end-to-end. Only the Discord questions (Q8–Q10)
and minor items remain.

---

*Next step is strategy and architecture design — deliberately not started here. The point of doing
research first is that those decisions rest on `07`, `05` and `04` rather than on guesses.*
