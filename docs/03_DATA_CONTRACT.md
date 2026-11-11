# PhuThanhAI V3.0 — 03_DATA_CONTRACT

> **NORMATIVE / QUAN TRỌNG NHẤT ĐỂ KHÔNG PHÁ V2**. Product version = `3.0.0`; data contract version có vòng đời độc lập. Toàn bộ `contracts/*.schema.json` là **định nghĩa structural authority**, tài liệu này thêm semantic invariants/serialization/import/migration. Fixtures trong `tests/fixtures/` là ví dụ đã được kiểm tra bằng schema + semantic validator mẫu; **không phản ánh inference thật**.

## 0. Contract matrix, không được lẫn phiên bản

| Object | `kind` | `schema_version` | Nguồn chuẩn | V3 xử lý |
|---|---|---|---|---|
| Báo cáo thị giác một clip | `phuthanhai.video_analysis` | `1.0.0` | `contracts/analysis.schema.json` (chính V2) | Giữ nguyên byte/schema, không thêm field |
| Dự án nhiều clip + note | `phuthanhai.project_bundle` | `1.0.0` | `contracts/project_bundle.schema.json` (chính V2) | Import/export V2 tương thích |
| Review/evidence/corrections V3 | `phuthanhai.workspace_extension` | `1.0.0` | `contracts/workspace_extension.schema.json` | **Mới**, không sửa bundle cũ |
| Gói portable V3 | `phuthanhai.workspace_package` | `1.0.0` | `contracts/workspace_package.schema.json` | Bundle V2 + extension V3 |
| Cache/DB/runtime | Internal implementation | không portable mặc định | SQLite/JSONL | Có version nội bộ, **không export đường dẫn OS** |
| Script/shot plan POV | `phuthanhai.script_options`/`render_plan` | thiết kế S06–S08 | `02_SPEC_POV_PIPELINE.md` | Chưa code engine trước G5 |

**Lý do không thêm field vào V2:** `ProjectBundle/1.0.0` cũ có `additionalProperties: false`; thêm `reviews` hay `quality_score` vào chính object ấy sẽ khiến v2 importer fail. V3 mở rộng qua **envelope** `WorkspacePackage` thay vì thay đổi schema cũ.

### Cách đọc V2 rồi nâng lên V3

```text
ProjectBundle/1.0.0 (nhập V2)
       │ (normalize + strict V2 validate)
       └── + empty WorkspaceExtension/1.0.0
                │
                └── WorkspacePackage/1.0.0 (portable V3)
                             │
                             ├── legacy_bundle: ProjectBundle gốc, unmodified
                             └── extension: review/evidence/correction overlays
```

Nếu import `AnalysisReport/1.0.0` một clip, dựng một ProjectBundle wrapper **mới** theo schema V2, có asset UUID mới và annotation set rỗng, rồi tạo extension rỗng. Không giả vờ đã có dữ liệu user review. `migrations` phải idempotent (`migrate(migrate(x))` không nhân đôi annotations). Khi xuất ra V2, xuất riêng `legacy_bundle` không phụ thuộc extension; **không mất extension trong bản V3**.

## 1. Identity và 3 trục thời gian

| Identifier/time | Ý nghĩa | Bất biến |
|---|---|---|
| `project_id` UUIDv4 | Dự án người dùng | Đồng nhất giữa ProjectBundle, Manifest, AnnotationSet và V3 Extension |
| `asset_id` UUIDv4 | Một source footage trong project | Không dựa filename; duy nhất trong manifest |
| `analysis_id` UUIDv4 | Một lần phân tích với model/profile cụ thể | Trỏ về source hash và asset đúng |
| `scene_id` (`S0001`...) | Một scene **trong analysis của asset** | Chỉ unique trong analysis; **phải dùng cùng asset_id để định vị** |
| `frame_id` (`F0001`...) | Sample frame theo PTS | Chỉ unique trong analysis; phải dùng asset_id + analysis_id |
| `start_ms/end_ms` | Timeline **riêng của asset** `[start,end)` | Có thể seek/cắt footage gốc; `0 <= start < end <= asset.duration_ms` |
| `wall_clock.time_of_day` | Giờ thật trong ngày do user nhập, như `05:00` | Không map sang clip `time_ms` nếu không có anchor độc lập |
| `capture_time` | Timestamp metadata hoặc user, có source | Có thể sai timezone/camera clock, không mặc định authoritative |

