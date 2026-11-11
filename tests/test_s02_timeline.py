"""S02 timeline/PTS tests: CFR, VFR, reordering, rotation, HDR, ProRes, negative origin."""

from __future__ import annotations

import dataclasses
import unittest

from phuthanhai.analyzer.timeline import (
    EXTRACTOR_VERSION,
    Timeline,
    TimelineError,
    extract_verified_frame,
    read_timeline,
)
from tests.media_fixtures import get


class TimelineShapeTests(unittest.TestCase):
    def test_cfr_is_not_vfr(self) -> None:
        timeline = read_timeline(get("cfr"))
        self.assertFalse(timeline.vfr_detected)
        self.assertGreaterEqual(timeline.vfr_evidence.get("packet_count", 0), 5)
        self.assertEqual(timeline.first_video_pts_ms, 0)
        self.assertEqual(timeline.fps_nominal, timeline.fps_nominal)  # present
        self.assertGreater(timeline.duration_ms, 2500)

    def test_vfr_is_detected_with_evidence(self) -> None:
        timeline = read_timeline(get("vfr"))
        self.assertTrue(timeline.vfr_detected)
        evidence = timeline.vfr_evidence
        self.assertGreater(evidence["max_over_median"], 1.25)
        self.assertGreater(evidence["stdev_s"], 0.0)

    def test_nonzero_start_is_preserved(self) -> None:
        timeline = read_timeline(get("nonzero"))
        self.assertGreaterEqual(timeline.first_video_pts_ms, 1900)
        self.assertLessEqual(timeline.first_video_pts_ms, 2100)

    def test_rotation_changes_display_dimensions(self) -> None:
        timeline = read_timeline(get("rotation"))
        self.assertEqual(timeline.rotation, 90)
        self.assertEqual((timeline.width, timeline.height), (320, 240))
        self.assertEqual((timeline.display_width, timeline.display_height), (240, 320))

    def test_hdr_transfer_is_recognised(self) -> None:
        timeline = read_timeline(get("hdr"))
        self.assertEqual(timeline.color_transfer, "arib-std-b67")
        self.assertTrue(timeline.is_hdr)

    def test_prores_is_probed(self) -> None:
        timeline = read_timeline(get("prores"))
        self.assertEqual(timeline.codec, "prores")


class VerifiedExtractionTests(unittest.TestCase):
    def test_cfr_frame_pts_is_verified(self) -> None:
        source = get("cfr")
        timeline = read_timeline(source)
        frame = extract_verified_frame(source, timeline, 500, timeline_source_path(source, "cfr_500"))
        self.assertFalse(frame.pts_mismatch)
        self.assertLessEqual(abs(frame.offset_ms), timeline.frame_duration_ms)
        self.assertGreater(frame.decoded_pts_ms, 0)
        self.assertEqual(frame.normalized_pts_ms - timeline.first_video_pts_ms, frame.decoded_pts_ms)
        self.assertEqual(frame.extractor_version, EXTRACTOR_VERSION)
        self.assertEqual(len(frame.sha256), 64)

    def test_nonzero_start_normalizes_to_request(self) -> None:
        source = get("nonzero")
        timeline = read_timeline(source)
        frame = extract_verified_frame(source, timeline, 500, timeline_source_path(source, "nz_500"))
        self.assertLessEqual(abs(frame.offset_ms), timeline.frame_duration_ms)
        self.assertFalse(frame.pts_mismatch)
        # absolute PTS still carries the +2 s offset
        self.assertGreaterEqual(frame.decoded_pts_ms, 2400)

    def test_bframes_keep_monotonic_normalized_pts(self) -> None:
        source = get("bframes")
        timeline = read_timeline(source)
        times = []
        for requested in (300, 700, 1100, 1500):
            frame = extract_verified_frame(
                source, timeline, requested, timeline_source_path(source, f"bf_{requested}")
            )
            self.assertFalse(frame.pts_mismatch)
            times.append(frame.normalized_pts_ms)
        self.assertEqual(times, sorted(times))
        self.assertEqual(len(set(times)), len(times))

    def test_rotation_frame_is_portrait(self) -> None:
        source = get("rotation")
        timeline = read_timeline(source)
        frame = extract_verified_frame(source, timeline, 500, timeline_source_path(source, "rot_500"))
        self.assertGreater(frame.height, frame.width)
        self.assertEqual((frame.width, frame.height), (240, 320))

    def test_prores_frame_extracts(self) -> None:
        source = get("prores")
        timeline = read_timeline(source)
        frame = extract_verified_frame(source, timeline, 100, timeline_source_path(source, "prores_100"))
        self.assertFalse(frame.pts_mismatch)
        self.assertGreater(frame.size_bytes, 1000)

    def test_request_outside_timeline_is_rejected(self) -> None:
        source = get("cfr")
        timeline = read_timeline(source)
        with self.assertRaises(TimelineError) as ctx:
            extract_verified_frame(source, timeline, timeline.duration_ms + 10, timeline_source_path(source, "bad"))
        self.assertEqual(ctx.exception.code, "SAMPLING_PLAN_INVALID")

    def test_extraction_timeout_is_typed(self) -> None:
        source = get("cfr")
        timeline = read_timeline(source)
        with self.assertRaises(TimelineError) as ctx:
            extract_verified_frame(
                source, timeline, 100, timeline_source_path(source, "timeout"), timeout_s=0.001
            )
        self.assertEqual(ctx.exception.code, "FFMPEG_TIMEOUT")


class NegativeOriginTests(unittest.TestCase):
    def test_negative_first_pts_normalizes_to_zero(self) -> None:
        base = read_timeline(get("cfr"))
        synthetic: Timeline = dataclasses.replace(base, first_video_pts_ms=-1200)
        self.assertEqual(synthetic.normalize_ms(-1200), 0)
        self.assertEqual(synthetic.normalize_ms(0), 1200)
        self.assertEqual(synthetic.normalize_ms(-200), 1000)


def timeline_source_path(source, name: str):
    from tests.media_fixtures import fixture_dir

    return fixture_dir() / f"{name}.jpg"


if __name__ == "__main__":
    unittest.main()
