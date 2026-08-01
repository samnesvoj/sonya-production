// Loads the REAL telegram-miniapp.js (unmodified) into a small vm sandbox
// and exercises its Telegram WebApp lifecycle handling: safe feature
// detection, single ready()/expand(), data-runtime marking, theme/viewport
// sync (with localStorage manual override taking precedence), and that it
// never throws when window.Telegram is absent (opened outside Telegram).

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

// Builds a fresh sandbox, optionally with a mock Telegram.WebApp, and runs
// telegram-miniapp.js in it. Returns handles for assertions.
function run({ webAppOverrides, storedTheme } = {}) {
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
  if (webAppOverrides === null) {
    webApp = undefined; // simulates opening /miniapp/ outside Telegram
  } else {
    webApp = Object.assign(
      {
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

  const windowObj = { Telegram: webApp ? { WebApp: webApp } : undefined, localStorage };
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

test('sets data-runtime="telegram" even when Telegram is absent', () => {
  const { documentElement } = run({ webAppOverrides: null });
  assert.equal(documentElement.getAttribute('data-runtime'), 'telegram');
});

test('never throws when window.Telegram is undefined (opened outside Telegram)', () => {
  assert.doesNotThrow(() => run({ webAppOverrides: null }));
});

test('never throws when window.Telegram.WebApp is a malformed/partial object', () => {
  assert.doesNotThrow(() => run({ webAppOverrides: { themeParams: null, colorScheme: undefined, onEvent: undefined } }));
});

test('calls ready() and expand() exactly once', () => {
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

test('never reads initData/initDataUnsafe (must not be used as trusted auth)', () => {
  assert.ok(!SRC.includes('initDataUnsafe'), 'telegram-miniapp.js must never read initDataUnsafe');
  assert.ok(!/\.initData\b/.test(SRC), 'telegram-miniapp.js must never read initData');
});

test('pauses the background video when the WebView is hidden, and resumes it when visible again — with or without Telegram present', () => {
  for (const webAppOverrides of [undefined, null]) {
    const { document, video } = run({ webAppOverrides });

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
  const sandbox = { window: { Telegram: undefined }, document, localStorage: { getItem: () => null }, console };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(SRC, { filename: 'telegram-miniapp.js' }).runInContext(sandbox);
  assert.doesNotThrow(() => listeners.visibilitychange.forEach((fn) => fn()));
});
