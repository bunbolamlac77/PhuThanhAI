#!/usr/bin/env python3
"""Validate V2/V3 contract examples and portable Markdown. No network or model required.

Usage: python3 tools/validate_specs.py
Requires: jsonschema >= 4.18, referencing (installed as dependency of jsonschema).
This only proves fixture/contract consistency, NOT an implemented PhuThanhAI app.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from uuid import UUID

try:
    from jsonschema import Draft202012Validator, FormatChecker
    from referencing import Registry, Resource
except ImportError as exc:
    sys.exit(f"Missing offline validation dependency: {exc}. Install jsonschema >=4.18 if permitted; no silent skip.")

ROOT = Path(__file__).resolve().parents[1]
S = ROOT / "contracts"
F = ROOT / "tests" / "fixtures"
MARK_BEGIN = "<!-- PHUTHANHAI_WORKSPACE_JSON_BEGIN -->"
MARK_END = "<!-- PHUTHANHAI_WORKSPACE_JSON_END -->"
URI = "https://phuthanhai.local/schemas/"


def reject_duplicates(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for k, v in pairs:
        if k in result:
            raise ValueError(f"duplicate key: {k}")
        result[k] = v
    return result


def reject_constant(value: str):
    raise ValueError(f"non-finite JSON constant: {value}")


def loads_strict(text: str) -> dict:
    result = json.loads(text, object_pairs_hook=reject_duplicates, parse_constant=reject_constant)
    if not isinstance(result, dict):
        raise ValueError("expected top-level object")
    return result


def canonical_bytes(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def digest(obj: dict) -> str:
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def uuid4(x: str) -> None:
    if str(UUID(x)) != x or UUID(x).version != 4:
        raise ValueError(f"invalid UUID4: {x}")


def build_validators():
    schemas = {p.stem: loads_strict(p.read_text(encoding="utf-8")) for p in S.glob("*.schema.json")}
    registry = Registry()
    for name, schema in schemas.items():
        Draft202012Validator.check_schema(schema)
        resource = Resource.from_contents(schema)
        registry = registry.with_resource(URI + name + ".json", resource)
        if "$id" in schema:
            registry = registry.with_resource(schema["$id"], resource)
    validators = {name: Draft202012Validator(s, registry=registry, format_checker=FormatChecker()) for name, s in schemas.items()}
    return validators


def check_schema(validator, obj: dict) -> None:
    errors = sorted(validator.iter_errors(obj), key=lambda e: (list(map(str, e.path)), e.message))
    if errors:
        err = errors[0]
        path = ".".join(map(str, err.path))
        raise ValueError(f"schema validation failed at {path}: {err.message}")


def semantic_validate(pkg: dict) -> None:
    b = pkg["legacy_bundle"]
    e = pkg["extension"]
    pid = pkg["project_id"]
    for val in (pkg["package_id"], pid, b["project_id"], b["manifest"]["project_id"], b["annotation_set"]["project_id"], e["project_id"]):
        uuid4(val)
    if not (pid == b["project_id"] == b["manifest"]["project_id"] == b["annotation_set"]["project_id"] == e["project_id"]):
        raise ValueError("cross-project ID mismatch")

    assets = {a["asset_id"]: a for a in b["manifest"]["assets"]}
    if len(assets) != len(b["manifest"]["assets"]):
        raise ValueError("duplicate asset_id")
    for aid in assets:
        uuid4(aid)
    notes = {a["annotation_id"]: a for a in b["annotation_set"]["annotations"]}
    if len(notes) != len(b["annotation_set"]["annotations"]):
        raise ValueError("duplicate annotation_id")

    analyses = {a["analysis_id"]: a for a in b["analyses"]}
    if len(analyses) != len(b["analyses"]):
        raise ValueError("duplicate analysis_id")
    frame_by_analysis = {}
    scene_by_analysis = {}
    for analysis in b["analyses"]:
        refid = analysis["analysis_id"]
        frame_by_analysis[refid] = set()
        scene_by_analysis[refid] = {}
        all_frame_times = []
        for scene in analysis["scenes"]:
            if scene["id"] in scene_by_analysis[refid]:
                raise ValueError("duplicate scene_id")
            scene_by_analysis[refid][scene["id"]] = scene
            if not (0 <= scene["start_ms"] < scene["end_ms"] <= analysis["source"]["duration_ms"]):
                raise ValueError("scene span invalid")
            for frame in scene["observations"]:
                if frame["id"] in frame_by_analysis[refid]:
                    raise ValueError("duplicate frame_id")
                frame_by_analysis[refid].add(frame["id"])
                if not (scene["start_ms"] <= frame["time_ms"] < scene["end_ms"]):
                    raise ValueError("frame PTS not within scene")
                all_frame_times.append(frame["time_ms"])
        if len(all_frame_times) != analysis["analysis"]["frame_count"]:
            raise ValueError("analysis frame count mismatch")

    for asset in assets.values():
        if asset["analysis_id"] is not None:
            refid = asset["analysis_id"]
            if refid not in analyses:
                raise ValueError("asset analysis_id not found")
            r = analyses[refid]
            if asset["source_sha256"] != r["source"]["sha256"] or asset["duration_ms"] != r["source"]["duration_ms"]:
                raise ValueError("asset source metadata differs from analysis")

    for note in notes.values():
        uuid4(note["annotation_id"])
        scope=note["scope"]
        aid=note["asset_id"]
        start,end=note["start_ms"],note["end_ms"]
        if scope=="project":
            if aid is not None or start is not None or end is not None:
                raise ValueError("project note must not bind asset/range")
        elif scope=="clip":
            if aid not in assets or start is not None or end is not None:
                raise ValueError("clip note requires asset and no range")
        elif scope=="time_range":
            if aid not in assets or start is None or end is None or not(0<=start<end<=assets[aid]["duration_ms"]):
                raise ValueError("clip note time range invalid")
        else:
            raise ValueError("unexpected note scope")

    def require_asset(aid):
        if aid not in assets:
            raise ValueError("unknown asset")
        return assets[aid]

    def require_analysis_for_asset(aid, refid):
        a=require_asset(aid)
        if a["analysis_id"] != refid or refid not in analyses:
            raise ValueError("asset/analysis cross-reference invalid")
        return analyses[refid]

    def check_unique(rows, field):
        ids = [r[field] for r in rows]
        if len(ids)!=len(set(ids)):
            raise ValueError(f"duplicate {field}")
        for i in ids:
            uuid4(i)

    for key, field in (("reviews", "review_id"), ("evidence", "event_id"), ("corrections", "correction_id"), ("conflicts", "conflict_id")):
        check_unique(e[key], field)

    for rev in e["reviews"]:
        a=require_asset(rev["asset_id"])
        for span in rev["selected_ranges"]:
            if not 0<=span["start_ms"]<span["end_ms"]<=a["duration_ms"]:
                raise ValueError("selected span out of bounds")
        t=rev["last_playhead_ms"]
        if t is not None and not 0<=t<=a["duration_ms"]:
            raise ValueError("playhead out of bounds")
        for nid in rev["note_ids"]:
            if nid not in notes:
                raise ValueError("review references missing note")
            src_note = notes[nid]
            if src_note["asset_id"] is not None and src_note["asset_id"] != rev["asset_id"]:
                raise ValueError("review references note for other asset")

    event_map={ev["event_id"]:ev for ev in e["evidence"]}
    for ev in e["evidence"]:
        a=require_asset(ev["asset_id"])
        require_analysis_for_asset(ev["asset_id"],ev["source_analysis_id"])
        if not 0<=ev["start_ms"]<ev["end_ms"]<=a["duration_ms"]:
            raise ValueError("evidence span out of bounds")
        for frame in ev["frame_ids"]:
            if frame not in frame_by_analysis[ev["source_analysis_id"]]:
                raise ValueError("evidence references missing frame")

    for corr in e["corrections"]:
        refid=corr["source_analysis_id"]
        require_analysis_for_asset(corr["asset_id"],refid)
        scene=scene_by_analysis[refid].get(corr["scene_id"])
        if scene is None:
            raise ValueError("correction references missing scene")
        frames={f["id"] for f in scene["observations"]}
        if not set(corr["frame_ids"]).issubset(frames):
            raise ValueError("correction references frames in wrong scene")

    for conflict in e["conflicts"]:
        aid=conflict["asset_id"]
        require_asset(aid)
        note=notes.get(conflict["annotation_id"])
        if note is None or note["asset_id"] not in (None,aid):
            raise ValueError("conflict note invalid for asset")
        for evid in conflict["evidence_event_ids"]:
            if evid not in event_map or event_map[evid]["asset_id"]!=aid:
                raise ValueError("conflict evidence invalid for asset")


def portable_report(pkg:dict)->str:
    text=f"""---
