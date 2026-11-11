"""S05.3: focused ARIA regressions for Inspector tabs and asset selection."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web" / "src" / "components"


class InspectorTabLinkageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = (WEB / "InspectorTabs.tsx").read_text(encoding="utf-8")

    def test_both_tab_buttons_have_ids_matching_panel_labels(self) -> None:
        for name in ("notes", "analysis"):
            self.assertIn(f'id={{`${{uid}}-tab-{name}`}}', self.source)
            self.assertIn(f'aria-labelledby={{`${{uid}}-tab-{name}`}}', self.source)
            self.assertIn(f'data-inspector-tab="{name}"', self.source)

    def test_every_tabpanel_label_resolves_to_a_tab_id_in_source(self) -> None:
        panel_labels = set(re.findall(r"aria-labelledby=\{`\$\{uid\}-tab-([a-z]+)`\}", self.source))
        tab_ids = set(re.findall(r"id=\{`\$\{uid\}-tab-([a-z]+)`\}", self.source))
        self.assertEqual(panel_labels, {"notes", "analysis"})
        self.assertEqual(tab_ids, {"notes", "analysis"})

    def test_tabs_expose_roles_and_selected_state(self) -> None:
        self.assertGreaterEqual(self.source.count('role="tab"'), 2)
        self.assertGreaterEqual(self.source.count("aria-selected="), 2)
        self.assertGreaterEqual(self.source.count('role="tabpanel"'), 2)


class AssetSelectionSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = (WEB / "AssetGrid.tsx").read_text(encoding="utf-8")

    def test_aria_selected_tracks_checkbox_not_active_clip(self) -> None:
        self.assertIn("aria-selected={checked}", self.source)
        self.assertNotIn("aria-selected={active}", self.source)

    def test_active_clip_uses_aria_current(self) -> None:
        self.assertIn('aria-current={active ? "true" : undefined}', self.source)

    def test_visual_classes_and_checkbox_remain(self) -> None:
        self.assertIn('active ? "asset-card--active" : ""', self.source)
        self.assertIn('checked ? "asset-card--selected" : ""', self.source)
        self.assertIn("onToggleSelect(asset)", self.source)
        self.assertIn('aria-label={`Chọn ${asset.filename}`}', self.source)


if __name__ == "__main__":
    unittest.main()