**Ví dụ bắt buộc:** Một người dùng ghi trên `asset_id=X`: `"Lên phà lúc 5 giờ sáng"` và bấm note toàn clip. Annotation lưu `scope=clip`, `asset_id=X`, `start_ms=null`, `end_ms=null`, `wall_clock={"time_of_day":"05:00","date":null,"timezone":null,"precision":"time_of_day"}`, `source=user_annotation`. VLM có thể xác nhận nhìn thấy phương tiện/phà nếu visual evidence đủ; không tự phát hiện giờ 05:00 bằng frame. UI báo `Vì sao biết 05:00? Ghi chú người dùng`.

## 2. Nguồn chân lý và bất biến provenance

1. **Raw observed:** `AnalysisReport.scenes[].observations[]` với frame PTS; không nhúng note người dùng vào raw prompt; raw không bị sửa khi người dùng đổi ghi chú.
2. **Temporal evidence (V2 candidate và V3 enrichments):** sự kiện chỉ có giá trị bằng chứng khi trỏ tới **frame ID thực tế hoặc clip infer đã được benchmark**. `event.method=sequential_frames` nghĩa là model đã xem các frame riêng; không nói đã xem video liên tục.
3. **User context:** `ProjectBundle.annotation_set` chứa sự thật theo người dùng, còn `assertion_status` không phải nhãn ``objectively_verified``. Nếu mâu thuẫn visual thì tạo `conflict` ở extension.
4. **Human corrections:** `extension.corrections[]` ghi `original_text/corrected_text`, refs, tác giả user; giữ raw VLM. Khuyến cáo corrections được hiển thị dưới `Người dùng đã sửa`, không đổi thành observed result.
5. **Editorial suggestions:** shot ratings, blur metrics, story hooks là heuristic/creative output, không thay thế raw observations; không dùng score 0–100 như xác suất đúng sự thật.
6. **Full source SHA-256:** trước phân tích canonical và trước render; `filename`, file size/mtime, inode, proxy sha hoặc pHash **không được coi là identity đủ** để render từ source.

## 3. Legacy objects: nghĩa chính xác

### 3.1 `AnalysisReport/1.0.0`

Các field bắt buộc theo `contracts/analysis.schema.json`:

- Root: `schema_version`, `kind`, `analysis_id`, `created_at`, `status`, `source`, `analysis`, `scenes`, `coverage`, `warnings`.
- `source`: `filename`, `sha256` 64 lowercase hex, `duration_ms`, `width/height`, `fps_nominal`, `vfr_detected`, `has_audio`, `first_video_pts_ms`, `time_origin=first_presented_video_frame`.
- `analysis`: `model_id`, `profile=quick|detailed|thorough`, `sampling_mode=adaptive_keyframes`, `frame_count`, `vision_only=true`.
- `scene`: `id`, `start_ms`, `end_ms`, `coverage_status`, `summary`, `visual_facts`, `uncertainties`, `observations`.
- `observation`: `id`, `time_ms`, `description`, `objects`, `actions`, `camera`, `lighting`, `visible_text`, `uncertainty`.
- `coverage`: `timeline_start_ms=0`, `timeline_end_ms=duration_ms`, `max_gap_ms`, `max_observed_gap_ms`, `observed_frame_count`, `intervals_without_visual_observation`, `is_every_frame_analyzed=false`.

Semantic validator bắt buộc: scenes sorted, cover timeline; unique IDs; frame PTS inside scene; `frame_count=sum(observations)`; `complete` không có missing scene và observed_gap <= profile max_gap; nếu không thì `partial` + warning; không được mô tả âm thanh khi `vision_only=true`; warning `sampled_frames_not_all_frames`, và có audio thì `audio_not_transcribed`.

**Quan trọng:** Ở V3, temporal evidence/corrections không được thêm vào đây. Lưu riêng trong `extension` để không break V2.

### 3.2 `ProjectBundle/1.0.0`

Đây là nội dung chuẩn V2, gồm `project_id`, `manifest.assets`, `annotation_set.annotations`, `analyses`, `temporal_events`, `warnings`, `report_profile`. V3 **giữ nguyên**.

