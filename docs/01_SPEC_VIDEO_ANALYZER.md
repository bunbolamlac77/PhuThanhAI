# PhuThanhAI V3.0 — 01_SPEC_VIDEO_ANALYZER

> **P0 / LÀM ĐẦU TIÊN** • Universal Video Analyzer, Media Library, Quick Review, Per-clip Notes. Gate S00–S05 trong `00_MASTER_ARCH.md`. Mọi video đời thường, cưới truyền thống, sự kiện, du lịch, hậu trường, phim sản phẩm đều dùng cùng pipeline thị giác; **POV không phải yêu cầu để dùng Analyzer**.

## 1. Mô hình tương tác người dùng: xem nhanh + ghi chú + phân tích tùy chọn

```text
┌───────────────────────────────────────────────────────────────────────────────────────┐
│ PhuThanhAI     [Thư viện & Phân tích] [AI Video POV] [Phần 3] [Phần 4]       [⚙]     │
├───────────────────┬───────────────────────────────────┬───────────────────────────────┤
│ PROJECT / LIBRARY │ PLAYER / TIMELINE                 │ GHI CHÚ / BẰNG CHỨNG         │
│ [+ Thư mục]       │ [ Video preview ]                 │ [Ghi chú toàn clip...]       │
│ [Tìm / Bộ lọc]    │ [00:01:14 / 00:03:50]             │ [Gắn vào đoạn tại playhead]  │
│ [✓ 001.mov  ★]    │ [Play][Prev][Next][Speed]        │ Đồng hồ thực tế: [05:00]     │
│ [· 002.mov   ]    │ [In/Out][Đánh dấu][Thumbnails]    │ AI nhìn thấy: ...            │
│ [! 003.mov   ]    │ Điều hướng bằng phím              │ Chưa chắc chắn: ...          │
│ ...virtualized    │ Thông tin: 4K / 29.97 / codec     │ [Sửa mô tả có audit]        │
├───────────────────┴───────────────────────────────────┴───────────────────────────────┤
│ [Phân tích clip] [Phân tích các clip đã chọn] [Hủy/Tiếp tục] [Xuất MD] [Xuất Portable]│
└───────────────────────────────────────────────────────────────────────────────────────┘
```

**Happy path 1:** Chọn thư mục 300 clip → thư viện xuất hiện khi index đang tăng → click clip #20 → xem video, tua bằng J/K/L, nhập "Lên phà lúc 5 giờ sáng" → badge `Đã lưu` → sang clip tiếp; không cần chạy model.

**Happy path 2:** Không ghi chú → chọn 15 clip → `Phân tích` → máy chạy lần lượt → báo cáo thị giác từng clip và báo cáo tổng hợp của project; người dùng đưa `project_report_ai.md` cho ChatGPT để lên kịch bản thể loại tùy ý.

**Happy path 3:** Có ghi chú toàn clip + một ghi chú đoạn `[00:00:08.000, 00:00:22.000)` → analyze → UI có **hai cột minh bạch** `AI đã quan sát` và `Người dùng bổ sung`. Mọi fact đi kèm `asset_id` và chứng cứ. Người dùng có thể sửa câu nhận diện sai mà không xóa bản gốc.

## 2. Thư viện media — yêu cầu chi tiết S01

### 2.1 Import/index không chiếm RAM

- Nhận `.mp4`, `.mov`, `.m4v`, `.mkv` và format ffprobe giải mã được; không dựa vào extension. Folder scan dùng `os.scandir` có phân trang, **không đọc toàn bộ video vào RAM**. Không theo symlink ngoài root đã cho phép; không quét vô hạn hay đi vòng với symlink; idempotent khi import cùng file.
- Sinh `asset_id` UUID4 bền vững. Metadata: basename, relative location nội bộ, size, mtime_ns, inode/dev nếu được, `duration_ms`, resolution/rotation, FPS nominal/VFR hint, codec, streams, color matrix, audio yes/no, `preview_status`, `hash_status`, `analysis_status`.
- `created_at/recording_at` lấy từ metadata chỉ là **suggested capture time**; user-entered clock time lưu riêng và có nguồn. Media không có timestamp đáng tin không được tự suy giờ chụp từ filesystem mtime.
- S01 chỉ lập fast identity; chỉ tính full SHA-256 khi cần phân tích/export portable/dựng; đọc theo chunk, chỉ một hash-heavy worker nếu disk pressure. Exact duplication dựa full SHA-256; near duplication pHash là flag. Không tự xóa video hay note khi thấy file trùng.
- Khi folder bị di chuyển/unmount, registry đổi `source_state=missing`; preview/analysis cũ vẫn xem được nếu có cache, nhưng không được render; có nút `Liên kết lại nguồn` và verify SHA trước khi khôi phục.

