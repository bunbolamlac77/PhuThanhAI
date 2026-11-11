"""VisualEvidenceSet construction and validation (S03, internal for S04).

Every observation must point at a frame that exists in the verified sampling
manifest. Frame ids, scene ids and timestamps are copied from the manifest - never
from the model - so a fabricated frame or timecode cannot become evidence.

Levels:

* ``observed``  - the model described something visible, with a frame reference.
* ``hypothesis`` - reserved for later fusion; must carry basis frames.
* ``unknown``   - inference failed or produced nothing usable; uncertainty is
  mandatory, and the frame is never reported as analysed.

No confidence numbers are produced: this runtime has no calibrated probability.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from phuthanhai.analyzer.vlm_worker import WorkerError, validate_observation

EVIDENCE_VERSION = "1.0.0"
COVERAGE_VALUES = {"sufficient", "insufficient", "unknown"}
LEVELS = {"observed", "hypothesis", "unknown"}


class EvidenceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def build_evidence(
    *,
    analysis_id: str,
    asset_id: str,
    source_sha256: str,
    sampling_fingerprint: str,
    sampling_profile: str,
    coverage_status: str,
    method: str,
    prompt_version: str,
    model_identity: dict[str, Any],
    runtime_versions: dict[str, Any],
    frame_refs: list[dict[str, Any]],
    results: dict[str, dict[str, Any]],
    visual_coverage: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Assemble evidence from verified refs plus per-frame results."""

    frames: list[dict[str, Any]] = []
    for ref in frame_refs:
        frame_id = ref["frame_id"]
        result = results.get(frame_id)
        base = {
            "frame_id": frame_id,
            "scene_id": ref["scene_id"],
            "normalized_pts_ms": int(ref["normalized_pts_ms"]),
            "source_sha256": source_sha256,
        }
        if result is None:
            frames.append(
                {
                    **base,
                    "method": method,
                    "interpretation_level": "unknown",
                    "observation": None,
                    "uncertainty": ["inference_failed_or_not_committed"],
                }
            )
            continue
        try:
            observation = validate_observation(result["observation"])
        except WorkerError as exc:
            frames.append(
                {
                    **base,
                    "method": method,
                    "interpretation_level": "unknown",
                    "observation": None,
                    "uncertainty": [f"invalid_model_output:{exc.code}"],
                }
            )
            continue
        frames.append(
            {
                **base,
                "method": result.get("method", method),
                "interpretation_level": "observed",
                "observation": observation,
                "uncertainty": list(observation.get("uncertainty", [])),
            }
        )

    evidence = {
        "kind": "phuthanhai.visual_evidence",
        "schema_version": EVIDENCE_VERSION,
        "analysis_id": analysis_id,
        "asset_id": asset_id,
        "source_sha256": source_sha256,
        "sampling_fingerprint": sampling_fingerprint,
        "sampling_profile": sampling_profile,
        "coverage_status": coverage_status,
        "analysis_method": method,
        "prompt_version": prompt_version,
        "model_identity": model_identity,
        "runtime_versions": runtime_versions,
        "frames": frames,
        "visual_coverage": visual_coverage or {},
        "warnings": list(warnings or []),
        "is_every_frame_analyzed": False,
        "created_at": _now_iso(),
    }
    validate_evidence(evidence, frame_refs)
    return evidence


def validate_evidence(evidence: dict[str, Any], frame_refs: list[dict[str, Any]]) -> None:
    """Reject fabricated refs, duplicate frames and malformed levels."""

    if evidence.get("kind") != "phuthanhai.visual_evidence":
        raise EvidenceError("EVIDENCE_INVALID", "wrong evidence kind")
    if evidence.get("coverage_status") not in COVERAGE_VALUES:
        raise EvidenceError("EVIDENCE_INVALID", "invalid coverage_status")
    lookup = {ref["frame_id"]: ref for ref in frame_refs}
    seen: set[str] = set()
    for entry in evidence.get("frames", []):
        frame_id = entry.get("frame_id")
        if frame_id in seen:
            raise EvidenceError("EVIDENCE_INVALID", f"duplicate frame in evidence: {frame_id}")
        seen.add(frame_id)
        ref = lookup.get(frame_id)
        if ref is None:
            raise EvidenceError("EVIDENCE_FRAME_UNKNOWN", f"evidence references unknown frame: {frame_id}")
        if entry.get("scene_id") != ref["scene_id"]:
            raise EvidenceError("EVIDENCE_FRAME_MISMATCH", f"scene mismatch for {frame_id}")
        if int(entry.get("normalized_pts_ms", -1)) != int(ref["normalized_pts_ms"]):
            raise EvidenceError("EVIDENCE_FRAME_MISMATCH", f"timestamp mismatch for {frame_id}")
        level = entry.get("interpretation_level")
        if level not in LEVELS:
            raise EvidenceError("EVIDENCE_INVALID", f"invalid interpretation_level for {frame_id}")
        if level == "observed":
            observation = entry.get("observation")
            try:
                validate_observation(observation)
            except WorkerError as exc:
                raise EvidenceError("EVIDENCE_INVALID", f"invalid observation for {frame_id}: {exc}") from exc
        if level in {"unknown", "hypothesis"} and not entry.get("uncertainty"):
            raise EvidenceError("EVIDENCE_INVALID", f"{level} entry needs uncertainty: {frame_id}")
        if "confidence" in entry or "confidence" in (entry.get("observation") or {}):
            raise EvidenceError("EVIDENCE_INVALID", "uncalibrated confidence values are not allowed")
    missing = set(lookup) - seen
    if missing:
        raise EvidenceError("EVIDENCE_INCOMPLETE", f"missing frames: {sorted(missing)[:5]}")
    if evidence.get("is_every_frame_analyzed") is not False:
        raise EvidenceError("EVIDENCE_INVALID", "sampled evidence must not claim every frame was analysed")
    if evidence["coverage_status"] == "insufficient" and not evidence.get("warnings"):
        raise EvidenceError("EVIDENCE_INVALID", "insufficient coverage requires a warning")
