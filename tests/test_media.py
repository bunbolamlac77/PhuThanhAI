"""Media integration tests: real ffprobe probing and real frame extraction."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from phuthanhai.analyzer.probe import ProbeError, probe_source, tool_version
from phuthanhai.analyzer.sampler import FrameExtractionError, extract_frame
from tests.helpers import make_test_video


class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.video = make_test_video(Path(cls._tmp.name) / "clip.mp4", seconds=1.0)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_tools_are_present(self) -> None:
        self.assertIsNotNone(tool_version("ffmpeg"))
        self.assertIsNotNone(tool_version("ffprobe"))

    def test_probe_reads_real_metadata(self) -> None:
        info = probe_source(self.video)
        self.assertEqual(info.codec, "h264")
        self.assertEqual((info.width, info.height), (320, 240))
        self.assertGreater(info.duration_ms, 0)
        self.assertFalse(info.has_audio)
        self.assertEqual(info.rotation, 0)
        block = info.as_source_block()
        self.assertEqual(block["time_origin"], "first_presented_video_frame")
        self.assertRegex(block["fps_nominal"] or "", r"^\d+/\d+$")

    def test_missing_source_raises_codec_error(self) -> None:
        with self.assertRaises(ProbeError) as ctx:
            probe_source(Path(self._tmp.name) / "absent.mp4")
        self.assertEqual(ctx.exception.code, "SOURCE_MISSING")


class FrameExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp_path = Path(cls._tmp.name)
        cls.video = make_test_video(cls.tmp_path / "clip.mp4", seconds=1.0, size="640x480")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_extracts_a_real_frame(self) -> None:
        output = self.tmp_path / "frame_200.jpg"
        frame = extract_frame(self.video, 200, output, max_edge=896)
        self.assertTrue(output.is_file())
        self.assertGreater(frame.size_bytes, 0)
        self.assertEqual(len(frame.sha256), 64)
        self.assertEqual((frame.width, frame.height), (640, 480))
        self.assertEqual(frame.time_ms, 200)

    def test_downscales_above_max_edge(self) -> None:
        output = self.tmp_path / "frame_small.jpg"
        frame = extract_frame(self.video, 400, output, max_edge=160)
        self.assertLessEqual(max(frame.width, frame.height), 160)
        self.assertEqual(frame.width % 2, 0)
        self.assertEqual(frame.height % 2, 0)

    def test_portrait_source_keeps_orientation(self) -> None:
        """A portrait source must stay portrait.

        Regression guard: sizing a rotated clip from ffprobe's stored width/height
        produced a squashed landscape frame on real DJI footage, which the VLM then
        described as if it were the truth.
        """

        portrait = make_test_video(self.tmp_path / "portrait.mp4", size="320x240", portrait=True)
        frame = extract_frame(portrait, 200, self.tmp_path / "portrait.jpg", max_edge=896)
        self.assertGreater(frame.height, frame.width)
        self.assertEqual((frame.width, frame.height), (240, 320))

    def test_longest_edge_cap_applies_to_portrait_sources(self) -> None:
        portrait = make_test_video(self.tmp_path / "portrait2.mp4", size="320x240", portrait=True)
        frame = extract_frame(portrait, 200, self.tmp_path / "portrait_small.jpg", max_edge=160)
        self.assertEqual(max(frame.width, frame.height), 160)
        self.assertEqual((frame.width, frame.height), (120, 160))

    def test_landscape_frame_records_zero_rotation(self) -> None:
        output = self.tmp_path / "frame_rot.jpg"
        frame = extract_frame(self.video, 100, output, max_edge=896)
        self.assertEqual(frame.source_rotation, 0)

    def test_out_of_range_time_is_rejected(self) -> None:
        with self.assertRaises(FrameExtractionError):
            extract_frame(self.video, 10_000, self.tmp_path / "nope.jpg")

    def test_negative_time_is_rejected(self) -> None:
        with self.assertRaises(FrameExtractionError):
            extract_frame(self.video, -1, self.tmp_path / "nope2.jpg")


if __name__ == "__main__":
    unittest.main()