### 2.2 UI danh sách lớn

- Fetch API phân trang cursor/keyset; chỉ render các thẻ đang thấy (virtualized). Scroll qua 1,000–5,000 assets không tạo tương ứng 5,000 video tags. Thumbnail `loading=lazy`, max 1 request thumbnail cho mỗi asset visible/near-visible; debounce tìm kiếm.
- Sắp xếp: tên tự nhiên, capture time (ghi nguồn), import order, duration; lọc: chưa xem, favorite, rating, có note, đã phân tích, lỗi decode, tag, thể loại. Batch select để phân tích/xuất/xếp hàng; search theo filename/tag/note/đoạn mô tả đã tạo.
- Người dùng có thể tạo collection/playlist logic bằng danh sách asset IDs; không nhân bản video vật lý.
- Keyboard: `Space` play/pause, `J/K/L` tương ứng tua ngược/tạm dừng/tua tới nếu player hỗ trợ; `←/→` nhảy ±5 s; `I/O` đặt in/out; `N` focus note; `[`/`]` clip trước/sau. **Không bắt phím tắt khi người dùng đang nhập textarea, input, combo hoặc có IME tiếng Việt**.

### 2.3 Player và proxy on-demand

1. Khi click, backend trả metadata và URL stream theo `asset_id`, có auth và **HTTP Range 206**; frontend chỉ giữ **một** player đang decode, stop/release blob/object URL khi chọn clip khác.
2. Thử phát source direct khi format/codec/sự ổn định browser cho phép. Nếu nhận `canPlayType`/decode error hoặc seek quá chậm, queue tạo **proxy H.264 720p/540p AAC MP4** với faststart và metadata mapping. Người dùng vẫn có thể thấy thumbnail trong lúc tạo proxy.
3. Proxy phải bảo toàn thứ tự hình ảnh và ánh xạ thời gian nguồn: lưu `proxy_duration_ms`, offset/mapping nếu khác PTS. Không dùng proxy timecode để cắt footage chính xác khi mapping chưa được xác minh.
4. Chỉ tạo proxy cho clip được chọn (hoặc 1–2 clip tiếp theo nếu dung lượng và memory cho phép); cache LRU theo quota; không tạo mọi proxy khi scan folder. Proxy never overwrites footage.
5. Trên Mac có thể làm **spike AVPlayer** để hỗ trợ ProRes tốt hơn; chỉ giữ nếu đo UX/CPU/sự phức tạp cải thiện, không ghép 2 player vào MVP.
6. Seek từ thumbnail/chú thích về đúng source timecode với sai số tối đa 1 frame ở fixture CFR và sai số PTS có thể chứng minh ở VFR. `seek-complete`/`seek-error` phải có feedback.

### 2.4 Ghi chú và autosave

- Ô note toàn clip và note dự án. Với time-range: user đặt playhead/in-out, chọn `Ghi chú cho đoạn`, time range `[start_ms,end_ms)` theo **timeline của clip**, không lấy ngày giờ 05:00 làm `start_ms=300000`.
- Phân tích chuỗi `lên phà lúc 5h sáng` để đề xuất `wall_clock.time_of_day=05:00`, nhưng cần user sửa/accept khi parser không chắc. `date=null`, `timezone=null` nếu chưa biết; không tự đoán ngày/múi giờ hoặc time-of-day từ frame tối.
- Mỗi annotation có ID, revision, scope (`project|clip|time_range`), text Unicode NFC, source=`user_annotation`, timestamps, `assertion_status`. Optimistic concurrency: `PUT` với `expected_revision`; 409 khi stale, UI cho lựa chọn giải quyết, không silent last-write-wins.
- Debounce 300–600 ms, flush khi blur/đổi clip/quit, hiển thị `Đang lưu… / Đã lưu / Lỗi lưu` và retry. Test kill ứng dụng ngay sau gõ/blur/switch; không dùng localStorage làm canonical store.
- Note có thể rỗng; không ép user phải nhập trước phân tích. Ghi chú có thể sửa sau phân tích, khi đó **chỉ invalidates contextual overlay/report**, không invalidates VLM raw evidence.
- `Ghi chú người dùng` là *assertion* không phải `observed`. Ví dụ: UI report có thể ghi:

