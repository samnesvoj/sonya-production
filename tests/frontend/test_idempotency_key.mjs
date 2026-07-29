// Regression tests for the frontend Idempotency-Key lifecycle (auth.js):
// generated once per logical submission via crypto.randomUUID(), reused
// across a network-error retry of the SAME attempt, and rotated (cleared,
// so the next call mints a fresh one) after any real HTTP response or an
// explicit form reset.
//
// Loads the REAL auth.js (unmodified) into a minimal Node vm sandbox --
// same approach as dom_harness.mjs's loadApp(), see that file's comments.
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadAuth } from './dom_harness.mjs';

function uploadedFile(name = 'clip.mp4') {
  const blob = new Blob(['bytes']);
  blob.name = name;
  return blob;
}

function idempotencyHeader(fetchCalls, index = 0) {
  const calls = fetchCalls.filter((c) => c.url.includes('/generation/jobs'));
  return calls[index]?.opts?.headers?.['Idempotency-Key'];
}

const AUTH_OK = { ok: true, status: 200, json: async () => ({ id: 'u1', email: 'user@example.com', plan_type: 'free' }) };

// checkAndCreateVideoJob() always calls GET /auth/me first -- these tests
// care about the SEPARATE POST /generation/jobs call, so /auth/me must
// stay a stable, successful no-op throughout.
function withAuthOk(jobFetchImpl) {
  return async (url, opts) => {
    if (url.includes('/auth/me')) return AUTH_OK;
    return jobFetchImpl(url, opts);
  };
}

test('apiCreateVideoJob sends a fresh crypto.randomUUID() Idempotency-Key', async () => {
  const { sandbox, fetchCalls } = loadAuth({
    fetchImpl: async () => ({ ok: true, status: 202, json: async () => ({ job_id: 'j1', status: 'queued', mode: 'virality' }) }),
  });
  sandbox.appState.uploadedFile = uploadedFile();

  await sandbox.apiCreateVideoJob({ source: {}, clipType: 'viral' });

  const key = idempotencyHeader(fetchCalls);
  assert.match(key, /^[0-9a-f-]{36}$/i, 'should be a UUID-shaped key');
});

test('the same key is generated only once per logical submission (not per event handler)', async () => {
  const { sandbox } = loadAuth();
  const key1 = sandbox._getOrCreateJobIdempotencyKey();
  const key2 = sandbox._getOrCreateJobIdempotencyKey();
  assert.equal(key1, key2, 'calling the getter twice without a POST in between must not mint a new key');
});

test('network error keeps the same key for a retry of the same attempt', async () => {
  let jobCall = 0;
  const { sandbox, fetchCalls } = loadAuth({
    fetchImpl: withAuthOk(async () => {
      jobCall += 1;
      if (jobCall === 1) throw new TypeError('network down'); // apiFetch turns this into _networkError
      return { ok: true, status: 202, json: async () => ({ job_id: 'j1', status: 'queued', mode: 'virality' }) };
    }),
  });
  sandbox.appState.uploadedFile = uploadedFile();

  const first = await sandbox.checkAndCreateVideoJob({ source: {}, clipType: 'viral' });
  assert.equal(first, 'error');

  const second = await sandbox.checkAndCreateVideoJob({ source: {}, clipType: 'viral' });
  assert.equal(second, 'ok');

  const key1 = idempotencyHeader(fetchCalls, 0);
  const key2 = idempotencyHeader(fetchCalls, 1);
  assert.ok(key1, 'first attempt must have sent a key');
  assert.equal(key2, key1, 'retry after a network error must reuse the same key');
});

test('a new logical submission after a successful job creation uses a new key', async () => {
  const { sandbox, fetchCalls } = loadAuth({
    fetchImpl: withAuthOk(async () => ({ ok: true, status: 202, json: async () => ({ job_id: 'j1', status: 'queued', mode: 'virality' }) })),
  });
  sandbox.appState.uploadedFile = uploadedFile();

  const first = await sandbox.checkAndCreateVideoJob({ source: {}, clipType: 'viral' });
  assert.equal(first, 'ok');

  sandbox.appState.uploadedFile = uploadedFile('second.mp4');
  const second = await sandbox.checkAndCreateVideoJob({ source: {}, clipType: 'viral' });
  assert.equal(second, 'ok');

  const key1 = idempotencyHeader(fetchCalls, 0);
  const key2 = idempotencyHeader(fetchCalls, 1);
  assert.notEqual(key1, key2, 'a second, independent submission must get its own key');
});

test('a 409 conflict response still rotates the key (round trip settled)', async () => {
  const { sandbox, fetchCalls } = loadAuth({
    fetchImpl: withAuthOk(async () => ({ ok: false, status: 409, json: async () => ({ detail: { error: 'idempotency_key_conflict' } }) })),
  });
  sandbox.appState.uploadedFile = uploadedFile();

  const first = await sandbox.checkAndCreateVideoJob({ source: {}, clipType: 'viral' });
  assert.equal(first, 'error');

  const second = await sandbox.checkAndCreateVideoJob({ source: {}, clipType: 'viral' });
  assert.equal(second, 'error');

  const key1 = idempotencyHeader(fetchCalls, 0);
  const key2 = idempotencyHeader(fetchCalls, 1);
  assert.ok(key1);
  assert.notEqual(key2, key1, 'a definitive (non-network-error) response must clear the key');
});

test('explicit clearJobIdempotencyKey() forces the next call to mint a new key', async () => {
  const { sandbox, fetchCalls } = loadAuth({
    fetchImpl: withAuthOk(async () => {
      throw new TypeError('network down'); // stays "in flight" -- key would normally be kept
    }),
  });
  sandbox.appState.uploadedFile = uploadedFile();

  await sandbox.checkAndCreateVideoJob({ source: {}, clipType: 'viral' });
  const keyBeforeReset = idempotencyHeader(fetchCalls, 0);
  assert.ok(keyBeforeReset);

  // Simulates app.js's btnNewProject handler calling this on explicit reset.
  sandbox.clearJobIdempotencyKey();

  await sandbox.checkAndCreateVideoJob({ source: {}, clipType: 'viral' });
  const keyAfterReset = idempotencyHeader(fetchCalls, 1);

  assert.notEqual(keyAfterReset, keyBeforeReset, 'explicit reset must win over "keep key after network error"');
});
