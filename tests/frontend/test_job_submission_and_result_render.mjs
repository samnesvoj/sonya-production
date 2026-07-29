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

test('malicious javascript: result URL is not inserted as HTML or executable href', () => {
  const { sandbox, document } = loadApp();
  const showRealResult = sandbox.window.__test_showRealResult || sandbox.showRealResult;
  // showRealResult lives inside the polling IIFE; exposed for tests via
  // window.sonyaPollJob's sibling hook is not present, so we drive it the
  // same way production code does: through the poll loop's completed path.
  // To keep this test focused and fast, call the exported entry point that
  // production uses to reach it: simulate a completed job by monkey-
  // patching fetch to return status=completed then a malicious result URL.
  const calls = [];
  sandbox.window.fetch = async (url) => {
    calls.push(url);
    if (url.endsWith('/result-url')) {
      return { ok: true, status: 200, json: async () => ({ url: 'javascript:alert(1)' }) };
    }
    return { ok: true, status: 200, json: async () => ({ status: 'completed', id: 'job-1' }) };
  };
  sandbox.fetch = sandbox.window.fetch;

  return sandbox.window.sonyaPollJob('job-1').then(() => {
    const box = document.getElementById('sonya-real-result');
    assert.ok(box, 'result box should be created');

    // Never inserted via innerHTML.
    assert.equal(box.innerHTML, '', 'innerHTML must never be used for result rendering');

    // No <a> or <video> child carries the malicious scheme anywhere.
    const walk = (el, out) => {
      out.push(el);
      el.children.forEach((c) => walk(c, out));
      return out;
    };
    const all = walk(box, []);
    for (const el of all) {
      if (el.href) assert.ok(!String(el.href).startsWith('javascript:'), 'no href carries javascript:');
      if (el.src) assert.ok(!String(el.src).startsWith('javascript:'), 'no src carries javascript:');
    }
    // A safe rejection message is shown instead.
    assert.match(box.textContent, /небезопасн/i);
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
    const box = document.getElementById('sonya-real-result');
    assert.equal(box.innerHTML, '');
    assert.doesNotMatch(box.textContent, /<script>/i);
    assert.match(box.textContent, /небезопасн/i);
  });
});

test('a normal https result URL renders a working video + download link', () => {
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
    const box = document.getElementById('sonya-real-result');
    assert.equal(box.innerHTML, '', 'still built via DOM APIs, not innerHTML');

    const video = box.children.find((c) => c.tagName === 'VIDEO');
    const link = box.children
      .flatMap((c) => (c.tagName === 'A' ? [c] : c.children))
      .find((c) => c.tagName === 'A');

    assert.ok(video, 'video element should exist');
    assert.equal(video.src, GOOD_URL);
    assert.ok(link, 'download link should exist');
    assert.equal(link.href, GOOD_URL);
    assert.equal(link.target, '_blank');
    assert.equal(link.rel, 'noopener');
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
