"""Behavior specifications keeping operator commands aligned with production."""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookbound import __main__ as cli  # noqa: E402
from bookbound.book import Book  # noqa: E402
from bookbound.config import Settings  # noqa: E402
from bookbound.cycle import CandidateFunnel  # noqa: E402
from bookbound.execute import OrderResult  # noqa: E402
from bookbound.exits import ExitLeg, ExitPlan  # noqa: E402


AS_OF = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)
SETTINGS = Settings(api_key="k", secret_key="s", universe=("SPY",))


def book() -> Book:
    return Book(
        100_000, 100_000, 100_000, 400_000, 100_000, [], [],
        spots={"SPY": 102},
    )


class TestProductionEquivalentScan(unittest.TestCase):
    def test_given_scan_then_same_as_of_context_and_funnel_are_used(self):
        context = {
            "underlyings": {
                "SPY": {"available": True, "realised_vol_annualised": .18}
            }
        }
        with (
            patch.object(cli, "market_clock", return_value={
                "is_open": True, "timestamp": AS_OF.isoformat(),
            }),
            patch.object(cli, "_chains", return_value=([], {}, {"SPY": 102})) as chains,
            patch.object(cli, "load_book", return_value=book()),
            patch.object(cli.market_context, "build", return_value=context) as build,
            patch.object(
                cli, "construct_candidate_funnel",
                return_value=CandidateFunnel((), (), ()),
            ) as funnel,
            patch("builtins.print"),
        ):
            status = cli.cmd_scan(SETTINGS)

        self.assertEqual(status, 0)
        scan_as_of = chains.call_args.kwargs["as_of"]
        self.assertEqual(scan_as_of, AS_OF)
        self.assertIs(build.call_args.kwargs["as_of"], scan_as_of)
        self.assertEqual(funnel.call_args.args[2], {"SPY": .18})
        self.assertIs(funnel.call_args.kwargs["as_of"], scan_as_of)


class TestAtomicOperatorFlatten(unittest.TestCase):
    def test_given_vertical_then_flatten_submits_one_structure_close(self):
        plan = ExitPlan(
            "SPY", date(2026, 9, 25), "C",
            (
                ExitLeg("SPY260925C00100000", 1, "sell", "sell_to_close"),
                ExitLeg("SPY260925C00105000", 1, "buy", "buy_to_close"),
            ),
            "flatten", "operator flatten",
        )
        result = OrderResult(True, None, "flatten", "DRY_RUN", {})
        with (
            patch.object(cli, "load_book", return_value=book()),
            patch.object(cli, "flatten_structure_plans", return_value=[plan]),
            patch("bookbound.execute.close_structure", return_value=result) as close,
            patch.object(cli.AuditLog, "write", return_value={}),
            patch("builtins.print"),
        ):
            status = cli.cmd_flatten(SETTINGS, live=False)

        self.assertEqual(status, 0)
        close.assert_called_once_with(plan, dry_run=True, settings=SETTINGS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
