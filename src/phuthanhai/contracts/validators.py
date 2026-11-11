"""Structural and semantic validation of the PhuThanhAI data contracts.

The four portable objects are validated in two layers:

1. **Structural** - Draft 2020-12 JSON Schema, resolved through an in-memory
   local registry. ``$ref`` is never fetched over the network.
2. **Semantic** - identity linkage, span containment, provenance separation and
   referential integrity, which JSON Schema cannot express.

A validator failure raises :class:`ContractError` carrying a stable ``code`` so
the UI can render a specific message and a specific next action instead of a
generic "something went wrong".
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource

MARK_BEGIN = "<!-- PHUTHANHAI_WORKSPACE_JSON_BEGIN -->"
MARK_END = "<!-- PHUTHANHAI_WORKSPACE_JSON_END -->"
SCHEMA_URI_BASE = "https://phuthanhai.local/schemas/"
PORTABLE_CONTRACT = "phuthanhai.workspace_package/1.0.0"

#: Refuse to parse portable reports above this size; a report is data, not a dump.
MAX_PORTABLE_BYTES = 64 * 1024 * 1024

_FRONTMATTER_LINE = re.compile(r"^[a-z][a-z0-9_]*: [A-Za-z0-9_./-]+$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_REQUIRED_FRONTMATTER = {"phuthanhai_contract", "package_id", "project_id", "payload_sha256"}


class ContractError(RuntimeError):
    """A contract object is invalid. ``code`` is stable and user-facing."""

    def __init__(self, code: str, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant: {value}")


def strict_json_loads(text: str) -> dict[str, Any]:
    """Decode untrusted JSON, rejecting duplicate keys and NaN/Infinity."""

    try:
        result = json.loads(text, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)
    except ValueError as exc:
        raise ContractError("CONTRACT_JSON_INVALID", f"invalid JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise ContractError("CONTRACT_JSON_INVALID", "top-level JSON value must be an object")
    return result


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    """Byte-stable serialization shared with V2 (do not change the options)."""

    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _is_uuid4(value: Any) -> bool:
    if not isinstance(value, str) or not _UUID_RE.match(value):
        return False
    parsed = UUID(value)
    return parsed.version == 4 and str(parsed) == value


@dataclass
class SchemaSet:
    """Loaded schemas plus a validators map and the registry used for ``$ref``."""

    validators: dict[str, Draft202012Validator]
    registry: Registry
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)

    def validator(self, name: str) -> Draft202012Validator:
        try:
            return self.validators[name]
        except KeyError as exc:
            raise ContractError("CONTRACT_SCHEMA_UNKNOWN", f"unknown contract schema: {name}") from exc


def load_schemas(contracts_dir: Path) -> SchemaSet:
    """Load every ``*.schema.json`` from ``contracts_dir`` into a local registry."""

    if not contracts_dir.is_dir():
        raise ContractError("CONTRACT_SCHEMA_MISSING", f"contracts directory not found: {contracts_dir.name}")

    sources: dict[str, dict[str, Any]] = {}
    for path in sorted(contracts_dir.glob("*.schema.json")):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ContractError("CONTRACT_SCHEMA_MISSING", f"cannot read schema {path.name}: {exc}") from exc
        name = path.name.removesuffix(".schema.json").removesuffix(".json")
        sources[name] = strict_json_loads(raw)

    required = {"analysis", "project_bundle", "workspace_extension", "workspace_package"}
    missing = sorted(required - set(sources))
    if missing:
        raise ContractError(
            "CONTRACT_SCHEMA_MISSING",
            f"required schemas missing from contracts/: {', '.join(missing)}",
        )

    registry = Registry()
    for name, schema in sources.items():
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise ContractError("CONTRACT_SCHEMA_INVALID", f"schema {name} is invalid: {exc.message}") from exc
        resource = Resource.from_contents(schema)
        # Register under the portable-URI convention and, when present, under $id.
        registry = registry.with_resource(f"{SCHEMA_URI_BASE}{name}.schema.json", resource)
        schema_id = schema.get("$id")
        if isinstance(schema_id, str):
            registry = registry.with_resource(schema_id, resource)

    validators = {
        name: Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
        for name, schema in sources.items()
    }
    return SchemaSet(validators=validators, registry=registry, sources=sources)


def _check(validator: Draft202012Validator, obj: Any, *, label: str) -> None:
    errors = sorted(validator.iter_errors(obj), key=lambda e: (tuple(map(str, e.path)), e.message))
    if errors:
        first = errors[0]
        path = ".".join(map(str, first.path)) or "<root>"
        raise ContractError(
            "REPORT_SCHEMA_INVALID",
            f"{label} failed schema validation at {path}: {first.message}",
            path=path,
        )


# --------------------------------------------------------------------------- #
# Semantic validation (package)                                                #
# --------------------------------------------------------------------------- #


def _require(condition: bool, message: str, *, code: str = "REPORT_SEMANTIC_INVALID") -> None:
    if not condition:
        raise ContractError(code, message)


def semantic_validate_package(pkg: dict[str, Any]) -> None:
    """Full referential/semantic validation of a WorkspacePackage/1.0.0.

    Mirrors ``tools/validate_specs.py`` so the CLI, the API and the shipped tool
    cannot disagree. Checked: identity linkage, scene/frame containment, note
    scopes, review/evidence/correction/conflict references, and span bounds.
    """

    bundle = pkg["legacy_bundle"]
    extension = pkg["extension"]
    project_id = pkg["project_id"]

    for value in (
        pkg["package_id"],
        project_id,
        bundle["project_id"],
        bundle["manifest"]["project_id"],
        bundle["annotation_set"]["project_id"],
        extension["project_id"],
    ):
        _require(_is_uuid4(value), f"not a UUIDv4: {value!r}")

    _require(
        project_id
        == bundle["project_id"]
        == bundle["manifest"]["project_id"]
        == bundle["annotation_set"]["project_id"]
        == extension["project_id"],
        "cross-project ID mismatch between package, bundle and extension",
    )

    assets = {asset["asset_id"]: asset for asset in bundle["manifest"]["assets"]}
    _require(len(assets) == len(bundle["manifest"]["assets"]), "duplicate asset_id in manifest")
    for asset_id in assets:
        _require(_is_uuid4(asset_id), f"asset_id is not a UUIDv4: {asset_id!r}")

    notes = {note["annotation_id"]: note for note in bundle["annotation_set"]["annotations"]}
    _require(
        len(notes) == len(bundle["annotation_set"]["annotations"]),
        "duplicate annotation_id in annotation set",
    )

    analyses = {analysis["analysis_id"]: analysis for analysis in bundle["analyses"]}
    _require(len(analyses) == len(bundle["analyses"]), "duplicate analysis_id")

    frame_by_analysis: dict[str, set[str]] = {}
    scene_by_analysis: dict[str, dict[str, dict[str, Any]]] = {}
    for analysis in bundle["analyses"]:
        ref = analysis["analysis_id"]
        frame_by_analysis[ref] = set()
        scene_by_analysis[ref] = {}
        frame_times: list[int] = []
        duration = analysis["source"]["duration_ms"]
        for scene in analysis["scenes"]:
            _require(scene["id"] not in scene_by_analysis[ref], f"duplicate scene_id {scene['id']}")
            scene_by_analysis[ref][scene["id"]] = scene
            _require(
                0 <= scene["start_ms"] < scene["end_ms"] <= duration,
                f"scene {scene['id']} span outside source duration",
            )
            for observation in scene["observations"]:
                _require(
                    observation["id"] not in frame_by_analysis[ref],
                    f"duplicate frame_id {observation['id']}",
                )
                frame_by_analysis[ref].add(observation["id"])
                _require(
                    scene["start_ms"] <= observation["time_ms"] < scene["end_ms"],
                    f"frame {observation['id']} PTS outside scene {scene['id']}",
                )
                frame_times.append(observation["time_ms"])
        _require(
            len(frame_times) == analysis["analysis"]["frame_count"],
            f"analysis {ref} frame_count does not match observation count",
        )

    for asset in assets.values():
        analysis_id = asset["analysis_id"]
        if analysis_id is None:
            continue
        _require(analysis_id in analyses, f"asset {asset['asset_id']} references unknown analysis")
        analysis = analyses[analysis_id]
        _require(
            asset["source_sha256"] == analysis["source"]["sha256"]
            and asset["duration_ms"] == analysis["source"]["duration_ms"],
            f"asset {asset['asset_id']} source metadata differs from its analysis",
        )

    for note in notes.values():
        _require(_is_uuid4(note["annotation_id"]), f"annotation_id is not a UUIDv4: {note['annotation_id']!r}")
        scope = note["scope"]
        asset_id = note["asset_id"]
        start, end = note["start_ms"], note["end_ms"]
        if scope == "project":
            _require(
                asset_id is None and start is None and end is None,
                "project-scoped annotation must not bind an asset or a range",
            )
        elif scope == "clip":
            _require(
                asset_id in assets and start is None and end is None,
                "clip-scoped annotation requires a known asset and no range",
            )
        elif scope == "time_range":
            _require(
                asset_id in assets and start is not None and end is not None,
                "time_range annotation requires a known asset and both bounds",
            )
            _require(
                0 <= start < end <= assets[asset_id]["duration_ms"],
                "time_range annotation outside asset duration",
            )
        else:
            raise ContractError("REPORT_SEMANTIC_INVALID", f"unexpected annotation scope: {scope!r}")

    def require_asset(asset_id: str) -> dict[str, Any]:
        _require(asset_id in assets, f"unknown asset reference: {asset_id}")
        return assets[asset_id]

    def require_analysis_for_asset(asset_id: str, analysis_id: str) -> dict[str, Any]:
        asset = require_asset(asset_id)
        _require(
            asset["analysis_id"] == analysis_id and analysis_id in analyses,
            f"asset/analysis cross-reference invalid for {asset_id}",
        )
        return analyses[analysis_id]

    def check_unique(rows: Iterable[dict[str, Any]], key: str) -> None:
        ids = [row[key] for row in rows]
        _require(len(ids) == len(set(ids)), f"duplicate {key}")
        for value in ids:
            _require(_is_uuid4(value), f"{key} is not a UUIDv4: {value!r}")

    for rows, key in (
        (extension["reviews"], "review_id"),
        (extension["evidence"], "event_id"),
        (extension["corrections"], "correction_id"),
        (extension["conflicts"], "conflict_id"),
    ):
        check_unique(rows, key)

    for review in extension["reviews"]:
        asset = require_asset(review["asset_id"])
        for span in review["selected_ranges"]:
            _require(
                0 <= span["start_ms"] < span["end_ms"] <= asset["duration_ms"],
                "selected range outside asset duration",
            )
        playhead = review["last_playhead_ms"]
        _require(
            playhead is None or 0 <= playhead <= asset["duration_ms"],
            "last_playhead_ms outside asset duration",
        )
        for note_id in review["note_ids"]:
            _require(note_id in notes, f"review references missing note {note_id}")
            note = notes[note_id]
            _require(
                note["asset_id"] is None or note["asset_id"] == review["asset_id"],
                "review references a note belonging to another asset",
            )

    event_map = {event["event_id"]: event for event in extension["evidence"]}
    for event in extension["evidence"]:
        asset = require_asset(event["asset_id"])
        require_analysis_for_asset(event["asset_id"], event["source_analysis_id"])
        _require(
            0 <= event["start_ms"] < event["end_ms"] <= asset["duration_ms"],
            "evidence span outside asset duration",
        )
        for frame_id in event["frame_ids"]:
            _require(
                frame_id in frame_by_analysis[event["source_analysis_id"]],
                f"evidence references missing frame {frame_id}",
            )

    for correction in extension["corrections"]:
        analysis_id = correction["source_analysis_id"]
        require_analysis_for_asset(correction["asset_id"], analysis_id)
        scene = scene_by_analysis[analysis_id].get(correction["scene_id"])
        _require(scene is not None, f"correction references missing scene {correction['scene_id']}")
        scene_frames = {observation["id"] for observation in scene["observations"]}
        _require(
            set(correction["frame_ids"]).issubset(scene_frames),
            "correction references frames outside the target scene",
        )

    for conflict in extension["conflicts"]:
        asset_id = conflict["asset_id"]
        require_asset(asset_id)
        note = notes.get(conflict["annotation_id"])
        _require(note is not None, f"conflict references missing annotation {conflict['annotation_id']}")
        _require(
            note["asset_id"] in (None, asset_id),
            "conflict annotation belongs to a different asset",
        )
        for event_id in conflict["evidence_event_ids"]:
            _require(
                event_id in event_map and event_map[event_id]["asset_id"] == asset_id,
                f"conflict references evidence {event_id} of another asset",
            )


def semantic_validate_analysis(report: dict[str, Any]) -> None:
    """Semantic checks for a single ``AnalysisReport/1.0.0``.

    Enforces ordered non-overlapping scenes that cover ``[0, duration_ms)``, unique
    ids, frame PTS inside their scene, honest coverage flags and the vision-only
    rule (never describe audio when nothing was transcribed).
    """

    duration = report["source"]["duration_ms"]
    _require(duration >= 1, "source duration_ms must be positive")
    expected_start = 0
    seen_scene_ids: set[str] = set()
    seen_frame_ids: set[str] = set()
    total_frames = 0

    for scene in report["scenes"]:
        _require(scene["id"] not in seen_scene_ids, f"duplicate scene_id {scene['id']}")
        seen_scene_ids.add(scene["id"])
        _require(scene["start_ms"] == expected_start, f"scene {scene['id']} does not start at {expected_start}")
        _require(scene["start_ms"] < scene["end_ms"] <= duration, f"scene {scene['id']} span invalid")
        expected_start = scene["end_ms"]
        for observation in scene["observations"]:
            _require(observation["id"] not in seen_frame_ids, f"duplicate frame_id {observation['id']}")
            seen_frame_ids.add(observation["id"])
            _require(
                scene["start_ms"] <= observation["time_ms"] < scene["end_ms"],
                f"frame {observation['id']} outside scene {scene['id']}",
            )
            total_frames += 1

    _require(expected_start == duration, "scenes do not cover the source timeline")
    _require(total_frames == report["analysis"]["frame_count"], "frame_count does not match observations")
    _require(
        report["coverage"]["timeline_end_ms"] == duration
        and report["coverage"]["timeline_start_ms"] == 0,
        "coverage timeline must span the source",
    )
    _require(report["analysis"]["vision_only"] is True, "vision_only must be true for V2 reports")

    warnings = set(report["warnings"])
    coverage = report["coverage"]
    # Sampling is always partial in the MVP; claiming otherwise must be visible.
    _require(
        "sampled_frames_not_all_frames" in warnings,
        "report must warn that sampled frames are not all frames",
    )
    if report["source"]["has_audio"]:
        _require(
            "audio_not_transcribed" in warnings,
            "report with an audio stream must state that audio was not transcribed",
        )
    if report["status"] == "complete":
        _require(
            not any(scene["coverage_status"] == "missing_observation" for scene in report["scenes"]),
            "complete report must not contain unscanned scenes",
        )
        _require(
            coverage["max_observed_gap_ms"] <= coverage["max_gap_ms"],
            "complete report exceeds the sampling gap budget",
        )


# --------------------------------------------------------------------------- #
# Portable Markdown                                                            #
# --------------------------------------------------------------------------- #


def render_portable_workspace_md(payload: dict[str, Any]) -> str:
    """Render the canonical portable report (frontmatter + exact markers)."""

    body = canonical_json_bytes(payload).decode("utf-8").rstrip()
    return (
        "---\n"
        f"phuthanhai_contract: {PORTABLE_CONTRACT}\n"
        f"package_id: {payload['package_id']}\n"
        f"project_id: {payload['project_id']}\n"
        f"payload_sha256: {payload_sha256(payload)}\n"
        "---\n"
        "# PhuThanhAI portable workspace report\n"
        "\n"
        "Ban ghi nay chua du lieu co cau truc de import lai PhuThanhAI.\n"
        "\n"
        f"{MARK_BEGIN}\n"
        "```json\n"
        f"{body}\n"
        "```\n"
        f"{MARK_END}\n"
    )


def parse_portable_workspace_md(text: str, *, max_bytes: int = MAX_PORTABLE_BYTES) -> dict[str, Any]:
    """Parse a portable workspace report defensively.

    Rejects: oversized input, non-UTF-8 content, missing/duplicated markers,
    malformed frontmatter, unknown contract versions, duplicate JSON keys,
    non-finite numbers and any payload/checksum disagreement.
    """

    if not isinstance(text, str):
        raise ContractError("PORTABLE_INVALID", "portable input must be text")
    if len(text.encode("utf-8")) > max_bytes:
        raise ContractError("PORTABLE_TOO_LARGE", f"portable report exceeds {max_bytes} bytes")

    lines = text.splitlines()
    if len(lines) < 8 or lines[0] != "---":
        raise ContractError("PORTABLE_INVALID", "missing strict frontmatter")
    try:
        front_end = lines.index("---", 1)
    except ValueError as exc:
        raise ContractError("PORTABLE_INVALID", "unterminated frontmatter") from exc

    frontmatter: dict[str, str] = {}
    for line in lines[1:front_end]:
        if not _FRONTMATTER_LINE.match(line):
            raise ContractError("PORTABLE_INVALID", f"invalid frontmatter scalar: {line!r}")
        key, value = line.split(": ", 1)
        if key in frontmatter:
            raise ContractError("PORTABLE_INVALID", f"duplicate frontmatter key: {key}")
        frontmatter[key] = value

    if set(frontmatter) != _REQUIRED_FRONTMATTER:
        raise ContractError(
            "PORTABLE_INVALID",
            f"unexpected frontmatter keys: {sorted(set(frontmatter) ^ _REQUIRED_FRONTMATTER)}",
        )
    if frontmatter["phuthanhai_contract"] != PORTABLE_CONTRACT:
        raise ContractError(
            "UNSUPPORTED_CONTRACT_VERSION",
            f"unsupported contract {frontmatter['phuthanhai_contract']!r}; expected {PORTABLE_CONTRACT}",
        )

    begin_count = sum(line == MARK_BEGIN for line in lines)
    end_count = sum(line == MARK_END for line in lines)
    if begin_count != 1 or end_count != 1:
        raise ContractError("PORTABLE_INVALID", "JSON markers must appear exactly once each")
    begin = lines.index(MARK_BEGIN)
    end = lines.index(MARK_END)
    if not (front_end < begin < end):
        raise ContractError("PORTABLE_INVALID", "JSON markers out of order relative to frontmatter")
    if begin + 1 >= len(lines) or lines[begin + 1] != "```json":
        raise ContractError("PORTABLE_INVALID", "missing ```json fence after begin marker")
    if end - 1 < 0 or lines[end - 1] != "```":
        raise ContractError("PORTABLE_INVALID", "missing closing fence before end marker")

    payload_lines = lines[begin + 2 : end - 1]
    if any(line.startswith("```") for line in payload_lines):
        raise ContractError("PORTABLE_INVALID", "nested fenced block inside portable JSON")
    payload = strict_json_loads("\n".join(payload_lines))

    if frontmatter["package_id"] != payload.get("package_id"):
        raise ContractError("PORTABLE_INVALID", "frontmatter package_id does not match payload")
    if frontmatter["project_id"] != payload.get("project_id"):
        raise ContractError("PORTABLE_INVALID", "frontmatter project_id does not match payload")
    actual = payload_sha256(payload)
    if frontmatter["payload_sha256"] != actual:
        raise ContractError(
            "PORTABLE_CHECKSUM_MISMATCH",
            "portable payload checksum does not match frontmatter",
        )
    return payload


# --------------------------------------------------------------------------- #
# Facade used by the CLI, API and tests                                        #
# --------------------------------------------------------------------------- #

_KIND_TO_SCHEMA = {
    "phuthanhai.video_analysis": "analysis",
    "phuthanhai.project_bundle": "project_bundle",
    "phuthanhai.workspace_extension": "workspace_extension",
    "phuthanhai.workspace_package": "workspace_package",
}


def validate_document(schemas: SchemaSet, document: dict[str, Any], *, semantic: bool = True) -> str:
    """Validate any portable document; return the schema name that matched."""

    kind = document.get("kind")
    schema_name = _KIND_TO_SCHEMA.get(str(kind))
    if schema_name is None:
        raise ContractError("UNSUPPORTED_CONTRACT_KIND", f"unsupported contract kind: {kind!r}")

    if schema_name == "workspace_package":
        _check(schemas.validator("workspace_package"), document, label="WorkspacePackage")
        if semantic:
            semantic_validate_package(document)
    elif schema_name == "project_bundle":
        _check(schemas.validator("project_bundle"), document, label="ProjectBundle")
    elif schema_name == "workspace_extension":
        _check(schemas.validator("workspace_extension"), document, label="WorkspaceExtension")
    elif schema_name == "analysis":
        _check(schemas.validator("analysis"), document, label="AnalysisReport")
        if semantic:
            semantic_validate_analysis(document)
    return schema_name
