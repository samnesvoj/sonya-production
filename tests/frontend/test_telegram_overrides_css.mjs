// Static checks on telegram-overrides.css: every rule must be scoped under
// html[data-runtime="telegram"] (so it can never leak into the plain site,
// which doesn't load this file anyway), decorative canvas/video layers must
// not intercept taps, and the functional Lite Editor video must be left
// alone.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..', '..');
const css = fs.readFileSync(path.join(REPO_ROOT, 'telegram-overrides.css'), 'utf8');

// Strip comments, then split into individual rule blocks ("selector { ... }").
function ruleSelectors(source) {
  const stripped = source.replace(/\/\*[\s\S]*?\*\//g, '');
  const selectors = [];
  const re = /([^{}]+)\{[^{}]*\}/g;
  let m;
  while ((m = re.exec(stripped))) {
    selectors.push(m[1].trim());
  }
  return selectors;
}

test('every rule in telegram-overrides.css is scoped under html[data-runtime="telegram"]', () => {
  const selectors = ruleSelectors(css);
  assert.ok(selectors.length > 0, 'no rules found — file may be empty or unparsable');
  for (const sel of selectors) {
    for (const part of sel.split(',')) {
      assert.ok(
        part.trim().startsWith('html[data-runtime="telegram"]'),
        `selector "${part.trim()}" is not scoped under html[data-runtime="telegram"]`
      );
    }
  }
});

test('decorative canvas/video/overlay layers get pointer-events:none', () => {
  const block = css.match(/([^{}]*canvas[^{}]*)\{([^{}]*)\}/);
  assert.ok(block, 'no rule targeting canvas found');
  assert.match(block[2], /pointer-events\s*:\s*none/);
});

test('the functional Lite Editor preview video is not blanket-disabled', () => {
  assert.ok(
    !/editor-stage-video|editor-preview/.test(css),
    'telegram-overrides.css must not touch the functional editor preview video/canvas'
  );
});

test('the heavy sphere is disabled via existing production CSS, not a telegram-only rule', () => {
  const stylesCss = fs.readFileSync(path.join(REPO_ROOT, 'styles.css'), 'utf8');
  assert.match(
    stylesCss,
    /\.app-container\.v2\s+#page-processing\s+\.sonya-sphere-wrap\s*\{[^}]*display\s*:\s*none/,
    'styles.css must unconditionally hide the sphere wrap for the current v2 UI — this, not telegram-overrides.css, is what actually stops sphere.js from ever starting its WebGL render loop'
  );
  const miniappHtml = fs.readFileSync(path.join(REPO_ROOT, 'miniapp', 'index.html'), 'utf8');
  // Checks for an actual <script ... sphere.js ...> tag, not the comment
  // that explains why there isn't one (which itself mentions "sphere.js").
  assert.ok(!/<script[^>]*sphere\.js[^>]*>/.test(miniappHtml), 'miniapp/index.html must not load sphere.js at all — the sphere is inert, so loading the Three.js module would only cost bandwidth for zero effect');
});

test('continuous blurred glow decorations (breathing animations) are frozen under the Telegram runtime', () => {
  const block = css.match(/((?:html\[data-runtime="telegram"\]\s*\.[\w-]+(?:::[\w-]+)?,?\s*)+)\{([^}]*)\}/);
  assert.ok(block, 'no rule disabling decorative glow animations found');
  assert.match(block[2], /animation\s*:\s*none/);
  assert.match(block[2], /filter\s*:\s*none/);
  assert.match(block[1], /\.processing-hero::before/, 'must cover .processing-hero::before — the sphere\'s replacement, active for the whole length of a generation job');
});
