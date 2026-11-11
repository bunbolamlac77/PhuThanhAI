# PhuThanhAI 3.0.0

Local-first, offline video library / analyzer for macOS Apple Silicon.

This repository is being built milestone by milestone. **Current state: S05 (native .app, WKWebView, scene-timecode/migration P0 fixes, recognition improvements).**
The POV tab stays locked until G0–G5 pass. Gate evidence for S00 and S01 lives in `artifacts/qa/` (git-ignored, regenerate locally).

## Layout

```text
pyproject.toml / uv.lock        isolated Python 3.12 environment (uv)
contracts/*.schema.json         V2 legacy + V3 extension schemas (authoritative, unchanged)
docs/                           the six core specification documents
src/phuthanhai/                 application package
  config.py                     paths, model/runtime discovery, offline environment guard
  cli.py                        `doctor`, `serve`, `validate`
  api/{app,routes,security}.py  loopback-only FastAPI shell, capability + origin/host checks
  contracts/{models,validators,migrate}.py
  runtime/{scheduler,resource,checkpoint}.py
  analyzer/{probe,sampler,vlm_worker}.py
  utils/{fs,hashes,logging}.py
  analyzer/
    timeline.py                 PTS/rotation/HDR timeline + decoder-verified frames
    scene_scan.py               full-timeline cuts, dark/blur/motion cues, segments
    sampling.py                 deterministic quick/detailed/thorough plans
    sampling_pipeline.py        cache, checkpoints, resume, cancel, provenance
    vision_service.py           heavy slot, worker lifecycle, inference cache, evidence
    evidence.py                 VisualEvidenceSet validation (frame refs only)
    context_profiles.py         environment presets + Vietnamese cultural vocabulary
    context_fusion.py           ContextOverlay (hypothesis vs user-asserted)
    report.py                   AI/portable report assembly + frozen V2/V3 packages
    vlm_worker.py               isolated Qwen3-VL subprocess protocol (JSONL v2)
  library/
    db.py                       SQLite WAL registry + migrations
    store.py                    assets/annotations/reviews/cache repositories
    index.py                    read-only, symlink-safe, idempotent folder scanner
    preview.py                  thumbnails, HTTP Range, on-demand proxy, LRU cache
    notes.py                    wall-clock suggestion + annotation scope rules
    service.py                  composition layer used by API and tests
  pov/                          locked until G5 (no engine)
web/                            React + TypeScript + Vite shell, four tabs, bundled offline
macos/PhuThanhAI/               SwiftUI + WKWebView launcher sources (packaged by tools/build_macos_app.py)
tests/                          unit + integration tests (stdlib unittest; pytest-compatible)
tools/                          validate_specs.py (contract QA), run_s00_gate.py (evidence collector)
artifacts/qa/S00/               gate evidence produced on this machine
```

## Commands

Run from the repository root.

```bash
uv run phuthanhai doctor --offline          # environment / model / runtime / security checks
uv run phuthanhai doctor --offline --json   # same, machine-readable
uv run phuthanhai serve --host 127.0.0.1    # loopback-only API, prints the real URL
uv run phuthanhai validate --input tests/fixtures/golden_v3.workspace_report_portable.md
python3 tools/validate_specs.py             # contract-only QA, no model, no app
python3 -m unittest discover -s tests -v    # test suite (pytest can collect the same files)
python3 tools/run_s00_gate.py --source /path/to/footage.mp4   # regenerate artifacts/qa/S00 evidence
python3 tools/run_s01_e2e.py                 # Chrome E2E: 1,000-asset DOM + real playback
python3 tools/run_s01_ui_flow.py             # Chrome E2E: note flow, flush, shortcuts
python3 tools/run_s02_kill_resume.py         # hard SIGKILL at two stages + resume proof
PHUTHANHAI_TEST_FOOTAGE=/path/to/footage \
  python3 tools/run_s01_gate.py              # regenerate artifacts/qa/S01 evidence
PHUTHANHAI_TEST_FOOTAGE=/path/to/footage \
  python3 tools/run_s02_gate.py              # regenerate artifacts/qa/S02 evidence
PHUTHANHAI_TEST_FOOTAGE=/path/to/footage \
  python3 tools/run_s03_gate.py              # regenerate artifacts/qa/S03 evidence (real inference)
PHUTHANHAI_TEST_FOOTAGE=/path/to/footage \
  python3 tools/run_s04_gate.py              # regenerate artifacts/qa/S04 evidence (context + reports)
python3 tools/build_macos_app.py             # build/sign/register dist/PhuThanhAI.app
python3 tools/run_s05_gate.py                # regenerate artifacts/qa/S05 evidence (G5)
```

