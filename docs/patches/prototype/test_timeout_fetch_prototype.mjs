// Real, runnable tests for the proposed apiFetch()/computeUploadTimeoutMs()
// logic (docs/patches/generation-upload-timeout-plan.md), exercised against
// the isolated prototype in timeout-fetch-prototype.mjs -- NOT against the
// real auth.js (which has not been modified; see the plan doc). These tests
// prove the timeout math and the timeout/aborted/network/success
// classification logic are correct in isolation, ahead of the real patch.
//
// Run with: node --test docs/patches/prototype/
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  makeApiFetch, computeUploadTimeoutMs,
  UPLOAD_BASE_TIMEOUT_MS, UPLOAD_MAX_TIMEOUT_MS, DEFAULT_NETWORK_ERROR_MESSAGES,
} from './timeout-fetch-prototype.mjs';

// ── computeUploadTimeoutMs(): the size-aware timeout model ───────────────

test('computeUploadTimeoutMs(0): a near-empty body gets just the fixed base timeout', () => {
  assert.equal(computeUploadTimeoutMs(0), UPLOAD_BASE_TIMEOUT_MS);
  assert.equal(computeUploadTimeoutMs(undefined), UPLOAD_BASE_TIMEOUT_MS);
});

test('computeUploadTimeoutMs(15MB): a short local video gets a modest, well-bounded timeout', () => {
  const ms = computeUploadTimeoutMs(15 * 1024 * 1024);
  // base 20s + 15MB/500KBps ≈ 30.7s ≈ 50.7s total -- generous for a slow
  // connection, but nowhere near the 20-minute cap.
  assert.ok(ms > 45_000 && ms < 60_000, `expected ~50.7s, got ${ms}ms`);
});

test('computeUploadTimeoutMs(): scales linearly with size below the cap', () => {
  const small = computeUploadTimeoutMs(10 * 1024 * 1024);
  const double = computeUploadTimeoutMs(20 * 1024 * 1024);
  // Both budgets, minus the shared fixed base, should be ~2x apart.
  const smallBudget = small - UPLOAD_BASE_TIMEOUT_MS;
  const doubleBudget = double - UPLOAD_BASE_TIMEOUT_MS;
  assert.ok(Math.abs(doubleBudget / smallBudget - 2) < 0.01);
});

test('computeUploadTimeoutMs(): a very large (multi-GB) file is clamped to the 20-minute cap, never unbounded', () => {
  const twoGB = 2 * 1024 * 1024 * 1024;
  assert.equal(computeUploadTimeoutMs(twoGB), UPLOAD_MAX_TIMEOUT_MS);
  const fiveGB = 5 * 1024 * 1024 * 1024; // larger than the server's own MAX_UPLOAD_SIZE_MB=2048 default -- still must not exceed the cap
  assert.equal(computeUploadTimeoutMs(fiveGB), UPLOAD_MAX_TIMEOUT_MS);
});

// ── apiFetch(): fake fetch implementations that behave like a real one ───