- `manifest.assets[]`: `asset_id`, `display_filename`, `source_sha256 nullable`, `analysis_id nullable`, `duration_ms`, `review_state`, `tags`, `capture_time nullable`, `capture_time_source`.
- `annotation_set.annotations[]`: `annotation_id`, `scope`, `asset_id`, `start_ms/end_ms nullable`, `text`, `note_type`, `source=user_annotation`, `assertion_status`, `wall_clock nullable`, `created_at/updated_at`, `revision`.
- `temporal_events[]`: `event_id`, `asset_id`, `start_ms`, `end_ms`, `frame_ids`, `action_summary`, `evidence_mode` (`multi_frame`/`sequential_frames`), `uncertainties`. Phải liên kết frame thuộc đúng asset; không xem motion heuristic là evidence.
- `report_profile`: `general`, `wedding_traditional`, `event`, `vlog_travel`, `documentary`, `pov_footage`, `custom`. Chọn profile chỉ thay **cách trình bày/narrative**; không sửa raw frames.

Validation ngoài JSON Schema: project_id của manifest/annotation_set = root; mỗi annotation scope hợp lệ (`project` không có asset/range; `clip` có asset không range; `time_range` có asset và `start_ms<end_ms`); time range <= duration từng asset; analysis/source SHA tham chiếu đúng; event frame IDs đúng analysis; source missing không bị ngụy tạo hash.

## 4. WorkspaceExtension/1.0.0 (MỚI)

`extension` không chứa path video gốc, không chứa frame image bytes, không thay thế V2 manifest. Structure schema nằm ở `contracts/workspace_extension.schema.json`:

```json
{
  "kind": "phuthanhai.workspace_extension",
  "schema_version": "1.0.0",
  "project_id": "9b11527e-7cb6-4737-a716-29c5ae28d390",
  "revision": 1,
  "created_at": "2026-11-11T10:10:00+07:00",
  "reviews": [
    {
      "review_id": "71e23e56-77af-4865-ab29-bb44d50592fd",
      "asset_id": "2a91a57f-4684-4f5e-8a7f-fd479b94c215",
      "status": "reviewed",
      "rating": 4,
      "favorite": true,
      "labels": ["phà", "ngoại cảnh"],
      "selected_ranges": [{"start_ms":6000,"end_ms":11000}],
      "last_playhead_ms": 7500,
      "note_ids": ["3e824ae0-77ca-4553-ac34-ff0fdced7261"]
    }
  ],
  "evidence": [],
  "corrections": [],
  "conflicts": []
}
```

### 4.1 `reviews[]`

Chứa `review_id` UUID, `asset_id`, `status=unreviewed|in_review|reviewed|needs_attention`, `rating=null|1..5`, `favorite=bool`, `labels[]`, `selected_ranges[]`, `last_playhead_ms=null|int`, `note_ids[]`. `last_playhead_ms` là vị trí UI resume, không là timestamp quay. `note_ids` phải thuộc annotation set trong cùng project; mỗi span có `0<=start<end<=asset.duration_ms`. Với `no source` vẫn giữ review record nhưng preview không phát.

### 4.2 `evidence[]`

Enrichment chỉ có sau model infer đã verify: `event_id`, `asset_id`, `source_analysis_id`, `start_ms/end_ms`, `frame_ids[]`, `method=multi_frame|sequential_frames`, `description`, `interpretation_level=observed|hypothesis`, `uncertainties[]`. Không có `confidence=0.97` tự bịa từ model. Empty array hợp lệ (chưa phân tích). Every frame_id phải tồn tại trong analysis của **đúng asset**.

### 4.3 `corrections[]`

`correction_id`, `asset_id`, `source_analysis_id`, `scene_id`, `original_text`, `corrected_text`, `frame_ids[]`, `author=user`, `created_at`. Correction là overlay; không được xóa hoặc viết đè `scenes[].summary` của legacy report. Nếu analysis thay đổi, correction phải được review/remap, không silently áp vào scene mới có cùng `S0001`.

### 4.4 `conflicts[]`

