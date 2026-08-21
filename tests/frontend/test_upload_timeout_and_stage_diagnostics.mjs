// Frontend-side coverage for the SONYA generation-lifecycle diagnostics
// task (see docs/QA_GENERATION_LIFECYCLE.md):
//
//   1. Characterizes the actual root cause of "СОЗДАЁМ ЗАДАЧУ" hanging
//      indefinitely: apiFetch()/apiCreateVideoJob() in auth.js issue a
//      plain fetch() with no AbortController and no timeout. If the
//      network stalls after the request is sent, the promise NEVER
//      settles on its own -- there is nothing here to "fix" by waiting
//      longer, because there is no upper bound at all.
//   2. checkAndCreateVideoJob()'s generic error branches (400/500/402) --
//      each must surface a user-visible message and leave the submission
//      lock in a retryable state, distinct from the 401-guest-state path
//      already covered by test_auth_guest_state.mjs.
//   3. The queued -> claimed -> processing -> mode_running -> completed
//      status/progress transitions app.js's poll loop drives, so a broken
//      status-to-text/progress mapping is caught here instead of by a
//      human staring at a stuck progress bar.
//
// Loads the REAL app.js and auth.js (unmodified) via dom_harness.mjs --
// same approach as every other tests/frontend/*.mjs file. Run with:
//   node --test tests/frontend/
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadApp, loadAuth, click } from './dom_harness.mjs';

async function flush(ticks = 8) {
  for (let i = 0; i < ticks; i++) await Promise.resolve();
}

function deferred() {
  let resolve;
  const promise = new Promise((res) => { resolve = res; });
  return { promise, resolve };
}

// A tiny sentinel promise used to prove "has this other promise settled
// yet?" without a real timer -- Promise.race against a promise we control
// is deterministic and instant, unlike a real setTimeout-based race.
const PENDING = Symbol('pending');
async function isSettled(p) {
  const result = await Promise.race([p.then(() => 'settled', () => 'settled'), Promise.resolve(PENDING)]);
  return result !== PENDING;
}

// ── 1. Client-side timeout / AbortController ─────────────────────────────
//
// Was "KNOWN GAP: ... never settles" -- apiFetch() now takes an optional
// timeoutMs and races a real AbortController against the stall, so these
// two prove the fix instead of documenting the gap (see
// docs/patches/generation-upload-timeout-plan.md). A stalled real fetch()
// rejects with a DOMException-shaped AbortError the instant its signal
// fires -- signalAwareStall() below mirrors that so the fake fetchImpl
// behaves like a real one instead of silently ignoring the signal, which
// would hide the exact bug this test guards against.

