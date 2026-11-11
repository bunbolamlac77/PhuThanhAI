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

## S05 result on this machine (2026-11-11)

`python3 tools/run_s05_gate.py` → **G5 NOT VERIFIED** (functional steps pass; mandatory
three-genre review and endurance items are missing, listed in the gate).

| Fact | Measured value |
|---|---|
| Tests | 314 passed, 0 failed, 1 skipped (optional real-footage scan when env unset) |
| Scene timecode | 16/16 sampled frames inside S02 manifest scene windows; all 16 first frames sat 1–3 s after scene start and are now reported at the true boundary |
| DB migration | real S03 (v4) SQLite built from the S03 source → v5: IDs/notes/rating/favorite/tags preserved; repeated migration is a no-op; simulated failure restores the pre-migration backup |
| Recognition | before 0.800/0.444 → after 1.000/0.778 (precision/recall on the same 2 clips — not held out) |
| Native app | `.app` built with `swiftc`+SDK, icon, Info.plist, ad-hoc signature; Finder/Spotlight launch, Dock foreground, one app + one backend (parent = app), clean quit reaps backend and frees the port |
| WKWebView | real React shell + health 200; 8/8 videos `readyState 4`: H.264, HEVC 4K, ProRes fixture, rotation-corrected 240×320, VFR |
| Portable/security | V2/V3 package roundtrip validates; private notes remain hidden in hidden export |
| Screen | `artifacts/qa/S05/app_screen2.png` (real WKWebView window on the MacBook display) |

### G5 NOT VERIFIED because

- **Golden dataset:** only wedding footage + synthetic controls exist on this machine; no
  reviewer-labeled daily vlog/travel or event/BTS set. Recognition was tuned and evaluated
  on the same two clips, so the improved numbers are not a generalization estimate.
- **Endurance:** the 1,000-asset suite passes, but 5,000 assets and sustained multi-clip
  real-model batches were not run; 16 GB swap limits long runs.
- **Real-model cancel / playback during inference:** cancel/reap is proven with the protocol
  worker and app quit, not by cancelling a live Qwen3-VL generate while playing video.
- **Accessibility:** no VoiceOver or reduced-motion pass on the native app.

## Known limitations (S00–S05)

- **G5 is NOT VERIFIED**, not failed: all functional/security/native checks pass, but the
  mandatory three-genre reviewer dataset, 5,000-asset endurance, live-model cancel and
  accessibility review are missing. Do not describe S05 as production-accepted.
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
- **5,000 assets / long real-model batches** are not verified; 1,000 assets are.
- **Live Qwen3-VL cancel** during `generate()` and playback-during-inference are not verified;
  process-level cancel/reap is verified with the fake protocol worker and app quit.
- **Accessibility:** labels exist for key controls, but VoiceOver, focus-order and
  reduced-motion passes were not performed.
- **POV remains locked**; S06 (script engine/TTS) was not started.
- **Skipped pytest:** `tests/test_scanner.py::RealFootageScanTests::test_scan_real_folder_read_only`
  skips when `PHUTHANHAI_TEST_FOOTAGE` is unset; this is intentional and matches the gate logs.
- `artifacts/qa/` (including app screenshots, footage labels and QA logs) and `dist/` are
  git-ignored; published code contains no footage, secrets or private paths.
