// Regression tests for the guest-vs-real-error auth bug: GET /api/auth/me
// returning 401 is the EXPECTED state for a not-yet-logged-in visitor and
// must never be treated as an application error (no red banner, no toast,
// no console.error, no generic error handling) -- while a genuine failure
// of that same endpoint (5xx / network) must never be silently repainted
// as "please log in", since that hides a real outage behind a normal-
// looking prompt.
//
// Loads the REAL auth.js (unmodified) into a Node vm sandbox via
// loadAuth() (see dom_harness.mjs). Run with:
//   node --test tests/frontend/
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadAuth, click } from './dom_harness.mjs';

function uploadedFile(name = 'clip.mp4') {
  const blob = new Blob(['bytes']);
  blob.name = name;
  return blob;
}

// Spy console -- lets a test assert "the general error handler was/was not
// invoked" without touching the real process-wide console object.
function spyConsole() {
  const calls = { error: [], log: [], warn: [] };
  return {
    calls,
    console: {
      error: (...a) => calls.error.push(a),
      log: (...a) => calls.log.push(a),
      warn: (...a) => calls.warn.push(a),
      info: (...a) => calls.warn.push(a),
    },
  };
}

const ME_401 = { ok: false, status: 401, json: async () => ({ detail: 'Not authenticated' }) };
const ME_500 = { ok: false, status: 500, json: async () => ({ detail: 'Internal server error' }) };

// initAuth() fires an un-awaited refreshAuthState() at load time; give its
// promise chain a turn to fully settle before a test resets the error spy,
// otherwise its console.error can land after the reset, interleaved with
// the test's own explicit call.
function flushMicrotasks() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function fetchRouter(routes) {
  return async (url, opts) => {
    for (const [match, res] of routes) {
      if (url.includes(match)) return typeof res === 'function' ? res(url, opts) : res;
    }
    throw new Error(`unexpected fetch in test: ${url}`);
  };
}

// ── GET /api/auth/me -> 401 (expected guest state) ─────────────────────

test('refreshAuthState(): /auth/me 401 becomes unauthenticated, no error surfaced', async () => {
  const { console: consoleImpl, calls } = spyConsole();
  const { sandbox, document } = loadAuth({
    fetchImpl: fetchRouter([['/auth/me', ME_401]]),
    consoleImpl,
    withAuthModalDom: true,
  });

  const user = await sandbox.refreshAuthState();

  assert.equal(user, null, 'guest state: no user');
  const errEl = document.getElementById('sonya-auth-error');
  assert.equal(errEl.hidden, true, 'no red error should be shown for expected guest state');
  assert.equal(errEl.textContent, '', 'error element must stay empty');
  assert.deepEqual(calls.error, [], 'general error handler (console.error) must not fire for expected 401');
});

test('checkAndCreateVideoJob(): /auth/me 401 opens the normal login form, no red error, login is still reachable', async () => {
  const { console: consoleImpl, calls } = spyConsole();
  const { sandbox, document } = loadAuth({
    fetchImpl: fetchRouter([['/auth/me', ME_401]]),
    consoleImpl,
    withAuthModalDom: true,
  });
  sandbox.appState.uploadedFile = uploadedFile();

  const result = await sandbox.checkAndCreateVideoJob({ source: {}, clipType: 'viral' });

  assert.equal(result, 'auth');
  const overlay = document.getElementById('sonya-auth-overlay');
  assert.equal(overlay.classList.contains('is-open'), true, 'auth modal (with the normal login form) must be reachable');
  const errEl = document.getElementById('sonya-auth-error');
  assert.equal(errEl.hidden, true, 'red error must be absent for an expected 401 guest state');
  assert.equal(errEl.textContent, '');
  assert.deepEqual(calls.error, [], 'general error handler must not fire for expected 401');
});

// ── GET /api/auth/me -> 500 (real error) ────────────────────────────────

test('refreshAuthState(): /auth/me 500 is a real error and is not silently treated as guest state', async () => {
  const { console: consoleImpl, calls } = spyConsole();
  const { sandbox } = loadAuth({
    fetchImpl: fetchRouter([['/auth/me', ME_500]]),
    consoleImpl,
    withAuthModalDom: true,
  });
  // initAuth() already ran one automatic refreshAuthState() at load time
  // (same /auth/me 500 route) -- only count what THIS explicit call does.
  await flushMicrotasks();
  calls.error.length = 0;

  const user = await sandbox.refreshAuthState();

  assert.equal(user, null, 'safe fallback: cannot assume logged in');
  assert.equal(calls.error.length, 1, 'a real /auth/me failure must be surfaced to the general error handler, unlike the expected 401 case');
});

