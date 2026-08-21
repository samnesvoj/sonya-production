# SONYA generation-pipeline lifecycle: diagnostics, root cause, stage map

Scope: why "СОЗДАЁМ ЗАДАЧУ" can hang indefinitely, the full FILE_SELECTED →
EDITOR_HANDOFF_AVAILABLE lifecycle with source of truth per stage, and how
to run the automated diagnostics this document accompanies
(`scripts/qa/lifecycle_diagnostics.py`, `scripts/qa/diagnose_generation_flow.py`,
`tests/test_generation_lifecycle_diagnostics.py`,
`tests/frontend/test_upload_timeout_and_stage_diagnostics.mjs`).

This investigation was read-only against the codebase (no production
system touched, no real S3/Postgres/vast.ai/GPU involved). Code
references are to the working tree as of 2026-08-21; `app.js`, `auth.js`,
`scripts/prod_generation_api.py`, `scripts/upload_security.py` and a few
other files have an unrelated URL-ingest feature in flight from a parallel
session -- none of the code paths below were touched by that diff (verified
via `git diff`), so these findings hold against both the pre- and
post-URL-ingest versions of those files.

---

## 1. Root cause: why "Создаём задачу" can hang for a 40s video and a 40min video alike

**The upload button's own busy-label is the only feedback for the entire
span from "click" to "job exists in Postgres," and that span is one
single, unbounded HTTP request with no timeout on either end.**

Concretely:

- `app.js::setGenerateButtonsBusy(true)` (app.js:880-897) sets the button
  text to `Создаём задачу…` synchronously, before anything happens over
  the network.
- `auth.js::checkAndCreateVideoJob()` → `apiCreateVideoJob()` (auth.js:226-270)
  builds a `FormData` with the mode, the raw file, and JSON params, and
  sends it as **one** `fetch()` POST to `/api/generation/jobs`.
- That single request/response span, from the browser's point of view,
  covers CLIENT_UPLOAD_START through JOB_CREATED in the lifecycle map
  below -- five to six of the requested stages collapsed into one opaque
  wait. `fetch()` has no upload-progress event at all (that requires
  `XMLHttpRequest`), so even in principle the browser cannot tell "still
  sending bytes" apart from "server is thinking."
- Server-side, `create_generation_job()` (scripts/prod_generation_api.py:354-534)
  does everything synchronously inside that one request: reads the full
  multipart body in 1MB chunks up to `MAX_UPLOAD_SIZE_MB` (default 2048MB,
  `scripts/upload_security.py:29`), runs magic-byte/size/extension
  validation, then calls `upload_bytes()` (a synchronous `boto3`
  `put_object`, `scripts/prod_s3_storage.py:165`) directly on the request
  handler's thread, then does the Postgres insert (`create_job_idempotent`),
  then an `add_job_file` insert. Nothing here streams a response back
  early or reports intermediate progress.
