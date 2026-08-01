// Static, dependency-free checks that the Telegram Mini App architecture
// (see docs/SONYA_AUDIT.md — "Legacy Telegram Mini App code coexists with
// the new web flow") actually keeps the two entry points separate:
//   - / (index.html)          — never loads or references the Telegram SDK
//   - /miniapp/ (miniapp/index.html) — loads the SDK before everything else
//   - app.js/auth.js/config.js/styles.css/sphere.js are the SAME files for
//     both entry points, referenced only via absolute paths from /miniapp/
//
// Deliberately text/regex based (no jsdom, no build step) — these are
// properties of the served files themselves, not runtime behavior.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..', '..');

const read = (relPath) => fs.readFileSync(path.join(REPO_ROOT, relPath), 'utf8');

// Explanatory comments in these files legitimately mention the very
// filenames/APIs the tests below check for the ABSENCE of (e.g. a comment
// in app.js explaining *why* it must not reference window.Telegram, or a
// comment next to a removed <script> tag explaining why sphere.js isn't
// loaded). Strip comments before every "must not contain" check so prose
// can't produce a false failure — actual code order/content is untouched.
const stripHtmlComments = (html) => html.replace(/<!--[\s\S]*?-->/g, '');
// Only strips whole-line `//` comments (line starts with `//`, ignoring
// leading whitespace) — NOT `//` appearing mid-line, which would wrongly
// truncate real code containing string literals like "https://...".
const stripJsComments = (js) => js.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^[ \t]*\/\/.*$/gm, '');

const indexHtmlRaw = read('index.html');
const miniappHtmlRaw = read('miniapp/index.html');
const appJsRaw = read('app.js');

const indexHtml = stripHtmlComments(indexHtmlRaw);
const miniappHtml = stripHtmlComments(miniappHtmlRaw);
const appJs = stripJsComments(appJsRaw);

test('plain site (index.html) never loads the Telegram SDK or the Telegram runtime file', () => {
  assert.ok(!indexHtml.includes('telegram-web-app.js'), 'index.html must not load telegram-web-app.js');
  assert.ok(!indexHtml.includes('telegram-miniapp.js'), 'index.html must not load telegram-miniapp.js');
});

test('plain site (index.html) never references window.Telegram', () => {
  assert.ok(!indexHtml.includes('window.Telegram'), 'index.html markup must not reference window.Telegram');
  assert.ok(!indexHtml.includes('Telegram.WebApp'), 'index.html markup must not reference Telegram.WebApp');
});

test('shared runtime (app.js) never references window.Telegram or the Telegram SDK', () => {
  // app.js is loaded, unmodified, by BOTH entry points — if it referenced
  // window.Telegram at all, the plain site would too, by construction.
  assert.ok(!appJs.includes('window.Telegram'), 'app.js must not reference window.Telegram');
  assert.ok(!appJs.includes('Telegram.WebApp'), 'app.js must not reference Telegram.WebApp');
  assert.ok(!/\btg\s*\./.test(appJs), 'app.js must not reference a Telegram WebApp SDK handle');
});

test('/miniapp/ loads the Telegram SDK, then telegram-miniapp.js, before config.js/app.js/auth.js', () => {
  const idx = (needle) => {
    const i = miniappHtml.indexOf(needle);
    assert.ok(i !== -1, `${needle} not found in miniapp/index.html`);
    return i;
  };

  const sdk = idx('telegram-web-app.js');
  const runtime = idx('telegram-miniapp.js');
  const config = idx('/config.js');
  const app = idx('/app.js');
  const auth = idx('/auth.js');

  assert.ok(sdk < runtime, 'telegram-web-app.js must load before telegram-miniapp.js');
  assert.ok(runtime < config, 'telegram-miniapp.js must load before config.js');
  assert.ok(runtime < app, 'telegram-miniapp.js must load before app.js');
  assert.ok(runtime < auth, 'telegram-miniapp.js must load before auth.js');
});

test('/miniapp/ references shared assets via absolute paths', () => {
  const assertAbsolute = (attr, filename) => {
    // Anchor on a path separator right before the filename so e.g. "app.js"
    // doesn't false-positive-match inside "telegram-web-app.js".
    const re = new RegExp(`${attr}="([^"]*/${filename.replace('.', '\\.')}[^"]*)"`);
    const m = miniappHtml.match(re);
    assert.ok(m, `${filename} not referenced via ${attr}= in miniapp/index.html`);
    assert.ok(m[1].startsWith('/'), `${filename} must be referenced with an absolute path, got "${m[1]}"`);
  };
  assertAbsolute('href', 'styles.css');
  assertAbsolute('href', 'telegram-overrides.css');
  assertAbsolute('src', 'config.js');
  assertAbsolute('src', 'app.js');
  assertAbsolute('src', 'auth.js');
  assertAbsolute('src', 'telegram-miniapp.js');
});

