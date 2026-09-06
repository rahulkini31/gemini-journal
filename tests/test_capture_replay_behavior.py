"""Behavior specifications for immutable decision capture and offline replay.

These scenarios deliberately avoid the live cycle orchestration.  They define
the persistence boundary which the cycle can later feed through the existing
``raw_sink`` hooks.
"""
from __future__ import annotations

import math
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookbound.capture import (  # noqa: E402
    CaptureIntegrityError,
    DecisionCapture,
    load_capture,
)
from bookbound.config import RiskBudget, Settings  # noqa: E402


AS_OF = datetime(2026, 9, 2, 14, 30, 1, 123456, tzinfo=timezone.utc)
SETTINGS = Settings(
    api_key="never-write-this-api-key",
    secret_key="never-write-this-secret",
    featherless_key="never-write-this-llm-key",
    paper=True,
    universe=("SPY", "QQQ"),
    risk=RiskBudget(shortlist_size=7),
    proposer_model="proposer/model@revision",
    adversary_model="adversary/model@revision",
)


def _record_complete_capture(root: Path, cycle_id: str = "cycle-001"):
    capture = DecisionCapture(
        root,
        cycle_id=cycle_id,
        as_of=AS_OF,
        settings=SETTINGS,
        model_provenance={
            "proposer": {
                "model": SETTINGS.proposer_model,
                "prompt_sha256": "abc123",
            },
            "adversary": {
                "model": SETTINGS.adversary_model,
                "prompt_sha256": "def456",
            },
        },
    )
    # The unusable row is intentional: capture happens before parsing/filtering.
    capture.record_chain_page(
        "SPY",
        {
            "SPY260918C00760000": {
                "impliedVolatility": 0.2,
                "latestQuote": {"bp": 1.0, "ap": 1.1, "t": "2026-09-02T14:30:00Z"},
            },
            "SPY260918C00761000": {
                "impliedVolatility": None,
                "greeks": None,
            },
        },
    )
    capture.record_chain_page("SPY", {"SPY260918P00760000": {"greeks": {}}})
    capture.record_option_snapshots(
        "selected",
        {"SPY260918C00760000": {"latestQuote": {"bp": 1.02, "ap": 1.08}}},
    )
    capture.record_underlier_snapshot(
        "SPY",
        {
            "latestTrade": {"p": 760.25, "t": "2026-09-02T14:30:00Z"},
            "dailyBar": {"c": 759.0, "t": "2026-09-02T04:00:00Z"},
        },
    )
    capture.record_context(
        {
            "as_of": AS_OF,
            "spot_prices": {"SPY": 760.25},
            "headlines": [],
        }
    )
    capture.record_book({"equity": 100_000.0, "positions": []})
    capture.record_working_orders([{"id": "order-1", "status": "new"}])
    return capture.seal()


