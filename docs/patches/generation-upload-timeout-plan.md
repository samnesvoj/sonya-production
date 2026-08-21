# Generation upload timeout — integration plan (NOT APPLIED)

Status: **draft only**. Nothing in this document has been applied to
`auth.js`, `app.js`, or any other production file. The proposed diffs
below are written against the exact current working-tree contents of
`auth.js`/`app.js` as of 2026-08-21, which already include the in-flight
URL-ingest work from a parallel session (`apiCreateVideoJobFromUrl`,
`detectPlatform`/`isValidVideoUrl` rewrite, `window.sonyaSetProcessingText`/
`window.sonyaSetProcessingProgress`). Verified via `git diff` immediately
before writing this plan that no further changes had landed since the
prior investigation in this session. See §6 for exactly how close each
hunk sits to that work and what to re-check before applying.

Companion reading: `docs/QA_GENERATION_LIFECYCLE.md` (root cause + full
stage map from the earlier investigation this plan builds on).

A runnable, isolated prototype of the core logic below (not wired into
the app) lives at `docs/patches/prototype/timeout-fetch-prototype.mjs`,
with tests at `docs/patches/prototype/test_timeout_fetch_prototype.mjs`
(12/12 passing — see §5).

---

## 1. Root cause (recap)

`auth.js::apiCreateVideoJob()` sends the entire multipart upload (file +
mode + params) as one `fetch()` POST to `/api/generation/jobs`, with no
`AbortController`, no timeout, and no way for the browser to observe
upload progress (`fetch()` has no upload-progress event — only
`XMLHttpRequest` does). The button label `Создаём задачу…`
(`app.js::setGenerateButtonsBusy`) is set the instant the click handler
fires and is the *only* feedback for the entire span from click to job
creation. If the network stalls at any point in that span — mid-upload,
waiting for the server, or mid-`S3 PUT` on the backend — the `fetch()`
promise never settles, `checkAndCreateVideoJob()` never returns, and the
button stays disabled with that label forever. There is no recovery path
short of a hard page reload. Full detail, including why this affects a
40-second video exactly as badly as a 40-minute one, is in
`docs/QA_GENERATION_LIFECYCLE.md` §1.

---

## 2. Proposed timeout model

**Size-aware, not one fixed number.** The request bundles the browser's
own upload with backend validation + S3 PUT + DB insert, so the timeout
budget needs a fixed floor (covers the backend-side work, which doesn't
scale with file size) plus a size-proportional budget (covers the upload
itself), capped at a firm maximum.

```
timeout(bytes) = min(
  BASE_TIMEOUT_MS + bytes / MIN_THROUGHPUT_BYTES_PER_SEC * 1000,
  MAX_TIMEOUT_MS
)

BASE_TIMEOUT_MS             = 20_000        (20s)
MIN_THROUGHPUT_BYTES_PER_SEC = 500 * 1024    (500 KB/s, ~4 Mbps)
MAX_TIMEOUT_MS               = 20 * 60_000   (20 minutes)
```

**Why these numbers:**

- **`BASE_TIMEOUT_MS = 20s`** — covers DNS/TLS handshake, magic-byte
  validation, the synchronous S3 `PUT` (boto3 default timeouts are 60s
  connect/read *per attempt*, but a healthy S3 endpoint responds in low
  hundreds of ms), and the Postgres insert, for a file so small its own
  upload time is negligible. 20s is generous slack above the typical
  sub-second case without being so long it delays detecting a truly dead
  connection on a tiny file.
- **`MIN_THROUGHPUT_BYTES_PER_SEC = 500 KB/s` (~4 Mbps)** — a
  conservative "the connection is slow but genuinely still working" floor
  (weak 4G / poor wifi), not a "this connection is unusable" floor. Below
  this we deliberately choose to report a timeout rather than let the
  button hang silently for tens of minutes on the hope it's merely slow.
  This is the one number in this model that's a judgment call, not a
  measured constant — see the explicit trade-off below.
- **`MAX_TIMEOUT_MS = 20 minutes`** — reuses the exact ceiling already
  established elsewhere in this codebase: `apiCreateVideoJobFromUrl()`'s
  own poll loop caps the URL-ingest flow at 400 iterations × 3s = 20
  minutes. Reusing that number keeps "how long SONYA will wait before
  giving up" consistent across both upload paths instead of introducing a
  second, different number with no precedent.

