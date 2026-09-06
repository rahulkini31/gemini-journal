"""Behavior contracts for unattended lifecycle handling."""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookbound import runner  # noqa: E402
from bookbound.book import Book  # noqa: E402
from bookbound.config import Settings  # noqa: E402
from bookbound.execute import OrderResult  # noqa: E402
from bookbound.exits import ExitLeg, ExitPlan  # noqa: E402


SETTINGS = Settings(api_key="k", secret_key="s")


class TestRunnerAtomicFlatten(unittest.TestCase):
    def test_given_deadline_vertical_then_runner_uses_one_atomic_close(self):
        plan = ExitPlan(
            "SPY", date(2026, 9, 25), "C",
            (
                ExitLeg("SPY260925C00100000", 1, "sell", "sell_to_close"),
                ExitLeg("SPY260925C00105000", 1, "buy", "buy_to_close"),
            ),
            "flatten", "deadline",
        )
        book = Book(100_000, 100_000, 100_000, 400_000, 100_000, [], [])
        audit = Mock()
        result = OrderResult(True, None, "close", "DRY_RUN", {})

        with (
            patch("bookbound.book.load_book", return_value=book),
            patch.object(runner, "flatten_structure_plans", return_value=[plan]),
            patch.object(runner.execute, "close_structure", return_value=result) as close,
            patch.object(runner, "print"),
        ):
            ok = runner._flatten(SETTINGS, audit, live=False)

        self.assertTrue(ok)
        close.assert_called_once_with(plan, dry_run=True, settings=SETTINGS)

    def test_given_structure_exit_report_then_console_handles_symbol_list(self):
        report = SimpleNamespace(
            outcome="EXIT_REQUIRED", equity=100_000,
            book_greeks={}, candidates=0,
            exits=[{"symbols": ["LONG", "SHORT"], "reason": "stop_loss",
                    "detail": "spread stop"}],
            detail="", health={"healthy": True},
        )

        with patch.object(runner, "print") as output:
            runner._print_cycle(1, report)

        rendered = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn("LONG,SHORT", rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
