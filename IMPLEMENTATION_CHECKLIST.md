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

> **AI-run result on 2026-11-11** (`MacBookPro18,3`, macOS 26.7.1): **G2 PASS (11/11 gate steps, 233 tests)**.
> Evidence: `artifacts/qa/S02/{gate.md,gate.json,device_metrics.json,pytest.xml,kill_resume.json,error_paths.log}`.
> Regenerate: `PHUTHANHAI_TEST_FOOTAGE=<footage> python3 tools/run_s02_gate.py`.

- [x] FFprobe PTS start/offset + VFR/timebase, rotation, HLG/PQ/HDR, audio streams.
      → `timeline.py`: first presented PTS (incl. non-zero), time base, packet-delta VFR evidence,
      rotation/display dimensions, SAR/DAR, colour transfer/primaries/HDR flag; fixtures CFR/VFR/non-zero/
      B-frames/rotation/HLG. Negative start covered by a synthetic-timeline test (no physical fixture possible
      with this FFmpeg build).
- [x] Scan toàn timeline bằng FFmpeg CV cheap; hard cuts/no-cut/dissolve/dark/motion; scene intervals cover timeline.
      → one 160 px pass (`signalstats+scdet+blurdetect+metadata`); luma/chroma delta cut detection catches the
      first cut that `scdet` misses; segments cover `[0,duration)` with no gap/overlap; 40 s no-cut subdivided.
- [x] Lập frame plan adaptive và event refinement; frame PTS decode verified, không đoán index/fps.
      → deterministic quick/detailed/thorough plans; quality-aware anchors; capped motion refinement; every frame
      extracted with `-copyts` + `showinfo`, requested vs decoded PTS recorded; max offset 66 ms, 0 mismatches.
- [x] Cache đúng fingerprint và giữ full SHA; thay nguồn phải invalidate; note sửa chỉ invalidate context overlay.
      → fingerprint = source SHA + profile + extractor/FFmpeg/scan versions + settings; rerun 0 re-extract;
      source change → new fingerprint, full re-extract, note preserved; stale fingerprint dir removed after commit.
- [x] Checkpoint/crash-resume không làm trùng frame; partial warnings đúng; test long clip + many-cuts.
      → atomic checkpoints every 5 frames; SIGKILL at extraction (5 committed → 18 final, unique) and during scan
      (resume complete); corrupted checkpoint recovered; disk full/timeout/source change have stable codes.
- [x] Có dữ liệu baseline speed/size/disk footprint trên Mac.
      → gate metrics: 54 real clips scanned 2.9 s; fixtures + real sampling cache 0.45/1.6 MiB; pipeline peak RSS 43 MiB;
      per-fixture timings in `gate.json`.

**G2 limitations:** negative-start physical fixture unavailable; cancellation cannot interrupt an in-flight FFmpeg
scan pass (timeout-bounded); CPU/RSS of short-lived FFmpeg children are not RSS-sampled; no model was loaded in S02.

## S03 — Qwen3-VL Offline, Scheduler, Evidence — Gate G3

> **AI-run result on 2026-11-11** (`MacBookPro18,3`, macOS 26.7.1): **G3 PASS (11/11 gate steps, 271 tests, owner-confirmed frame QA)**.
> Evidence: `artifacts/qa/S03/{gate.md,gate.json,device_metrics.json,vlm_benchmark.json,worker_recovery.json,qa_frame.jpg,qa_observation.json}`.
> Regenerate: `PHUTHANHAI_TEST_FOOTAGE=<footage> python3 tools/run_s03_gate.py`.

- [x] P0-A frame-cache integrity (path containment, symlink, decode, dimensions, SHA, target refs).
- [x] P0-B full source SHA verified by reading bytes, not trusting size/mtime; same-size/same-mtime replacement test.
- [x] P0-C execution status separated from coverage status; insufficient coverage produces warnings and partial evidence.
- [x] Worker protocol v2: verified frame list, echo-only ids/timestamps, strict observation schema, size caps, seq, stdout/stderr split.
- [x] Real offline Qwen3-VL inference on 4 sourced frames; 4/4 observed, no fabricated frame/timecode, no confidence numbers.
- [x] Benchmark 1/2/4 frames (10.8/15.6/27.3 s); multi-image output invalid → sequential fallback.
- [x] ModelScheduler single `AI_HEAVY` owner, governor check, worker reaped before slot release, bounded retries.
- [x] Inference cache/checkpoint keyed by source SHA, sampling fingerprint, frame hashes, model snapshot, runtime, prompt and generation settings.
- [x] Crash after 1 committed frame → resume to 3 unique frames; cancel → 0 leftovers.
- [x] UI/API progress from committed frames; notes usable during inference; S01 E2E regression PASS.