> Dữ kiện bạn cung cấp: “Lên phà lúc 05:00.”  
> AI quan sát: “Có sông và phương tiện ven bờ; không xác minh giờ thực tế hoặc việc chờ 30 phút từ khung hình.”  
> Trạng thái: `user_asserted` / `visual_not_verified`.

- Khi note mâu thuẫn ảnh (`AI: trời sáng` vs `user: 05:00 sáng`) **không xóa note** và không chép note vào visual fact; tạo `conflict_candidate`, yêu cầu người dùng review. Việc trời sáng có thể do ngày/mùa/thiết lập camera; không được tự kết luận ai sai.
- Không tự gắn note về người A qua mọi clip nếu user chỉ ghi cho một clip; project-level note có scope project và cần hiển thị phạm vi áp dụng.

## 3. Máy trạng thái, hành vi khi chuyển clip

| Entity | States | Quy tắc chuyển |
|---|---|---|
| `asset.preview` | `unprobed → direct_ready → proxy_queued → proxy_ready` hoặc `error` | lỗi preview không xóa asset/notes |
| `asset.analysis` | `not_started → queued → sampling → inferring → assembling → complete` hoặc `partial/failed/cancelled` | cache & evidence vẫn giữ khi partial |
| `annotation.save` | `clean → dirty → saving → saved` hoặc `conflict/failed` | không chuyển clip mà bỏ dirty state âm thầm |
| `source_state` | `available/changed/missing/relinked` | source changed phải invalidate inference cache đúng scope |
| `job` | `queued/running/cancelling/cancelled/partial/failed/complete` | `complete` chỉ sau manifest/report checksum kiểm tra |

UI có thể di chuyển giữa tab mà job vẫn chạy, có `Job Drawer` cho progress. Không có tình trạng callback UI duy nhất khóa toàn bộ màn hình.

## 4. Pipeline phân tích thị giác 2 lượt — S02/S03

### P0 — Kiểm tra nguồn và PTS

- `ffprobe -v error -show_format -show_streams -of json` đọc streams, rotation/display matrix, HDR flags, duration, start PTS, color transfer, sample aspect ratio. Decode sample đầu/giữa/cuối để xác định nguồn không hỏng. Ngưỡng filesize/disk tạm theo config, không cố copy 4K video nguyên.
- Trục chuẩn `time_ms=0` tại **frame đầu được hiển thị**, không suy timestamp bằng `frame_index/fps` với VFR. Frame extraction log frame PTS thực hoặc được decoder chứng thực; chuẩn hóa thứ tự frame và end-exclusive. API seek/FFmpeg -ss có khác biệt theo vị trí option nên cần integration golden PTS.
- Nếu nguồn thay đổi giữa job, phát hiện fingerprint + full hash khi cần; **fail/restart** thay vì trộn frame từ 2 phiên bản.

### P1 — Quét nhanh toàn timeline, chưa load AI

- FFmpeg `scdet/scene` heuristic cắt cảnh ở stream downscaled; song song tính chỉ số blur/darkness/light jump/motion proxy (CV tùy cần) với giới hạn CPU. **Motion score chỉ là đo chuyển động hình**, không coi là hành động có nghĩa.
- Bắt buộc `0` và `duration_ms` làm ranh giới; hard cuts, dissolve/no-cut, cảnh tối/thiếu ánh sáng, đoạn chuyển động nhanh, video tĩnh và camera di chuyển đều có fallback phân đoạn theo thời lượng.
- Sắp xếp candidate `SceneWindow` theo timeline không overlap/gap; long scene chia `subwindow` theo max gap để kiểm soát coverage. Mỗi candidate có `asset_id`, `start_ms`, `end_ms`, cues, shot-quality flags; **không suy nội dung** từ cue.
- Kết quả quét được cache theo source hash + ffmpeg/filter/config version; khi note thay đổi không quét lại.

### P2 — Lập kế hoạch lấy mẫu thích ứng

Mỗi `SceneWindow`:

