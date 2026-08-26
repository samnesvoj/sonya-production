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
  // Find the specific rule block containing .processing-hero::before (the
  // sphere's replacement, animating for the whole length of a generation
  // job) rather than the first comma-group in the file, since other rules
  // (e.g. the hero-pill background compensation) also list several
  // html[data-runtime="telegram"] .selector, ... groups.
  const block = css.match(/((?:html\[data-runtime="telegram"\][^,{]+,?\s*)*html\[data-runtime="telegram"\]\s*\.processing-hero::before[^{]*)\{([^}]*)\}/);
  assert.ok(block, 'no rule disabling .processing-hero::before found');
  assert.match(block[2], /animation\s*:\s*none/);
  assert.match(block[2], /filter\s*:\s*none/);
});

// Regression test for the dark-rectangle artifact bug: html[data-runtime=
// "telegram"] * { backdrop-filter: blur(6px) !important } forced EVERY
// element -- including plain text spans and icon <i> tags nested inside an
// already-blurred glass pill -- to independently sample-and-blur whatever
// sat behind its own smaller bounding box, rendering as a visibly darker
// rectangle around "Ссылка"/"Файл", the input placeholder, the source
// icons and the "ПРОДОЛЖИТЬ" button. The fix is backdrop-filter: none
// (this property's own initial value, so it's a true no-op wherever it
// wasn't already declared) -- never a smaller blur radius, which would
// still create the same per-element artifact, just fainter.
test('no rule sets a non-"none" backdrop-filter value anywhere (the fix for the rectangular artifact bug)', () => {
  const backdropDeclarations = css.match(/-?(?:webkit-)?backdrop-filter\s*:\s*[^;]+;/g) || [];
  assert.ok(backdropDeclarations.length > 0, 'expected at least the blanket backdrop-filter reset rule');
  for (const decl of backdropDeclarations) {
    assert.match(decl, /:\s*none\s*!important\s*;/, `backdrop-filter must always be reset to none, found: "${decl.trim()}"`);
  }
});

test('the blanket backdrop-filter reset is scoped broadly (html[data-runtime="telegram"] *) so it also reaches nested glass panels', () => {
  assert.match(css, /html\[data-runtime="telegram"\]\s*\*\s*\{[^}]*backdrop-filter\s*:\s*none\s*!important/);
});

test('no rule applies filter/transform/will-change to broad or text-like selectors (only removals, never additions)', () => {
  // The file must never introduce transform or will-change at all -- there
  // is no legitimate Telegram-only use for either, and adding them to a
  // broad selector is exactly the kind of per-element GPU-layer promotion
  // that caused the backdrop-filter bug in the first place.
  assert.ok(!/\btransform\s*:/.test(css), 'telegram-overrides.css must not set transform anywhere');
  assert.ok(!/\bwill-change\s*:/.test(css), 'telegram-overrides.css must not set will-change anywhere');
  // Every plain `filter:` declaration (not backdrop-filter, checked above)
  // must be a removal (`none`), never a value that would force a new
  // compositing layer on whatever it's applied to.
  const allDeclarations = css.match(/([\w-]*filter)\s*:\s*[^;]+;/g) || [];
  const plainFilterDeclarations = allDeclarations.filter((d) => /^filter\s*:/.test(d));
  assert.ok(plainFilterDeclarations.length > 0, 'expected at least the glow-decoration filter:none rule');
  for (const decl of plainFilterDeclarations) {
    assert.match(decl, /:\s*none\s*!important\s*;/, `filter must only ever be reset to none, found: "${decl.trim()}"`);
  }
});