**G3 limitations:** human QA covered one frame (owner-confirmed); multi-frame disabled; swap/memory varies with desktop load;
cancel cannot interrupt a single generate call; no calibrated confidence.

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

## S04 — Context Fusion, Presets, Vietnamese Wedding Recognition, Reports — Gate G4

> **AI-run result on 2026-11-11** (`MacBookPro18,3`, macOS 26.7.1): **G4 PASS (12/12 steps, 297 tests, owner-confirmed labels)**.
> Evidence: `artifacts/qa/S04/{gate.md,gate.json,device_metrics.json,context_evaluation.json,scenario_matrix.json,report_samples/}`.
> Regenerate: `PHUTHANHAI_TEST_FOOTAGE=<footage> python3 tools/run_s04_gate.py`.

- [x] Environment presets (general/wedding/event/vlog/documentary/POV/custom) persisted per project with revision-safe updates (409 on conflict).
- [x] ContextOverlay derived from immutable raw evidence + profile + user labels; no raw prompt change, no inference on profile switch.
- [x] Vietnamese wedding vocabulary (12 groups) with visual signals, uncertainties and frame references; altar/decoration/banquet distinction enforced.
- [x] User confirm/correct/dismiss labels stored as annotations, provenance-tagged, survive restart and clip switching.
- [x] Level-2 targeted review reuses worker/scheduler, strict schema, per-frame cache, single heavy slot, raw evidence untouched.
- [x] AI and portable reports (video/project, V2 bundle, V3 package, portable markdown) validated against frozen contracts; private notes hidden by default.
- [x] Real evaluation: 2 wedding clips + 2 controls, precision 0.800 / recall 0.444, 0 altar false positives on controls, unknown rate 0.167.
- [x] 18-scenario matrix PASS (1–11 synthetic rule-level, 12–18 real/restart/contract/E2E), with coverage limits documented.

**G4 limitations:** small real evaluation set (2 clips); scenarios 4–10 only rule-level; recall 0.444; no calibrated confidence; migration tested by reopen, not against a captured S03 DB; multi-clip inference is memory-bound.

## S05 — Native App, Recognition, Reliability — Gate G5

> **AI-run result on 2026-11-11** (`MacBookPro18,3`, macOS 26.7.1): **G5 NOT VERIFIED**.
> 314 tests pass; P0 scene timecode/migration fixed; native `.app` and WKWebView tested on the
> real machine. Missing: three-genre reviewer dataset, 5,000-asset endurance, live-model cancel,
> accessibility QA. Evidence: `artifacts/qa/S05/{gate.md,gate.json,device_metrics.json,recognition.json,scene_audit.json,app_selftest.json,app_launch.json,app_screen2.png}`.

- [x] P0 scene boundaries from S02 manifest; frames outside windows rejected; unverified manifest never reported complete.
- [x] P0 real v4→v5 migration with backup and restore-on-failure; repeated migration idempotent.
- [x] P1 recognition: negation/boundary-aware matching, synonym dedupe, altar/trays/prep/photo signals; before/after measured.
- [x] P1 label dismissal scoped to scene/frame/clip; no cross-scene removal.
- [x] Native `.app`: Finder/Spotlight launch, Dock foreground, icon, single app/backend, backend parented by app, clean quit reap.
- [x] WKWebView real shell + H.264/HEVC/ProRes/rotation/VFR playback; browser fallback implemented.
- [x] Security/portable regression; governor swap-rate monitoring.
- [ ] Golden 3-genre dataset with reviewer labels (NOT VERIFIED).
- [ ] 5,000-asset and sustained real-model endurance (NOT VERIFIED).
- [ ] Live-model cancel during generate + UI playback during inference (NOT VERIFIED).
- [ ] Accessibility/VoiceOver/reduced-motion pass (NOT VERIFIED).

**G5 note:** functional, native and security checks pass; the gate stays NOT VERIFIED until the
dataset, endurance, live-cancel and accessibility items above are executed.

## S05.1 — G5 closure attempt

> **Result 2026-11-11:** **G5 NOT VERIFIED**. 314 tests pass; native app, 5,000-asset,
> live cancel, playback-during-inference and automated accessibility are verified.
> Evidence: `artifacts/qa/S05/{gate.md,gate.json,bench5000.json,endurance.json,playback_inference.json,app_selftest.json,app_launch.json,golden/}`.