function neverSettlingFetch(signal) {
  // A stalled connection: never resolves on its own, but DOES honor
  // AbortSignal the way a real browser fetch() does -- rejects with a
  // DOMException-shaped AbortError the instant the signal fires. This is
  // the realistic shape a stalled `fetch()` actually has; a fetch that
  // ignores its own AbortSignal is not something any real browser produces.
  return new Promise((resolve, reject) => {
    const onAbort = () => reject(Object.assign(new Error('The operation was aborted.'), { name: 'AbortError' }));
    if (signal.aborted) return onAbort();
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

function typeErrorFetch() {
  // What a real fetch() throws for DNS failure / connection refused / CORS
  // rejection -- a plain TypeError, name !== 'AbortError'.
  return Promise.reject(new TypeError('Failed to fetch'));
}

function okFetch(body = {}) {
  return async () => ({ ok: true, status: 200, json: async () => body });
}

test('apiFetch(): a stalled request with timeoutMs set resolves as a classified timeout, not a hang', async () => {
  const apiFetch = makeApiFetch({
    fetchImpl: (url, { signal }) => neverSettlingFetch(signal),
  });

  const res = await apiFetch('/generation/jobs', { method: 'POST', timeoutMs: 30 });

  assert.equal(res.ok, false);
  assert.equal(res.status, 0);
  assert.equal(res._networkError, true, 'existing call sites check _networkError -- must stay true for a timeout');
  assert.equal(res._networkErrorKind, 'timeout');
  const body = await res.json();
  assert.equal(body.detail, DEFAULT_NETWORK_ERROR_MESSAGES.timeout);
});

test('apiFetch(): timeoutMessage overrides the default message for a timeout, and only for a timeout', async () => {
  const apiFetch = makeApiFetch({
    fetchImpl: (url, { signal }) => neverSettlingFetch(signal),
  });

  const res = await apiFetch('/generation/jobs', {
    method: 'POST', timeoutMs: 30, timeoutMessage: 'Загрузка видео заняла слишком много времени.',
  });

  const body = await res.json();
  assert.equal(body.detail, 'Загрузка видео заняла слишком много времени.');
});

test('apiFetch(): a plain network failure (TypeError) is classified "network", never mistaken for a timeout', async () => {
  const apiFetch = makeApiFetch({ fetchImpl: typeErrorFetch });

  const res = await apiFetch('/auth/me', { timeoutMs: 30_000, timeoutMessage: 'should not appear' });

  assert.equal(res._networkError, true);
  assert.equal(res._networkErrorKind, 'network');
  const body = await res.json();
  assert.equal(body.detail, DEFAULT_NETWORK_ERROR_MESSAGES.network,
    'a real network error must get the generic message, not the timeout override');
});

test('apiFetch(): an externally-supplied AbortSignal firing is classified "aborted", distinct from a timeout', async () => {
  const apiFetch = makeApiFetch({ fetchImpl: (url, { signal }) => neverSettlingFetch(signal) });
  const externalController = new AbortController();

  const p = apiFetch('/generation/jobs', { method: 'POST', signal: externalController.signal, timeoutMs: 60_000 });
  externalController.abort(); // fires well before the 60s timeout would

  const res = await p;
  assert.equal(res._networkError, true);
  assert.equal(res._networkErrorKind, 'aborted', 'must not be reported as "timeout" -- the deadline never fired');
  const body = await res.json();
  assert.equal(body.detail, DEFAULT_NETWORK_ERROR_MESSAGES.aborted);
});

test('apiFetch(): no timeoutMs means no deadline at all -- a slow-but-real response still resolves normally (backward compatible)', async () => {
  const apiFetch = makeApiFetch({
    fetchImpl: (url, { signal }) => new Promise((resolve, reject) => {
      const onAbort = () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
      signal.addEventListener('abort', onAbort);
      setTimeout(() => {
        signal.removeEventListener('abort', onAbort);
        resolve({ ok: true, status: 202, json: async () => ({ job_id: 'job-1' }) });
      }, 50); // a real, if slightly slow, response
    }),
  });

  const res = await apiFetch('/generation/jobs/from-url/abc'); // no timeoutMs -- URL-ingest poll call shape

  assert.equal(res.ok, true);
  assert.equal(res.status, 202);
  assert.equal((await res.json()).job_id, 'job-1');
});

test('apiFetch(): a normal successful response is returned completely untouched', async () => {
  const apiFetch = makeApiFetch({ fetchImpl: okFetch({ hello: 'world' }) });
  const res = await apiFetch('/auth/me', { timeoutMs: 15_000 });
  assert.equal(res.ok, true);
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { hello: 'world' });
});

test('apiFetch(): diagnostic breadcrumb (window.SONYA_LAST_NETWORK_ERROR) carries no secrets', async () => {
  const windowRef = {};
  const apiFetch = makeApiFetch({
    fetchImpl: (url, { signal }) => neverSettlingFetch(signal),
    windowRef,
  });

  await apiFetch('/generation/jobs', {
    method: 'POST', timeoutMs: 20,
    headers: { 'Idempotency-Key': 'super-secret-should-not-leak', Authorization: 'Bearer should-not-leak' },
  });

  const rec = windowRef.SONYA_LAST_NETWORK_ERROR;
  assert.ok(rec, 'a diagnostic breadcrumb must be recorded');
  assert.equal(rec.path, '/generation/jobs');
  assert.equal(rec.kind, 'timeout');
  assert.equal(typeof rec.elapsedMs, 'number');
  const serialized = JSON.stringify(rec);
  assert.doesNotMatch(serialized, /super-secret-should-not-leak/);
  assert.doesNotMatch(serialized, /Bearer/);
  assert.deepEqual(Object.keys(rec).sort(), ['elapsedMs', 'kind', 'path', 'timeoutMs', 'timestamp']);
});

test('apiFetch(): a FormData body still gets its Content-Type stripped (unchanged behavior)', async () => {
  let seenHeaders = null;
  const apiFetch = makeApiFetch({
    fetchImpl: async (url, opts) => { seenHeaders = opts.headers; return { ok: true, status: 202, json: async () => ({}) }; },
  });

  const fd = new FormData();
  fd.append('mode', 'virality');
  await apiFetch('/generation/jobs', { method: 'POST', body: fd, timeoutMs: 30_000 });

  assert.equal(seenHeaders['Content-Type'], undefined, 'must not force JSON Content-Type onto a multipart body');
});