phuthanhai_contract: phuthanhai.workspace_package/1.0.0
package_id: {pkg['package_id']}
project_id: {pkg['project_id']}
payload_sha256: {digest(pkg)}
---
# PhuThanhAI portable workspace report

## Ghi chú
Báo cáo này là dữ liệu mẫu, **không phải kết quả phân tích AI thật**.

{MARK_BEGIN}
```json
{canonical_bytes(pkg).decode('utf-8').rstrip()}
```
{MARK_END}
"""
    return text


def parse_portable(text: str)->dict:
    lines=text.splitlines()
    if len(lines)<8 or lines[0] != '---':
        raise ValueError('missing strict frontmatter')
    try:
        idx=lines.index('---',1)
    except ValueError:
        raise ValueError('unterminated frontmatter')
    front={}
    for line in lines[1:idx]:
        if not re.match(r'^[a-z][a-z0-9_]*: [A-Za-z0-9_./-]+$',line):
            raise ValueError('invalid frontmatter scalar')
        k,v=line.split(': ',1)
        if k in front:
            raise ValueError('duplicate frontmatter key')
        front[k]=v
    if set(front) != {'phuthanhai_contract','package_id','project_id','payload_sha256'}:
        raise ValueError('unexpected frontmatter keys')
    if front['phuthanhai_contract']!='phuthanhai.workspace_package/1.0.0':
        raise ValueError('unsupported contract')
    if sum(line==MARK_BEGIN for line in lines)!=1 or sum(line==MARK_END for line in lines)!=1:
        raise ValueError('marker missing or duplicated')
    b=lines.index(MARK_BEGIN); e=lines.index(MARK_END)
    if not (idx<b<e and lines[b+1]=='```json' and lines[e-1]=='```'):
        raise ValueError('malformed portable JSON fenced block')
    payload_lines=lines[b+2:e-1]
    if any(x.startswith('```') for x in payload_lines):
        raise ValueError('nested fenced block')
    pkg=loads_strict('\n'.join(payload_lines))
    if front['package_id']!=pkg['package_id'] or front['project_id']!=pkg['project_id'] or front['payload_sha256']!=digest(pkg):
        raise ValueError('payload hash/frontmatter mismatch')
    return pkg


def main():
    validators=build_validators()
    print('Schema definitions valid:', ', '.join(sorted(validators)))
    legacy=loads_strict((F/'golden_v2.project_bundle.json').read_text(encoding='utf-8'))
    golden=loads_strict((F/'golden_v3.workspace_package.json').read_text(encoding='utf-8'))
    check_schema(validators['project_bundle.schema'],legacy)
    check_schema(validators['workspace_extension.schema'],golden['extension'])
    check_schema(validators['workspace_package.schema'],golden)
    semantic_validate(golden)
    print('PASS V2 legacy fixture and V3 golden fixture structural/semantic')
    if golden['legacy_bundle']!=legacy:
        raise AssertionError('Legacy bundle bytes/structure changed')
    for name in ('invalid_v3.cross_project.json','invalid_v3.out_of_bounds_span.json'):
        bad=loads_strict((F/name).read_text(encoding='utf-8'))
        try:
            check_schema(validators['workspace_package.schema'],bad)
            semantic_validate(bad)
        except ValueError as err:
            print(f'PASS negative fixture {name}: {err}')
        else:
            raise AssertionError(f'Negative fixture unexpectedly accepted: {name}')
    rendered=portable_report(golden)
    parsed=parse_portable(rendered)
    check_schema(validators['workspace_package.schema'],parsed)
    semantic_validate(parsed)
    if canonical_bytes(parsed)!=canonical_bytes(golden):
        raise AssertionError('portable roundtrip mismatch')
    (F/'golden_v3.workspace_report_portable.md').write_text(rendered,encoding='utf-8')
    try:
        parse_portable(rendered.replace('payload_sha256: '+digest(golden),'payload_sha256: '+('0'*64),1))
    except ValueError:
        print('PASS tamper hash detection')
    else:
        raise AssertionError('tampered report accepted')
    print('PASS V3 portable MD exact roundtrip / local schema refs / checksum / referential integrity')
    print('LIMITATION: These tests do not execute model inference or app E2E on M1 Pro.')

if __name__=='__main__':
    main()