1. Chọn frame gần đầu/cut + giữa + gần cuối; tránh đen/blur/transition bằng cách tìm trong cửa sổ lân cận và ghi offset thực; nếu frame thiếu thì ghi `missing_observation`.
2. Giới hạn khoảng cách giữa **các frame dự kiến đã decode hợp lệ** theo profile: `quick` 12s, `detailed` 6s, `thorough` 3s **là seed chính sách V2, G2 có thể benchmark lại**. Không dùng mốc cứng làm bằng chứng đầy đủ sự kiện.
3. Dành ngân sách **event refinement** cho đoạn có motion/cut/text/subject change; thêm 2–4 frame tuần tự (nếu đủ ngân sách) để nhận hành động. Khi backend MLX-VLM thiếu multi-frame, phân tích từng frame rồi tạo `temporal_candidate` với nhãn `sequential_frames`, không nói model đã nhìn clip liên tục.
4. Ưu tiên quality với thuật toán đơn giản: blur Laplacian, exposure histogram, rough duplicate pHash, rule-based ranking. Không áp dụng model mới để tiết kiệm RAM.
5. Mọi sample lưu `frame_id`, `asset_id`, `source_pts_ms`, `scene_id`, blur/exposure flags, cache path nội bộ. Kích thước ảnh mặc định 768–896px cạnh dài, tối đa một frame/infer cho đường an toàn; profile thorough phải benchmark trước.
6. Segment dài/gián đoạn giữa chừng phải checkpoint theo chunk; không cố giữ tất cả frame trong RAM. Cache preview có thể chung ảnh thumbnail nhưng metadata PTS vẫn độc lập.

### P3 — VLM chạy riêng và chống hallucinatory context

- `Qwen3-VL-4B-Instruct-4bit` tại model local. **Chạy lần 1 visual-only**, prompt chỉ miêu tả những gì frame/sequence cho thấy; model không nhận ghi chú người dùng trong giai đoạn này.
- Output JSON strict: mô tả, objects, visible actions, camera/lighting, visible text, uncertainties, source_frame_ids và sample mode. Parser/schema validator reject dữ liệu thừa/lệnh ngoài protocol; retry cấu trúc giới hạn, nếu vẫn sai thì warning/partial.
- Thử nghiệm multi-frame ở G3: 1 frame baseline → 2 frames → 4 frames short sequence, so sánh RAM peak, throughput, lỗi, temporal accuracy. API Qwen3-VL hỗ trợ video ở một số backend, **MLX snapshot trên máy không được mặc định tương đương**. Chỉ bật `multi_frame` nếu real smoke pass; nếu không dùng `sequential_frames` và ghi rõ giới hạn.
- ModelWorker process group duy nhất: load một lần cho job/batch, JSONL events, max tokens/timeout. Controller có single heavy slot và ResourceGovernor; worker exit/reap trước POV LLM/TTS/FFmpeg heavy. Khi OOM giữ checkpoint, giảm resolution/visual tokens và retry tối đa 2.
- `TemporalEvidence` được tạo **từ các frame/clip thực sự đã xử lý**, với interval/frames và `method`; nhãn event không được tự diễn giải lịch sử/phong tục/quan hệ người chỉ từ trang phục.

### P4 — ContextFusion không phá evidence

```python
# Contract interface, không phải implementation đã có
analyze_visual(asset_id: str, sampling_plan_id: str) -> VisualEvidenceSet
contextualize(evidence: VisualEvidenceSet, notes: AnnotationSet) -> ContextOverlay
validate_context(evidence: VisualEvidenceSet, overlay: ContextOverlay) -> ValidationReport
assemble_reports(project_id: str, asset_ids: list[str]) -> ReportArtifacts
```

- Input `evidence` bất biến đã hash; `notes` tại revision cụ thể; output overlay luôn chứa `note_id` nếu dùng user assertion; `VisualEvidenceSet` không bị sửa bởi notes. Nếu đổi note revision, re-create overlay/report, không re-run VLM.
- Kết quả mô tả được tách: `Directly observed`, `User-provided context`, `Tentative inference`, `Unknown`. Mọi câu chứa dữ kiện người dùng phải có nguồn, không dùng từ "AI phát hiện" cho thông tin chỉ do người dùng cung cấp.
- Người dùng được sửa `scene.summary` thông qua `CorrectionOverlay`: ghi old/new, scene/asset, author=user, timestamp, raw evidence refs. `Reset correction` trở về model text; không sửa log gốc.