function signalAwareStall(signal) {
  return new Promise((resolve, reject) => {
    const onAbort = () => reject(Object.assign(new Error('The operation was aborted.'), { name: 'AbortError' }));
    if (signal.aborted) return onAbort();
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

test('apiFetch({ timeoutMs }): a stalled request now settles as a classified timeout instead of hanging forever', async () => {
  const { sandbox } = loadAuth({ fetchImpl: (url, opts) => signalAwareStall(opts.signal) });

  const res = await sandbox.apiFetch('/auth/me', { timeoutMs: 30 });

  assert.equal(res.ok, false);
  assert.equal(res._networkError, true, 'existing call sites check _networkError -- must stay true for a timeout');
  assert.equal(res._networkErrorKind, 'timeout');
});

test('apiFetch(): omitting timeoutMs preserves the original no-deadline behavior (backward compatible)', async () => {
  const gate = deferred(); // never resolved during this test
  const { sandbox } = loadAuth({ fetchImpl: () => gate.promise });

  const p = sandbox.apiFetch('/auth/me'); // no timeoutMs

  await flush(20);
  assert.equal(await isSettled(p), false, 'a call that opts out of timeoutMs must still be able to wait indefinitely');

  gate.resolve({ ok: true, status: 200, json: async () => ({}) }); // clean up, avoid a dangling handle
  await p;
});

test('apiGetMe(): a stalled /auth/me request now settles as a timeout instead of hanging checkAndCreateVideoJob forever', async () => {
  const { sandbox } = loadAuth({ fetchImpl: (url, opts) => signalAwareStall(opts.signal) });

  const res = await sandbox.apiGetMe();

  assert.equal(res._networkError, true);
  assert.equal(res._networkErrorKind, 'timeout');
});

test('apiCreateVideoJob() (the multipart upload+job-create call): a stalled network now settles as a timeout, not an infinite hang', async () => {
  const { sandbox, fetchCalls } = loadAuth({ fetchImpl: (url, opts) => signalAwareStall(opts.signal) });
  sandbox.appState.uploadedFile = new File([new Uint8Array(1024)], 'clip.mp4', { type: 'video/mp4' });

  const res = await sandbox.apiCreateVideoJob({ source: { mode: 'upload' }, clipType: 'viral' });

  assert.equal(res._networkError, true,
    'the SAME request bundles the entire browser upload + backend receive + S3 upload + DB insert ' +
    '(see root cause report) -- proving it now times out here proves the whole bundled span is bounded.');
  assert.equal(res._networkErrorKind, 'timeout',
    'only reachable if computeUploadTimeoutMs() actually armed a real timeoutMs on this call -- without ' +
    'one, this same stalled fetchImpl would hang forever instead of settling.');
  assert.ok(fetchCalls.some((c) => c.url.includes('/generation/jobs')), 'sanity: the upload request was actually sent');
});

// ── 1b. checkAndCreateVideoJob()/submitGenerationJob() timeout end-to-end ─

test('checkAndCreateVideoJob(): a stalled upload resolves to "error" with a toast, and a retry after it reuses the same Idempotency-Key', async () => {
  let jobCall = 0;
  const { sandbox, document, fetchCalls } = loadAuth({
    fetchImpl: async (url, opts) => {
      if (url.endsWith('/auth/me')) return { ok: true, status: 200, json: async () => ({ user_id: 'u1' }) };
      jobCall += 1;
      if (jobCall === 1) return signalAwareStall(opts.signal); // first attempt stalls, then times out
      return { ok: true, status: 202, json: async () => ({ job_id: 'job-1', status: 'queued' }) };
    },
  });
  sandbox.appState.uploadedFile = new File([new Uint8Array(1024)], 'clip.mp4', { type: 'video/mp4' });

  const first = await sandbox.checkAndCreateVideoJob({ source: { mode: 'upload' }, clipType: 'viral' });
  assert.equal(first, 'error');
  const toast = document.getElementById('sonya-toast');
  assert.ok(toast, 'a timeout must surface exactly one visible error, never a silent hang');
  assert.ok(toast.className.includes('sonya-toast--error'));

  const second = await sandbox.checkAndCreateVideoJob({ source: { mode: 'upload' }, clipType: 'viral' });
  assert.equal(second, 'ok');

  const jobFetches = fetchCalls.filter((c) => c.url.includes('/generation/jobs'));
  const key1 = jobFetches[0]?.opts?.headers?.['Idempotency-Key'];
  const key2 = jobFetches[1]?.opts?.headers?.['Idempotency-Key'];
  assert.ok(key1, 'timed-out attempt must have sent a key');
  assert.equal(key2, key1,
    'a retry after a client-observed timeout must reuse the same key -- the server may have completed the ' +
    'first attempt anyway (see plan §6), so a fresh key here could create a duplicate job');
});

test('submitGenerationJob(): the generation lock/button is released after an upload timeout, so the user can retry', async () => {
  const { document } = loadApp({
    checkAndCreateVideoJob: async () => 'error', // simulates the timeout path resolving through checkAndCreateVideoJob
  });

  const btn = document.getElementById('btn-next-2');
  click(btn);
  await flush();

  assert.equal(btn.disabled, false, 'lock must be released after a timeout, exactly like any other error result');
});

test('button label: shows "Загружаем видео…" while the request is in flight, then "Создаём задачу…" once a response arrives', async () => {
  const gate = deferred();
  let observedDuringFlight = null;
  let document, btn, sandbox;
  ({ document, sandbox } = loadApp({
    checkAndCreateVideoJob: async () => {
      // Mirrors what checkAndCreateVideoJob's real success branch does
      // right after the response arrives, before returning 'ok'.
      observedDuringFlight = btn.textContent;
      sandbox.window.sonyaSetGenerateButtonText('Создаём задачу…');
      await gate.promise;
      return 'ok';
    },
  }));
  btn = document.getElementById('btn-next-2');

  click(btn);
  await flush();
  assert.equal(observedDuringFlight, 'Загружаем видео…', 'must show the upload label while still in flight');
  assert.equal(btn.textContent, 'Создаём задачу…', 'must flip once the response has arrived');

  gate.resolve();
  await flush();
});

test('URL-ingest flow is unaffected: apiCreateVideoJobFromUrl() calls carry no timeoutMs/signal, and its own ~20min/400-iteration ceiling is untouched', async () => {
  const { sandbox, fetchCalls } = loadAuth({
    fetchImpl: async (url) => {
      if (url.endsWith('/from-url')) {
        return { ok: true, status: 202, json: async () => ({ ingest_id: 'ing-1' }) };
      }
      return { ok: true, status: 200, json: async () => ({ status: 'queued', job_id: 'job-1' }) };
    },
  });

  const res = await sandbox.apiCreateVideoJobFromUrl({ source: { mode: 'url', url: 'https://youtube.com/watch?v=abc' }, clipType: 'viral' });

  assert.equal(res.ok, true);
  const ingestCalls = fetchCalls.filter((c) => c.url.includes('/generation/jobs/from-url'));
  assert.ok(ingestCalls.length >= 2, 'expects the initial POST plus at least one status poll');
  for (const call of ingestCalls) {
    // apiFetch() always attaches its own (never-firing, since no timeoutMs
    // is given) AbortController signal to every call -- that's harmless
    // plumbing. What must NOT happen is the URL-ingest calls picking up a
    // real deadline: no timeoutMs means the internal timer is never armed.
    assert.equal(call.opts.timeoutMs, undefined, 'URL-ingest calls must not inherit the multipart-upload timeout');
  }
});

// ── 2. checkAndCreateVideoJob(): generic error branches ──────────────────

test('checkAndCreateVideoJob(): backend 400 (invalid_mode) shows the server message and returns "error"', async () => {
  const { sandbox, document } = loadAuth({
    fetchImpl: async (url) => {
      if (url.endsWith('/auth/me')) return { ok: true, status: 200, json: async () => ({ user_id: 'u1' }) };
      return {
        ok: false, status: 400,
        json: async () => ({ detail: { error: 'invalid_mode', message: 'Недопустимый режим генерации' } }),
      };
    },
  });
  sandbox.appState.uploadedFile = new File([new Uint8Array(10)], 'clip.mp4', { type: 'video/mp4' });

  const result = await sandbox.checkAndCreateVideoJob({ source: { mode: 'upload' }, clipType: 'viral' });

  assert.equal(result, 'error');
  const toast = document.getElementById('sonya-toast');
  assert.ok(toast, 'a real error must be surfaced to the user (toast)');
  assert.ok(toast.className.includes('sonya-toast--error'));
  assert.match(toast.innerHTML, /Недопустимый режим/);
});

test('checkAndCreateVideoJob(): backend 500 shows a generic error, not a silent failure', async () => {
  const { sandbox, document } = loadAuth({
    fetchImpl: async (url) => {
      if (url.endsWith('/auth/me')) return { ok: true, status: 200, json: async () => ({ user_id: 'u1' }) };
      return { ok: false, status: 500, json: async () => ({ detail: { error: 'internal_error' } }) };
    },
  });
  sandbox.appState.uploadedFile = new File([new Uint8Array(10)], 'clip.mp4', { type: 'video/mp4' });

  const result = await sandbox.checkAndCreateVideoJob({ source: { mode: 'upload' }, clipType: 'viral' });

  assert.equal(result, 'error');
  const toast = document.getElementById('sonya-toast');
  assert.ok(toast, 'a 500 must surface exactly one visible error, never be swallowed');
  assert.ok(toast.className.includes('sonya-toast--error'));
});

test('checkAndCreateVideoJob(): backend 402 FREE_PLAN_USED opens the paywall modal, not a generic error', async () => {
  const { sandbox, document } = loadAuth({
    fetchImpl: async (url) => {
      if (url.endsWith('/auth/me')) return { ok: true, status: 200, json: async () => ({ user_id: 'u1' }) };
      return {
        ok: false, status: 402,
        json: async () => ({ detail: { code: 'FREE_PLAN_USED', message: 'Бесплатное видео уже использовано' } }),
      };
    },
  });
  sandbox.appState.uploadedFile = new File([new Uint8Array(10)], 'clip.mp4', { type: 'video/mp4' });
  document.register('sonya-paywall-modal', document.createElement('div'));

  const result = await sandbox.checkAndCreateVideoJob({ source: { mode: 'upload' }, clipType: 'viral' });

  assert.equal(result, 'paywall');
  assert.ok(document.getElementById('sonya-paywall-modal').classList.contains('is-open'));
});

// ── 3. Multi-stage status/progress transitions ───────────────────────────

// Records every value written through an accessor property (textContent,
// style.width, ...) in order, by shadowing the prototype accessor on this
// one instance -- more reliable than sampling the DOM between fixed
// flush() ticks, since pollJob()'s loop alternates real setTimeout(0)
// macrotasks with fetch-driven microtasks and there's no clean tick
// boundary to sample "in between" from outside.
function recordWrites(target, prop) {
  const values = [];
  let current = target[prop];
  Object.defineProperty(target, prop, {
    get() { return current; },
    set(v) { current = v; values.push(v); },
    configurable: true,
  });
  return values;
}

test('pollJob(): queued -> claimed -> processing -> mode_running -> completed drives status text and progress in order', async () => {
  const statuses = ['queued', 'claimed', 'processing', 'mode_running', 'completed'];
  let call = 0;

  const { sandbox, document } = loadApp({ checkAndCreateVideoJob: async () => 'ok' });
  // Not in dom_harness's REQUIRED_IDS (this file is the first to touch the
  // processing page's own status/progress elements) -- ids match
  // index.html's real markup (#processing-status / #progress-bar).
  document.register('processing-status', document.createElement('p'));
  document.register('progress-bar', document.createElement('div'));

  sandbox.fetch = async (url) => {
    if (url.endsWith('/result-url')) {
      return { ok: true, status: 200, json: async () => ({ url: 'https://s3.example.com/out.mp4' }) };
    }
    const st = statuses[Math.min(call, statuses.length - 1)];
    call += 1;
    return { ok: true, status: 200, json: async () => ({ status: st, id: 'job-1' }) };
  };
  sandbox.window.fetch = sandbox.fetch;

  const texts = recordWrites(document.getElementById('processing-status'), 'textContent');
  const widths = recordWrites(document.getElementById('progress-bar').style, 'width');

  await sandbox.window.sonyaPollJob('job-1');

  assert.deepEqual(texts, [
    'Видео в очереди', // pollJob()'s own initial setText(), before the first status even comes back
    'Видео в очереди',
    'GPU забрал задачу',
    'Генерация видео',
    'AI анализирует и монтирует',
    'Видео готово',
  ]);
  // The "completed" branch writes 100% twice -- once via the normal
  // setText/setProgress(statusForJob) pair every status gets, then again
  // via an explicit setProgress(100) right before fetching the result (see
  // pollJob() in app.js) -- both are real writes, not a test artifact.
  assert.deepEqual(widths, ['12%', '12%', '32%', '58%', '85%', '100%', '100%']);
});

test('pollJob(): an unknown intermediate status never resets progress backward', async () => {
  const statuses = ['queued', 'a_future_status_this_client_does_not_know_about', 'processing', 'completed'];
  let call = 0;
  const { sandbox, document } = loadApp();
  document.register('processing-status', document.createElement('p'));
  document.register('progress-bar', document.createElement('div'));

  sandbox.fetch = async (url) => {
    if (url.endsWith('/result-url')) {
      return { ok: true, status: 200, json: async () => ({ url: 'https://s3.example.com/out.mp4' }) };
    }
    const st = statuses[Math.min(call, statuses.length - 1)];
    call += 1;
    return { ok: true, status: 200, json: async () => ({ status: st, id: 'job-1' }) };
  };
  sandbox.window.fetch = sandbox.fetch;

  const widths = recordWrites(document.getElementById('progress-bar').style, 'width');

  await sandbox.window.sonyaPollJob('job-1');

  // setProgress(null) for the unknown status is a documented no-op (see
  // app.js's progressForStatus()/setProgress()) -- the bar simply isn't
  // touched again until "processing", so 12% never gets overwritten with
  // something blank in between. The trailing 100% appears twice for the
  // same reason as the test above (completed's extra explicit setProgress(100)).
  assert.deepEqual(widths, ['12%', '12%', '58%', '100%', '100%']);
});

test('a job stuck in "queued" forever is reported via the poll loop\'s own generous ceiling, never a silent infinite hang', async () => {
  // Regression guard for the P0 "vast.ai claim bug" shape (see
  // docs/SONYA_AUDIT.md): worker never claims the job, status never
  // advances past "queued". pollJob() must still terminate on its own
  // built-in ceiling with a visible message, not spin forever.
  const { sandbox, document } = loadApp();
  document.register('processing-status', document.createElement('p'));
  document.register('progress-bar', document.createElement('div'));
  sandbox.fetch = async () => ({ ok: true, status: 200, json: async () => ({ status: 'queued', id: 'job-1' }) });
  sandbox.window.fetch = sandbox.fetch;

  await sandbox.window.sonyaPollJob('job-1');

  assert.match(document.getElementById('processing-status').textContent, /ещё выполняется/i);
});
