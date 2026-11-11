# PhuThanhAI V3.0 — Milestones & Acceptance Checklist

> Checklist **triển khai trên Mac của người dùng**. Các ô bên dưới ban đầu để trống: tài liệu được tạo không có nghĩa một bước đã PASS. AI Coder phải cập nhật từng mục từ log thực và ghi người thực hiện, ngày, commit.

## Giao ước chung cho mọi gate

- [ ] Ghi `MILESTONE`, commit hash, cây file thay đổi, lệnh thật đã chạy và exit code.
- [ ] Có unit test, integration test, E2E và log test thật; ghi passed/failed/skipped, **không** giấu test skipped.
- [ ] Có `device_metrics.json` với thông số máy, OS, các bản Python/MLX/FFmpeg, peak RSS, memory_pressure, swap nếu API đo được.
- [ ] Có rollback/migration notes, ảnh chụp/screen recording QA UI khi có UI.
- [ ] Không sửa/xóa video nguồn hoặc AIVideo model/runtime; không dùng cloud/API key cho core offline.
- [ ] Bảo đảm `GATE: PASS` chỉ nếu tất cả hạng mục P0 của bước PASS; `NOT VERIFIED` khi chưa chạy máy thật.

## S00 — Foundation & Doctor — Gate G0

> **AI-run result on 2026-11-11** (`MacBookPro18,3`, macOS 26.7.1, M1 Pro, 16 GiB): **PASS**.
> Evidence: `artifacts/qa/S00/{gate.md,gate.json,doctor.json,doctor_deep.json,tests.json,pytest.xml,vlm_smoke.json,ram_repeats.json,server_e2e.json,network_check.json,orphans.json,device_metrics.json}`.
> No commit hash: this workspace has **no git repository** (not initialized; not created without the owner's decision).
> Re-run everything with `python3 tools/run_s00_gate.py`.

- [x] Tạo `src/phuthanhai`, `pyproject.toml`, uv lockfile, môi trường Python riêng, không mutate legacy venv.
      → `.venv` Python 3.12.11 + `uv.lock`; `AIVideo` models/runtime opened read-only, mtimes unchanged.
- [x] Tạo FastAPI localhost + React/TS/Vite shell 4 tabs, SwiftUI launcher scaffold và fallback browser.
      → `web/dist` built locally (`npm install --offline`, `npm run build`); launcher compiled to `Mach-O arm64`;
      browser fallback = same React bundle served at `/`. WKWebView runtime rendering is **not** verified yet (S05).
- [x] `doctor --offline` kiểm paths, snapshots, versions, FFmpeg/ffprobe, Metal/MLX compat, free disk và filesystem permission.
      → 17 checks, `RESULT: PASS`; `--deep` verified SHA-256 of 5 visual-model artifacts against the install manifest.
- [x] Chạy V2/V3 schema+fixture+semantic+portable test (tool trong ZIP); triển khai cùng validator trong app.
      → `tools/validate_specs.py` PASS + app validators (`contracts/validators.py`, `selfcheck.py`); 105 tests.
- [x] Chạy 1 frame **thật** qua model Qwen3-VL local theo API `mlx-vlm` đã cài và capture stderr/peak RSS; nếu thất bại báo G0 BLOCKED.
      → 504×896 frame at 5000 ms from local 4K HEVC **portrait** footage (stored 3840×2160, display matrix
      −90 → 2160×3840); mlx-vlm 0.7.10, Metal true; load 0.8–1.5 s, infer 6.5–8.2 s warm (9.5–17.6 s
      cold), MLX peak ≈4.11 GiB, worker peak RSS 2.26–3.44 GiB across recorded runs. Sampler is
      rotation-agnostic (regression test with a portrait fixture). Run under `sandbox-exec` `deny network*`.
- [x] Server không cho remote origin/path traversal; no token in URL, no public share; app offline frontend không CDN.
      → host `evil.example.com` → 400, origin `http://evil.example.com` → 403, traversal → 400, POST without
      capability → 403, capability file mode `0600`, bundle has zero remote attributes/fetches.
- [x] No model loaded khi chỉ mở UI; shutdown server process/reap child; single instance.
      → `model_loaded_in_process=false` in `/health`; `fcntl` instance lock; `PROCESSES.shutdown()` on lifespan exit;
      `orphans.json` = 0 leftover worker/server processes after the run.
- [x] Gate report có bằng chứng.
      → `artifacts/qa/S00/gate.md`.

**S00 limitations recorded (not hidden):** WKWebView playback/file-picker not exercised; `.app` bundle/icon/Spotlight
are S05; the library/player/notes/sampler endpoints do not exist yet (S01–S02); `AI_HEAVY` arbitration is unit-tested
but has no job queue until S03; `swift build` fails due to a broken Command Line Tools `swift-package` binary —
the scaffold is compiled directly with `swiftc` against `MacOSX26.5.sdk` instead.


## S01 — Media Library / Quick Preview / Notes — Gate G1

> **AI-run result on 2026-11-11** (`MacBookPro18,3`, macOS 26.7.1): **G1 PASS (10/10 gate steps, 185 tests)**.
> Evidence: `artifacts/qa/S01/{gate.md,gate.json,pytest.xml,bench.json,scan_real.json,api.json,security.json,resources.json,restart.json,e2e/}`.
> Regenerate: `PHUTHANHAI_TEST_FOOTAGE=<footage> python3 tools/run_s01_gate.py`.
> No commit hash is printed here; the milestone commit is in git history.

- [x] Scan folder idempotent, file permission/symlink loop/corrupt file không crash; asset UUID bền vững.
      → scanner tracks `(dev,ino)`, skips symlinks, records corrupt files as `source_state=error`, re-scan `added=0`;
      asset ids stable across scans. 1,000 synthetic + 54 real clips.
- [x] SQLite WAL + migrate, projects/assets/annotations/review/jobs/cache; backup restore làm được.
      → 7 tables, `PRAGMA user_version`, WAL + busy_timeout, `BEGIN IMMEDIATE` writer, SQLite backup API tested.
- [x] Grid/list virtualized + tìm/ lọc/ tag/star/rating; test **1,000 fake assets** và fixtures thật.
      → 1,000 assets render **18 DOM cards**; natural sort/search/rating/favorite/tag filters; batch favourite.
- [x] Playback H264/HEVC/MOV/ProRes có fallback proxy on-demand; HTTP Range 206, chỉ một video player hoạt động.
      → H.264 and 4K HEVC portrait play in Chrome (`readyState 4`, 2160×3840); HEVC→H.264 720×1280 proxy on
      demand, duration delta 0 ms; Range 206/416 verified; exactly one `<video>` element. **ProRes N/A** (no fixture).
- [x] Seek, keyboard shortcuts, in/out và clip next/prev; timecode source/proxy mapping đúng golden.
      → shortcuts implemented (IME/text guard); E2E asserts `]`/`[` navigation and playback; proxy mapping probed
      against source duration. Space/J/K/L/I/O use the same handler but are not browser-asserted.
- [x] Note clip/project/time range, `wall_clock=05:00` không thành PTS; autosave flush, 409 revision conflict,
      kill/restart không mất note đã xác nhận lưu.
      → clip + project + time-range notes; suggestion `05:00` only after user accepts; `start_ms` stays null after
      restart; stale revision 409; E2E typed inside the debounce window, switched clip, note flushed with no
      cross-clip leak; note survives backend restart.
- [x] File source read-only, proxy cache bounded LRU, no background transcode toàn thư viện.
      → source size/mtime unchanged after scans; thumbnails/proxies only under `caches/`; LRU quota eviction never
      touches source; proxy generated only when requested.
- [x] G1 report có p95 library load/scroll/notes latency + screenshot và test thật.
      → list p95 4.26 ms, search p95 4.65 ms, note save p95 1.60 ms; server RSS 57.9 MiB; screenshots
      `artifacts/qa/S01/e2e/{bench,real,ui_flow}.png`.

**G1 limitations:** ProRes untested (no fixture); WKWebView/`.app`/Spotlight remain S05; collections/playlists not
implemented; E2E runs in headless Chrome, not WKWebView.

## S02 — Two-pass Sampling/PTS/Checkpoint — Gate G2

- [ ] FFprobe PTS start/offset + VFR/timebase, rotation, HLG/PQ/HDR, audio streams.
- [ ] Scan toàn timeline bằng FFmpeg CV cheap; hard cuts/no-cut/dissolve/dark/motion; scene intervals cover timeline.
- [ ] Lập frame plan adaptive và event refinement; frame PTS decode verified, không đoán index/fps.
- [ ] Cache đúng fingerprint và giữ full SHA; thay nguồn phải invalidate; note sửa chỉ invalidate context overlay.
- [ ] Checkpoint/crash-resume không làm trùng frame; partial warnings đúng; test long clip + many-cuts.
- [ ] Có dữ liệu baseline speed/size/disk footprint trên Mac.

## S03 — Qwen3-VL offline / ModelScheduler — Gate G3

- [ ] VLM subprocess đọc frame path qua validated IPC JSONL, không import MLX trong API/UI.
- [ ] Prompt visual-only không nhận user note; output facts/uncertainties với frame refs.
- [ ] Benchmark 1 frame baseline, 2 và 4 frames **chỉ nếu API runtime cho phép**; chọn fallback single-frame khi bất ổn.
- [ ] Heavy slot=1 toàn server, ResourceGovernor áp lực RAM, real OOM handling, timeout/cancel/retry/reap.
- [ ] Job progress, logs, recovery; UI xem clip và ghi note được trong khi AI xử lý.
- [ ] Test concurrently click analyze + POV placeholder, restart/kill không orphan; measurement peak RSS thật.

## S04 — Reports / Context Fusion / Legacy migration — Gate G4

- [ ] Visual observations và user annotations tách nguồn; conflict candidate hiển thị, không hallucinate time-of-day.
- [ ] Review corrections có version, asset+analysis+scene/frame refs; raw evidence unchanged.
- [ ] Export `video_report_ai.md`, `project_report_ai.md`, legacy JSON/portable V2 và V3 workspace package/portable.
- [ ] V2 import→V3 export→import again **roundtrip** giữ note/source/scene IDs/PTS; malformed JSON/digest fail.
- [ ] Note revision updates reports mà không reload VLM; user selected reporting profile chỉ đổi narrative.
- [ ] Markdown AI-friendly dễ hiểu mọi thể loại, không buộc POV/wedding; có partial/unknown warnings.
- [ ] Test security: report nội dung OCR/note không thực thi lệnh, path tuyệt đối không leak.

## S05 — Product QA/UX & Spotlight — Gate G5

- [ ] App `.app` mở qua Spotlight, icon riêng, bundle/signing phù hợp, no duplicate backend.
- [ ] WKWebView hoặc fallback browser chạy đúng file picker/seek/Range/CORS, không dùng CDN; đóng app flush note/reap.
- [ ] 3 video genres có golden evaluation by human: wedding traditional, daily vlog/travel, event/BTS.
- [ ] Đo metric content precision, key-event recall, timestamp accuracy, unsupported claims; ghi các lỗi thực tế & cách xử lý.
- [ ] 100/1,000/5,000 indexed asset benchmark, memory pressure/CPU/disk/proxy-cache budgets, notes reliability.
- [ ] Thử offline end-to-end, import V2/V3, app restart, cancellation/OOM, no orphan.
- [ ] **Chỉ khi toàn bộ G0–G5 PASS** mới chuyển sang POV S06.

## S06 — 3 kịch bản (sau G5) — Gate G6

- [ ] Qwen3-8B offline isolated worker, 3 phương án khác nhau đủ beats; JSON schema/shot refs validated.
- [ ] Multi-asset shot selection, no missing source render; note provenance bảo toàn.
- [ ] Context-size management, locked beats, script editor, model reap, test prompt injection.

## S07 — Giọng nam miền Nam (cần nghe duyệt) — Gate G7

- [ ] Test voicepacks Kokoro và đánh giá accent với câu mẫu; nếu chưa có giọng đạt yêu cầu, nêu limitation.
- [ ] Pronunciation dictionary, correct WAV durations, captions beat-level, no clipping.
- [ ] TTS chunk cache; đổi một dòng chỉ regen dòng đó; worker reap.

## S08 — Preview/Edit/Render — Gate G8

- [ ] Editable preview timeline, clip/source hash verified; locked shot không đổi ngoài ý muốn.
- [ ] FFmpeg 9:16 crop/contain manual, subtitle safe zone, original audio ducking, AAC/H264 VideoToolbox verified + fallback.
- [ ] ffprobe QA đầu/giữa/cuối, audio non-empty, MP4 atomic, no final corrupted output.
- [ ] Performance / memory during render, no overlap other heavy model.

## Báo cáo kết thúc milestone (copy template)

```text
MILESTONE: S0X
BRANCH/COMMIT: ...
CHANGED FILES: path + purpose ...
COMMANDS RUN (exit status): ...
TEST RESULTS: passed / failed / skipped ...
DEVICE RESULTS: Mac model / runtime versions / RSS peak / memory pressure / elapsed ...
USER DATA SAFETY: original footage unchanged / notes persisted / no secret exposure ...
GATE: PASS | FAIL | NOT VERIFIED
BLOCKERS/KNOWN LIMITATIONS: ...
NEXT STEP: ... (nếu PASS)
```
