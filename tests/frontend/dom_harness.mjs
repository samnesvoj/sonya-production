// Minimal, dependency-free DOM/browser shim used to load and execute the
// REAL app.js (unmodified) inside a Node vm sandbox, so regression tests
// exercise actual production code rather than a re-implementation of it.
//
// Deliberately NOT jsdom (or any npm package) -- per instructions, no heavy
// JS test framework. This implements only the small subset of DOM behavior
// app.js actually touches: getElementById/querySelectorAll registries,
// classList, dataset, style, textContent/innerHTML (innerHTML is recorded,
// never parsed, so a test can assert it was never used for backend data),
// addEventListener/dispatchEvent, and a couple of element properties
// (value, checked, disabled, src, href).

import vm from 'node:vm';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..', '..');

class EventTargetMixin {
  _listeners = {};
  addEventListener(type, fn) {
    (this._listeners[type] = this._listeners[type] || []).push(fn);
  }
  removeEventListener(type, fn) {
    if (!this._listeners[type]) return;
    this._listeners[type] = this._listeners[type].filter((f) => f !== fn);
  }
  dispatchEvent(evt) {
    evt.target = evt.target || this;
    (this._listeners[evt.type] || []).slice().forEach((fn) => fn(evt));
    return true;
  }
}

class FakeClassList {
  constructor() {
    this._set = new Set();
  }
  add(...names) {
    names.forEach((n) => this._set.add(n));
  }
  remove(...names) {
    names.forEach((n) => this._set.delete(n));
  }
  toggle(name, force) {
    const has = this._set.has(name);
    const want = force === undefined ? !has : !!force;
    if (want) this._set.add(name);
    else this._set.delete(name);
    return want;
  }
  contains(name) {
    return this._set.has(name);
  }
}

export class FakeElement extends EventTargetMixin {
  constructor(tagName, doc) {
    super();
    this.tagName = String(tagName || 'div').toUpperCase();
    this._doc = doc;
    this._id = '';
    this.children = [];
    this.parentNode = null;
    this.classList = new FakeClassList();
    this.dataset = {};
    this.style = {};
    this._attrs = {};
    this._text = '';
    this._html = ''; // recorded but never parsed -- see innerHTML setter
    this._value = '';
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.files = [];
  }
  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  removeChild(child) {
    this.children = this.children.filter((c) => c !== child);
    return child;
  }
  get firstChild() {
    return this.children[0] || null;
  }
  querySelector(sel) {
    return this.querySelectorAll(sel)[0] || null;
  }
  querySelectorAll(sel) {
    return this._doc._queryAllWithin(sel, this);
  }
  setAttribute(name, val) {
    this._attrs[name] = String(val);
  }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this._attrs, name) ? this._attrs[name] : null;
  }
  // Matches real DOM: reading textContent on an element with children
  // recursively concatenates descendant text, not just locally-set text.
  get textContent() {
    if (this.children.length === 0) return this._text;
    return this.children.map((c) => c.textContent).join('');
  }
  set textContent(v) {
    this._text = String(v);
    this.children = [];
  }
  // Recorded verbatim, never parsed as markup -- tests assert on this
  // string (and on .children) to prove backend data never flows through
  // innerHTML.
  get innerHTML() {
    return this._html;
  }
  set innerHTML(v) {
    this._html = String(v);
  }
  get id() {
    return this._id;
  }
  set id(v) {
    if (this._doc && this._id) this._doc._byId.delete(this._id);
    this._id = String(v || '');
    if (this._doc && this._id) this._doc._byId.set(this._id, this);
  }
  get value() {
    return this._value;
  }
  set value(v) {
    this._value = v;
  }
}

// Minimal text-node stand-in for document.createTextNode() -- app.js uses
// it (via renderResultTags) exactly like a real DOM text node: appended as
// a plain child, contributing to the parent's computed textContent, never
// itself carrying markup.
class FakeTextNode {
  constructor(text) {
    this.nodeType = 3;
    this._text = String(text);
    this.parentNode = null;
    this.children = [];
  }
  get textContent() {
    return this._text;
  }
  set textContent(v) {
    this._text = String(v);
  }
}