## Environment separation (non-negotiable)

- The app owns `.venv` (Python 3.12) and never installs into, upgrades, or writes to the legacy
  `AIVideo` `.venv`, runtime, or model snapshots. Those are opened **read-only**.
- Model inference never runs inside the FastAPI process. `analyzer/vlm_worker.py` is launched as a
  subprocess through the discovered MLX runtime interpreter, and is reaped before the heavy slot is released.
- Core functionality makes no network calls. `doctor --offline` enforces `HF_HUB_OFFLINE`,
  `TRANSFORMERS_OFFLINE`, `HF_HUB_DISABLE_TELEMETRY`, and refuses to proceed if a download is requested.
  The gate script additionally re-runs the model smoke test inside `sandbox-exec` with `deny network*`.

## Data location

Runtime data is written to `~/Library/Application Support/PhuThanhAI/` (override with
`PHUTHANHAI_DATA_DIR`), never into the source tree. Source footage is always opened read-only.

## S00 gate result on this machine (2026-11-11)

`python3 tools/run_s00_gate.py --source <real footage>` → **verdict PASS** (10/10 steps). Evidence is written to
`artifacts/qa/S00/gate.md` **locally** and is git-ignored (it records absolute paths and model
descriptions of private footage - see `artifacts/README.md`). Measured, not estimated:

| Fact | Measured value |
|---|---|
| Host | MacBookPro18,3 · macOS 26.7.1 · arm64 · 10 CPUs |
| RAM | 16 GiB |
| App Python | 3.12.11 (`.venv`, uv-managed) |
| MLX runtime | mlx 0.32.4 · mlx-vlm 0.7.10 · mlx-lm 0.32.2 · Metal available · qwen3_vl supported |
| FFmpeg / ffprobe | 7.1.1 |
| Visual model | 5 required artifacts, SHA-256 verified against install manifest |
| Frame smoke | 504×896 frame @ 5000 ms from a 4K HEVC **portrait** clip (stored 3840×2160, display matrix −90) |
| Model load | 0.8–1.5 s |
| Single-frame inference | 6.5–8.2 s warm; 9.5–17.6 s on cold/loaded runs |
| Peak RSS (worker) | 2.26–3.44 GiB across recorded runs |
| MLX-reported peak memory | 4.11 GiB (stable across 6 runs) |
| Tests | 105 passed, 0 failed, 0 skipped |
| Offline proof | `sandbox-exec` `deny network*`: curl exit 6, TCP connect blocked; unsandboxed control reachable |
| Server security | bad host 400 · bad origin 403 · traversal 400 · POST without capability 403 · session file `0600` |
| Process hygiene | 0 leftover worker/server processes after the run |

## S01 gate result on this machine (2026-11-11)

`PHUTHANHAI_TEST_FOOTAGE=<real footage> python3 tools/run_s01_gate.py` → **verdict PASS** (10/10 steps).
Evidence in `artifacts/qa/S01/` (local-only). Measured:

| Fact | Measured value |
|---|---|
| Tests | 185 passed, 0 failed, 0 skipped |
| Scan 1,000 synthetic assets | 1000 added, 0 errors, 55.5 s (≈18 files/s) |
| Scan real footage | 54 clips, 0 errors, 3.1 s, source bytes/mtime unchanged |
| HEVC → H.264 proxy | 720×1280, duration delta 0 ms, on demand only |
| Library list p95 (HTTP) | 4.26 ms for 100 rows of 1,000 |
| Search p95 (HTTP) | 4.65 ms |
| Note save p95 (HTTP) | 1.60 ms |
| Range responses | 206 with `Content-Range`, 416 with `bytes */size` |
| Stale note revision | 409 `ANNOTATION_CONFLICT`, no overwrite |
| Server RSS | 57.9 MiB (no model loaded) |
| Data root (DB + thumbs + proxy) | 14.2 MiB for the gate dataset |
| Browser E2E, 1,000 rows | 18 DOM cards (virtualized), total label "1000", playback readyState 4 |
| Browser E2E, real HEVC | 2160×3840 decoded, readyState 4 |
| Browser E2E, notes | typed → saved → wall-clock 05:00 applied, `start_ms` null |
| Flush on clip switch | typed inside the debounce window, switched with `]`, note persisted with no cross-clip leak |
| Security | foreign host 400 · foreign origin 403 · no capability 403 · traversal 400 |
| Restart | note still present after backend restart |
| Orphan processes | 0 |