### P5 — Export phổ quát và không phình context

Cho từng clip:

- `analysis.json`: **AnalysisReport/1.0.0** như V2, raw visual-only canonical.
- `video_report_ai.md`: ngắn, dễ đọc ChatGPT, bố cục: metadata, summary theo thể loại, timeline có asset/timecode, notes, events, unknown; không nhúng JSON lớn; **không import ngược**.
- `video_report_portable.md`: có `AnalysisReport` JSON + checksum để import V2-compatible; nếu cần cả annotation/extension thì phải dùng `workspace_report_portable.md`.

Cho project đa clip:

- `project_report_ai.md`: giới thiệu dự án, danh mục footage/asset IDs, timeline **riêng từng clip**, gợi ý shot theo score **có phương pháp**, ghi chú theo scope, các mâu thuẫn/chưa chắc chắn và công thức nội dung phổ quát. Chế độ `brief/standard/full`, không bỏ cuối footage im lặng. Có `estimated_context_tokens` chỉ là ước tính.
- `project_bundle.json`: **ProjectBundle/1.0.0** đúng V2; thêm `workspace_extension.json` dữ liệu review/corrections, `workspace_package.json` container 2 phần, và `workspace_report_portable.md` (full package JSON + SHA-256 canonical). Legacy ProjectBundle import vẫn hợp lệ; không thêm trường vào bundle cũ `additionalProperties:false`.
- `manifest.json`: model/runtime versions, source hashes, source mapping, export revision và file hashes; report portable không chứa absolute path, icon thumbnails/footage binary. Mỗi output được ghi atomic + fsync + reload/validate trước đánh `complete`.
- Nút `Chuyển sang POV`: truyền `project_id`/`workspace_package_id` nội bộ, kiểm hash/notes revision. **Không tự chạy Qwen3-8B** ở Phase 1.

## 5. Cache/checkpoint/incremental recomputation

| Thay đổi | Cache nào phải refresh? | Giữ lại được |
|---|---|---|
| Sửa note/giờ thực tế | ContextOverlay + human/AI report | Frame, VLM raw observations, proxy |
| Đổi tag/star/rating | Search index, UI, export if included | Model results |
| Sửa mô tả bằng human overlay | Report + downstream selected script nếu phụ thuộc | Raw evidence |
| Đổi profile sampling hoặc model/prompt | Sampling + visual evidence (new run ID) | Original media, note, proxy nếu codec vẫn phù hợp |
| Nguồn file thay đổi hash | All source-dependent frame/proxy/evidence | User notes review, với cảnh báo re-link / remap |
| Chỉ đổi chủ đề báo cáo `wedding → documentary` | Narrative assembly | Frame + raw evidence |

Checkpoint mỗi scene/chunk, có `schema_version`, `job_id`, `source_sha256`, version processor/model/prompt, config digest, last committed frame IDs; resume chỉ khi **tất cả** fingerprint liên quan match, không tạo frame trùng. Write event theo JSONL append-only với dedup key `job_id+frame_id+stage`. Đóng app giữa chừng không mất cache/evidence đã commit.

## 6. Bảo mật, media untrusted, file phục hồi

- Không pass filename vào shell string; dùng FFmpeg argv. Giới hạn subprocess timeout, file size/output size, streams và decode resource. Giữ stdout JSON, stderr logs riêng. Bất kỳ text OCR/video/notes đều là dữ liệu chưa tin cậy, không được phép điều khiển tool/FS/network qua prompt injection.
- API preview/thumbnail/range chỉ qua `asset_id` registry và session token; kiểm tra realpath, symlink, nguồn user chọn, Host/Origin; cấm `..` path, local URL injection, mở file home/models vì một asset_id giả.
- Không lưu trực tiếp JSON prompt chứa khuôn mặt/địa chỉ/điện thoại vào log mặc định; report xuất có nút ẩn note riêng tư/tên người/đường dẫn theo selection nhưng không âm thầm xóa các fact của bản gốc.
- Import untrusted Markdown: strict marker, parse JSON, checksum/schema/semantic validation, size limits, duplicate key rejection, no eval/pickle, no arbitrary remote includes.

## 7. E2E & quality test matrix để Gate G1–G5