`conflict_id`, `annotation_id`, `asset_id`, `evidence_event_ids[]`, `status=unresolved|user_reviewed|dismissed`, `explanation`. Conflict là vấn đề cần review, không có nghĩa note sai. Annotation ID, asset ID và event IDs phải tồn tại. User xem được cả hai bằng chứng và quyết định.

### 4.5 Revision

`extension.revision` tăng sau mỗi transaction thay đổi overlay. Autosave có `expected_revision`, server trả `409 Conflict` khi trễ, không ghi đè. Khi chỉ đổi note V2, extension không cần tự tăng nếu chưa đổi review/correction; report assembly fingerprint phải chứa `annotation_set.revision` **và** `extension.revision`.

## 5. WorkspacePackage/1.0.0 — portable V3

```json
{
  "kind": "phuthanhai.workspace_package",
  "schema_version": "1.0.0",
  "package_id": "0b63d2f8-b3a9-4bd7-a447-2f8f96a52789",
  "created_at": "2026-11-11T10:15:00+07:00",
  "project_id": "9b11527e-7cb6-4737-a716-29c5ae28d390",
  "legacy_bundle": {"...": "ProjectBundle/1.0.0 canonical object: see golden fixture"},
  "extension": {"...": "WorkspaceExtension/1.0.0 canonical object: see golden fixture"},
  "warnings": []
}
```

**Ví dụ trên là shape minh họa với placeholders, không phải JSON hợp lệ theo JSON Schema.** Golden đủ nội dung được lưu tại `tests/fixtures/golden_v3.workspace_package.json`, và schema tại `contracts/workspace_package.schema.json` tham chiếu `project_bundle.schema.json` + `workspace_extension.schema.json`. Các schema được đóng gói **locally**; validator không được truy cập URL `$id` qua mạng.

Semantic invariants: `package.project_id == legacy_bundle.project_id == legacy_bundle.manifest.project_id == legacy_bundle.annotation_set.project_id == extension.project_id`; unique asset ID; reviews/evidence/corrections/conflicts trỏ asset/analysis/note/scene/frame có thật; thời gian trong duration; `source_sha256` đúng analysis; distinct IDs; UTC offset trong date-time; không NaN/Infinity, không JSON duplicate keys.

## 6. Định dạng ba nhóm report và nhập/xuất

| Artifact | Mục tiêu | Import canonical? |
|---|---|---|
| `video_report_ai.md` / `project_report_ai.md` | Người dùng đưa ChatGPT, DeepSeek để viết kịch bản | **Không**; chỉ prose, muốn import cần portable |
| `video_report_portable.md` | 1 clip V2-compatible, embedded `AnalysisReport` JSON checksum | **Có**, per-clip |
| `project_report_portable.md` (legacy V2) | Embedded `ProjectBundle` V2 | **Có**, legacy |
| `workspace_report_portable.md` | Embedded `WorkspacePackage` V3 | **Có**, preferred V3 |
| `analysis.json`, `project_bundle.json`, `workspace_extension.json`, `workspace_package.json` | Canonical model output | **Có**, strict schema+semantic+hash khi có manifest |

### 6.1 AI-friendly Markdown template (readable, nhỏ)

```markdown
# Video: example.mov
## Tóm tắt khách quan
- Chủ đề (nếu thấy rõ): ...
- Chưa chắc chắn: ...
## Dòng thời gian footage
| Asset | Khoảng nguồn | AI quan sát | Bằng chứng | Tình trạng |
|---|---|---|---|---|
| X | 00:00:06.000–00:00:12.000 | Mặt nước và phương tiện ven bờ | S0002, F0003–F0004 | sampled |
## Dữ kiện do người quay cung cấp
- [user_note: N1, clip X] “Lên phà lúc 05:00.” (Giờ thực tế do người dùng xác nhận, không phải timecode video.)
## Các chi tiết chưa thể xác minh
- Không thể suy ra thời gian chờ chính xác từ ảnh tĩnh.
```

Với project dài, profile `brief|standard|full`: brief gom theo chủ đề nhưng **giữ danh sách asset/timecode anchors và những cảnh quan trọng**. Nói rõ `partial` và những đoạn chưa quan sát. Ghi chú có thể chứa thông tin nhạy cảm; dialog export cho phép ẩn user notes nhưng không thay source canonical.

