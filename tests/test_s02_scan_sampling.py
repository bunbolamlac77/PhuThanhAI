"""S02 scene-scan and sampling-plan tests: cuts, coverage, quality flags, profiles."""

from __future__ import annotations

import unittest

from phuthanhai.analyzer.sampling import (
    PROFILE_GAPS_MS,
    build_plan,
    observed_max_gap,
)
from phuthanhai.analyzer.scene_scan import ScanError, scan_scenes
from phuthanhai.analyzer.timeline import read_timeline
from tests.media_fixtures import get


def scan(name: str, **kwargs):
    path = get(name)
    timeline = read_timeline(path)
    return path, timeline, scan_scenes(path, timeline, **kwargs)


class CutDetectionTests(unittest.TestCase):
    def test_hard_cuts_are_detected_exactly(self) -> None:
        _, timeline, result = scan("cuts")
        self.assertEqual(result.cut_times_ms, [1000, 2000, 3000])
        self.assertEqual(len(result.segments), 4)
        self.assertEqual(result.segments[0].start_ms, 0)
        self.assertEqual(result.segments[-1].end_ms, timeline.duration_ms)
        self.assertTrue(result.segments[1].cut_in)

    def test_rapid_cuts_are_not_merged_away(self) -> None:
        _, timeline, result = scan("rapid")
        self.assertGreaterEqual(len(result.cut_times_ms), 5)
        for previous, current in zip(result.segments, result.segments[1:]):
            self.assertEqual(previous.end_ms, current.start_ms)
        self.assertEqual(result.segments[-1].end_ms, timeline.duration_ms)

    def test_no_cut_long_video_is_subdivided_and_covered(self) -> None:
        _, timeline, result = scan("long_nocut")
        self.assertEqual(result.cut_times_ms, [])
        self.assertGreaterEqual(len(result.segments), 2, "40 s must be subdivided")
        self.assertEqual(result.segments[0].start_ms, 0)
        self.assertEqual(result.segments[-1].end_ms, timeline.duration_ms)
        for previous, current in zip(result.segments, result.segments[1:]):
            self.assertEqual(previous.end_ms, current.start_ms)

    def test_scan_rejects_empty_metadata(self) -> None:
        path = get("cfr")
        timeline = read_timeline(path)
        with self.assertRaises(ScanError) as ctx:
            scan_scenes(path, timeline, timeout_s=0.001)
        self.assertEqual(ctx.exception.code, "FFMPEG_TIMEOUT")


class QualityFlagTests(unittest.TestCase):
    def test_dark_footage_is_flagged(self) -> None:
        _, _, result = scan("dark")
        self.assertGreater(result.segments[0].dark_ratio, 0.9)
        self.assertLess(result.segments[0].mean_luma, 20)

    def test_blurred_footage_is_flagged(self) -> None:
        _, _, result = scan("blurred")
        self.assertGreater(result.segments[0].blur_ratio, 0.9)

    def test_short_motion_event_raises_motion_cue(self) -> None:
        _, _, result = scan("motion_short")
        self.assertGreaterEqual(max(segment.max_motion for segment in result.segments), 8.0)

    def test_sharp_footage_is_not_blurred(self) -> None:
        _, _, result = scan("cfr")
        self.assertLess(result.segments[0].blur_ratio, 0.1)
        self.assertLess(result.segments[0].dark_ratio, 0.1)


class PlanTests(unittest.TestCase):
    def test_profiles_produce_increasing_coverage(self) -> None:
        _, timeline, result = scan("long_nocut")
        counts = {}
        for profile in ("quick", "detailed", "thorough"):
            plan = build_plan(timeline, result.segments, profile, metrics=result.frame_metrics)
            counts[profile] = len(plan.targets)
            self.assertLessEqual(plan.expected_max_gap_ms, PROFILE_GAPS_MS[profile])
            self.assertGreater(plan.expected_max_gap_ms, 0)
        self.assertLess(counts["quick"], counts["detailed"])
        self.assertLess(counts["detailed"], counts["thorough"])

    def test_plan_is_deterministic(self) -> None:
        _, timeline, result = scan("cuts")
        first = build_plan(timeline, result.segments, "detailed", metrics=result.frame_metrics)
        second = build_plan(timeline, result.segments, "detailed", metrics=result.frame_metrics)
        self.assertEqual([t.to_dict() for t in first.targets], [t.to_dict() for t in second.targets])
        self.assertEqual(first.expected_max_gap_ms, second.expected_max_gap_ms)

    def test_every_segment_has_a_frame(self) -> None:
        _, timeline, result = scan("rapid")
        plan = build_plan(timeline, result.segments, "quick", metrics=result.frame_metrics)
        covered = {target.scene_id for target in plan.targets}
        self.assertEqual(covered, {segment.scene_id for segment in result.segments})

    def test_motion_refinement_frames_are_added(self) -> None:
        _, timeline, result = scan("motion_short")
        plan = build_plan(timeline, result.segments, "detailed", metrics=result.frame_metrics)
        self.assertTrue(any(target.kind == "refine" for target in plan.targets))

    def test_frame_ids_are_sequential_and_unique(self) -> None:
        _, timeline, result = scan("cuts")
        plan = build_plan(timeline, result.segments, "detailed", metrics=result.frame_metrics)
        ids = [target.frame_id for target in plan.targets]
        self.assertEqual(ids, [f"F{index:04d}" for index in range(1, len(ids) + 1)])
        self.assertEqual(len(ids), len(set(ids)))

    def test_unknown_profile_is_rejected(self) -> None:
        _, timeline, result = scan("cfr")
        from phuthanhai.analyzer.sampling import SamplingPlanError

        with self.assertRaises(SamplingPlanError) as ctx:
            build_plan(timeline, result.segments, "cinematic")
        self.assertEqual(ctx.exception.code, "SAMPLING_PROFILE_INVALID")


class ObservedGapTests(unittest.TestCase):
    def test_gap_includes_both_boundaries(self) -> None:
        self.assertEqual(observed_max_gap([1000, 2000], 5000), 3000)
        self.assertEqual(observed_max_gap([], 5000), 5000)
        self.assertEqual(observed_max_gap([0, 5000], 5000), 5000)


if __name__ == "__main__":
    unittest.main()