| Fixture/Test | Đầu ra phải đúng |
|---|---|
| 1,000 fake assets + 20 playable clips | Scroll/search/filter mượt, DOM virtual, không phát nhiều video cùng lúc, đo p95 |
| `.mov` ProRes, `.mp4` H264/H265, VFR 29.97, portrait rotated, HDR | Probe/preview/proxy fallback, đúng rotation và mapping timecode; cảnh báo HDR nếu convert |
| Folder có symlink loop, duplicate names, file corrupt, read permission denied | Không crash/index loop, lỗi từng clip, không xóa nguồn |
| Note 05:00, note time range 00:08–00:22, note rỗng | Wall clock khác clip timecode, optional note không chặn analyzer |
| Kill app sau gõ note / đổi clip thật nhanh / revision conflict | Không mất note confirmed, không overwrite revision stale, báo lỗi conflict rõ |
| 2 frames mâu thuẫn note / model hallucinated action | Overlay conflict, không chép note thành observed fact |
| No-cut 20 phút, rapid-cuts, đen/tối, video rung/motion | Scan có intervals đầy đủ, sampling metrics đúng và cảnh báo thiếu evidence |
| FFmpeg fail/worker OOM/cancel/resume/quit | checkpoint consistent, no orphan, partial/failed không đổi thành complete |
| 3 thể loại: cưới truyền thống / đời thường / sự kiện (>=10 clip/genre có nhãn) | Báo cáo trung tính, không tự biến thành kịch bản POV, reviewer đánh đúng sự kiện/không bịa facts |
| V2 import + V3 export + import lần nữa | V2 payload không đổi, extension roundtrip, SHA/asset/timecode đúng |

**Rubric nội dung đề xuất (phải chốt threshold sau khi tạo golden dataset):** mỗi fact được reviewer đánh `supported/unsupported/ambiguous`; đo precision; mỗi event chính đánh `detected/missed`; đo recall; validate tất cả timecode trong source. Không tự đưa các xác suất chắc chắn do LLM sinh làm điểm độ tin cậy. Báo cáo G5 phải có confusion/error cases thật và thay đổi sau review.

## 8. Lỗi ổn định theo mã (không nuốt exception)

`SOURCE_MISSING`, `SOURCE_CHANGED`, `CODEC_UNSUPPORTED`, `PROXY_FAILED`, `RANGE_NOT_SATISFIABLE`, `ANNOTATION_CONFLICT`, `ANNOTATION_SAVE_FAILED`, `HASH_MISMATCH`, `MODEL_SNAPSHOT_MISSING`, `MODEL_RUNTIME_INCOMPATIBLE`, `MODEL_OOM`, `WORKER_TIMEOUT`, `WORKER_ORPHAN_REAP_FAILED`, `PTS_UNVERIFIED`, `SAMPLING_GAP`, `REPORT_SCHEMA_INVALID`, `PORTABLE_CHECKSUM_MISMATCH`, `STORAGE_FULL`, `SECURITY_DENIED`.

Mỗi lỗi có `code`, `job_id?`, `asset_id?`, `retryable`, `user_message`, `debug_details_redacted`, `next_action`. UI chỉ dùng mã để hiển thị text và hành động hợp lệ; không bao giờ hiện toàn bộ Python traceback cho người dùng.

## 9. CLI/API và tiêu chí DoD

**CLI mục tiêu sau S00 (không phải lệnh đã có):**

```bash
cd "/Users/phuthanh/APP_CaNhan/PhuThanhAI"
uv run phuthanhai doctor --offline
uv run phuthanhai serve --host 127.0.0.1
uv run phuthanhai scan --project PROJECT_ID --directory "/path/to/footage"
uv run phuthanhai analyze --project PROJECT_ID --asset ASSET_ID --profile detailed
uv run phuthanhai export --project PROJECT_ID --format ai-md
uv run phuthanhai validate --input "/path/to/workspace_report_portable.md"
uv run pytest -q
```

G0 phải triển khai hoặc sửa CLI sao cho code/docs/README đồng nhất; không tự tuyên bố các lệnh hiện đã chạy. **Analyzer DoD:** nhập thư mục → preview clip → lưu note → tắt/mở app vẫn còn → phân tích offline → review evidence/correction → xuất AI-friendly/portable → import lại → Spotlight mở/đóng, không orphan, không sửa clip nguồn; có log G0–G5 thực.