### 6.2 Workspace portable format (NORMATIVE)

YAML frontmatter **chỉ parse scalar keys whitelist**, không thực thi YAML tags:

~~~~text
---
phuthanhai_contract: phuthanhai.workspace_package/1.0.0
package_id: 0b63d2f8-b3a9-4bd7-a447-2f8f96a52789
project_id: 9b11527e-7cb6-4737-a716-29c5ae28d390
payload_sha256: <64 lowercase hex computed from canonical JSON>
---
# Báo cáo dự án PhuThanhAI (portable)
... human readable prose ...
<!-- PHUTHANHAI_WORKSPACE_JSON_BEGIN -->
```json
{ complete canonical WorkspacePackage JSON }
```
<!-- PHUTHANHAI_WORKSPACE_JSON_END -->
~~~~

**CHÚ Ý:** Marker trong minh họa ở trên cũng chính là marker **chính xác** trong file export thực tế. Dùng đúng hai marker dưới đây, mỗi marker đứng riêng một dòng:

- `<!-- PHUTHANHAI_WORKSPACE_JSON_BEGIN -->`
- `<!-- PHUTHANHAI_WORKSPACE_JSON_END -->`

Cả hai marker phải xuất hiện đúng một lần, đúng thứ tự, bao một fenced code block `json` duy nhất. Không scan marker bằng substring. Parser phải reject các marker giả trong text/JSON, frontmatter mismatch, duplicate keys, payload quá kích thước cấu hình, không valid UTF-8, NaN/Infinity và path traversal. Legacy marker trong V2 giữ nguyên như V2, V3 importer hỗ trợ cả hai.

### 6.3 Canonical JSON serializer và checksum

```python
import hashlib
import json


def canonical_json_bytes(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                       indent=2, allow_nan=False) + "\n").encode("utf-8")


def payload_sha256(payload: dict) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
```

**Tương thích V2:** code serializer phải bảo toàn thuật toán V2 (đã dùng `sort_keys=True`, `indent=2`, `ensure_ascii=False`, `allow_nan=False` và newline). Không thay đổi canonical serializer hoặc Unicode normalization của payload cũ **sau khi file đã xuất**; nếu normalize NFC, làm trước serialize và lưu record normalized. AI Coder phải test byte-for-byte trên legacy fixture.

- Tính digest trên canonical bytes, không tính toàn bộ Markdown. Trước khi export ghi `*.tmp` + `fsync`, validate lại, `os.replace` sang đích, rồi manifest commit. Nếu một output fail, trạng thái `export_incomplete`; UI không hiện `Hoàn tất` giả.
- Import flow: parse frontmatter restricted → extract exact code JSON → strict JSON decode (`object_pairs_hook` reject duplicate, `parse_constant` reject NaN/Infinity) → validate structural schemas + semantic rules → recompute digest → compare with frontmatter → map legacy source registry (user relink nếu thiếu) → commit transaction. Không chạy Python, shell, subprocess từ nội dung report.
- Khi import V2 project report, tạo extension rỗng; khi import V3, giữ nguyên `legacy_bundle` bytes in-memory semantically, không nhân đôi notes. Payload version unknown major bị reject với mã `UNSUPPORTED_CONTRACT_VERSION` và hướng dẫn upgrade.

## 7. Semantic validation mandatory; JSON Schema chưa đủ

Validation phải kiểm ở cả 4 object:

1. UUIDv4, timezone-aware date-time, name basename không path separators; source sha lowercase 64 hex; no duplicate JSON keys.
2. Project/analysis/asset IDs liên kết đúng; mỗi scene có `[start,end)` hợp lệ và frame IDs unique, PTS sorted; video VFR đúng normalized timeline, không vượt source duration.
3. Project annotation scopes đúng; `wall_clock` không tự map sang video; `user_asserted` không được đổi thành `observed`; note revised không tự làm raw evidence khác.
4. Asset duplicate filename hợp lệ nếu `asset_id` khác; exact SHA có thể trùng nếu user import cùng clip hai lần, nhưng không tự gộp notes.
5. Reviews: asset existent, selected ranges within duration, `last_playhead_ms` inclusive 0..duration, note IDs existent. Nếu không có raw analysis, reviews vẫn import được.
6. Evidence: analysis id match asset manifest, referenced frame IDs existent; interval source PTS hợp lệ; `method=multi_frame` chỉ được set khi benchmark-backed runtime used; không đổi `sequential_frames` thành video thật.
7. Corrections: source analysis, scene, frames belong correct asset; corrected text không sửa legacy.
8. Conflicts: annotation exists, same asset when clip-scoped, referenced evidence events exists; `status=unresolved` không bị gắn nhận định đúng/sai tự động.
9. Model/caches không có hệ thống đường dẫn `/Users/...` trong portable JSON fields. Với metadata videos có `source.filename` basename an toàn.
10. `source_hash` phải verify từ original bytes trước render; SHA từ proxy không đủ. Cache invalidation dựa `source_sha256+versions+config`, không chỉ `mtime`.
11. `complete` có coverage đã xác minh theo sampling policy; `is_every_frame_analyzed=false` luôn trong MVP; source audio không được mô tả nếu chưa có ASR.
12. Nếu thiếu clip ở máy import: giữ package ở trạng thái `needs_relink`; cho phép view/report nhưng khóa rendering. Không tự sửa path theo tên gần giống.

## 8. Invalidation & versioning

```text
User edits note -> annotation_set.revision ↑ -> overlay/report fingerprints change
            └-> no VLM reanalysis
User edits correction/review -> extension.revision ↑ -> report fingerprint changes
            └-> no raw evidence overwrite
Source bytes changed -> new SHA -> source-dependent samples/proxy/evidence stale
            └-> requires re-analysis/relink, no silent adoption
Model/prompt/processor/sampling config changed -> invalidate evidence cache for new run
Presentation profile changed -> only reassemble report, no model inference
```

- Không đổi required field hoặc type trong 1.0.0; nếu bắt buộc, tạo contract `2.0.0` mới + explicit migration & golden fixtures. Không tái sử dụng cùng `analysis_id` cho kết quả inference khác.
- Mọi output JSON phải ghi `kind/schema_version`. Các bảng SQLite dùng schema migration riêng, không coi `PRAGMA user_version` đồng nhất với portable schema.
- Paths trong DB cục bộ, Portable không chứa file paths; `asset_id` và digest là interface giữa UI, CLI, API, POV.

## 9. Test suite & ví dụ cần chạy từ thư mục V3

```bash
python3 tools/validate_specs.py
```

Script trong ZIP sử dụng `jsonschema` hiện diện trong môi trường kiểm tra (không tự cài), resolve `$ref` **bằng registry cục bộ**; kiểm V2 fixture và V3 golden, 2 negative fixtures (cross-project mismatch/time span outside source), semantic asset/note/scene/frame links, checksum và metadata. Kết quả PASS ở môi trường soạn tài liệu **không thay thế** các integration tests trên Mac người dùng.

**Những test phải bổ sung khi code app:** V2 standalone MD import; V2 bundle import; V3 package roundtrip; JSON duplicate keys fail; malformed marker fail; oversized file fail; annotation scope fail; 05:00 wall-clock preserved; note conflict diff; nested filesystem path rejected; corrupt video/relink; VFR end exclusive; corrupted cache; atomic write crash.

## 10. Contract-freeze checklist trước S01 & S06

- [ ] Legacy `AnalysisReport/1.0.0` và `ProjectBundle/1.0.0` schemas không đổi; schema local refs resolve offline.
- [ ] WorkspaceExtension & WorkspacePackage schemas + positive/negative fixtures PASS.
- [ ] V3 `.md` portable parser có marker/checksum đúng; AI-friendly `.md` không bị hiểu nhầm có thể import.
- [ ] User note tách biệt visual evidence; project/clip/range scopes và `wall_clock` khác PTS.
- [ ] VFR + multiple clips có asset ID ở mọi shot. No silent source remap.
- [ ] Cache invalidation và mọi corrections có provenance; backward migration giữ nguyên user data.
- [ ] CLI/API và UI cùng dùng một semantic validator, không tạo hai ý nghĩa timecode khác nhau.

> **End of normative contract.** Nếu một tài liệu khác trái với file schema + bất biến trong file này, AI Coder phải báo conflict và dừng gate; không tự đoán rồi code vượt qua.