- **No layer enforces a timeout.** `auth.js::apiFetch()` (auth.js:33-52)
  passes no `signal`/`AbortController` and no client-side deadline.
  Server-side, `boto3`'s S3 client is constructed with no `Config(connect_timeout=..., read_timeout=...)`
  override (`scripts/prod_s3_storage.py:42-58`), so it falls back to
  boto3's defaults (60s connect / 60s read, retried up to boto3's default
  attempt count) -- and there is no reverse proxy in front of uvicorn in
  this deployment (`deploy/COMMANDS_VPS.md` runs `uvicorn ... --host 0.0.0.0 --port 8000`
  directly, no nginx/Caddy config found anywhere in `deploy/`), so there is
  no outer L7 timeout either. A genuinely stalled TCP connection (S3
  endpoint unreachable, client's network drops mid-upload) can hang for
  as long as the OS-level TCP stack allows before either side notices.

**This is why the symptom appears identically for a 40-second and a
40-minute video.** Video length only changes how many bytes have to move
through this one unbounded span -- it does not change the fact that the
span has no upper bound, no intermediate feedback, and covers 5-6 distinct
lifecycle stages that are, today, indistinguishable from outside a single
HTTP request. A 40-second video that hits a stalled network hangs exactly
the same way a 40-minute video does; a 40-minute video on a healthy
network can also simply take that long for legitimate reasons (large
body, slow uplink) and looks identical to the user.

**"Создаём задачу" can currently mean any of:** the browser is still
sending bytes; the browser finished sending but the server hasn't started
reading yet (rare, but possible under server load); the server is
running magic-byte validation; the server is mid-`PUT` to S3; the server
is inserting the Postgres row; or -- separately, but with the exact same
visible symptom -- **the job was created successfully and is sitting in
`queued`, never claimed by a worker.** `docs/SONYA_AUDIT.md` P0-1 already
documents a production bug where jobs routed through vast.ai are
structurally unclaimable (`mark_worker_started()` is never called, so
`claim_specific_job`'s `status='queued'` precondition is never met after
the dispatcher flips the job to `gpu_requested`) -- from the frontend,
this looks identical to a stuck upload, because `app.js::pollJob()`
(app.js:1523-1571) also just says "Видео в очереди" and keeps polling,
with its own 720×5s ≈ 1-hour ceiling before it gives up with a visible
message. **Diagnosing "stuck at Создаём задачу" by eye cannot distinguish
a client-side upload stall from a server-side stuck-in-queued job** --
they present identically. This is exactly the ambiguity
`scripts/qa/lifecycle_diagnostics.py` exists to remove: it instruments
each boundary function directly, so a stall gets attributed to the real
stage it happened in instead of one undifferentiated "still creating the
task" wait.

### Fix direction (not applied in this session -- see §5)

The wire contract (one multipart request that both uploads and creates
the job) is the thing that makes stage-level feedback structurally
impossible from the client. A real fix needs either (a) a client-side
timeout/AbortController plus a distinct in-flight UI state so a stall is
at least visibly different from a normal wait, or (b) splitting upload
from job-creation into two steps (pre-signed S3 PUT from the browser,
then a small JSON POST to create the job referencing the uploaded key) so
each phase is independently observable and timeoutable -- the same shape
`apiCreateVideoJobFromUrl()` already uses server-side for the URL-ingest
path (checking → downloading → uploading, each polled and reported
separately). (b) is the more invasive change; (a) is a same-session-safe
minimal patch. Both touch `app.js`/`auth.js`, which have unrelated changes
in flight from another session as of this writing -- see §5 for why this
session did not apply either.

---

## 2. The "Multiple instances of Three.js being imported" warning

**Root cause:** three independent copies of Three.js exist in this repo,
resolved from three different sources:

| File | Resolves `three` via | Version pinned |
|---|---|---|
| `sphere.js` (loaded by `index.html`, `result-preview.html`) | `import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.160.1/build/three.module.js'` | 0.160.1 |
| `widgets/interactive-cloth/src/*.js` | bare `import * as THREE from 'three'` (npm, via its own `package.json`/Vite) | `^0.180.0` |
| `widgets/hyperframes-bg/src/{liquid-surface,starfield}.js` | `import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.181.2/+esm'` | 0.181.2 |

Three.js emits that console warning whenever more than one of its module
instances is evaluated on the same page (it tracks itself via a global
registry independent of import source/version).

**Current impact: none in production.** Checked every HTML entry point in
the repo (`index.html`, `result-preview.html`, `opencut.html`,
`streamer-mode.html`, and the `widgets/interactive-cloth/{preview,studio,chrome-preview}.html`
dev pages) -- no single page loads more than one of these three sources.
`widgets/interactive-cloth/package.json` says so explicitly: *"Isolated
prototype ... Not wired into the main SONYA app yet."* So this warning is
not currently reproducible from the committed static site, and is not
causing any generation/UI-flow bug today.

**Why it matters anyway:** `widgets/interactive-cloth`'s own
`PRODUCTION_INTEGRATION_PLAN.md` describes it as a widget for **the
processing screen** -- i.e. the exact same page as `sphere.js`'s animated
background. The moment that integration lands without deduplicating the
Three.js import, this warning becomes live, and depending on what each
copy's module-level state does (renderer/context creation, animation-loop
registration), duplicate instances can mean duplicate WebGL contexts on a
budget-constrained page (the same processing screen already showing a
progress bar during a potentially multi-minute wait) -- worth resolving
*before* that integration, not after.