class TestImmutableCaptureBehavior(unittest.TestCase):
    def test_given_complete_cycle_inputs_when_sealed_then_raw_data_and_provenance_replay(self):
        """Given one cycle, sealing preserves raw evidence and excludes secrets."""
        with tempfile.TemporaryDirectory() as directory:
            reference = _record_complete_capture(Path(directory))

            replay = load_capture(reference.path)

            self.assertEqual(replay.cycle_id, "cycle-001")
            self.assertEqual(replay.as_of, AS_OF)
            self.assertEqual(len(replay.chain_pages), 2)
            self.assertIn(
                "SPY260918C00761000",
                replay.chain_pages[0]["snapshots"],
                "unusable market rows are still audit evidence",
            )
            self.assertEqual(
                replay.option_snapshot_batches[0]["snapshots"]
                ["SPY260918C00760000"]["latestQuote"]["bp"],
                1.02,
            )
            self.assertEqual(
                replay.underlier_snapshots["SPY"]["latestTrade"]["p"], 760.25
            )
            self.assertEqual(replay.context["as_of"], AS_OF.isoformat())
            self.assertEqual(replay.book["equity"], 100_000.0)
            self.assertEqual(replay.working_orders[0]["id"], "order-1")
            self.assertEqual(replay.settings["risk"]["shortlist_size"], 7)
            self.assertEqual(
                replay.models["proposer"]["prompt_sha256"], "abc123"
            )

            persisted = "".join(
                path.read_text(encoding="utf-8")
                for path in reference.path.rglob("*.json")
            )
            self.assertNotIn(SETTINGS.api_key, persisted)
            self.assertNotIn(SETTINGS.secret_key, persisted)
            self.assertNotIn(SETTINGS.featherless_key, persisted)

    def test_given_observation_and_pre_submit_spot_when_recorded_then_both_replay_in_order(self):
        """Repeated symbols are evidence from distinct phases, not overwrites."""
        with tempfile.TemporaryDirectory() as directory:
            capture = DecisionCapture(
                Path(directory),
                cycle_id="two-phase-underlier",
                as_of=AS_OF,
                settings=SETTINGS,
                model_provenance={},
            )
            capture.record_underlier_snapshot(
                "SPY", {"latestTrade": {"p": 760.00, "t": "phase-one"}}
            )
            capture.record_underlier_snapshot(
                "SPY", {"latestTrade": {"p": 760.75, "t": "pre-submit"}}
            )

            replay = load_capture(capture.seal().path)
            observed: list[tuple[str, dict]] = []
            replay.replay_underlier_snapshots(
                lambda symbol, snapshot: observed.append((symbol, snapshot))
            )

            self.assertEqual(
                [batch["sequence"] for batch in replay.underlier_snapshot_batches],
                [0, 1],
            )
            self.assertEqual(
                [snapshot["latestTrade"]["p"] for _, snapshot in observed],
                [760.00, 760.75],
            )
            self.assertEqual(
                replay.underlier_snapshots["SPY"]["latestTrade"]["p"],
                760.75,
                "the compatibility view returns the most recent observation",
            )

    def test_given_frozen_inputs_when_written_twice_then_bundle_bytes_are_stable(self):
        """Given a frozen as-of and ID, canonical JSON is byte reproducible."""
        with (
            tempfile.TemporaryDirectory() as left,
            tempfile.TemporaryDirectory() as right,
        ):
            first = _record_complete_capture(Path(left))
            second = _record_complete_capture(Path(right))

            first_files = {
                path.relative_to(first.path): path.read_bytes()
                for path in first.path.rglob("*")
                if path.is_file()
            }
            second_files = {
                path.relative_to(second.path): path.read_bytes()
                for path in second.path.rglob("*")
                if path.is_file()
            }
            self.assertEqual(first.bundle_sha256, second.bundle_sha256)
            self.assertEqual(first_files, second_files)

    def test_given_a_sealed_cycle_id_when_reused_then_existing_evidence_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _record_complete_capture(root)
            before = (first.path / "manifest.json").read_bytes()

            with self.assertRaises(FileExistsError):
                _record_complete_capture(root)

            self.assertEqual((first.path / "manifest.json").read_bytes(), before)

    def test_given_invalid_non_json_evidence_when_seal_fails_then_no_partial_cycle_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = DecisionCapture(
                root,
                cycle_id="will-not-publish",
                as_of=AS_OF,
                settings=SETTINGS,
                model_provenance={},
            )
            capture.record_context({"invalid": math.nan})

            with self.assertRaises(ValueError):
                capture.seal()

            self.assertFalse((root / "will-not-publish").exists())


class TestReplayIntegrityBehavior(unittest.TestCase):
    def test_given_a_modified_payload_when_loaded_then_replay_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            reference = _record_complete_capture(Path(directory))
            context_path = reference.path / "decision" / "context.json"
            context_path.chmod(0o644)
            context_path.write_text('{"spot_prices":{"SPY":999.0}}\n', encoding="utf-8")

            with self.assertRaisesRegex(
                CaptureIntegrityError, "(?:byte length|hash) mismatch"
            ):
                load_capture(reference.path)

    def test_given_an_unmanifested_file_when_loaded_then_replay_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            reference = _record_complete_capture(Path(directory))
            reference.path.chmod(0o755)
            (reference.path / "injected.json").write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(CaptureIntegrityError, "unexpected file"):
                load_capture(reference.path)

    def test_given_a_verified_capture_then_raw_chain_hooks_can_be_replayed_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            reference = _record_complete_capture(Path(directory))
            replay = load_capture(reference.path)
            observed: list[tuple[str, dict]] = []

            replay.replay_chain_pages(
                lambda underlying, snapshots: observed.append((underlying, snapshots))
            )

            self.assertEqual([item[0] for item in observed], ["SPY", "SPY"])
            self.assertIn("SPY260918P00760000", observed[1][1])


if __name__ == "__main__":
    unittest.main()
