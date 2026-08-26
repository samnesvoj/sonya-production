// Loads the REAL telegram-miniapp.js (unmodified) into a small vm sandbox
// and exercises its Telegram WebApp lifecycle handling: the mere presence
// of window.Telegram.WebApp (which the official SDK creates even outside
// Telegram) must NOT activate the Telegram runtime — only a non-empty
// webApp.initData or a real tgWebApp* launch param in the URL may. Only
// once that's confirmed: single ready()/expand(), data-runtime marking,
// theme/viewport sync (with localStorage manual override taking
// precedence). Must never throw regardless of scenario.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..', '..');
const SRC = fs.readFileSync(path.join(REPO_ROOT, 'telegram-miniapp.js'), 'utf8');

class FakeStyle {
  constructor() {
    this._props = {};
  }
  setProperty(name, value) {
    this._props[name] = value;
  }
  getPropertyValue(name) {
    return this._props[name] || '';
  }
}

class FakeDocumentElement {
  constructor() {
    this._attrs = {};
    this.style = new FakeStyle();
  }
  setAttribute(name, value) {
    this._attrs[name] = String(value);
  }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this._attrs, name) ? this._attrs[name] : null;
  }
}

class FakeVideo {
  constructor() {
    this.calls = { play: 0, pause: 0 };
  }
  play() {
    this.calls.play++;
    return Promise.resolve();
  }
  pause() {
    this.calls.pause++;
  }
}

// Non-empty by default -- most tests below simulate a CONFIRMED real
// Telegram launch (a Mini App actually opened from inside Telegram).
// Tests for the "SDK loaded but not really launched from Telegram"
// scenario explicitly override this to '' via webAppOverrides.
const DEFAULT_INIT_DATA = 'query_id=AAH&user=%7B%22id%22%3A1%7D&auth_date=1&hash=abc123';