class FakeDocument extends EventTargetMixin {
  constructor() {
    super();
    this._byId = new Map();
    this._all = [];
    this.body = new FakeElement('body', this);
    this.documentElement = new FakeElement('html', this);
    this._all.push(this.body, this.documentElement);
  }
  register(id, el) {
    el.id = id;
    this._byId.set(id, el);
    this._all.push(el);
    return el;
  }
  createElement(tag) {
    const el = new FakeElement(tag, this);
    this._all.push(el);
    return el;
  }
  createTextNode(text) {
    return new FakeTextNode(text);
  }
  getElementById(id) {
    return this._byId.get(id) || null;
  }
  querySelector(sel) {
    return this.querySelectorAll(sel)[0] || null;
  }
  querySelectorAll(sel) {
    return this._queryAllWithin(sel, null);
  }
  // Tiny selector engine: supports #id, .class, tag[name="x"],
  // tag[name="x"]:checked, and bare [attr="x"] -- the exact subset app.js
  // uses. `scope` (a FakeElement) restricts matches to that element's
  // descendants; null means "whole document".
  _queryAllWithin(sel, scope) {
    sel = sel.trim();
    const pool = scope
      ? this._all.filter((el) => this._isDescendantOf(el, scope))
      : this._all;

    const checkedOnly = /:checked$/.test(sel);
    const base = sel.replace(/:checked$/, '');

    const idMatch = base.match(/^#([\w-]+)$/);
    if (idMatch) {
      const el = this._byId.get(idMatch[1]);
      return el ? [el] : [];
    }
    const classMatch = base.match(/^\.([\w-]+)$/);
    if (classMatch) {
      return pool.filter((el) => el.classList.contains(classMatch[1]));
    }
    const attrMatch = base.match(/^([a-zA-Z]*)\[([\w-]+)="([^"]*)"\]$/);
    if (attrMatch) {
      const [, tag, attr, val] = attrMatch;
      return pool.filter((el) => {
        if (tag && el.tagName !== tag.toUpperCase()) return false;
        const attrVal = attr === 'name' ? el._attrs.name : el.getAttribute(attr);
        if (attrVal !== val) return false;
        if (checkedOnly && !el.checked) return false;
        return true;
      });
    }
    // Fallback: unsupported selector -> empty result (matches jsdom-less
    // reality for the handful of selectors app.js uses that we don't need
    // for these tests, e.g. editor-only elements).
    return [];
  }
  _isDescendantOf(el, ancestor) {
    let cur = el.parentNode;
    while (cur) {
      if (cur === ancestor) return true;
      cur = cur.parentNode;
    }
    return el === ancestor;
  }
}

// IDs app.js's top-level `elements = {...}` block resolves via
// getElementById, with the tag needed for correct property semantics.
const REQUIRED_IDS = {
  'main-tagline': 'div',
  'menu-toggle': 'button',
  'menu-overlay': 'div',
  'menu-close': 'button',
  'theme-toggle': 'button',
  'page-source': 'div',
  'page-type': 'div',
  'page-customize': 'div',
  'page-processing': 'div',
  'page-result': 'div',
  'mode-url': 'div',
  'mode-upload': 'div',
  'video-url': 'input',
  'upload-zone': 'div',
  'file-input': 'input',
  'file-info': 'div',
  'file-name': 'span',
  'remove-file': 'button',
  'btn-next-1': 'button',
  'btn-next-2': 'button',
  'btn-back-2': 'button',
  'btn-back-3': 'button',
  'btn-generate': 'button',
  'btn-new-project': 'button',
  'btn-trailer': 'button',
  'subtitles-toggle': 'input',
  'subtitle-styles': 'div',
  'voiceover-toggle': 'input',
  'voice-options': 'div',
  'voice-select': 'select',
  'result-headline': 'h1',
  'result-sub': 'p',
  'result-tags': 'div',
  'result-clip-grid': 'div',
  'result-actions': 'div',
};

/**
 * Builds a fresh sandboxed environment, loads the real app.js into it, and
 * fires DOMContentLoaded (init()). Returns handles for driving the tests.
 */
export function loadApp({ checkAndCreateVideoJob } = {}) {
  const document = new FakeDocument();
  for (const [id, tag] of Object.entries(REQUIRED_IDS)) {
    document.register(id, document.createElement(tag));
  }

  const fetchCalls = [];
  const fakeFetch = async (url, opts) => {
    fetchCalls.push({ url, opts });
    return { ok: true, status: 200, json: async () => ({}) };
  };

  // The real poll loop waits 5000ms between iterations. For tests, fire on
  // the next tick instead -- keeps the suite fast and avoids leaving a
  // long-lived timer alive after a test completes (which would hang
  // `node --test`). Real setTimeout/clearTimeout are used unmodified.
  const fastSetTimeout = (fn, _ms, ...args) => setTimeout(fn, 0, ...args);

  const store = {};
  const localStorage = {
    getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
    setItem: (k, v) => {
      store[k] = String(v);
    },
    removeItem: (k) => {
      delete store[k];
    },
  };

  const location = {
    search: '',
    hash: '',
    pathname: '/', // deliberately NOT "processing" -- avoids an unwanted
                    // auto-resume pollJob() call firing at load time.
    href: 'https://sonya-e.com/',
    origin: 'https://sonya-e.com',
  };

  const windowObj = {
    document,
    location,
    localStorage,
    fetch: fakeFetch,
    URL,
    URLSearchParams,
    console,
    setTimeout: fastSetTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    SONYA_API_BASE: '/api',
    Telegram: undefined,
  };

  const sandbox = {
    window: windowObj,
    document,
    localStorage,
    fetch: fakeFetch,
    URL,
    URLSearchParams,
    console,
    setTimeout: fastSetTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    // auth.js is NOT loaded (its own DOM structure is out of scope for
    // these tests) -- checkAndCreateVideoJob is provided directly, exactly
    // as app.js expects to find it as a global ("defined in auth.js,
    // loaded after app.js").
    checkAndCreateVideoJob: checkAndCreateVideoJob || (async () => 'ok'),
    tg: null,
  };
  sandbox.globalThis = sandbox;
  sandbox.window.checkAndCreateVideoJob = sandbox.checkAndCreateVideoJob;

  vm.createContext(sandbox);

  const src = fs.readFileSync(path.join(REPO_ROOT, 'app.js'), 'utf8');
  new vm.Script(src, { filename: 'app.js' }).runInContext(sandbox);

  // app.js registers `document.addEventListener('DOMContentLoaded', init)`
  // (and a second listener in the polling IIFE) -- fire it now.
  document.dispatchEvent({ type: 'DOMContentLoaded' });

  return { sandbox, document, localStorage, fetchCalls };
}

/**
 * Loads the REAL auth.js (unmodified) into a fresh sandbox. auth.js's own
 * initAuth() guards every DOM lookup it makes (getElementById results are
 * either optional-chained or null-checked before use -- verified by
 * inspection), so unlike app.js this needs no pre-registered elements at
 * all to load and run cleanly; a bare FakeDocument is enough.
 *
 * `fetchImpl` lets each test control exactly what apiCreateVideoJob's
 * POST /generation/jobs call observes (network error vs a real response).
 */
export function loadAuth({ fetchImpl } = {}) {
  const document = new FakeDocument();

  const fetchCalls = [];
  const defaultFetch = async (url, opts) => {
    fetchCalls.push({ url, opts });
    return { ok: true, status: 200, json: async () => ({}) };
  };
  const fetchFn = fetchImpl
    ? (url, opts) => {
        fetchCalls.push({ url, opts });
        return fetchImpl(url, opts);
      }
    : defaultFetch;

  const location = {
    search: '', hash: '', pathname: '/',
    href: 'https://sonya-e.com/', origin: 'https://sonya-e.com',
  };
  // showToast's auto-dismiss uses a real 4200ms timer; fire on the next
  // tick instead so tests stay fast (same rationale as loadApp's
  // fastSetTimeout for the poll loop).
  const fastSetTimeout = (fn, _ms, ...args) => setTimeout(fn, 0, ...args);

  const windowObj = {
    document, location, fetch: fetchFn, console,
    setTimeout: fastSetTimeout, clearTimeout,
    SONYA_API_BASE: '/api',
  };
  const sandbox = {
    window: windowObj,
    document,
    fetch: fetchFn,
    console,
    setTimeout: fastSetTimeout, clearTimeout,
    crypto, // Node's global WebCrypto (randomUUID) -- not auto-visible inside a vm context
    FormData, // Node's global FormData (undici) -- same reason
    appState: { uploadedFile: null },
  };
  sandbox.globalThis = sandbox;

  vm.createContext(sandbox);

  const src = fs.readFileSync(path.join(REPO_ROOT, 'auth.js'), 'utf8');
  new vm.Script(src, { filename: 'auth.js' }).runInContext(sandbox);

  // auth.js's own bottom-of-file guard checks document.readyState; a fresh
  // FakeDocument has none set (undefined !== 'loading'), so it already
  // called initAuth() synchronously during the script run above.

  return { sandbox, document, fetchCalls };
}

export function click(el) {
  el.dispatchEvent({ type: 'click' });
}

export function pressEnter(el) {
  el.dispatchEvent({ type: 'keydown', key: 'Enter' });
}

export { FakeDocument };
