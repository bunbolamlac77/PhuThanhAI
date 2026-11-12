# Codex S05 Repair Report

Date: 2026-11-12
Scope: S05.4R.2 one-click/batch production hardening. S06/POV remains closed.

## A. Git State

- Repository: `https://github.com/bunbolamlac77/PhuThanhAI`
- Baseline: `s05.4r2-oneclick-batch` at `b66c923`
- Main reference: `ea2143a`
- Repair branch: `codex/s05-unified-hardening`
- Final code commit: `72397e5`
- Change set: 25 files before this report, 977 insertions and 95 deletions relative to baseline.
- Working tree was clean before the repair. No user database, cache, footage, old branch, or uncommitted user change was deleted.

Commits are intentionally split into RED regression tests and GREEN fixes. The complete ordered list is reproducible with:

```sh
git log --oneline b66c923..codex/s05-unified-hardening
```

## B. Verified Findings

### P0 — false batch completion

- Root cause: aggregate logic treated absence of failed/active items as completion and did not preserve partial/cancelled semantics.
- Reproduction: mixed complete/partial/failed and active item tests in `tests/test_s05_hardening.py` and `tests/test_s05_batch.py`.
- Fix: central aggregate state function separates execution terminality, coverage and lifecycle. Partial remains partial; active/waiting cannot complete; cancelled is not overwritten.

### P0 — non-atomic asset claims

- Root cause: claim pre-check and run creation occurred in separate SQLite operations; single-clip and batch paths also used different in-memory arbitration.
- Reproduction: 20 concurrent creators, cross-process lock tests, batch-versus-single claim race.
- Fix: schema v7 active-item unique partial index, `BEGIN IMMEDIATE`, atomic recheck/insert, and shared durable claim checks in both directions.

### P0 — stale/incorrect cache reuse

- Root cause: reuse validation was weaker than the inference inputs; additionally `start_auto_analysis()` changed valid evidence from `complete` to `queued` before reuse validation, forcing reinference.
- Reproduction: changed model, prompt/config, ranges, sampling and source tests; a worker-call assertion proves a second identical one-click run does not call inference.
- Fix: exact evidence fingerprint validation covering source content, sampling, selected frames/hashes, model weight identity, runtime, prompt, method, generation and schema inputs. Job queue state no longer destroys evidence state.
- User notes/profile overlays remain outside raw visual inference invalidation.

### P0 — sampling profile over-reuse

- Root cause: profile naming could be treated as sufficient evidence of coverage.
- Fix: `sampling_satisfies()` validates manifest version, coverage, effective settings and observed maximum gap. Denser compatible sampling may satisfy a lighter request; weaker sampling cannot satisfy a stronger request.

### P0 — streaming/checkpoint identity gaps

- Root cause: streamed and final fallback observations could reach commit paths without complete job/asset/analysis/source/frame identity validation.
- Fix: strict JSONL envelope and frame identity checks, monotonic/duplicate protection, final fallback validation before commit, checkpoint/evidence revalidation on load, nonzero worker exit protection, and target-interpreter runtime identity preservation.

### P0 — machine-wide heavy slot and startup recovery

- Root cause: a process-local lock could not exclude another backend process; startup reconciliation could occur before acquiring the server instance lock.
- Fix: `fcntl.flock` machine-wide heavy slot with crash release, and instance lock acquisition before queue reconciliation. Tests cover cross-process exclusion and stale completion/recovery behavior.

### P0 — migration/data integrity

- Schema advanced from v6 to v7 with an active asset claim uniqueness constraint.
- Tests cover v4 migration path, v6→v7, idempotence, future-schema refusal, foreign keys, `integrity_check`, claim release and persisted S04 data.
- No frozen V2/V3 contract was changed.

### P1 — frontend/macOS workflow

- One-click analysis is available before sampling and reports real progress.
- Partial batch items count as finished execution without being represented as full coverage.
- Persistent runs are rehydrated after app restart.
- Failed note save blocks clip switching and preserves the draft.
- S06/POV is hidden unless `VITE_ENABLE_POV=1`.
- Project creation fields now have accessible names. Native WKWebView self-test found the issue before the fix and verified zero unnamed buttons/inputs after it.

### Security review

