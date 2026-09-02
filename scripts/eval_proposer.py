#!/usr/bin/env python3
"""Does the proposer beat 'always take rank 1'?

The LLM chooses from a shortlist that deterministic code has already built,
priced, filtered and RANKED. If it always picks rank 1 it adds nothing but
latency and cost. If it picks elsewhere, it is overriding a quantitative
ordering with narrative, and the burden is on it to be right.

WHAT THIS CAN AND CANNOT SHOW
-----------------------------
It CANNOT show that either policy makes money. That needs forward returns over
many trades, which do not exist here.

It CAN show, on real shortlists:
  * how often the proposer deviates from rank 1, and in which direction on the
    ranker's own metric;
  * how often it refuses outright;
  * how often the adversary then vetoes it;
  * whether its picks are reproducible run to run.

A proposer that deviates and lands systematically WORSE on the ranking metric is
destroying value relative to the baseline. One that never deviates is inert.
Neither result proves profitability; both are decision-relevant.

    PYTHONPATH=src python3 scripts/eval_proposer.py --trials 12
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookbound.agents import decide  # noqa: E402
from bookbound.book import load_book  # noqa: E402
from bookbound.config import load_settings  # noqa: E402
from bookbound.context import build as build_ctx  # noqa: E402
from bookbound.market import option_chain, underlying_price  # noqa: E402
from bookbound.risk import evaluate  # noqa: E402
from bookbound.structures import (  # noqa: E402
    build_vertical_credit_spreads,
    build_vertical_debit_spreads,
    execution_cost,
    expected_value,
    probability_of_profit,
    quality_score,
)


def build_shortlist(settings, top_n=10):
    contracts, quotes, spots = [], {}, {}
    for symbol in settings.universe:
        spot = underlying_price(settings, symbol)
        if spot is None:
            continue
        spots[symbol] = spot
        chain = option_chain(settings, symbol, spot=spot)
        contracts.extend(chain)
        quotes.update({c.symbol: c for c in chain})

    ctx = build_ctx(settings, spots)
    vols = {
        s: n.get("realised_vol_annualised") or 0.0
        for s, n in ctx["underlyings"].items() if n.get("available")
    }
    book = load_book(settings, quotes)
    cap = book.equity * settings.risk.max_loss_per_trade_pct

    built = []
    for kind in ("C", "P"):
        built.extend(build_vertical_debit_spreads(
            contracts, settings, kind=kind, max_loss_cap=cap, realised_vols=vols))
        built.extend(build_vertical_credit_spreads(
            contracts, settings, kind=kind, max_loss_cap=cap, realised_vols=vols))
    admissible = [c for c in built if evaluate(c, book, settings).admitted]
    admissible.sort(key=lambda c: -quality_score(c))

    context = {
        "portfolio": {"equity": round(book.equity, 2),
                      "book_greeks": book.greeks.as_dict()},
        **ctx,
    }
    return admissible[:top_n], context


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=12)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--out", default="state/proposer_eval.json")
    args = parser.parse_args()

    settings = load_settings()
    shortlist, context = build_shortlist(settings, args.top_n)
    if len(shortlist) < 3:
        print(f"only {len(shortlist)} candidates; not enough to evaluate")
        return 1

    baseline = shortlist[0]
    print(f"shortlist of {len(shortlist)}; rank-1 baseline = {baseline.key}")
    print(f"  baseline quality={quality_score(baseline):.4f} "
          f"PoP={probability_of_profit(baseline):.3f} "
          f"exec_cost=${execution_cost(baseline):.2f} "
          f"maxloss=${baseline.max_loss:.0f}\n")

    by_key = {c.key: c for c in shortlist}
    ranks = {c.key: i for i, c in enumerate(shortlist)}
    trials = []

    for i in range(1, args.trials + 1):
        started = time.monotonic()
        decision = decide(shortlist, context, settings)
        elapsed = time.monotonic() - started
        picked = decision.structure
        record = {
            "trial": i,
            "outcome": decision.outcome,
            "seconds": round(elapsed, 1),
            "pick": picked.key if picked else None,
            "pick_rank": ranks.get(picked.key) if picked else None,
            "proposer_choice": decision.proposer.get("choice"),
            "proposer_rank": ranks.get(str(decision.proposer.get("choice"))),
            "confidence": decision.proposer.get("confidence"),
            "thesis": (decision.proposer.get("thesis") or "")[:160],
            "adversary": (decision.adversary.get("reason") or "")[:160],
        }
        chosen = by_key.get(str(decision.proposer.get("choice")))
        if chosen is not None:
            record["quality_delta_vs_rank1"] = round(
                quality_score(chosen) - quality_score(baseline), 4)
            record["ev_delta_vs_rank1"] = round(
                expected_value(chosen) - expected_value(baseline), 2)
        trials.append(record)
        print(f"  trial {i:>2}: {record['outcome']:<10} "
              f"proposer_rank={record['proposer_rank']} "
              f"qual_delta={record.get('quality_delta_vs_rank1')} "
              f"({record['seconds']}s)")

    picked_ranks = [t["proposer_rank"] for t in trials if t["proposer_rank"] is not None]
    deltas = [t["quality_delta_vs_rank1"] for t in trials
              if "quality_delta_vs_rank1" in t]
    outcomes = {}
    for t in trials:
        outcomes[t["outcome"]] = outcomes.get(t["outcome"], 0) + 1

    summary = {
        "trials": len(trials),
        "shortlist_size": len(shortlist),
        "outcomes": outcomes,
        "selected_a_candidate": len(picked_ranks),
        "picked_rank_1": sum(1 for r in picked_ranks if r == 0),
        "deviated_from_rank_1": sum(1 for r in picked_ranks if r != 0),
        "mean_picked_rank": round(statistics.fmean(picked_ranks), 2) if picked_ranks else None,
        "mean_quality_delta": round(statistics.fmean(deltas), 4) if deltas else None,
        "worse_than_rank_1": sum(1 for d in deltas if d < 0),
        "better_than_rank_1": sum(1 for d in deltas if d > 0),
        "distinct_picks": len({t["pick"] or t["proposer_choice"] for t in trials}),
    }

    print("\n=== summary ===")
    print(json.dumps(summary, indent=2))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"summary": summary, "baseline": baseline.summary(), "trials": trials},
        indent=2, default=str))
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
