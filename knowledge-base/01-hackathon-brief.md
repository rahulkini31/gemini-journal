# 01 — Hackathon Brief

> Source of truth: <https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon>
> Retrieved 2026-09-01. The page is bot-blocked (HTTP 403 to plain fetchers) — read it in a browser.

## The clock

| | |
|---|---|
| Kick-off | Fri 28 Aug 2026, 20:30 IST |
| **Submission deadline** | **Fri 4 Sep 2026, 15:00 UTC / 20:30 IST** |
| Today | 1 Sep 2026 |
| **Time remaining** | **~3 days — 3.5 US market sessions (see below)** |
| Format | Online, 7 days |
| Prize pool | $6,300 (page header says $6,000 in one place, $6,300 in another) |

### Market sessions actually available for judged P&L

US regular hours are 13:30–20:00 UTC. No market holiday falls inside the window (Labor Day 2026 is
Mon 7 Sep, *after* the deadline).

| Session | Date | Available |
|---|---|---|
| 1 | Tue 1 Sep | Full — as of writing it is 05:31 UTC, pre-market. Nothing lost yet. |
| 2 | Wed 2 Sep | Full |
| 3 | Thu 3 Sep | Full |
| 4 | Fri 4 Sep | **13:30 → 15:00 UTC only — 90 minutes** before the cutoff |

**≈3.5 sessions of live trading.** That is enough to demonstrate an agent working, and far too little
for a P&L number to mean anything statistically. See `07-competitive-landscape.md` for why this makes
P&L the *least* controllable judging axis — and why bounding downside beats chasing upside.

## Hard gating requirements

Fail any of these and the submission is not eligible:

1. **Autonomous agent.** Must be an autonomous AI trading agent built on Alpaca's Trading API.
2. **MCP or CLI.** The project must use *either* Alpaca's MCP server *or* its CLI tools. The SDK
   alone does not satisfy this.
3. **Options.** *All strategies must incorporate options trading.* This is not optional and not a
   bonus — an equities-only agent is out.
4. **Fresh paper account.** Explore on any paper account you like, but the submitted project must run
   on a **brand-new Alpaca paper trading account created for this hackathon**. "Projects run on an
   existing or reused account will not be eligible for judging."
5. **$100,000 starting balance** on that competition account.
6. **One-page write-up** covering AI logic, risk gates, and Alpaca infrastructure implementation.

## The challenge — "Options Alpha Agents"

> Build an autonomous AI trading agent designed to generate P&L using Alpaca's trading platform.
> Develop a clear, testable trading strategy and demonstrate how your agent identifies opportunities,
> makes trading decisions, manages positions, and performs over the course of the competition.

## Prizes

| Place | Cash | Extra |
|---|---|---|
| 🥇 1st | $2,500 | + $300 Featherless credits |
| 🥈 2nd | $1,500 | — |
| 🥉 3rd | $1,000 | — |
| Social engagement × **2 teams** | $500 per team | 1-month Algo Trader Plus **per team member** |

The social prize is a genuinely separate pool with only two winners and a much smaller field of
serious entrants. See "Extra challenge" below — it is the cheapest expected value on the board.

### Prize terms
- Paid by AlpacaDB, Inc. directly in USD.
- 18+. Not open to Alpaca employees/contractors/household, or sanctioned countries.
- **Paid to individuals, not teams.** A winning team designates one member (or pre-arranges a split
  with Finance).
- W-9 (US) or W-8BEN (non-US) + government photo ID + bank details required before payment.
- Payment within 90 days of event end, after sanctions screening.
- US winners over $600 get a 1099-MISC. **Non-US payments are generally subject to 30% US
  withholding unless a valid treaty claim is made on the W-8BEN.** Gross prize may be reduced by
  withholding and wire fees.
- Winners must complete documentation within 90 days of notification or forfeit.
- Skill contest; judging is final. Submissions must be original and MIT-compliant.
- Alpaca may use winner name, likeness and project for publicity.

## Judging criteria

Listed on the page **without published weights** — that absence is itself strategic information.

| Criterion | What the page says |
|---|---|
| **P&L Performance** | Trading performance in the Alpaca paper environment; P&L and how effectively the strategy performs through its trading activity. |
| **Technology Implementation** | How effectively the project uses Alpaca's Trading API, MCP server, CLI and other required technologies to build an autonomous trading agent. |
| **Creativity & Originality** | Originality of concept, trading strategy, agent behaviour and overall approach. |
| **Presentation & Execution** | How clearly the project communicates its idea, demonstrates the agent in action, and presents the reasoning behind its strategy and results. |
| **Social engagement** | Both content quality *and* engagement generated (likes, comments, shares). |

Four of the five criteria are fully under your control and are judged on artefacts, not luck. Only
P&L depends on ~2.5 sessions of market noise.

## Submission checklist

- [ ] Project title, short description, long description
- [ ] Technology & category tags
- [ ] Cover image
- [ ] **Video presentation** (demo of the agent in action)
- [ ] Slide presentation
- [ ] **Public GitHub repository**
- [ ] Demo application platform + application URL
- [ ] **Alpaca paper trading account ID** — required for judging; lets judges pull your trading
      activity and evaluate P&L. Without it you cannot be scored on P&L at all.
- [ ] One-page write-up: AI logic, risk gates, Alpaca infrastructure
- [ ] Up to **5** social post links (X / LinkedIn)

## Extra challenge — Build in Public

Share progress publicly on **X and LinkedIn** while building: process, reasoning, and setbacks. Tag
both parties in every post.

| | X | LinkedIn |
|---|---|---|
| lablab.ai | `@lablabai` | `lablab.ai` |
| Alpaca | `@AlpacaHQ` | `Alpaca` |

Up to 5 post links submitted with the final project. Two teams win $500 + Algo Trader Plus each.

## Teams & logistics

- Teams of **1–6**. Free to join. All skill levels.
- Register on both the lablab.ai platform **and** the lablab.ai Discord.
- Find teammates on the dashboard or Discord.

## Speakers, mentors & judges

| Name | Role |
|---|---|
| Pawel Czech | CEO (lablab.ai / NativelyAI) |
| Chiranjeev Shah | Technical Content Marketing Associate |
| Tony Lee | Chief Brokerage Officer (Alpaca) |
| Grace Gao | Product Manager |
| Brandon Meyerowitz | Team Lead, Trading API (Alpaca) |

Two of five are Alpaca platform people (Trading API lead, Chief Brokerage Officer). That is a strong
hint that **depth of correct Alpaca API usage** will be recognised and rewarded — sloppy or shallow
integration will be visible to them immediately.

## Disclosures worth internalising

- Paper trading is a simulation; results are hypothetical.
- lablab and Alpaca are unaffiliated and separately liable.
- Options trading carries inherent high risk; complex options strategies carry additional risk.

---

### What this page implies for design

1. **Options + MCP-or-CLI + autonomous** are non-negotiable. Architecture must show all three
   explicitly, or the write-up must point at where each lives.
2. **The account ID is a judging dependency.** Create the fresh $100k account *early* so trading
   history accumulates across all available sessions, not just the last hours.
3. **P&L is ~3.5 sessions of noise.** Optimise for the four deterministic criteria; treat P&L as a
   risk to be *bounded* (defined-risk structures, no blow-up) rather than a number to be maximised.
   A small positive or flat P&L with a flawless audit trail beats a big number nobody can explain —
   and beats a crater.
4. **Social is a separate, under-contested prize.** Five posts is a bounded, hours-long cost against
   a $500 + subscription prize with only two winners.
