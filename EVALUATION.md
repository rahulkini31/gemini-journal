# Evaluation: does the LLM proposer earn its place?

**Question.** The proposer selects from a shortlist that deterministic code has
already built, priced, gated and ranked. If it always picks rank 1 it adds only
latency and cost. If it picks elsewhere it is overriding a quantitative ordering
with narrative, and the burden is on it to be right.

**Harness.** `scripts/eval_proposer.py`. Repeated trials against one real,
live shortlist; records the rank it selects, its deviation from rank 1 on the
ranker's own metric, refusal rate, and reproducibility.

## What this can and cannot show

It **cannot** show that either policy makes money — that needs forward returns
over many trades, which do not exist here. It **can** show whether the model
deviates, in which direction, how often it refuses, and whether it is stable.

## Result

| Run | Trials | Rank-1 candidate | Selected a candidate | Deviated from rank 1 |
|---|---|---|---|---|
| 1 | 12 | negative expectancy (−$21.75) | **0** | 0 |
| 3 | 10 | positive expectancy, PoP 0.768, $2 friction | **0** | 0 |

**22 of 22 trials returned NO_TRADE**, across two different shortlists. The
proposer contributes no selection signal. Its only output is abstention, which
`if True: return NO_TRADE` reproduces at zero cost and zero latency.

It is also **not reproducible**: 12 identical inputs produced 8 distinct
rationales, some contradicting each other — trial 1 *"IV is lower than RV"*,
trial 2 *"IV is slightly above RV"*, on the same numbers.

## The test was worth more than its verdict

It found two defects that review had not.

**The ranking recommended a negative-expectancy trade.** Run 1's rank 1 had
PoP 0.795, max gain $151, max loss $949 — expected value **−$21.75**. The score
`PoP − friction` cannot see loss magnitude, so it favoured exactly the trade
that collects a little very often and returns it all at once. The proposer's
refusal was *correct* and the fault was in the ranker. Fixed by a
`positive_expectancy` gate.

**A crash on the only path never exercised.** Renaming a summary key left
`agents.py` indexing one that no longer existed. It raises `KeyError` while
building the adversary's payload — which happens *only when the proposer picks
something*. Every prior dry run ended in NO_TRADE or a veto, so nothing reached
it, and the refusal-heavy behaviour was **masking a crash on the trading path**.
Found on trial 3.

## Caveat on the verdict

The proposer's system prompt contains *"NO_TRADE is a legitimate and frequently
correct answer. Prefer it when the shortlist offers no clear edge."* That biases
it toward refusal, so the refusal rate partly measures the prompt, not the
model's assessment. An ablation removing that instruction separates the two.

## What follows from this

On this evidence the proposer earns nothing **as a selector**. The adversary is
a different case — it caught an inverted IV/RV comparison in a live cycle, which
was a genuine save.

The defensible simplification is to drop the proposer, take rank 1
deterministically, and keep the adversary as a veto. That is testable with the
same harness and would remove one model, one failure mode, and roughly half the
cycle latency.
