"""Contract validation tests: schemas, semantics, portable Markdown, strict JSON."""

from __future__ import annotations

import json
import unittest

from phuthanhai.contracts.migrate import upgrade
from phuthanhai.contracts.validators import (
    MARK_BEGIN,
    MARK_END,
    ContractError,
    canonical_json_bytes,
    load_schemas,
    parse_portable_workspace_md,
    payload_sha256,
    render_portable_workspace_md,
    semantic_validate_analysis,
    semantic_validate_package,
    strict_json_loads,
    validate_document,
)
from tests.helpers import contracts_dir, fixtures_dir, load_fixture


class SchemaLoadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schemas = load_schemas(contracts_dir())

    def test_all_four_schemas_load(self) -> None:
        self.assertEqual(
            set(self.schemas.validators),
            {"analysis", "project_bundle", "workspace_extension", "workspace_package"},
        )

    def test_golden_v2_bundle_is_schema_valid(self) -> None:
        bundle = load_fixture("golden_v2.project_bundle.json")
        validate_document(self.schemas, bundle, semantic=False)

    def test_golden_v3_package_is_structurally_and_semantically_valid(self) -> None:
        package = load_fixture("golden_v3.workspace_package.json")
        self.assertEqual(validate_document(self.schemas, package, semantic=True), "workspace_package")

    def test_cross_project_fixture_is_rejected(self) -> None:
        bad = load_fixture("invalid_v3.cross_project.json")
        with self.assertRaises(ContractError) as ctx:
            semantic_validate_package(bad)
        self.assertEqual(ctx.exception.code, "REPORT_SEMANTIC_INVALID")

    def test_out_of_bounds_span_fixture_is_rejected(self) -> None:
        bad = load_fixture("invalid_v3.out_of_bounds_span.json")
        with self.assertRaises(ContractError) as ctx:
            validate_document(self.schemas, bad, semantic=True)
        self.assertEqual(ctx.exception.code, "REPORT_SEMANTIC_INVALID")

    def test_unknown_kind_is_rejected(self) -> None:
        with self.assertRaises(ContractError) as ctx:
            validate_document(self.schemas, {"kind": "phuthanhai.nope"}, semantic=False)
        self.assertEqual(ctx.exception.code, "UNSUPPORTED_CONTRACT_KIND")


class StrictJsonTests(unittest.TestCase):
    def test_duplicate_keys_are_rejected(self) -> None:
        with self.assertRaises(ContractError):
            strict_json_loads('{"a": 1, "a": 2}')

    def test_nan_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            strict_json_loads('{"a": NaN}')

    def test_non_object_root_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            strict_json_loads("[1, 2]")


class PortableMarkdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schemas = load_schemas(contracts_dir())
        cls.package = load_fixture("golden_v3.workspace_package.json")
        cls.portable = (fixtures_dir() / "golden_v3.workspace_report_portable.md").read_text(encoding="utf-8")

    def test_fixture_roundtrips_to_identical_canonical_bytes(self) -> None:
        parsed = parse_portable_workspace_md(self.portable)
        self.assertEqual(canonical_json_bytes(parsed), canonical_json_bytes(self.package))

    def test_rendered_report_parses_back(self) -> None:
        rendered = render_portable_workspace_md(self.package)
        parsed = parse_portable_workspace_md(rendered)
        self.assertEqual(payload_sha256(parsed), payload_sha256(self.package))

    def test_tampered_checksum_is_rejected(self) -> None:
        tampered = self.portable.replace(payload_sha256(self.package), "0" * 64, 1)
        with self.assertRaises(ContractError) as ctx:
            parse_portable_workspace_md(tampered)
        self.assertEqual(ctx.exception.code, "PORTABLE_CHECKSUM_MISMATCH")

    def test_duplicate_marker_is_rejected(self) -> None:
        tampered = self.portable.replace(MARK_END, MARK_END + "\n" + MARK_END, 1)
        with self.assertRaises(ContractError):
            parse_portable_workspace_md(tampered)

    def test_missing_marker_is_rejected(self) -> None:
        tampered = self.portable.replace(MARK_BEGIN, "<!-- not the marker -->", 1)
        with self.assertRaises(ContractError):
            parse_portable_workspace_md(tampered)

    def test_rejects_oversized_document(self) -> None:
        with self.assertRaises(ContractError) as ctx:
            parse_portable_workspace_md(self.portable, max_bytes=64)
        self.assertEqual(ctx.exception.code, "PORTABLE_TOO_LARGE")

    def test_rejects_unsupported_contract_version(self) -> None:
        tampered = self.portable.replace(
            "phuthanhai.workspace_package/1.0.0", "phuthanhai.workspace_package/9.9.9", 1
        )
        with self.assertRaises(ContractError) as ctx:
            parse_portable_workspace_md(tampered)
        self.assertEqual(ctx.exception.code, "UNSUPPORTED_CONTRACT_VERSION")


class MigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schemas = load_schemas(contracts_dir())
        cls.bundle = load_fixture("golden_v2.project_bundle.json")

    def test_bundle_upgrade_produces_valid_package(self) -> None:
        package = upgrade(self.bundle, schemas=self.schemas)
        self.assertEqual(package["kind"], "phuthanhai.workspace_package")
        self.assertEqual(package["legacy_bundle"], self.bundle)
        self.assertEqual(package["extension"]["reviews"], [])
        validate_document(self.schemas, package, semantic=True)

    def test_upgrade_is_idempotent(self) -> None:
        once = upgrade(self.bundle, schemas=self.schemas)
        twice = upgrade(once, schemas=self.schemas)
        self.assertIs(once, twice)
        self.assertEqual(
            len(twice["legacy_bundle"]["annotation_set"]["annotations"]),
            len(self.bundle["annotation_set"]["annotations"]),
        )

    def test_upgrade_does_not_mutate_the_legacy_bundle(self) -> None:
        before = json.dumps(self.bundle, sort_keys=True)
        upgrade(self.bundle, schemas=self.schemas)
        self.assertEqual(json.dumps(self.bundle, sort_keys=True), before)

    def test_analysis_report_cannot_be_upgraded_without_a_project(self) -> None:
        report = self.bundle["analyses"][0]
        with self.assertRaises(ContractError) as ctx:
            upgrade(report, schemas=self.schemas)
        self.assertEqual(ctx.exception.code, "MIGRATION_NEEDS_PROJECT")


class AnalysisSemanticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = load_fixture("golden_v2.project_bundle.json")
        cls.report = cls.bundle["analyses"][0]

    def test_golden_analysis_passes_semantic_checks(self) -> None:
        semantic_validate_analysis(self.report)

    def test_missing_sampling_warning_is_rejected(self) -> None:
        mutated = json.loads(json.dumps(self.report))
        mutated["warnings"] = [w for w in mutated["warnings"] if w != "sampled_frames_not_all_frames"]
        with self.assertRaises(ContractError):
            semantic_validate_analysis(mutated)

    def test_scene_gap_is_rejected(self) -> None:
        mutated = json.loads(json.dumps(self.report))
        mutated["scenes"][0]["start_ms"] = 1
        with self.assertRaises(ContractError):
            semantic_validate_analysis(mutated)

    def test_frame_outside_scene_is_rejected(self) -> None:
        mutated = json.loads(json.dumps(self.report))
        mutated["scenes"][0]["observations"][0]["time_ms"] = mutated["scenes"][0]["end_ms"]
        with self.assertRaises(ContractError):
            semantic_validate_analysis(mutated)


if __name__ == "__main__":
    unittest.main()
