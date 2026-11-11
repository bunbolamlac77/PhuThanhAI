"""CLI tests: doctor JSON report, validate command, loopback-only serve guard."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from phuthanhai.cli import EXIT_GATE_FAIL, EXIT_OK, EXIT_USAGE, main
from phuthanhai.contracts.validators import payload_sha256
from tests.helpers import fixtures_dir, load_fixture


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class DoctorCliTests(unittest.TestCase):
    def test_doctor_json_reports_real_machine_facts(self) -> None:
        code, stdout, _ = _run(["doctor", "--offline", "--json"])
        report = json.loads(stdout)
        self.assertEqual(code, EXIT_OK)
        self.assertIn(report["status"], {"PASS", "PASS_WITH_WARNINGS"})
        self.assertTrue(report["offline"])
        ids = {check["id"]: check for check in report["checks"]}
        for required in ("platform", "python_app", "ffmpeg", "ffprobe", "mlx_runtime", "contracts"):
            self.assertIn(required, ids)
            self.assertEqual(ids[required]["status"], "PASS", ids[required])
        self.assertIn("qwen3_vl", json.dumps(ids["mlx_runtime"]["evidence"]))
        self.assertGreater(report["hardware"]["memory_bytes"] or 0, 0)

    def test_doctor_text_mode_marks_failures_explicitly(self) -> None:
        code, stdout, _ = _run(["doctor", "--offline"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("RESULT:", stdout)
        self.assertIn("contracts", stdout)


class ValidateCliTests(unittest.TestCase):
    def test_golden_portable_report_passes(self) -> None:
        path = fixtures_dir() / "golden_v3.workspace_report_portable.md"
        code, stdout, _ = _run(["validate", "--input", str(path), "--json"])
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "pass")
        self.assertEqual(payload["contract"], "workspace_package")

    def test_tampered_portable_report_fails(self) -> None:
        package = load_fixture("golden_v3.workspace_package.json")
        text = (fixtures_dir() / "golden_v3.workspace_report_portable.md").read_text(encoding="utf-8")
        tampered = text.replace(payload_sha256(package), "f" * 64, 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tampered.md"
            path.write_text(tampered, encoding="utf-8")
            code, stdout, _ = _run(["validate", "--input", str(path), "--json"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertEqual(json.loads(stdout)["status"], "fail")

    def test_json_bundle_contract_passes(self) -> None:
        path = fixtures_dir() / "golden_v2.project_bundle.json"
        code, stdout, _ = _run(["validate", "--input", str(path), "--json"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(stdout)["contract"], "project_bundle")

    def test_missing_input_is_usage_error(self) -> None:
        code, _, err = _run(["validate", "--input", "/nonexistent/file.md"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("input not found", err)


class ServeGuardTests(unittest.TestCase):
    def test_non_loopback_host_is_refused(self) -> None:
        code, _, err = _run(["serve", "--host", "0.0.0.0"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("loopback", err)

    def test_public_hostname_is_refused(self) -> None:
        code, _, _ = _run(["serve", "--host", "192.168.1.10"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertNotEqual(code, EXIT_GATE_FAIL)


if __name__ == "__main__":
    unittest.main()
