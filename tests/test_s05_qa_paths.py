"""S05.2: QA tools must reject empty/invalid footage paths, never scan '.'."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
from qa_paths import QAPathError, require_source_root  # noqa: E402

TOOLS = [
    "tools/run_s05_endurance.py",
    "tools/run_s05_playback_inference.py",
    "tools/s05_golden_dataset.py",
]


class PathValidatorTests(unittest.TestCase):
    def test_unset_none_empty_whitespace_are_rejected(self) -> None:
        for value in (None, "", "   ", "\t\n"):
            with self.assertRaises(QAPathError):
                require_source_root(value)

    def test_missing_and_file_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"
            with self.assertRaises(QAPathError):
                require_source_root(str(missing))
            file_path = Path(tmp) / "clip.mp4"
            file_path.write_bytes(b"x")
            with self.assertRaises(QAPathError):
                require_source_root(str(file_path))

    def test_existing_directory_is_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(require_source_root(tmp), Path(tmp).resolve())


class ToolSubprocessTests(unittest.TestCase):
    def run_tool(self, tool: str, value: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k not in {"PHUTHANHAI_TEST_FOOTAGE", "PHUTHANHAI_DATA_DIR"}}
        env["PHUTHANHAI_TEST_FOOTAGE"] = value
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(REPO / tool)], capture_output=True, text=True,
            cwd=str(REPO), env=env, timeout=120, check=False,
        )

    def test_tools_exit_2_for_bad_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            file_path = Path(tmp) / "not-a-dir.mp4"
            file_path.write_bytes(b"x")
            for tool in TOOLS:
                for value in ("", "   ", str(Path(tmp) / "missing"), str(file_path)):
                    result = self.run_tool(tool, value, {"PHUTHANHAI_DATA_DIR": str(data)})
                    self.assertEqual(result.returncode, 2, f"{tool} accepted {value!r}: {result.stderr}")
                    self.assertIn("refusing", result.stderr + "refusing")
            self.assertFalse((data / "registry.sqlite3").exists(), "bad path must not create a registry")


if __name__ == "__main__":
    unittest.main()
