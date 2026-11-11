"""S04: environment profile registry and Vietnamese vocabulary."""

from __future__ import annotations

import unittest

from phuthanhai.analyzer import context_profiles as profiles


class ProfileRegistryTests(unittest.TestCase):
    def test_all_required_profiles_exist(self) -> None:
        required = {
            "general", "wedding_traditional", "event", "vlog_travel",
            "documentary", "pov_footage", "custom",
        }
        self.assertEqual(set(profiles.PROFILE_IDS), required)
        self.assertEqual(profiles.DEFAULT_PROFILE, "general")

    def test_general_has_no_wedding_vocabulary(self) -> None:
        general = profiles.profile("general")
        keys = {label.key for label in general.labels}
        self.assertNotIn("ancestor_altar", keys)
        self.assertNotIn("wedding_gate", keys)
        self.assertNotIn("wedding_reception", keys)

    def test_wedding_vocabulary_covers_required_groups(self) -> None:
        wedding = profiles.profile("wedding_traditional")
        labels = {label.label_vi for label in wedding.labels}
        for expected in (
            "Bàn gia tiên",
            "Phông gia tiên / trang trí gia tiên",
            "Cổng cưới",
            "Mâm quả / tráp lễ",
            "Cô dâu, chú rể chuẩn bị",
            "Dàn bưng quả",
            "Lễ gia tiên",
            "Thắp hương / dâng lễ",
            "Rước dâu / đón dâu",
            "Chụp ảnh gia đình",
            "Phỏng vấn",
            "Tiệc cưới",
        ):
            self.assertIn(expected, labels)

    def test_altar_requires_ritual_signals(self) -> None:
        altar = profiles.label("wedding_traditional", "ancestor_altar")
        self.assertIsNotNone(altar)
        self.assertIn("incense", altar.required_any)
        self.assertGreaterEqual(altar.min_hits, 3)

    def test_guardrails_forbid_identity_claims(self) -> None:
        wedding = profiles.profile("wedding_traditional")
        self.assertTrue(any("vai trò" in rule.lower() for rule in wedding.guardrails))
        prep = profiles.label("wedding_traditional", "bride_groom_prep")
        self.assertTrue(prep.identity_label)
        self.assertIn("Không xác định danh tính", prep.uncertainty_vi)


if __name__ == "__main__":
    unittest.main()
