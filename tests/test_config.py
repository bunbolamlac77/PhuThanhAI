"""Configuration, offline guard and POV lock tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from phuthanhai.config import (
    OFFLINE_ENVIRONMENT,
    Settings,
    legacy_aivideo_root,
    mlx_runtime_candidates,
    model_specs,
    offline_environment,
)
from phuthanhai.pov import LOCKED_CODE, PovLockedError, require_unlocked
from tests.helpers import isolated_settings


class SettingsTests(unittest.TestCase):
    def test_repo_and_contract_paths_exist(self) -> None:
        settings = Settings.load()
        self.assertTrue(settings.contracts_dir.is_dir())
        self.assertTrue((settings.contracts_dir / "workspace_package.schema.json").is_file())
        self.assertTrue(settings.fixtures_dir.is_dir())
        self.assertEqual(settings.host, "127.0.0.1")

    def test_data_root_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with isolated_settings(Path(tmp)) as settings:
                self.assertEqual(settings.data.root, Path(tmp).resolve())
                settings.data.ensure()
                self.assertTrue(settings.data.projects.is_dir())
                self.assertTrue(settings.data.caches.is_dir())
                self.assertEqual(settings.data.session_file.name, "session.json")

    def test_data_root_is_outside_source_tree(self) -> None:
        settings = Settings.load()
        self.assertNotIn(settings.repo, settings.data.root.parents)

    def test_model_roles_are_declared(self) -> None:
        roles = {spec.role for spec in model_specs()}
        self.assertEqual(
            roles,
            {"visual_analysis_primary", "story_synthesis", "tts_kokoro_vi", "g0_text_optional"},
        )

    def test_visual_model_requires_processor_files(self) -> None:
        spec = next(spec for spec in model_specs() if spec.role == "visual_analysis_primary")
        self.assertIn("preprocessor_config.json", spec.required_files)
        self.assertEqual(spec.used_by.split(",")[0], "S00 smoke")

    def test_legacy_root_is_separate_from_data_root(self) -> None:
        self.assertNotEqual(legacy_aivideo_root(), Settings.load().data.root)

    def test_mlx_runtime_candidates_are_ordered_documented_first(self) -> None:
        candidates = mlx_runtime_candidates()
        self.assertIn("g0-mlx-runtime", str(candidates[0].python))
        self.assertEqual(candidates[0].origin, "documented-g0-mlx-runtime")


class OfflineGuardTests(unittest.TestCase):
    def test_environment_variables_are_forced(self) -> None:
        env = offline_environment({"PATH": "/usr/bin", "HF_HUB_OFFLINE": "0"})
        for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY", "TOKENIZERS_PARALLELISM"):
            self.assertEqual(env[key], OFFLINE_ENVIRONMENT[key])
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_guard_does_not_mutate_the_input_mapping(self) -> None:
        base = {"HF_HUB_OFFLINE": "0"}
        offline_environment(base)
        self.assertEqual(base["HF_HUB_OFFLINE"], "0")


class PovLockTests(unittest.TestCase):
    def test_pov_is_locked(self) -> None:
        with self.assertRaises(PovLockedError) as ctx:
            require_unlocked()
        self.assertEqual(ctx.exception.code, LOCKED_CODE)

    def test_pov_can_be_unlocked_explicitly_by_a_later_gate(self) -> None:
        require_unlocked(gates_passed=True)  # returns None, no exception


if __name__ == "__main__":
    unittest.main()
