"""S05.4 API/schema selection-mode contract."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from phuthanhai.api.schemas import AnalyzeRequest
from phuthanhai.library.service import LibraryService
from phuthanhai.library.store import StoreError
from tests.helpers import isolated_settings


class SelectionModeRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env = isolated_settings(Path(self._tmp.name))
        self.settings = self._env.__enter__()
        self.service = LibraryService(self.settings)

    def tearDown(self) -> None:
        self.service.close()
        self._env.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_default_is_full_evidence(self) -> None:
        request = AnalyzeRequest()
        self.assertEqual(request.selection_mode, "full")
        self.assertIsNone(request.user_ranges)

    def test_all_modes_and_user_ranges_validate(self) -> None:
        for mode in ("smart", "balanced", "full"):
            request = AnalyzeRequest(selection_mode=mode, user_ranges=[[1000, 4000]])
            self.assertEqual(request.selection_mode, mode)
            self.assertEqual(request.user_ranges, [[1000, 4000]])

    def test_unknown_mode_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            AnalyzeRequest(selection_mode="turbo")

    def test_service_rejects_unknown_mode_before_io(self) -> None:
        with self.assertRaises(StoreError) as caught:
            self.service.start_analysis("missing-asset", selection_mode="turbo")
        self.assertEqual(caught.exception.code, "SELECTION_MODE_INVALID")


if __name__ == "__main__":
    unittest.main()