The S01 E2E caught two real defects that unit tests could not: a virtualizer that measured a 0×0 container
(one column for 1,000 clips) and a clip switch that briefly kept the previous draft in the editor (risk of writing
clip A's note onto clip B). Both are fixed and regression-guarded.

## S02 gate result on this machine (2026-11-11)

`PHUTHANHAI_TEST_FOOTAGE=<real footage> python3 tools/run_s02_gate.py` → **verdict PASS** (11/11 steps).
Evidence is local-only in `artifacts/qa/S02/`.

| Fact | Measured value |
|---|---|
| Tests | 233 passed, 0 failed, 0 skipped |
| Fixture matrix | 13 fixtures: CFR, VFR, non-zero start, B-frames, rotation matrix, HLG/HDR, hard cuts, rapid cuts, 40 s no-cut, dark, blurred, short motion, ProRes |
| PTS accuracy | max abs(decoded − requested) = 66 ms; 0 frames flagged `pts_mismatch` |
| Scene coverage | cuts exact (1.0/2.0/3.0 s); segments cover `[0, duration)` with no gap/overlap; 40 s no-cut subdivided |
| Profiles | quick/detailed/thorough deterministic; observed gap recomputed from decoded PTS |
| Cache | rerun: 0 re-extracted, all cache hits; source change: new fingerprint, full re-extract, note preserved |
| Hard kill | SIGKILL after 5 committed frames → resume extracted 13 + reused 5 = 18 unique; SIGKILL during scan → resume complete |
| Error paths | disk full → `STORAGE_FULL`; FFmpeg timeout → `FFMPEG_TIMEOUT`; mid-run source change → `SOURCE_CHANGED` |
| Real footage | 54 clips scanned in 2.9 s; 3 shortest sampled; HEVC 2160×3840; 0 PTS mismatch |
| Resources | pipeline peak RSS 43 MiB; sampling cache 0.45 MiB (fixtures) + 1.3 MiB (real); gate 147 s wall, 232 s child CPU |
| UI regression | notes/wall-clock/flush + Space/J/K/L/I/O + textarea suppression all PASS in Chrome |

## S03 gate result on this machine (2026-11-11)

`PHUTHANHAI_TEST_FOOTAGE=<real footage> python3 tools/run_s03_gate.py` → **verdict PASS**
(11/11 steps, owner-confirmed frame QA). Evidence stays local in `artifacts/qa/S03/`.

| Fact | Measured value |
|---|---|
| Tests | 271 passed, 0 failed, 0 skipped |
| Runtime | Python 3.12.11 · mlx 0.32.4 · mlx-vlm 0.7.10 · mlx-lm 0.32.2 · transformers 4.57.6 |
| Real inference | 4-frame sequential, 4/4 observed, coverage sufficient, 28.0 s |
| Benchmark | 1 frame 10.8 s · 2 frames 15.6 s · 4 frames 27.3 s, all schema-valid |
| Multi-frame | **disabled**: 2-image request runs but output fails strict JSON validation → `sequential_frames` fallback |
| Memory | worker peak RSS 1.93 GiB · MLX peak 3.91 GiB · swap delta 0.03 GiB on the final run |
| Scheduler | max concurrent heavy workers = 1; no second scheduler introduced |
| Recovery | crash after 1 committed frame → resume to 3 unique observed frames; cancel reaped with 0 leftovers |
| P0 fixes | corrupt JPEG, same-size/same-mtime source replacement, execution vs coverage split: 7 dedicated tests |
| Offline | worker launched under `sandbox-exec` `(deny network*)` + in-process socket block; model loaded from local snapshot |
| UI safety | notes created while real inference was running; S01 note/shortcut E2E still PASS |

## S04 gate result on this machine (2026-11-11)

`PHUTHANHAI_TEST_FOOTAGE=<footage> python3 tools/run_s04_gate.py` → **verdict PASS**
(12/12 steps, owner-confirmed labels). Evidence stays local in `artifacts/qa/S04/`.

| Fact | Measured value |
|---|---|
| Tests | 297 passed, 0 failed, 1 skipped (known S02 negative-start fixture gap) |
| Profiles | 7 presets persisted per project; migration v5 keeps IDs, notes, evidence |
| Context evaluation | 2 real wedding clips + 2 non-wedding controls: precision 0.800, recall 0.444 |
| Altar safety | 0 altar false positives on controls; a shrine-like altar stayed `unresolved` instead of being over-claimed |
| Unknown rate | 0.167 of emitted candidates/unresolved entries |
| Profile switch | 0.001 s, raw evidence digest unchanged, **zero** worker/VLM calls |
| Reports | project bundle / extension / package validate against frozen V2/V3; portable roundtrip PASS |
| Privacy | private note absent in hidden report, present only when explicitly shared |
| Targeted review | level 2 runs on one real frame, cached on repeat, raw evidence unchanged |
| Scenario matrix | 18/18 (1–11 rule-level synthetic observations, 12–18 real/restart/contract/E2E) |
| UI regression | player/notes/autosave/shortcuts PASS in Chrome |

## S05 / S05.1 result on this machine (2026-11-11)

`python3 tools/run_s05_gate.py` → **G5 NOT VERIFIED**. All functional, native,
migration, 5,000-asset, cancel, playback-during-inference and automated
accessibility checks pass. Two mandatory gates remain open.

| Fact | Measured value |
|---|---|
| Tests | 318 passed, 0 failed, 1 skipped (optional real-footage scan when env unset) |
| Scene timecode | 16/16 sampled frames inside S02 manifest windows; all 16 first frames sat 1–3 s after scene start and now report the true boundary |
| DB migration | real S03/v4 SQLite → v5: IDs/notes/rating/favorite/tags preserved; repeat no-op; failed migration restores backup |
| Recognition | 0.800/0.444 → 1.000/0.778 on the **same 2 clips** (not held out) |
| 5,000 assets | insert 0.10 s; list p50/p95 5.8/5.7 ms; search p95 8.3 ms; restart total 5,000; 18 DOM cards; playback OK |
| Live cancel | real Qwen3-VL: 1 committed frame → cancel 0.51 s → 0 workers → resume complete; unique frames |
| Playback during inference | job running before/after; play/seek PASS; note saved in 3.19 s; clip switch draft clean; one worker; cancelled |
| Native app | `.app` launch via Finder/Spotlight, Dock foreground, one app + one backend (parent=app), quit reaps backend and port |
| WKWebView | 8/8 videos readyState 4: H.264, HEVC 4K, ProRes, rotation, VFR |
| Accessibility (automated) | `lang=vi`, 10 buttons / 0 unnamed, 19 inputs / 0 unlabeled, 0 images without alt, 39 focusable |
| Golden dataset | 10 diverse wedding clips, manifest scenes with PTS previews, **0 labels confirmed**; review.html + apply tool; no authorized vlog/travel or event/BTS footage |

### Why G5 is still NOT VERIFIED

- **Golden dataset:** 10 wedding clips are captured with scene/PTS previews and a
  review UI, but **0 labels are confirmed**; 10 vlog/travel and 10 event/BTS real
  clips are missing. The split is deliberately unlocked (one shoot = leakage), so
  no held-out precision/recall exists. The 0.778 recall is on tuning clips.
- **Real-model endurance:** after one 4-frame batch (swap +2.67 GiB) the
  ResourceGovernor refused the next clip with `MEMORY_PRESSURE_RISING`; a second
  safety profile (512 px, batch 1) was also refused before starting. This is
  protective behaviour, but only 1/3 planned clips completed.
- **Manual VoiceOver/reduced-motion:** automated DOM audit passes; the checklist
  (`artifacts/qa/S05/voiceover_checklist.md`) and owner confirmation are pending.

## Studio UI refresh (S05.3)

The app shell is now a three-region editing workspace: **Library ~23% · Player
~54% · Inspector ~23%** (360 / flexible / 360 px at 1440 px). The Inspector has
two tabs (Ghi chú and Phân tích AI), both kept mounted so drafts, autosave and
sampling/AI polling survive tab switches.

- Advanced library filters are collapsed under "Bộ lọc nâng cao".
- Player shortcuts are suppressed when focus is on a button, tab or summary.
- WKWebView video fix: the native HEVC/ProRes video layer composited black even
  though frames decoded. Inside WKWebView the Player now (a) loads a poster and
  decodes the first frame, (b) automatically uses the on-demand H.264 proxy for
  non-H.264 codecs (visible "proxy H.264" status; first 4K proxy took ~33 s, then
  cached), and (c) draws decoded frames onto a canvas overlay so portrait video is
  actually visible and captured. Chrome keeps direct playback.
- Thumbnail failures show a film-strip fallback ("Không có ảnh xem trước") instead
  of a broken image. Root cause of the app failure was ffmpeg discovery under the
  Finder-minimal PATH; `find_tool` now falls back to `/opt/homebrew/bin` and
  `/usr/local/bin` when PATH lacks it (verified: thumbnail endpoint returns
  `image/jpeg` with `PATH=/usr/bin:/bin`).
- Footer no longer claims S01/S02–S03 are unimplemented.

Validated with `npm run build`, full pytest, `tools/run_s01_e2e.py` (1,000 and 54
assets), `tools/run_s01_ui_flow.py` (note/autosave/shortcuts),
`tools/run_s05_studio_ui.py` (layout ratio, Inspector state, thumbnail fallback,
keyboard guard, 1200/820 px resize), `tools/verify_studio_ui.py`, and the signed
`.app` WKWebView self-test (8/8 codecs, 0 accessibility issues).
Screenshot: `artifacts/qa/S05/studio_app2.png` (real app window).

This is a UI-only milestone: contracts, Qwen3-VL, sampling, reports and the G5
verdict (**NOT VERIFIED**) are unchanged.

## Known limitations (S00–S05)

- **G5 is NOT VERIFIED**, not failed: functional/native/security, 5,000-asset, live
  cancel, playback-during-inference and automated accessibility checks pass. Missing:
  human three-genre labels, completed sustained endurance, and manual VoiceOver QA.
  Do not describe S05 as production-accepted.
- **Recognition remains narrow:** the 1.000/0.778 numbers come from the same two wedding
  clips used for tuning; altar recall improved, but reception on vid_022 is still missed
  and there is no held-out genre evaluation.
- **Native app is locally built and ad-hoc signed**, not notarized. It expects the project
  venv and source tree via `backend_root.txt`; it is not a self-contained standalone bundle
  and must be rebuilt with `tools/build_macos_app.py` after source changes.
- **`swift build`/SwiftPM still fails** on this machine; the supported build path is
  `tools/build_macos_app.py` (swiftc + `MacOSX26.sdk`). Upgrading macOS/Xcode was avoided
  because AIVideo compatibility is not established.
- **Browser fallback** is implemented (opens the loopback UI and shows the WKWebView error),
  but its manual failure path was not exercised in an E2E test.
- **5,000 assets** are verified; sustained multi-clip real-model batches are not (one
  batch completed, then the governor stopped the run).
- **Live Qwen3-VL cancel** and **playback/notes during inference** are verified on the
  real worker; the model job was cancelled while the browser played/seeking and saved a note.
- **Accessibility:** automated DOM audit passes (names/labels/alt/lang); manual
  VoiceOver, full focus-order walk and system reduced-motion toggle remain
  unverified; the owner checklist is in `artifacts/qa/S05/voiceover_checklist.md`.
- **QA tools reject empty/invalid footage paths** (unset/empty/whitespace/file),
  use isolated data roots, and require `PHUTHANHAI_TEST_FOOTAGE`; covered by
  `tests/test_s05_qa_paths.py`.
- **POV remains locked**; S06 (script engine/TTS) was not started.
- **Skipped pytest:** `tests/test_scanner.py::RealFootageScanTests::test_scan_real_folder_read_only`
  skips when `PHUTHANHAI_TEST_FOOTAGE` is unset; this is intentional and matches the gate logs.
- `artifacts/qa/` (including app screenshots, footage labels and QA logs) and `dist/` are
  git-ignored; published code contains no footage, secrets or private paths.