- Crafted worker output: strict identity/schema/frame validation prevents cross-job or cross-asset evidence commit.
- Resource exhaustion: one machine-wide AI-heavy lock; bounded worker termination/reaping; ResourceGovernor refusal behavior remains enforced.
- SQLite concurrency: DB-enforced active claims and immediate transactions replace process-local assumptions.
- Cache poisoning/stale model reuse: source and model content identities are part of validation.
- Startup ownership: a second backend cannot mutate queue recovery state before failing the instance lock.
- No external scanner or service received source, footage, database or secrets.

## C. Backend

- Batch correctness: verified mixed aggregate states, cancel preservation, retry terminal-state guard and idempotent/concurrent creation.
- Atomic claims: verified 20-way contention and batch/single arbitration.
- Cache correctness: verified same-config reuse and invalidation for model, ranges, sampling and changed source content.
- Sampling correctness: verified stronger-versus-weaker profile compatibility using effective manifest data.
- Crash/recovery: verified persisted run discovery, stale worker protection and machine-wide heavy slot crash release.
- Database: schema v7 migrations and integrity checks PASS on temporary fixtures.

## D. Runtime

- Streaming identity and evidence load validation: PASS.
- Valid committed frames survive partial worker failure; resume avoids already committed frame IDs in request-log tests.
- Cancellation, timeout, child reap and no-orphan tests: PASS.
- Short-lived worker RSS collection now uses live sampling with a reaped-child fallback.
- Persistent worker remains experimental/not enabled by this repair; no fair A/B evidence justified changing the default worker-per-batch design.
- New 448/640/896 × 192/256/320 real-model matrix was not run in this repair session.

## E. Frontend/macOS

- `npm run build`: PASS, 44 modules transformed.
- `tools/verify_studio_ui.py`: PASS (`Studio UI markers OK`).
- Native `.app` build/ad-hoc signing: PASS.
- Native WKWebView self-test on four generated 480×270 H.264 clips: PASS (`ok: true`, backend ready, health 200, video/player `readyState: 4`, painted frame, `objectFit: contain`).
- Accessibility native snapshot: zero unnamed buttons, zero unlabeled inputs, zero images without alt.
- Manual VoiceOver and Reduce Motion review was not performed.

## F. Testing

Executed on final code:

```text
uv run pytest -q                         PASS
407 collected: 406 passed, 1 skipped
npm run build                            PASS
uv run python tools/verify_studio_ui.py  PASS
tools/build_macos_app.py                 PASS
PhuThanhAI --self-test (seeded temp data) PASS
git diff --check                         PASS
```

The single skip is `RealFootageScanTests.test_scan_real_folder_read_only`: `PHUTHANHAI_TEST_FOOTAGE` was not supplied. The generated native fixtures and all temporary databases/data roots were isolated from user data.

One non-failing warning remains: Starlette deprecates importing its `TestClient` through the installed FastAPI compatibility module and suggests `httpx2`.

Not executed:

- New real Qwen3-VL adaptive resolution/token matrix.
- Long real-model endurance across owner footage.
- Manual VoiceOver and Reduce Motion QA.
- Human-reviewed three-genre held-out quality evaluation.

## G. Remaining Limitations

- Performance numbers from previous reports were not reused as new evidence. A controlled before/after run with equal cache and memory-pressure conditions is still required.
- No owner-authorized real footage path was supplied for the read-only real-folder scan.
- G5 still lacks owner/reviewer labels, held-out evaluation, real-model endurance and manual accessibility review.
- S06/POV must remain closed.

## H. Final Verdict

**CODE STABILITY GATE:** PASS — automated, migration, concurrency, frontend build and native WKWebView gates executed in this session are green.

**PERFORMANCE GATE:** NOT VERIFIED — no new fair real-model matrix or before/after benchmark.

**G5 PRODUCT GATE:** NOT VERIFIED — human-reviewed labels, held-out quality, endurance and manual accessibility evidence are missing.

**MERGE READY:** YES for the S05 code-stability repair branch; performance and G5 claims must not be merged into release status.

**NEXT MILESTONE RECOMMENDATION:** S05 performance and G5 evidence closure. Open S06/POV only after G5 prerequisites are independently satisfied.