test('both entry points load the exact same shared files — no per-entrypoint duplicates', () => {
  // sphere.js is deliberately NOT loaded by /miniapp/ (see the comment next
  // to its removed <script> tag in miniapp/index.html) — it's inert on
  // both entry points today (styles.css always hides the sphere wrap via
  // .app-container.v2), so skipping the Three.js download on Telegram
  // costs nothing visually. Every other shared file must still be the
  // same single copy on both entry points.
  const basenames = ['app.js', 'auth.js', 'config.js', 'styles.css'];
  for (const name of basenames) {
    assert.ok(indexHtml.includes(name), `index.html should reference ${name}`);
    assert.ok(miniappHtml.includes(name), `miniapp/index.html should reference ${name}`);
  }
  assert.ok(indexHtml.includes('sphere.js'), 'index.html should still reference sphere.js');
  assert.ok(!miniappHtml.includes('sphere.js'), 'miniapp/index.html must not load sphere.js — see rationale above');
  // And there must be no shadow copies of shared business-logic files under miniapp/.
  for (const name of [...basenames, 'sphere.js']) {
    assert.ok(!fs.existsSync(path.join(REPO_ROOT, 'miniapp', name)), `miniapp/${name} must not exist — reuse the root copy`);
  }
});

test('only one background video source is ever active, and /miniapp/ preloads less than the plain site', () => {
  for (const [label, html] of [['index.html', indexHtml], ['miniapp/index.html', miniappHtml]]) {
    const sourceTags = (html.match(/<source[^>]*id="cinema-video-source"[^>]*>/g) || []);
    assert.equal(sourceTags.length, 1, `${label}: exactly one <source> for #cinema-video`);
  }
  assert.match(indexHtml, /id="cinema-video"[\s\S]{0,80}preload="auto"/, 'index.html should keep its existing preload="auto"');
  assert.match(miniappHtml, /id="cinema-video"[\s\S]{0,80}preload="metadata"/, 'miniapp/index.html should use a lighter preload="metadata" to avoid an upfront multi-MB download in a Telegram WebView');
});

test('file picker: real <input type=file>, wired via <label for>, not display:none', () => {
  for (const [label, html] of [['index.html', indexHtml], ['miniapp/index.html', miniappHtml]]) {
    assert.match(
      html,
      /<input type="file" id="file-input"[^>]*class="visually-hidden"/,
      `${label}: file input must be a real, non-hidden <input type="file">`
    );
    assert.match(
      html,
      /<label[^>]*id="upload-zone"[^>]*for="file-input"/,
      `${label}: upload zone must be a <label for="file-input">`
    );
  }

  const stylesCss = read('styles.css');
  const visuallyHiddenBlock = stylesCss.match(/\.visually-hidden\s*\{([^}]*)\}/);
  assert.ok(visuallyHiddenBlock, '.visually-hidden rule not found in styles.css');
  assert.ok(
    !/display\s*:\s*none/.test(visuallyHiddenBlock[1]),
    '.visually-hidden must not use display:none (removes the file input from the tab/focus order and can break some WebView file pickers)'
  );
});

test('file input click is handled natively by the label — no input.click() and no async before opening the picker', () => {
  // A synchronous label-driven click is required for Telegram WebViews to
  // treat the file picker as originating from a real user gesture; an
  // async handler in between (or a duplicate input.click()) breaks that.
  const changeHandlerMatch = appJs.match(/elements\.fileInput\.addEventListener\('change',\s*\(e\)\s*=>\s*\{([^}]*)\}/);
  assert.ok(changeHandlerMatch, 'file-input change handler not found in app.js');
  assert.ok(!/async/.test(changeHandlerMatch[0].split('=>')[0]), 'file-input change handler must not be async before opening the picker');
  assert.ok(!appJs.includes('fileInput.click()'), 'app.js must not call fileInput.click() — the native label already opens the picker');
});

test('re-selecting the same file works (input value is reset on remove)', () => {
  const removeFileMatch = appJs.match(/function removeFile\(\)\s*\{([^}]*)\}/);
  assert.ok(removeFileMatch, 'removeFile() not found in app.js');
  assert.ok(/elements\.fileInput\.value\s*=\s*''/.test(removeFileMatch[1]), 'removeFile() must reset fileInput.value so selecting the same file again fires a change event');
});
