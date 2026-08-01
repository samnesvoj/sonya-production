// Regression tests for P0: unsafe result rendering + duplicate job
// submission.
//
// Loads the REAL app.js (unmodified) into a minimal Node vm sandbox (see
// dom_harness.mjs) -- no jsdom, no npm install, only Node built-ins
// (node:vm, node:test, node:assert). Run with:
//   node --test tests/frontend/
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadApp, click, pressEnter } from './dom_harness.mjs';

// Async handlers here chain through a handful of promise resolutions
// (event dispatch -> submitGenerationJob -> await checkAndCreateVideoJob
// -> resetGenerationLock). Flush generously rather than guessing an exact
// microtask-tick count.
async function flush(ticks = 8) {
  for (let i = 0; i < ticks; i++) await Promise.resolve();
}

// ── XSS: unsafe result URL rendering ─────────────────────────────────────
//
// showRealResult() lives inside the polling IIFE; it's driven the same way
// production code reaches it -- through the poll loop's completed path --
// by monkey-patching fetch to return status=completed then a malicious
// result URL. It now renders into the redesigned result grid
// (#result-clip-grid / #result-actions) via renderSonyaResult() instead of
// the old ad-hoc #sonya-real-result box, but the security contract is
// identical: never innerHTML with backend-derived data, and any URL that
// fails isSafeResultUrl() must never reach a href/src anywhere.

function walk(el, out = []) {
  out.push(el);
  el.children.forEach((c) => walk(c, out));
  return out;
}

test('malicious javascript: result URL is not inserted as HTML or executable href', () => {
  const { sandbox, document } = loadApp();
  sandbox.window.fetch = async (url) => {
    if (url.endsWith('/result-url')) {
      return { ok: true, status: 200, json: async () => ({ url: 'javascript:alert(1)' }) };
    }
    return { ok: true, status: 200, json: async () => ({ status: 'completed', id: 'job-1' }) };
  };
  sandbox.fetch = sandbox.window.fetch;

  return sandbox.window.sonyaPollJob('job-1').then(() => {
    const grid = document.getElementById('result-clip-grid');
    const actions = document.getElementById('result-actions');

    // Never inserted via innerHTML.
    assert.equal(grid.innerHTML, '', 'innerHTML must never be used for the clip grid');
    assert.equal(actions.innerHTML, '', 'innerHTML must never be used for result actions');
    // An unsafe clip is dropped entirely -- no card, no dead action link.
    assert.equal(grid.children.length, 0, 'unsafe clip must not produce a card');
    assert.equal(actions.children.length, 0, 'unsafe clip must not produce an action link');

    // No element anywhere in the result area carries the malicious scheme.
    for (const el of [...walk(grid), ...walk(actions)]) {
      if (el.href) assert.ok(!String(el.href).startsWith('javascript:'), 'no href carries javascript:');
      if (el.src) assert.ok(!String(el.src).startsWith('javascript:'), 'no src carries javascript:');
    }
    // A safe rejection message is shown instead, as plain text.
    assert.match(document.getElementById('result-sub').textContent, /небезопасн/i);
  });
});

test('malicious data: result URL is rejected the same way', () => {
  const { sandbox, document } = loadApp();
  sandbox.window.fetch = async (url) => {
    if (url.endsWith('/result-url')) {
      return { ok: true, status: 200, json: async () => ({ url: 'data:text/html,<script>alert(1)</script>' }) };
    }
    return { ok: true, status: 200, json: async () => ({ status: 'completed', id: 'job-1' }) };
  };
  sandbox.fetch = sandbox.window.fetch;

  return sandbox.window.sonyaPollJob('job-1').then(() => {
    const grid = document.getElementById('result-clip-grid');
    const actions = document.getElementById('result-actions');
    assert.equal(grid.innerHTML, '');
    assert.equal(actions.innerHTML, '');
    assert.equal(grid.children.length, 0);
    assert.doesNotMatch(document.getElementById('result-sub').textContent, /<script>/i);
    assert.match(document.getElementById('result-sub').textContent, /небезопасн/i);
  });
});

test('a normal https result URL renders a working clip card and download link, never via innerHTML', () => {
  const { sandbox, document } = loadApp();
  const GOOD_URL = 'https://s3.example.com/bucket/output.mp4?sig=abc';
  sandbox.window.fetch = async (url) => {
    if (url.endsWith('/result-url')) {
      return { ok: true, status: 200, json: async () => ({ url: GOOD_URL }) };
    }
    return { ok: true, status: 200, json: async () => ({ status: 'completed', id: 'job-1' }) };
  };
  sandbox.fetch = sandbox.window.fetch;

  return sandbox.window.sonyaPollJob('job-1').then(() => {
    const grid = document.getElementById('result-clip-grid');
    const actions = document.getElementById('result-actions');

    assert.equal(grid.innerHTML, '', 'still built via DOM APIs, not innerHTML');
    assert.equal(actions.innerHTML, '', 'still built via DOM APIs, not innerHTML');
    assert.equal(grid.children.length, 1, 'one clip card should be rendered');

    const cardLink = walk(grid.children[0]).find((c) => c.tagName === 'A' && c.href === GOOD_URL);
    assert.ok(cardLink, 'clip card should carry a link to the validated URL');
    assert.equal(cardLink.target, '_blank');
    assert.equal(cardLink.rel, 'noopener');

    const actionLink = actions.children.find((c) => c.tagName === 'A');
    assert.ok(actionLink, 'a download action should exist for the single clip');
    assert.equal(actionLink.href, GOOD_URL);
    assert.equal(actionLink.target, '_blank');
    assert.equal(actionLink.rel, 'noopener');
  });
});