**Recommendation (not applied — no current bug to fix):** when
`interactive-cloth` is wired into the processing screen, either (a) pin it
to the same jsdelivr URL+version `sphere.js` uses instead of a bare `three`
import, or (b) add an import map in the page's `<head>` so both `sphere.js`
and the widget's bare `three` specifier resolve to one shared module URL.

---

## 3. Full lifecycle map

Stage names match the chain requested for this investigation. "Backend
mock" = what `scripts/qa/lifecycle_diagnostics.py::run_backend_lifecycle()`
in mock mode (in-process `TestClient`, S3/Postgres boundary functions
replaced by in-memory fakes) can observe and time directly by
timestamping each mocked boundary call. "Wire" = what a real browser or
a live HTTP client sees today (no mocking possible) -- deliberately
coarser, because that coarseness *is* the finding from §1.

| # | Stage | Source of truth | Wire-observable today? | Timeout today | Diagnostic evidence |
|---|---|---|---|---|---|
| 1 | FILE_SELECTED | `appState.uploadedFile` set (`app.js::handleFileSelect`) | Yes (client-only, instant) | n/a | Covered by existing `tests/frontend/*` (button-enable tests) |
| 2 | CLIENT_UPLOAD_START | `fetch()` call fires in `apiCreateVideoJob()` | **No** — folded into one span with #3 | **None** | `lifecycle_diagnostics.py` reports this as "request sent" immediately; real start-of-bytes-sent is not observable via `fetch()` |
| 3 | CLIENT_UPLOAD_COMPLETE | `fetch()` promise resolves | **No** — same span as #2, #4-#8 on the wire | **None** (client); ~boto3 default 60s×retries + no proxy timeout (server) | `CLIENT_UPLOAD_COMPLETE` stage timing in this tool == the full round trip; §1's stalled-request test proves it never times out |
| 4 | BACKEND_REQUEST_RECEIVED | FastAPI enters `create_generation_job()` | No distinct signal (no entry log) | n/a | Inferred: any real HTTP status code proves this stage was reached |
| 5 | INPUT_VALIDATED | `validate_upload()` returns (`scripts/upload_security.py:141`) | No (only failure path logs `EVT_UPLOAD_REJECTED`) | None | Mock mode: `probe_async()` timestamps the real call; wire: only inferable from a 400 vs later status |
| 6 | S3_INPUT_UPLOAD_START/COMPLETE | `upload_bytes()` call/return (`prod_s3_storage.py:165`) | No (only failure is logged) | boto3 default (~60s×retries), no app-level override | Mock mode: `probe()` timestamps entry+exit; wire: not observable at all |
| 7 | JOB_CREATED | `create_job_idempotent()` returns; `audit(EVT_JOB_CREATED, ...)` + `logger.info("[api] job_created ...")` (`prod_generation_api.py:519-526`) | **Yes** — first point in the whole chain with a real log line and a `job_id` in the 202 response | n/a | `job_id` present in response body; server log `job_created` |
| 8 | QUEUED | DB row `status='queued'`; `GET /api/generation/jobs/{id}` | Yes | `pollJob()`'s own 720×5s ≈ 1h ceiling | Status field in poll response |
| 9 | WORKER_CLAIMED | `POST /api/worker/claim` returns a job (`prod_generation_api.py:863-882`) | Yes, from the worker's side; frontend only sees the status flip | Bounded by `VAST_STARTUP_TIMEOUT_SEC` (see `docs/SONYA_AUDIT.md` P0-1 — currently broken for vast.ai) | `EVT_JOB_CLAIMED` audit log; job status `claimed` |
| 10 | PROCESSING / mode_running | `POST /api/worker/jobs/{id}/status` | Yes | none observed at API layer | job status field |
| 11 | OUTPUT_UPLOADED / JOB_COMPLETED | `POST /api/worker/jobs/{id}/complete` (`prod_generation_api.py:927-948`) | Yes | none | `EVT_JOB_COMPLETED` audit log; status `completed` |
| 12 | FRONTEND_RESULT_RECEIVED | `app.js::pollJob()` sees `status==='completed'`, fetches `/result-url` | Yes | none | Result URL present in response |
| 13 | RESULT_CARD_DISPLAYED | `showRealResult()` → `renderSonyaResult()` (app.js:1458-1515) | Yes (DOM) | n/a | Covered by `tests/frontend/test_job_submission_and_result_render.mjs` |
| 14 | EDITOR_HANDOFF_AVAILABLE | `opencut.html` receiving the real job's result | **Not implemented** — `docs/SONYA_AUDIT.md` P1-8: `opencut.html` opens with no job/result passed to it today | n/a | No automated coverage possible until this is wired up; flagged as a known gap, not tested here |

