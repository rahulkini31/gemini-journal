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

## Ablation: the refusals were the PROMPT, not the model

The system prompt contained *"NO_TRADE is a legitimate and frequently correct
answer. Prefer it when the shortlist offers no clear edge."* Replacing that one
instruction with a neutral one, on the same shortlist, same model, same context:

| Prompt | Selected a candidate | Picks (rank index) | Adversary |
|---|---|---|---|
| WITH refusal bias | **0 / 6** | all NO_TRADE | n/a |
| WITHOUT refusal bias | **6 / 6** | `[3, 0, 0, 0, 0, 0]` | **vetoed 6/6** |

**This overturns the reading above.** The 22/22 refusal rate measured an
instruction I wrote, not the model's assessment of the trades. "It refuses
regardless" was wrong.

The corrected finding is narrower but still decisive: given a neutral prompt the
proposer **agrees with rank 1 in five of six trials**. It deviated once, to
rank 3, and whether that deviation was an improvement cannot be determined
without forward returns. As a selector it is close to inert - it mostly
reproduces the deterministic ranking at the cost of an API call and several
seconds.

## The pipeline currently cannot produce a trade

The two arms together show the system is blocked at every configuration:

* with the biased prompt, the **proposer** refuses everything;
* with a neutral prompt, the **adversary** vetoes everything (6/6).

Whichever way it is configured, no order is reachable. This is the single most
important operational finding in this document, and neither dry runs nor unit
tests surfaced it - only running the two arms side by side did.

## What follows from this

The proposer adds almost nothing as a selector: five of six picks reproduce
rank 1. Dropping it and taking rank 1 deterministically removes one model, one
failure mode and roughly half the cycle latency, at a cost this evaluation
cannot distinguish from zero.

The adversary is the harder question. It caught an inverted IV/RV comparison in
a live cycle - a genuine save - but it also vetoes 6 out of 6 otherwise-valid
candidates, which makes it indistinguishable from a blanket refusal in exactly
the way the proposer was. It needs the same ablation before it can be trusted as
a filter rather than a brake.