// Builds a fresh sandbox, optionally with a mock Telegram.WebApp and/or
// launch-param URL, and runs telegram-miniapp.js in it. Returns handles
// for assertions. `noWebApp: true` simulates window.Telegram being
// entirely absent (SDK script blocked/failed) -- different from
// `webAppOverrides: { initData: '' }`, which simulates the SDK loading
// fine (as it always does, even outside Telegram) but reporting no real
// launch.
function run({ webAppOverrides, storedTheme, locationSearch = '', locationHash = '', noWebApp = false } = {}) {
  const documentElement = new FakeDocumentElement();
  const video = new FakeVideo();
  const listeners = {};
  const document = {
    documentElement,
    hidden: false,
    getElementById: (id) => (id === 'cinema-video' ? video : null),
    addEventListener: (type, fn) => {
      (listeners[type] = listeners[type] || []).push(fn);
    },
    _fire(type) {
      (listeners[type] || []).forEach((fn) => fn());
    },
  };

  const store = storedTheme ? { theme: storedTheme } : {};
  const localStorage = {
    getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
  };

  const calls = { ready: 0, expand: 0, onEvent: [] };

  let webApp;
  if (noWebApp) {
    webApp = undefined; // window.Telegram.WebApp itself never loaded
  } else {
    webApp = Object.assign(
      {
        initData: DEFAULT_INIT_DATA,
        colorScheme: 'dark',
        themeParams: { bg_color: '#111111', text_color: '#ffffff' },
        viewportHeight: 600,
        viewportStableHeight: 580,
        ready() { calls.ready++; },
        expand() { calls.expand++; },
        onEvent(name, handler) { calls.onEvent.push([name, handler]); },
      },
      webAppOverrides || {}
    );
  }

  const location = { search: locationSearch, hash: locationHash };
  const windowObj = { Telegram: webApp ? { WebApp: webApp } : undefined, localStorage, location };
  const sandbox = {
    window: windowObj,
    document,
    localStorage,
    console,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(SRC, { filename: 'telegram-miniapp.js' }).runInContext(sandbox);

  return { documentElement, calls, webApp, document, video };
}

// ── The core bug fix: SDK presence alone is not a real launch ─────────

test('window.Telegram.WebApp present but NOT really launched from Telegram (empty initData, no URL params) does not set data-runtime', () => {
  const { documentElement } = run({ webAppOverrides: { initData: '' } });
  assert.equal(documentElement.getAttribute('data-runtime'), null);
});

test('window.Telegram.WebApp present but NOT really launched from Telegram does not call ready()/expand()', () => {
  const { calls } = run({ webAppOverrides: { initData: '' } });
  assert.equal(calls.ready, 0, 'ready() must not fire in a plain Chrome/Safari visit');
  assert.equal(calls.expand, 0, 'expand() must not fire in a plain Chrome/Safari visit');
});

test('window.Telegram.WebApp present but NOT really launched from Telegram does not apply theme/viewport CSS vars', () => {
  const { documentElement } = run({ webAppOverrides: { initData: '' } });
  assert.equal(documentElement.getAttribute('data-theme'), null);
  assert.equal(documentElement.style.getPropertyValue('--tg-theme-bg-color'), '');
  assert.equal(documentElement.style.getPropertyValue('--tg-viewport-height'), '');
});

test('a non-empty webApp.initData IS treated as a confirmed real Telegram launch', () => {
  const { documentElement, calls } = run({ webAppOverrides: { initData: DEFAULT_INIT_DATA } });
  assert.equal(documentElement.getAttribute('data-runtime'), 'telegram');
  assert.equal(calls.ready, 1);
  assert.equal(calls.expand, 1);
});

test('a real tgWebAppVersion/tgWebAppPlatform launch param in the URL activates the runtime even if initData is empty', () => {
  const { documentElement, calls } = run({
    webAppOverrides: { initData: '' },
    locationSearch: '?tgWebAppVersion=7.10&tgWebAppPlatform=ios',
  });
  assert.equal(documentElement.getAttribute('data-runtime'), 'telegram');
  assert.equal(calls.ready, 1);
  assert.equal(calls.expand, 1);
});

test('a tgWebAppData launch param in the URL hash also activates the runtime', () => {
  const { documentElement } = run({
    webAppOverrides: { initData: '' },
    locationHash: '#tgWebAppData=abc123&tgWebAppVersion=7.10',
  });
  assert.equal(documentElement.getAttribute('data-runtime'), 'telegram');
});

test('an unrelated query string (e.g. a marketing UTM link) does not activate the runtime', () => {
  const { documentElement, calls } = run({
    webAppOverrides: { initData: '' },
    locationSearch: '?utm_source=telegram&utm_campaign=launch',
  });
  assert.equal(documentElement.getAttribute('data-runtime'), null);
  assert.equal(calls.ready, 0);
});

// ── No window.Telegram at all (SDK blocked, or plain browser without it) ──

test('sets no data-runtime when window.Telegram is entirely absent', () => {
  const { documentElement } = run({ noWebApp: true });
  assert.equal(documentElement.getAttribute('data-runtime'), null);
});

test('never throws when window.Telegram is undefined (opened outside Telegram)', () => {
  assert.doesNotThrow(() => run({ noWebApp: true }));
});

test('never throws when window.Telegram.WebApp is a malformed/partial object, even with a real launch confirmed', () => {
  assert.doesNotThrow(() =>
    run({ webAppOverrides: { initData: DEFAULT_INIT_DATA, themeParams: null, colorScheme: undefined, onEvent: undefined } })
  );
});

// ── Confirmed-launch behavior (unchanged from before this fix) ─────────

test('calls ready() and expand() exactly once on a confirmed real launch', () => {
  const { calls } = run({});
  assert.equal(calls.ready, 1);
  assert.equal(calls.expand, 1);
});

test('re-applying theme (e.g. via a themeChanged event) does not call ready()/expand() again', () => {
  const { calls } = run({});
  const themeChanged = calls.onEvent.find(([name]) => name === 'themeChanged');
  assert.ok(themeChanged, 'themeChanged listener was not registered');
  themeChanged[1](); // simulate Telegram firing the event
  assert.equal(calls.ready, 1, 'ready() must only ever be called once');
  assert.equal(calls.expand, 1, 'expand() must only ever be called once');
});

test('registers themeChanged and viewportChanged listeners', () => {
  const { calls } = run({});
  const names = calls.onEvent.map(([name]) => name);
  assert.ok(names.includes('themeChanged'));
  assert.ok(names.includes('viewportChanged'));
});

test('applies Telegram colorScheme as data-theme when no manual theme is stored', () => {
  const { documentElement } = run({});
  assert.equal(documentElement.getAttribute('data-theme'), 'dark');
});

test('a manual localStorage theme choice overrides Telegram colorScheme', () => {
  const { documentElement } = run({ storedTheme: 'light' });
  assert.notEqual(documentElement.getAttribute('data-theme'), 'dark');
});

test('themeChanged re-fired by Telegram still does not override a manual theme choice', () => {
  const { documentElement, calls } = run({ storedTheme: 'light' });
  const themeChanged = calls.onEvent.find(([name]) => name === 'themeChanged');
  themeChanged[1]();
  assert.notEqual(documentElement.getAttribute('data-theme'), 'dark');
});

test('applies themeParams as --tg-theme-* CSS custom properties', () => {
  const { documentElement } = run({});
  assert.equal(documentElement.style.getPropertyValue('--tg-theme-bg-color'), '#111111');
  assert.equal(documentElement.style.getPropertyValue('--tg-theme-text-color'), '#ffffff');
});

test('applies viewport height as CSS custom properties', () => {
  const { documentElement } = run({});
  assert.equal(documentElement.style.getPropertyValue('--tg-viewport-height'), '600px');
  assert.equal(documentElement.style.getPropertyValue('--tg-viewport-stable-height'), '580px');
});

test('reads webApp.initData only as a non-empty launch-detection signal — never parses it, never reads initDataUnsafe (must not be used as trusted auth)', () => {
  assert.ok(!SRC.includes('initDataUnsafe'), 'telegram-miniapp.js must never read initDataUnsafe');
  assert.ok(!SRC.includes('JSON.parse'), 'telegram-miniapp.js must never parse initData for user fields');
  assert.ok(!/initData\s*\.\s*(user|hash|auth_date|query_id)/.test(SRC), 'telegram-miniapp.js must never read fields out of initData');
});

// ── Background-video pause/resume: independent of Telegram detection ──

test('pauses the background video when the WebView is hidden, and resumes it when visible again — with or without a confirmed Telegram launch', () => {
  for (const opts of [{}, { webAppOverrides: { initData: '' } }, { noWebApp: true }]) {
    const { document, video } = run(opts);

    document.hidden = true;
    document._fire('visibilitychange');
    assert.equal(video.calls.pause, 1, 'video.pause() must be called when the tab/WebView is hidden');
    assert.equal(video.calls.play, 0);

    document.hidden = false;
    document._fire('visibilitychange');
    assert.equal(video.calls.play, 1, 'video.play() must be called when the tab/WebView becomes visible again');
  }
});

test('visibilitychange handling never throws when #cinema-video is not in the DOM yet', () => {
  const documentElement = new FakeDocumentElement();
  const listeners = {};
  const document = {
    documentElement,
    hidden: true,
    getElementById: () => null, // script runs in <head>, before <body> exists
    addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
  };
  const sandbox = {
    window: { Telegram: undefined, location: { search: '', hash: '' } },
    document,
    localStorage: { getItem: () => null },
    console,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(SRC, { filename: 'telegram-miniapp.js' }).runInContext(sandbox);
  assert.doesNotThrow(() => listeners.visibilitychange.forEach((fn) => fn()));
});
