"""Note semantics: wall-clock suggestion, scopes, Unicode, provenance."""

from __future__ import annotations

import unicodedata
import unittest

from phuthanhai.library.notes import (
    infer_note_type,
    normalize_text,
    suggest_wall_clock,
    validate_note_type,
    validate_scope,
)
from phuthanhai.library.store import StoreError


class WallClockSuggestionTests(unittest.TestCase):
    def test_morning_phrase(self) -> None:
        self.assertEqual(
            suggest_wall_clock("Lên phà lúc 5 giờ sáng, đợi khoảng 30 phút"),
            {"time_of_day": "05:00", "precision": "time_of_day"},
        )

    def test_afternoon_and_evening(self) -> None:
        self.assertEqual(suggest_wall_clock("5h chiều")["time_of_day"], "17:00")
        self.assertEqual(suggest_wall_clock("7 giờ tối")["time_of_day"], "19:00")
        self.assertEqual(suggest_wall_clock("12 giờ trưa")["time_of_day"], "12:00")

    def test_night_hours(self) -> None:
        self.assertEqual(suggest_wall_clock("10 giờ đêm")["time_of_day"], "22:00")
        self.assertEqual(suggest_wall_clock("12 giờ đêm")["time_of_day"], "00:00")
        self.assertEqual(suggest_wall_clock("2 giờ đêm")["time_of_day"], "02:00")

    def test_24h_notation(self) -> None:
        self.assertEqual(suggest_wall_clock("quay lúc 17:30")["time_of_day"], "17:30")
        self.assertEqual(suggest_wall_clock("05:00 sáng")["time_of_day"], "05:00")

    def test_ambiguous_hour_is_not_guessed(self) -> None:
        self.assertIsNone(suggest_wall_clock("5 giờ"))
        self.assertIsNone(suggest_wall_clock("ăn trưa"))

    def test_no_clock_returns_none(self) -> None:
        self.assertIsNone(suggest_wall_clock("Cô dâu chuẩn bị bước xuống phà"))

    def test_suggestion_is_not_a_video_timestamp(self) -> None:
        suggestion = suggest_wall_clock("5 giờ sáng")
        self.assertEqual(set(suggestion or {}), {"time_of_day", "precision"})


class TextNormalizationTests(unittest.TestCase):
    def test_nfc_normalization(self) -> None:
        decomposed = "e\u0301"
        self.assertEqual(normalize_text(decomposed), unicodedata.normalize("NFC", "é"))

    def test_empty_note_is_rejected(self) -> None:
        with self.assertRaises(StoreError) as ctx:
            normalize_text("   ")
        self.assertEqual(ctx.exception.code, "ANNOTATION_EMPTY")

    def test_too_long_note_is_rejected(self) -> None:
        with self.assertRaises(StoreError):
            normalize_text("x" * 4001)


class ScopeValidationTests(unittest.TestCase):
    def test_project_scope_must_not_bind_asset(self) -> None:
        with self.assertRaises(StoreError) as ctx:
            validate_scope(scope="project", asset_id="a", start_ms=None, end_ms=None, duration_ms=1000)
        self.assertEqual(ctx.exception.code, "ANNOTATION_SCOPE_INVALID")

    def test_clip_scope_rejects_range(self) -> None:
        with self.assertRaises(StoreError):
            validate_scope(scope="clip", asset_id="a", start_ms=0, end_ms=10, duration_ms=1000)

    def test_time_range_requires_bounds_inside_duration(self) -> None:
        validate_scope(scope="time_range", asset_id="a", start_ms=0, end_ms=1000, duration_ms=1000)
        with self.assertRaises(StoreError):
            validate_scope(scope="time_range", asset_id="a", start_ms=0, end_ms=1001, duration_ms=1000)
        with self.assertRaises(StoreError):
            validate_scope(scope="time_range", asset_id="a", start_ms=500, end_ms=500, duration_ms=1000)

    def test_unknown_scope(self) -> None:
        with self.assertRaises(StoreError):
            validate_scope(scope="scene", asset_id=None, start_ms=None, end_ms=None, duration_ms=1000)


class NoteTypeTests(unittest.TestCase):
    def test_clock_note_inferred(self) -> None:
        self.assertEqual(infer_note_type("Lên phà lúc 5 giờ sáng"), "clock_time")

    def test_location_note_inferred(self) -> None:
        self.assertEqual(infer_note_type("Quay ở bến sông"), "location")

    def test_unknown_type_rejected(self) -> None:
        with self.assertRaises(StoreError):
            validate_note_type("gossip")


if __name__ == "__main__":
    unittest.main()