**Sample outputs** (verified in `test_timeout_fetch_prototype.mjs`):

| File size | Timeout |
|---|---|
| 0 B (edge case) | 20.0s |
| 15 MB (~40s clip) | ~50.7s |
| 200 MB | ~7.2 min |
| 1.5 GB (~40 min 1080p) | 20 min (capped) |
| 2 GB+ (at/above the server's own `MAX_UPLOAD_SIZE_MB` default) | 20 min (capped) |

**Explicit trade-off, stated plainly (per the task's instruction not to
paper over this):** for a very large file on a connection running at or
below the 500 KB/s floor, the 20-minute cap can fire on an upload that is
*genuinely, if slowly, still succeeding* — e.g. a 1.5 GB file at exactly
500 KB/s needs ~52 minutes to fully transfer, and will be aborted at the
20-minute mark having sent only ~39% of it. This is intentional, not an
oversight: `fetch()` gives us no way to distinguish "still sending bytes,
slowly" from "connection is dead", so the only honest choices are (a) no
cap at all (the current, broken behavior), or (b) a firm cap that
occasionally cuts off a slow-but-real transfer. (b) is the better product
trade-off — a clear, actionable "try again on a better connection" beats
an indefinite silent hang — and it's why preserving the idempotency key
across a timeout (§2.1) matters: if the upload actually completed
server-side just after the client gave up, the very next retry attempt
detects that via idempotency replay instead of creating a duplicate job.
The real fix for this trade-off is architectural (§7, "smallest
architecture-compatible UX improvement" and beyond) — split the upload
from job-creation so each phase can be timed/retried independently — not
something a client-side timeout alone can solve.

A **separate, short, fixed timeout (`AUTH_CHECK_TIMEOUT_MS = 15s`)** is
also proposed for `apiGetMe()` (`/auth/me`) — `checkAndCreateVideoJob()`'s
very first `await` — because a hang there produces the *exact same*
"stuck on Создаём задачу" symptom before the upload even starts. This is
flagged as a separable, lower-risk addition (§4.2) that can be dropped
from the patch independently of the main upload-timeout change if a
reviewer wants to scope it down.

### 2.1 Why `_networkError` stays `true` for timeout/aborted (not a new top-level flag)

`checkAndCreateVideoJob()` already special-cases `res._networkError` to
preserve the idempotency key across an ambiguous outcome (client doesn't
know if the server got the request) instead of clearing it the way a
definitive HTTP response would. A timeout or a caller-initiated abort is
*exactly* that same kind of ambiguous outcome — arguably more so, since
aborting client-side does not guarantee the server-side handler, which is
running synchronous boto3/Postgres calls with no cancellation awareness,
actually stops (see §6, "the server may finish the job anyway"). So the
proposed `apiFetch()` keeps `_networkError: true` for all three failure
kinds (network / timeout / aborted) — this means **every existing
`if (res._networkError)` call site in `auth.js` keeps working completely
unchanged**, with zero control-flow edits required anywhere except the
two functions in §4 that opt into a timeout. A new `_networkErrorKind:
'network' | 'timeout' | 'aborted'` field is added *alongside* it, purely
additive, for more specific user messaging and QA diagnostics (§2.2).

### 2.2 Error classification

| Kind | When | `res._networkError` | `res._networkErrorKind` | User message |
|---|---|---|---|---|
| HTTP 4xx | Real response, decline | `false` (unchanged) | n/a | existing per-status handling in `checkAndCreateVideoJob` (400/401/402), unchanged |
| HTTP 5xx | Real response, server error | `false` (unchanged) | n/a | existing generic-error branch, unchanged |
| Network failure | `fetch()` rejects with non-`AbortError` (DNS, connection refused, CORS) | `true` | `'network'` | "Сервер недоступен. Проверьте соединение." (existing default text, unchanged) |
| Timeout | Our own `AbortController` fires after `timeoutMs` | `true` | `'timeout'` | generic: "Превышено время ожидания ответа сервера…", or the caller's `timeoutMessage` override (upload call uses a video-specific message) |
| Caller/navigation abort | An externally-supplied `signal` aborts before our timer fires | `true` | `'aborted'` | "Запрос отменён." |

No caller in the current codebase passes its own `signal` today (there is
no Cancel button anywhere in the upload flow), so the `'aborted'` kind is
forward-looking plumbing, not something reachable by a user action yet —
included because the task asked for it to be distinguished, and because
it costs nothing to support now rather than needing another `apiFetch`
rewrite later if a Cancel button is added.

---

## 3. Smallest safe UX improvement: "Загружаем видео" vs "Создаём задачу"

**Honest assessment first, per the task's instruction:** true stage
separation between "still uploading bytes" and "backend is now creating
the job" is **not observable** on the current architecture, in either
direction:

- The browser cannot tell when its own upload finishes and the server
  starts processing — `fetch()` has no upload-complete event, only a
  single response-received event for the *entire* round trip.
- Worse: by the time any response arrives at all, the job has **already
  been created** server-side (`create_generation_job()` in
  `prod_generation_api.py` creates the DB row before returning). There is
  no "now creating the task" moment that happens *while the client is
  still waiting* — job creation is the fast tail-end of the request, not
  a distinct waiting phase.

Given that, faking a "Загружаем видео" → "Создаём задачу" progression
*during* the wait would misrepresent what's actually happening — the
smallest **honest** improvement is two independent, real changes:

1. **Relabel the entire wait from `Создаём задачу…` to `Загружаем
   видео…`.** This is a more accurate description of what dominates the
   span from the client's own point of view (bytes are being sent, or the
   connection is at least still open) than "creating the task" is — and
   "creating the task" is actively misleading, since it implies a fast
   operation is what's slow, when the actual bottleneck is almost always
   the upload itself.
2. **Add a distinct, brief `Создаём задачу…` state that fires only after
   a successful HTTP response is received**, covering the real (if
   usually sub-second) local bookkeeping between "response received" and
   "navigate to the processing page" (`clearJobIdempotencyKey()`,
   `refreshAuthState()`). This is tied to a genuine, observable
   transition (the response arrived) rather than a fabricated one, and
   costs one line in `app.js` (a new `window.sonyaSetGenerateButtonText`
   hook, mirroring the existing `window.sonyaSetProcessingText` pattern
   already used for the URL-ingest flow) plus one call site in
   `checkAndCreateVideoJob()`.

This is *not* upload-progress UI (no percentage, no progress bar tied to
bytes sent) — per the task's constraint, that would require real
upload-progress events, which `fetch()` cannot provide, and faking one
was explicitly ruled out.

---

## 4. Exact functions to modify

All in `auth.js` unless noted. Diffs are unified-diff style against the
current working-tree content (line numbers as of this writing — see §6
before applying).

### 4.1 `apiFetch()` + `apiGetMe()` — auth.js:33-56

```diff
+const DEFAULT_NETWORK_ERROR_MESSAGES = {
+  timeout: 'Превышено время ожидания ответа сервера. Проверьте соединение и попробуйте снова.',
+  aborted: 'Запрос отменён.',
+  network: 'Сервер недоступен. Проверьте соединение.',
+};
+
+function _classifyFetchError(err, { timedOut }) {
+  if (err && err.name === 'AbortError') return timedOut ? 'timeout' : 'aborted';
+  return 'network';
+}
+
 /* ─────────────────────────────────────────────
    API CLIENT
 ───────────────────────────────────────────── */
 async function apiFetch(path, options = {}) {
+  // timeoutMs: opt-in per call -- omitting it preserves the exact current
+  // behavior (no deadline at all). timeoutMessage: only used for the
+  // 'timeout' classification, lets a caller give a more specific message
+  // than the generic default (see apiCreateVideoJob below).
+  // signal: an optional caller-supplied AbortSignal, composed with our own
+  // timeout controller so either can cancel the request; not used by any
+  // current call site (forward-looking, see §2.2).
+  const { timeoutMs, timeoutMessage, signal: callerSignal, ...fetchOptions } = options;
   const url = SONYA_API_BASE + path;
   const defaults = {
     credentials: 'include',  // send HttpOnly session cookie
     headers: { 'Content-Type': 'application/json' },
   };
+
+  const controller = new AbortController();
+  let timedOut = false;
+  let timer = null;
+  if (timeoutMs) {
+    timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
+  }
+  let onCallerAbort = null;
+  if (callerSignal) {
+    if (callerSignal.aborted) controller.abort();
+    else {
+      onCallerAbort = () => controller.abort();
+      callerSignal.addEventListener('abort', onCallerAbort, { once: true });
+    }
+  }
+
+  const startedAt = Date.now();
   try {
-    const headers = { ...defaults.headers, ...(options.headers || {}) };
-    if (typeof FormData !== 'undefined' && options.body instanceof FormData) {
+    const headers = { ...defaults.headers, ...(fetchOptions.headers || {}) };
+    if (typeof FormData !== 'undefined' && fetchOptions.body instanceof FormData) {
       delete headers['Content-Type'];
     }
-    const res = await fetch(url, { ...defaults, ...options, headers });
+    const res = await fetch(url, { ...defaults, ...fetchOptions, headers, signal: controller.signal });
     return res;
   } catch (e) {
-    // Network / CORS failure
-    console.error('[SONYA API] Network error:', e);
-    return { ok: false, status: 0, _networkError: true,
-      json: async () => ({ detail: 'Сервер недоступен. Проверьте соединение.' }) };
+    const kind = _classifyFetchError(e, { timedOut });
+    const elapsedMs = Date.now() - startedAt;
+    // Diagnostic only: path + classification + timing. Never headers,
+    // cookies, the request body, or any token/secret -- see
+    // test_timeout_fetch_prototype.mjs's breadcrumb test for the exact
+    // field allowlist this must stay within.
+    console.error(`[SONYA API] ${kind} error path=${path} elapsedMs=${elapsedMs}` +
+      (timeoutMs ? ` timeoutMs=${timeoutMs}` : ''));
+    if (typeof window !== 'undefined') {
+      window.SONYA_LAST_NETWORK_ERROR = { path, kind, timeoutMs: timeoutMs || null, elapsedMs, timestamp: Date.now() };
+    }
+    const detail = (kind === 'timeout' && timeoutMessage) || DEFAULT_NETWORK_ERROR_MESSAGES[kind];
+    return { ok: false, status: 0, _networkError: true, _networkErrorKind: kind,
+      json: async () => ({ detail }) };
+  } finally {
+    if (timer) clearTimeout(timer);
+    if (callerSignal && onCallerAbort) callerSignal.removeEventListener('abort', onCallerAbort);
   }
 }

+// Timeout model constants -- see docs/patches/generation-upload-timeout-plan.md §2
+// for the full rationale and the explicit large-file/slow-connection trade-off.
+const UPLOAD_BASE_TIMEOUT_MS = 20_000;
+const UPLOAD_MIN_THROUGHPUT_BYTES_PER_SEC = 500 * 1024; // ~4 Mbps floor
+const UPLOAD_MAX_TIMEOUT_MS = 20 * 60 * 1000; // matches apiCreateVideoJobFromUrl's existing ~20min ceiling
+const AUTH_CHECK_TIMEOUT_MS = 15_000;
+
+function computeUploadTimeoutMs(fileSizeBytes) {
+  const size = Math.max(0, Number(fileSizeBytes) || 0);
+  const sizeBudgetMs = (size / UPLOAD_MIN_THROUGHPUT_BYTES_PER_SEC) * 1000;
+  return Math.min(UPLOAD_BASE_TIMEOUT_MS + sizeBudgetMs, UPLOAD_MAX_TIMEOUT_MS);
+}
+
 async function apiGetMe() {
-  return apiFetch('/auth/me');
+  return apiFetch('/auth/me', { timeoutMs: AUTH_CHECK_TIMEOUT_MS });
 }
```

*(This is the "also recommended" `apiGetMe` change from §2 — separable;
drop the one-line body change and the `AUTH_CHECK_TIMEOUT_MS` constant if
scoping this down. Nothing else in this hunk depends on it.)*

### 4.2 `apiCreateVideoJob()` — auth.js:226-270 (the actual upload call)

Only the final `return apiFetch(...)` changes; everything above it
(file/mode resolution, `FormData` construction) is untouched:

```diff
   return apiFetch('/generation/jobs', {
     method: 'POST',
     body: fd,
-    headers: { 'Idempotency-Key': _getOrCreateJobIdempotencyKey() }
+    headers: { 'Idempotency-Key': _getOrCreateJobIdempotencyKey() },
+    timeoutMs: computeUploadTimeoutMs(uploadedFile.size),
+    timeoutMessage: 'Загрузка видео заняла слишком много времени. Проверьте соединение и попробуйте снова.',
   });
 }
```

### 4.3 `checkAndCreateVideoJob()` success branch — auth.js:762-768

```diff
   if ([200, 201, 202].includes(jobRes.status)) {
+    // See docs/patches/generation-upload-timeout-plan.md §3 -- this is
+    // the one point where "job creation" is a real, observable
+    // transition (the response just arrived), unlike the wait leading up
+    // to it.
+    if (typeof window.sonyaSetGenerateButtonText === 'function') window.sonyaSetGenerateButtonText('Создаём задачу…');
     // Refresh account state to update free_video_used counter
     window.SONYA_LAST_JOB = jobData;
     console.log('[SONYA] generation job created', jobData);
     await refreshAuthState();
     return 'ok';
   }
```

No other branch in `checkAndCreateVideoJob()` changes — the existing
`if (jobRes._networkError)` block (auth.js:748-755) needs **zero** edits;
it already does the right thing for timeout/aborted because of §2.1.

### 4.4 `app.js::setGenerateButtonsBusy()` — app.js:880-897

```diff
 function setGenerateButtonsBusy(busy) {
         [elements.btnNext2, elements.btnGenerate].forEach(btn => {
                 if (!btn) return;
                 if (busy) {
                         if (btn.dataset.origText === undefined) {
                                 btn.dataset.origText = btn.textContent;
                         }
                         btn.disabled = true;
-                        btn.textContent = 'Создаём задачу…';
+                        btn.textContent = 'Загружаем видео…';
                 } else {
                         btn.disabled = false;
                         if (btn.dataset.origText !== undefined) {
                                 btn.textContent = btn.dataset.origText;
                                 delete btn.dataset.origText;
                         }
                 }
         });
 }
+
+// Lets auth.js flip the busy label once a real response has arrived (see
+// checkAndCreateVideoJob's success branch) -- mirrors the existing
+// window.sonyaSetProcessingText hook already used for the URL-ingest
+// flow's processing-page text. Guarded by dataset.origText so this is a
+// no-op on any button that isn't currently mid-submission.
+window.sonyaSetGenerateButtonText = function (text) {
+        [elements.btnNext2, elements.btnGenerate].forEach(btn => {
+                if (btn && btn.dataset.origText !== undefined) btn.textContent = text;
+        });
+};
```

**Total footprint: 2 files, 4 hunks, ~55 added lines, 6 removed lines.**
No function signatures change in a way that breaks any existing caller;
every new parameter (`timeoutMs`, `timeoutMessage`, `signal`) is optional
and additive.

---

## 5. Tests

### 5.1 Already written and passing (isolated prototype, run now)

`docs/patches/prototype/test_timeout_fetch_prototype.mjs` — 12 tests
against the exact logic in §4.1, run with:

```bash
node --test docs/patches/prototype/test_timeout_fetch_prototype.mjs
```

Covers: `computeUploadTimeoutMs` at 0 B / 15 MB / linear scaling / capped
at 2GB+; a stalled request resolving as a classified timeout instead of
hanging; `timeoutMessage` override applying only to the timeout kind; a
plain `TypeError` classified as `'network'`, never `'timeout'`; an
externally-supplied `AbortSignal` classified as `'aborted'`, distinct from
a timeout; no-`timeoutMs` backward compatibility (a slow-but-real response
still resolves normally); a normal success response passed through
untouched; the diagnostic breadcrumb carrying no secrets (asserts the
serialized record's exact key set and that it doesn't contain injected
`Idempotency-Key`/`Authorization` header values); `FormData` `Content-Type`
stripping preserved.

All 12 pass as of this writing (verified in this session).

### 5.2 To add to `tests/frontend/` once the real patch lands

These reuse the QA infrastructure added earlier this session
(`tests/frontend/dom_harness.mjs`'s `loadAuth()`/`loadApp()`, the same
pattern as `tests/frontend/test_upload_timeout_and_stage_diagnostics.mjs`
and `tests/frontend/test_idempotency_key.mjs`). Once `auth.js`/`app.js`
carry the diffs in §4, the two **existing "KNOWN GAP" tests** in
`tests/frontend/test_upload_timeout_and_stage_diagnostics.mjs` need to
flip from proving the *absence* of a timeout to proving its presence —
see the note already left in that file:

> *"this assertion should start failing the moment a timeout/AbortController
> is added ... update this test alongside that fix, don't just delete it."*

New/updated tests, all via `loadAuth()`:

| Test | Scenario | Assertion |
|---|---|---|
| Normal successful upload | `fetchImpl` resolves 202 quickly | `checkAndCreateVideoJob()` returns `'ok'`; `window.SONYA_LAST_JOB` set; no timer left dangling (test completes without a hanging handle) |
| HTTP 400 | `fetchImpl` resolves `{status:400}` | unchanged from existing coverage — returns `'error'`, toast shown (already covered; just re-run to confirm no regression) |
| HTTP 401 | `fetchImpl` resolves `{status:401}` on `/auth/me` | unchanged — `openAuthModal` called, returns `'auth'` (already covered by `test_auth_guest_state.mjs`; re-run for regression) |
| HTTP 402 | `fetchImpl` resolves `{status:402, detail:{code:'FREE_PLAN_USED'}}` | unchanged — paywall opens, returns `'paywall'` (already covered by the new suite added this session; re-run for regression) |
| HTTP 500 | `fetchImpl` resolves `{status:500}` | unchanged — generic error toast (already covered; re-run for regression) |
| **Stalled fetch → timeout (updated "KNOWN GAP" test)** | `fetchImpl` returns a promise that only settles on its `AbortSignal` firing (never resolves otherwise); real timers, `timeoutMs` small in the test | `apiCreateVideoJob()`/`apiFetch()` now **does** settle, with `_networkErrorKind: 'timeout'`; `checkAndCreateVideoJob()` returns `'error'`; a toast with the timeout message appears |
| **AbortError classification** | Force `fetchImpl` to reject with `name: 'AbortError'` via an externally-supplied `signal.abort()` (not the internal timer) | `_networkErrorKind === 'aborted'`, not `'timeout'` |
| **Network TypeError** | `fetchImpl` rejects with a plain `TypeError` | `_networkErrorKind === 'network'`; message is the generic one, not the timeout-specific override |
| **No duplicate submission after timeout** | Simulate a timeout, then a retry that succeeds | Both requests carry the *same* `Idempotency-Key` header (assert on the two recorded `fetchCalls[i].opts.headers['Idempotency-Key']`); exactly one `checkAndCreateVideoJob` object was ever "in flight" per `jobSubmitInFlight` semantics — reuses `test_idempotency_key.mjs`'s existing pattern for "network error keeps the same key for a retry of the same attempt", extended to the timeout case |
| **Generation lock released after timeout** | Same timeout scenario, driven through `submitGenerationJob()` (via `loadApp()` + a `checkAndCreateVideoJob` stub returning `'error'` after simulating the timeout path) | `btn.disabled === false` after the timeout resolves (reuses the existing "a POST error releases the frontend lock so the user can retry" test's shape from `test_job_submission_and_result_render.mjs`, parameterized for a timeout instead of a generic error) |
| **URL-ingest flow unaffected** | Drive `apiCreateVideoJobFromUrl()` end to end (checking → downloading → uploading → queued) with **no** `timeoutMs` on any of its `apiFetch` calls (verify by inspecting `fetchCalls[i].opts` in the harness — none should carry a `timeoutMs`/`signal` key, since §4 deliberately does not touch this function) | Existing behavior identical to pre-patch: same status-text progression (`URL_INGEST_STATUS_TEXT`), same ~20-minute/400-iteration ceiling, same return shape into `checkAndCreateVideoJob()` |
| **Button label transition** | Successful upload | `btn.textContent` observed as `'Загружаем видео…'` while the request is in flight, then `'Создаём задачу…'` for the brief window between response-received and page navigation (via the same `recordWrites()`-style property-shadowing technique already used in `test_upload_timeout_and_stage_diagnostics.mjs`) |
| `apiGetMe` timeout (if the §4.2/2 optional change is included) | `/auth/me` never resolves | `checkAndCreateVideoJob()` reaches the `!meRes.ok` branch (not the 401 guest-state branch), shows a real error, returns `'error'` — must NOT be presented as "please log in" (mirrors the existing 5xx-vs-401 distinction test already in `test_auth_guest_state.mjs`) |

### 5.3 Backend suite

No backend changes are proposed in this plan (everything is client-side),
so `pytest tests/` needs no new cases for this patch specifically. The
existing `scripts/qa/diagnose_generation_flow.py --scenario stalled-s3`
already proves the *server-side* half of the story (no server-side
timeout either) — that finding stands independent of this frontend patch
and is out of scope here (see `docs/QA_GENERATION_LIFECYCLE.md` §1 "Fix
direction" for the longer-term architectural note).

---

## 6. Integration risks with the URL-ingest work

Checked against the current, already-in-flight `auth.js`/`app.js` diffs
(`git diff` re-verified immediately before writing this plan — unchanged
from the state analyzed):

- **`apiFetch()` (auth.js:33-52) — untouched by the URL-ingest diff.**
  Zero overlap; safe to apply directly.
- **`apiGetMe()` (auth.js:54-56) — untouched by the URL-ingest diff.**
  Zero overlap.
- **`apiCreateVideoJob()` (auth.js:226-270) — the URL-ingest diff already
  modified this function** (removed a URL-related fallback message from
  the `file_required` error branch, lines ~236-247 in the current file).
  My change only touches the trailing `return apiFetch(...)` block
  (lines ~265-269), a few lines after their edit, not overlapping it —
  but **re-read the function fresh before applying**, since a textual
  patch/diff can drift if either session's line numbers move.
- **`apiCreateVideoJobFromUrl()` — a whole new function added by the
  URL-ingest work.** Not touched by this plan at all, deliberately (§4
  only lists `apiFetch`, `apiGetMe`, `apiCreateVideoJob`,
  `checkAndCreateVideoJob`'s success branch). No `timeoutMs` is added to
  any of its `apiFetch` calls — its own ~20-minute/400-iteration polling
  ceiling is left exactly as-is, out of scope for this patch.
- **`checkAndCreateVideoJob()` (auth.js:724-787) — the URL-ingest diff
  added the `isUrlMode` dispatch line (auth.js:745-746), immediately
  before the `if (jobRes._networkError)` block.** My one addition (§4.3)
  is inside the `if ([200, 201, 202].includes(jobRes.status))` block,
  ~15 lines further down — adjacent to, but not overlapping, their edit.
  Low conflict risk, but same caution: re-verify current line numbers
  before applying.
- **`app.js`'s `window.sonyaSetProcessingText`/`sonyaSetProcessingProgress`
  exposure (added by the URL-ingest work, inside the polling IIFE near
  the bottom of the file) — a completely different region of `app.js`**
  from `setGenerateButtonsBusy()` (§4.4, non-IIFE top-level scope). Zero
  overlap. The new `window.sonyaSetGenerateButtonText` hook this plan adds
  is a sibling of that existing pattern, not a replacement or conflict.
- **The server may finish the job anyway, after the client times out.**
  Aborting a `fetch()` client-side stops the browser from sending further
  bytes / closes the connection, but the FastAPI handler's synchronous
  `boto3.put_object()`/Postgres calls have no cancellation awareness —
  Python code blocked in a synchronous call doesn't notice a client
  disconnect mid-call. So a client-observed timeout does **not** guarantee
  the server didn't complete the job anyway. This is exactly why §2.1
  keeps the idempotency key alive across a timeout: the next retry (same
  key) either creates a fresh job or replays the one that quietly
  succeeded, never a duplicate. This is a pre-existing characteristic of
  the request shape (true today with a hard page reload mid-upload too),
  not something this patch introduces or needs to additionally solve.
- **No interaction with `scripts/prod_generation_api.py`,
  `scripts/upload_security.py`, `requirements-backend.txt`, or
  `.env.example`** — this plan is 100% frontend (`auth.js`/`app.js`
  only), so the backend-side URL-ingest changes in those files are
  entirely unaffected regardless of application order.

**Recommended apply order:** after the URL-ingest session's work is
merged/frozen. Re-run `git diff -- auth.js app.js` immediately before
applying to confirm the four hunks in §4 still match context; if line
numbers shifted, re-derive the hunks against the then-current file rather
than force-applying stale line numbers.

---

## 7. Manual verification procedure

Once applied:

1. **Fast connection, small file** — upload a short local clip. Confirm
   the button shows "Загружаем видео…", flips briefly to "Создаём
   задачу…" right before the processing page appears, and the flow
   completes exactly as before.
2. **Fast connection, large file** (a few hundred MB, e.g. record a
   longer local screen capture) — confirm no premature timeout (should
   comfortably fit under the size-aware budget) and the same completion
   as (1).
3. **Simulated stall** — using Chrome DevTools → Network → "Offline" (or
   a custom slow 3G profile with the connection cut mid-upload), start an
   upload of a small file, then go offline before it completes. Confirm:
   the button becomes clickable again within the computed timeout window
   (not instantly, not never), a toast with a clear message appears, and
   a second click after going back online successfully creates a job
   (verifies the idempotency key survived and no duplicate was created —
   check `network` tab for the `Idempotency-Key` header value on both
   attempts, and/or check the backend logs for at most one
   `job_created`/`idempotency_replay` per logical attempt).
4. **`window.SONYA_LAST_NETWORK_ERROR` in DevTools console** after
   forcing any of the above failure kinds — confirm it's populated with
   `{path, kind, timeoutMs, elapsedMs, timestamp}` and nothing else
   (no headers, no cookie values, no request body).
5. **URL-ingest regression check** — submit a job via a video URL (not a
   file upload) and confirm the "Проверяем ссылку… / Получаем видео… /
   Загружаем видео…" progression and timing are unchanged from before this
   patch (this flow is untouched by design — see §6).
6. Run the full existing suite for a clean baseline before/after:
   `./scripts/qa/run_sonya_qa.sh` (added earlier this session) plus
   `node --test docs/patches/prototype/test_timeout_fetch_prototype.mjs`.

---

## 8. Rollback strategy

Because the change is additive and confined to two files with no backend
or schema involvement:

- **Full rollback:** `git checkout -- auth.js app.js` (or revert the
  specific commit that applied §4) — restores the pre-patch multipart
  upload with no timeout. No data migration, no backend redeploy, no
  cache invalidation needed; the server-side contract (`POST
  /api/generation/jobs`) is completely unchanged by this patch, so the
  frontend can be rolled back independently at any time.
- **Partial rollback (keep the timeout, drop the label change):** revert
  only the §4.4 `app.js` hunk if the new button-text transition turns out
  to be visually jarring or conflicts with an in-flight design change —
  the timeout/classification logic in §4.1-4.3 has no dependency on the
  label text.
- **Partial rollback (drop the `apiGetMe` timeout, keep the upload
  timeout):** revert just the one-line `apiGetMe` body change in §4.1 —
  it's independent of `computeUploadTimeoutMs`/`apiCreateVideoJob`.
- **If the size-aware timeout proves too aggressive in practice** (real
  users on legitimately slow connections hitting the 20-minute cap more
  than expected): the constants in §4.1 (`UPLOAD_MIN_THROUGHPUT_BYTES_PER_SEC`,
  `UPLOAD_MAX_TIMEOUT_MS`) are the only numbers to tune — no structural
  change needed, and `docs/patches/prototype/test_timeout_fetch_prototype.mjs`'s
  `computeUploadTimeoutMs` tests should be updated alongside any constant
  change to keep the documented trade-off (§2) accurate.
- No database migration, no server-side deploy, and no coordination with
  the vast.ai/worker pipeline is required at any point — this entire
  patch is a static-file change.
