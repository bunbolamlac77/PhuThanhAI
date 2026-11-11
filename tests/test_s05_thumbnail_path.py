"""S05.3: ffmpeg discovery must survive a Finder-minimal PATH."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from phuthanhai.analyzer.probe import find_tool


class ToolDiscoveryTests(unittest.TestCase):
    def test_homebrew_ffmpeg_is_found_without_terminal_path(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            with mock.patch("phuthanhai.analyzer.probe.shutil.which", return_value=None):
                binary = find_tool("ffmpeg")
        if binary is None:
            self.skipTest("no ffmpeg in standard Homebrew/system directories")
        self.assertTrue(os.access(binary, os.X_OK))
        self.assertIn(binary, {"/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg", "/bin/ffmpeg"})

    def test_path_still_wins(self) -> None:
        with mock.patch("phuthanhai.analyzer.probe.shutil.which", return_value="/custom/ffmpeg"):
            self.assertEqual(find_tool("ffmpeg"), "/custom/ffmpeg")


if __name__ == "__main__":
    unittest.main()