// ── Duplicate submission: single-flight guard ────────────────────────────

function deferred() {
  let resolve;
  const promise = new Promise((r) => (resolve = r));
  return { promise, resolve };
}

test('two rapid clicks on the generate button call checkAndCreateVideoJob exactly once', async () => {
  const calls = [];
  const gate = deferred();
  const { document } = loadApp({
    checkAndCreateVideoJob: async (formData) => {
      calls.push(formData);
      await gate.promise; // stay "in flight" so the second click races the first
      return 'ok';
    },
  });

  const btn = document.getElementById('btn-next-2');
  click(btn); // first call takes the lock synchronously, then awaits `gate`
  click(btn); // must be a no-op -- lock is already held

  assert.equal(calls.length, 1, 'checkAndCreateVideoJob must be called exactly once');
  assert.equal(btn.disabled, true, 'button must be disabled immediately');
  assert.equal(btn.textContent, 'Создаём задачу…');

  gate.resolve();
  await flush();
});

test('click followed immediately by Enter calls checkAndCreateVideoJob exactly once', async () => {
  const calls = [];
  const gate = deferred();
  const { document } = loadApp({
    checkAndCreateVideoJob: async () => {
      calls.push(1);
      await gate.promise;
      return 'ok';
    },
  });

  const btn = document.getElementById('btn-generate');
  click(btn);
  pressEnter(btn);

  assert.equal(calls.length, 1);
  gate.resolve();
});

test('a POST error releases the frontend lock so the user can retry', async () => {
  let attempt = 0;
  const { document } = loadApp({
    checkAndCreateVideoJob: async () => {
      attempt += 1;
      return attempt === 1 ? 'error' : 'ok';
    },
  });

  const btn = document.getElementById('btn-next-2');
  click(btn);
  await flush();

  assert.equal(btn.disabled, false, 'lock must be released after an error result');
  assert.equal(attempt, 1);

  // Retry must actually be possible -- second click goes through.
  click(btn);
  await flush();
  assert.equal(attempt, 2);
});

test('a successful submission starts exactly one polling loop', async () => {
  // Let the REAL simulateProcessing -> pollJob chain run (it lives inside
  // app.js's own IIFE and is not interceptable from outside), and observe
  // it the same way production does: via the status-endpoint fetch calls
  // it makes. A single click must produce exactly one loop's worth of
  // calls for the one job that was created.
  let statusFetchCount = 0;
  const { sandbox, document } = loadApp({
    checkAndCreateVideoJob: async () => {
      sandbox.window.SONYA_LAST_JOB = { id: 'job-xyz' };
      return 'ok';
    },
  });
  sandbox.fetch = async (url) => {
    if (url.endsWith('/result-url')) {
      return { ok: true, status: 200, json: async () => ({ url: 'https://s3.example.com/out.mp4' }) };
    }
    statusFetchCount += 1;
    return { ok: true, status: 200, json: async () => ({ status: 'completed', id: 'job-xyz' }) };
  };
  sandbox.window.fetch = sandbox.fetch;

  const btn = document.getElementById('btn-next-2');
  click(btn);
  // checkAndCreateVideoJob resolves -> simulateProcessing() -> pollJob()
  // -> one status fetch -> completed -> result-url fetch.
  await flush();

  assert.equal(statusFetchCount, 1, 'exactly one polling loop must start for one submission');
});

test('starting a poll while one is already active does not start a second concurrent loop', async () => {
  const { sandbox } = loadApp();

  let statusFetchCount = 0;
  sandbox.fetch = async (url) => {
    if (url.endsWith('/result-url')) {
      return { ok: true, status: 200, json: async () => ({ url: 'https://s3.example.com/out.mp4' }) };
    }
    statusFetchCount += 1;
    const st = statusFetchCount >= 2 ? 'completed' : 'mode_running';
    return { ok: true, status: 200, json: async () => ({ status: st, id: 'job-1' }) };
  };
  sandbox.window.fetch = sandbox.fetch;

  // Two trigger sources racing for the same job -- e.g. the submit flow and
  // the DOMContentLoaded resume-from-localStorage check.
  const first = sandbox.window.sonyaPollJob('job-1');
  const second = sandbox.window.sonyaPollJob('job-1');

  await second; // the guarded (no-op) call resolves immediately
  await first; // the one real loop runs to completion (2 iterations, via the fast test timer)

  assert.equal(statusFetchCount, 2, 'exactly one loop drove the status checks, not two');
});

test('resubmitting while a job is active does not create a new job', async () => {
  const calls = [];
  const { document } = loadApp({
    checkAndCreateVideoJob: async () => {
      calls.push(1);
      return 'ok'; // job created, stays "active" -- lock is not released
    },
  });

  const btn = document.getElementById('btn-next-2');
  const btnGen = document.getElementById('btn-generate');

  click(btn);
  await flush();
  assert.equal(calls.length, 1);
  assert.equal(btn.disabled, true, 'button stays disabled while the job is active');

  // Any trigger -- same button, the other button, Enter -- must still be a no-op.
  click(btn);
  click(btnGen);
  pressEnter(btn);
  await flush();

  assert.equal(calls.length, 1, 'no new job while the previous one is still active');
});
