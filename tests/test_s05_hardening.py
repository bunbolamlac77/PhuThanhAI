"""Regression tests for S05 batch and sampling correctness."""

from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from phuthanhai.analyzer.vision_service import model_identity
from phuthanhai.library.batch_service import aggregate_run_status
from phuthanhai.library.service import sampling_satisfies


class BatchAggregateTests(unittest.TestCase):
    def test_terminal_coverage_matrix(self) -> None:
        self.assertEqual(aggregate_run_status("running", {"complete": 54}), "complete")
        self.assertEqual(aggregate_run_status("running", {"complete": 50, "partial": 4}), "partial")
        self.assertEqual(aggregate_run_status("running", {"complete": 50, "failed": 4}), "partial")
        self.assertEqual(aggregate_run_status("running", {"failed": 4}), "failed")

    def test_active_and_cancelled_runs_never_report_complete(self) -> None:
        self.assertEqual(aggregate_run_status("running", {"complete": 50, "queued": 4}), "running")
        self.assertEqual(
            aggregate_run_status("waiting_for_memory", {"complete": 50, "queued": 4}),
            "waiting_for_memory",
        )
        self.assertEqual(aggregate_run_status("cancelled", {"complete": 54}), "cancelled")


class SamplingCompatibilityTests(unittest.TestCase):
    @staticmethod
    def manifest(*, profile: str, observed_gap: int, coverage: str = "sufficient") -> dict:
        return {
            "manifest_version": "s02-sampling-1.0.0",
            "profile": profile,
            "coverage_status": coverage,
            "observed_max_gap_ms": observed_gap,
            "coverage": {"status": coverage, "reasons": []},
            "settings": {"max_edge": 896, "quality": 3, "max_scene_ms": 120_000},
        }

    def test_weaker_manifest_does_not_satisfy_stronger_request(self) -> None:
        quick = self.manifest(profile="quick", observed_gap=12_000)
        self.assertTrue(sampling_satisfies("quick", quick))
        self.assertFalse(sampling_satisfies("detailed", quick))
        self.assertFalse(sampling_satisfies("thorough", quick))

    def test_verified_dense_coverage_can_satisfy_lighter_profiles(self) -> None:
        dense = self.manifest(profile="thorough", observed_gap=3_000)
        self.assertTrue(sampling_satisfies("thorough", dense))
        self.assertTrue(sampling_satisfies("detailed", dense))
        self.assertTrue(sampling_satisfies("quick", dense))

    def test_label_alone_is_not_evidence_and_insufficient_coverage_is_rejected(self) -> None:
        mislabeled = self.manifest(profile="thorough", observed_gap=12_000)
        insufficient = self.manifest(profile="thorough", observed_gap=3_000, coverage="insufficient")
        self.assertFalse(sampling_satisfies("thorough", mislabeled))
        self.assertFalse(sampling_satisfies("quick", insufficient))


class ModelIdentityTests(unittest.TestCase):
    def test_same_size_weight_replacement_changes_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp)
            (model / "config.json").write_text("{}", encoding="utf-8")
            weights = model / "model.safetensors"
            weights.write_bytes(b"aaaa")
            before = model_identity(model)
            weights.write_bytes(b"bbbb")
            after = model_identity(model)
        self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()