Stages 1-8 (client + upload + job creation + queueing) are covered by
`scripts/qa/lifecycle_diagnostics.py` / `diagnose_generation_flow.py` in
mock mode, with fine-grained sub-stage timing recovered via boundary-call
probes that a real wire client cannot get. Stages 9-12 are covered by the
same tool by directly driving the real `/api/worker/*` endpoints (never a
real vast.ai instance or GPU). Stage 13 is covered by the frontend `node --test`
suite. **Stage 14 has no automated coverage** because the feature it
depends on doesn't exist yet in the frontend — this is a coverage gap to
close once opencut.html integration lands, not something fakeable
meaningfully today.

---

## 4. How to run the QA suite

One command, safe to run any time (no network, no real S3/Postgres/GPU,
no cost):

```bash
./scripts/qa/run_sonya_qa.sh
```

Runs, in order: the full backend pytest suite (`pytest tests/ -v`,
excluding the Postgres-only test which needs a real database — see below),
the frontend `node --test` suite, and the standalone stage-by-stage
diagnostic CLI across all four scenarios (happy path, bad mode, S3 down,
stalled S3). Non-zero exit if anything fails.

Pieces individually:

```bash
# Full backend suite (as CI runs it)
pytest tests/ -v

# Just the new lifecycle diagnostics (pytest, with printed PASS/FAIL report)
pytest tests/test_generation_lifecycle_diagnostics.py -v -s

# Frontend suite (as CI runs it)
node --test tests/frontend/*.mjs

# Standalone CLI, human-readable stage report, exit code reflects pass/fail
python scripts/qa/diagnose_generation_flow.py --scenario happy
python scripts/qa/diagnose_generation_flow.py --scenario bad-mode
python scripts/qa/diagnose_generation_flow.py --scenario s3-down
python scripts/qa/diagnose_generation_flow.py --scenario stalled-s3 --stage-timeout 1

# Postgres-backed idempotency test (needs a real DATABASE_URL, run separately -- see .github/workflows/test.yml)
DATABASE_URL=postgresql://... pytest tests/test_job_idempotency_postgres.py -v
```

`--mode live` is scaffolded in `diagnose_generation_flow.py` for pointing
this at a real staging/production deployment later, but is **not
implemented in this session** — see the `_run_live()` docstring in that
file for exactly what it needs (dedicated QA account, real cookie login,
`simulate_worker=False`, explicit cost confirmation) before it's safe to
run. Per this task's constraints, no real GPU/S3 cost was spent and
production was not touched.

---

## 5. What still requires production validation

- The vast.ai claim bug (`docs/SONYA_AUDIT.md` P0-1) can only be
  confirmed fixed against the real dispatcher/orchestrator/vast.ai
  pipeline — mocked here deliberately (task constraints: no
  dispatcher runs, no real GPU instances).
- Real network-stall behavior (a phone losing signal mid-upload, a flaky
  hotel wifi) can only be characterized precisely on a real device/network
  — the mocked "stalled S3" scenario here proves the *code path* has no
  timeout, but not the exact wall-clock behavior a real user hits.
- Whether boto3's real default timeouts (60s × retries) actually bound a
  real S3 outage in production, or whether Timeweb Cloud's S3-compatible
  endpoint behaves differently under a real network partition, needs a
  live check against the actual endpoint.
- The client-side timeout/AbortController fix and the opencut.html
  editor-handoff wiring (stage 14) are both real product changes, not
  applied in this session (see §1 and the table's stage 14 row) — they
  need implementation and their own testing once decided on, and they
  touch `app.js`/`auth.js`, which have unrelated work in flight from
  another session as of this writing.