- [x] 5,000-asset backend + virtualized DOM + restart benchmark.
- [x] Real Qwen3-VL live cancel with committed evidence and resume.
- [x] Browser playback/seek/notes/clip-switch during a real model job; one worker; cancel.
- [x] Automated accessibility audit in WKWebView (0 unnamed buttons/unlabeled inputs).
- [x] Golden dataset manager with frame extraction, pending labels and split field (10 wedding).
- [ ] Human labels for 10 wedding + 10 vlog/travel + 10 event/BTS real clips, held-out evaluation.
- [ ] Sustained endurance past the first batch (governor stopped on rising swap; protective, not bypassed).
- [ ] Manual VoiceOver / reduced-motion QA.

**G5 next:** owner supplies/authorizes the missing genres and labels, endurance is re-run
under a cooled memory state, and accessibility is reviewed manually. No S06/POV.

## S05.2 — Final gate closure attempt

> **Result 2026-11-11:** **G5 NOT VERIFIED**. 318 tests pass. QA path bug fixed;
> golden tool rebuilt with scene/PTS previews; gate now derives status from artifacts.
> Evidence: `artifacts/qa/S05/{gate.md,gate.json,golden/,endurance.json,endurance_optimized.json,accessibility_manual.json,voiceover_checklist.md}`.

- [x] QA path validator: unset/empty/whitespace/file paths exit 2; no fallback to "."; isolated data roots.
- [x] Golden manifest: 10 diverse clips, scenes/time ranges, PTS previews, sha256, group-aware split marked unlocked.
- [x] Review UI (`review.html`) + `s05_apply_labels.py`; owner_confirmed only after human runs it.
- [x] Gate golden/a11y statuses computed from evidence, not hardcoded.
- [x] Optimized endurance profile (512 px, batch 1) attempted; governor refused with MEMORY_PRESSURE_RISING (recorded, not bypassed).
- [ ] 10 confirmed wedding labels + 10 vlog/travel + 10 event/BTS real clips; split locked; held-out metrics.
- [ ] Sustained real-model endurance past the first batch.
- [ ] Manual VoiceOver/reduced-motion checklist signed by owner.

**G5 remains NOT VERIFIED until the three items above are completed. No S06/POV.**

## S05.3 — Studio UI refresh (UI-only)

> Branch `ui/studio-refresh`. No backend contracts, model, sampling or report logic changed.
> G5 remains **NOT VERIFIED**; this milestone does not change that verdict.

- [x] 23/54/23 three-region workspace; library two-column grid, player dominant.
- [x] Inspector tabs (Ghi chú / Phân tích AI) keep both panels mounted and preserve drafts/polling.
- [x] Advanced filters collapsed; player shortcuts ignored when a button/tab/summary has focus.
- [x] Thumbnail fallback UI; backend ffmpeg discovery fixed for Finder-minimal PATH (verified 200 JPEG).
- [x] Footer no longer shows stale S01/S02–S03 claims.
- [x] Automated Studio UI E2E: layout ratios, tabs, draft preservation, fallback, resize, keyboard.
- [x] Focused ARIA regressions: tab ids/labels/controls resolve; active clip uses
      `aria-current`; checkbox selection uses `aria-selected` and does not change
      the active player (`tests/test_s05_studio_accessibility.py` + browser E2E).
- [x] Full pytest, S01 1,000/54 E2E, note flow, app self-test (8/8 codecs, automated a11y).
- [x] Real WKWebView screenshot `artifacts/qa/S05/studio_app2.png`.
- [x] WKWebView video visibility: HEVC/ProRes auto-proxy to H.264 + canvas frame
      rendering; portrait video visible (`player_snapshot.png`, `video_fix_selftest.json`).
- [ ] Manual visual review of spacing/typography by project owner (requested next).

## S05.4 — Smart analysis (PARTIAL; GATE FAIL)

- [x] Real-stage profile: SHA-256, FFprobe, scene scan, plan, extraction, model
      load, inference, evidence/cache. Scene scan dominates sampling wall time.
- [x] Smart/Balanced/Full planner with budgets, scene coverage, motion/boundary
      priority, In/Out priority, luma-aware duplicate suppression and reasons.
- [x] Partial/unknown coverage is explicit; no timestamp/scene invention.
- [x] Per-frame cache independent of batch size/order; validates and deletes
      corrupt entries; user-note changes do not invalidate raw frame results.
- [x] Inspector AI mode selector, selected/sampling, progress, elapsed/ETA,
      In/Out range, deeper analysis, cancel and resume.
- [x] Tests: 353 passed, 1 skipped on S05.4R tree.
- [x] Real benchmark on 3-frame 4K clip: Smart 2 frames / 22.45 s vs Full
      3 frames / 39.19 s; cache hit 1.0 on rerun; governor refusal recorded.
