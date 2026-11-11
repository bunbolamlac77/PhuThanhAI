# PhuThanhAI 3.0.0

Local-first, offline video library / analyzer for macOS Apple Silicon.

This repository is being built milestone by milestone. **Current state: S00 (Foundation & Doctor) only.**
The media library, player, notes persistence, sampler and VLM pipeline arrive in S01–S03; the POV tab stays a
placeholder until G0–G5 pass. Nothing here claims a gate has passed; gate evidence lives in `artifacts/qa/`.

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
  library/, pov/                explicit placeholders (no fake implementations)
web/                            React + TypeScript + Vite shell, four tabs, bundled offline
macos/PhuThanhAI/               thin SwiftUI + WKWebView launcher scaffold (SwiftPM)
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

## Known limitations at S00

- Media library, playback, HTTP Range streaming, notes persistence and adaptive sampling are **not implemented yet** (S01–S02).
- Frame extraction is rotation-agnostic (FFmpeg autorotate + longest-edge cap) and verified on portrait HEVC; VFR/PTS policy is S02.
  The API exposes no asset endpoints, and the UI shows explicit empty states rather than fake data.
- `AI_HEAVY` slot arbitration exists and is unit-tested, but no job queue/worker pool is wired until S03.
- The macOS launcher is a compiling SwiftPM scaffold (`Mach-O arm64`). Actual WKWebView rendering, `.app` bundling,
  icon and Spotlight registration are S05. `swift build` itself fails on this machine because the Command Line Tools
  `swift-package` binary aborts with a `dyld` symbol error, and the default `MacOSX27.0.sdk` needs Swift 6.4 while the
  installed compiler is 6.3.3; the scaffold is therefore compiled with `swiftc` against `MacOSX26.5.sdk`.
- `mlx-vlm` on this machine is **0.7.10**, while `docs/00_MASTER_ARCH.md` mentions 0.7.12. The installed version is
  what `doctor` reports and what the smoke test exercised.
- No git repository exists in this workspace, so no commit hash is reported. `git init` is left to the owner.