test('checkAndCreateVideoJob(): /auth/me 500 shows a real error, does NOT masquerade as "please log in"', async () => {
  const { console: consoleImpl, calls } = spyConsole();
  const { sandbox, document } = loadAuth({
    fetchImpl: fetchRouter([['/auth/me', ME_500]]),
    consoleImpl,
    withAuthModalDom: true,
  });
  sandbox.appState.uploadedFile = uploadedFile();
  // initAuth() already ran one automatic refreshAuthState() at load time
  // (same /auth/me 500 route) -- only count what THIS explicit call does.
  await flushMicrotasks();
  calls.error.length = 0;

  const result = await sandbox.checkAndCreateVideoJob({ source: {}, clipType: 'viral' });

  assert.equal(result, 'error', 'a real backend failure must not be reported as the normal "auth" outcome');
  const overlay = document.getElementById('sonya-auth-overlay');
  assert.equal(overlay.classList.contains('is-open'), false, 'the login modal must not open in response to a real server error');
  assert.equal(calls.error.length, 1, 'a real /auth/me failure must reach the general error handler');

  const toast = document.getElementById('sonya-toast');
  assert.ok(toast, 'a real error must be surfaced to the user (toast)');
  assert.equal(toast.className.includes('sonya-toast--error'), true);
});

// ── stale error clearing (req. 4) ───────────────────────────────────────

test('openAuthModal() clears a stale error left over from a previous attempt', async () => {
  const { sandbox, document } = loadAuth({ withAuthModalDom: true });

  sandbox.setAuthError('Неверный код. Попробуйте снова.');
  const errEl = document.getElementById('sonya-auth-error');
  assert.equal(errEl.hidden, false, 'sanity check: error is visible before reopening');

  sandbox.openAuthModal('login');

  assert.equal(errEl.hidden, true, 'opening the modal must clear a stale error');
  assert.equal(errEl.textContent, '');
});

test('switching the login/register tab clears a stale error from the previous attempt', async () => {
  const { sandbox, document } = loadAuth({ withAuthModalDom: true });

  sandbox.openAuthModal('login');
  sandbox.setAuthError('Слишком много попыток. Подождите и повторите.');
  const errEl = document.getElementById('sonya-auth-error');
  assert.equal(errEl.hidden, false, 'sanity check: error is visible before switching tabs');

  click(document.getElementById('sonya-tab-register'));

  assert.equal(errEl.hidden, true, 'switching tabs must clear the previous tab error, not carry it over');
  assert.equal(errEl.textContent, '');
  const registerView = document.getElementById('sonya-view-register');
  assert.equal(registerView.classList.contains('is-active'), true, 'tab click must actually switch the view');
});

// ── POST /api/auth/request-code -> 429 must NOT be masked by guest logic ──

test('handleRequestCode(): 429 shows a real, user-visible error, unrelated to /auth/me 401 handling', async () => {
  const { sandbox, document } = loadAuth({
    fetchImpl: fetchRouter([
      ['/auth/request-code', { ok: false, status: 429, json: async () => ({ detail: 'rate_limited' }) }],
    ]),
    withAuthModalDom: true,
  });

  document.getElementById('sonya-login-email').value = 'user@example.com';
  await sandbox.handleRequestCode('login');

  const errEl = document.getElementById('sonya-auth-error');
  assert.equal(errEl.hidden, false, '429 must be shown to the user, not suppressed');
  assert.equal(errEl.textContent, 'Слишком много попыток. Подождите и повторите.');
});

test('handleRequestCode(): 403 (request forbidden) is shown, not swallowed', async () => {
  const { sandbox, document } = loadAuth({
    fetchImpl: fetchRouter([
      ['/auth/request-code', { ok: false, status: 403, json: async () => ({ detail: 'Origin not allowed' }) }],
    ]),
    withAuthModalDom: true,
  });

  document.getElementById('sonya-login-email').value = 'user@example.com';
  await sandbox.handleRequestCode('login');

  const errEl = document.getElementById('sonya-auth-error');
  assert.equal(errEl.hidden, false);
  assert.equal(errEl.textContent, 'Origin not allowed');
});

test('handleRequestCode(): 5xx is shown, not swallowed', async () => {
  const { sandbox, document } = loadAuth({
    fetchImpl: fetchRouter([
      ['/auth/request-code', { ok: false, status: 500, json: async () => ({}) }],
    ]),
    withAuthModalDom: true,
  });

  document.getElementById('sonya-login-email').value = 'user@example.com';
  await sandbox.handleRequestCode('login');

  const errEl = document.getElementById('sonya-auth-error');
  assert.equal(errEl.hidden, false);
  assert.equal(errEl.textContent, 'Ошибка отправки. Попробуйте позже.');
});

test('handleRequestCode(): network error is shown, not swallowed', async () => {
  const { sandbox, document } = loadAuth({
    fetchImpl: fetchRouter([
      ['/auth/request-code', () => { throw new TypeError('network down'); }],
    ]),
    withAuthModalDom: true,
  });

  document.getElementById('sonya-login-email').value = 'user@example.com';
  await sandbox.handleRequestCode('login');

  const errEl = document.getElementById('sonya-auth-error');
  assert.equal(errEl.hidden, false);
  assert.equal(errEl.textContent, 'Сервер недоступен. Проверьте соединение и повторите попытку.');
});