- [ ] Bounded persistent worker A/B benchmark (worker-per-batch remains).
- [ ] Adaptive 448/640/896 px and 192/256/320-token quality/speed matrix.
- [ ] Human-labelled event recall, false negatives, timestamp accuracy.
- [ ] Large-clip/endurance + memory-pressure acceptance.

**GATE: FAIL** — required S05.4 worker and adaptive-resolution benchmarks are not
implemented/measured. G5 remains **NOT VERIFIED**.

## S05.4R — Performance & correctness closure (PARTIAL; GATE FAIL)

- [x] Hard Smart/Balanced budget (100-scene tests, missing scenes explicit).
- [x] Planner determinism, user-range budget cap, long-scene dedup, empty input.
- [x] Scan cache restores frame metrics; fresh/cached plans identical; corrupt
      cache rebuilds; fresh scan invalidates plan.
- [x] VideoToolbox scan default with software fallback; exact cut/PTS parity on
      real + cuts/rapid/VFR/rotation.
- [x] Single-run pipeline stage timings (`stage_timings`) and corrected
      cold/warm/end-to-end benchmark; no double scan.
- [x] Execution status separated from partial visual coverage; UI cannot say the
      whole video was analyzed for Smart.
- [x] Per-frame cache cross-batch/order hit validation; warm rerun 2/2.
- [x] Full regression: 353 passed, 1 skipped; S01 E2E, note flow and WKWebView 8/8 PASS.
- [ ] Adaptive resolution/token 3×3 matrix (not run; token telemetry unavailable).
- [ ] Bounded persistent worker A/B (not implemented; worker-per-batch).
- [ ] Durable per-frame commit during a running batch (not implemented;
      `communicate()` buffers worker stdout until batch completion).
- [ ] Human-labelled event recall/false negatives/timestamp accuracy.
- [ ] Endurance and memory-pressure shutdown/live cancel under load.

**GATE: FAIL.** G5 remains **NOT VERIFIED**. Do not merge to main or start S06.

## S05.4R.1 — Streaming & durability (PARTIAL; GATE FAIL)

- [x] Streaming JSONL reader: bounded queue, concurrent stderr drain, output cap,
      increasing sequence, job/asset/frame identity, frame hash and PTS checks.
- [x] Validated `frame_result` is atomically committed to per-frame cache and
      checkpoint before progress is emitted.
- [x] Reader thread never touches SQLite; orchestration thread commits.
- [x] Retry after crash filters already-committed frames.
- [x] Tests: duplicate sequence, duplicate frame, invalid JSONL, oversized output,
      stderr flood, valid-frame-then-error, kill after two, resume uniqueness.
- [x] Real Qwen3-VL kill/resume: first cancelled 1/3 committed, resume complete
      3/3 with 2 new inferences, unique IDs, 0 orphan processes.
- [x] Real per-frame timing: checkpoint p50 0.448 ms / p95 0.774 ms; first
      committed frame 25.62 s (model load + first inference).
- [x] Full regression: 360 passed, 1 skipped (see final run).
- [ ] Adaptive 448/640/896 × 192/256/320 matrix.
- [ ] Bounded persistent worker A/B.
- [ ] Human-labelled recall and endurance.

**GATE: FAIL.** Streaming durability is fixed; adaptive and persistent-worker
acceptance plus labelled quality remain. G5 remains **NOT VERIFIED**.

## S05.4R.2 addendum — One-click & batch (PARTIAL)

- [x] Auto endpoint/job: verify source, reuse valid sampling, auto-sample, infer,
      skip existing valid results; one primary UI button.
- [x] Persistent analysis_runs/items migration v6 with snapshot asset IDs,
      idempotency key, per-item status/counts/cache/error.
- [x] One-heavy-worker batch loop; pause/resume/cancel, retry failed,
      waiting_for_memory stop, restart reconciliation, no auto-complete after crash.
- [x] Selected and entire-project snapshot tests; 54-asset snapshot test.
- [x] API endpoints for auto, create/get/items/pause/resume/cancel.
- [x] Studio queue bar and one-click primary button; Studio E2E passes.
- [x] Source-change test forces a new sampling fingerprint; existing-result skip tested.
- [ ] Full real-model 54-clip batch (fake-worker queue tests used by policy).
- [ ] Persistent worker A/B, human-labelled quality, endurance (still open).
- [ ] Batch UI ETA/count accuracy beyond loaded page (server counts used for scope;
      completed estimate is omitted for project-wide confirmation).

**GATE: FAIL.** Addendum workflow is implemented and tested, but the parent
S05.4R.2 gate still has persistent-worker/quality/endurance gaps. G5 NOT VERIFIED.

