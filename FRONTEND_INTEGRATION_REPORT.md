# Frontend Redesign Integration Report

Branch: `frontend-redesign-integration` (not merged, not pushed, no commits made — per instructions, this is a working-tree result awaiting review).

Sources:
- Approved design: `/Users/samnesvoj/Documents/sonya-design-impeccable`
- Pre-design baseline: `/Users/samnesvoj/Downloads/sonya-cleaned`
- Production (this repo): `/Users/samnesvoj/Documents/sonya`

## Method

Three-way diff (`baseline → design`, `baseline → production`) per file before touching anything, to separate "what the redesign changed" from "what production already fixed since the design was forked."

## Files transferred as-is from the design

Verified byte-identical between `production` and `baseline` before copying (i.e. production never diverged from baseline for these), so replacing with the design version carries no risk of dropping a production fix:

- `index.html`
- `styles.css`
- `sphere.js`
- `opencut.html`
- `legal/*.html`, `legal/_legal.css`
- `payment/success.html`, `payment/fail.html` (byte-identical across all three repos — copied, no-op)
- `hf_20260419_161836_77c00607-b936-40e5-b41d-240635ddc9d9 (1).mp4` — the dark-theme/opencut background video. This file existed in baseline and design but was **missing from production** (this is audit finding P1-6, the dark-theme 404). Added; still untracked in git pending your review of the `.gitignore`/LFS story for a 26 MB binary (see "Media assets" below).

`opencut.html` confirmed self-contained (own inline CSS/JS, doesn't load `styles.css`/`app.js`/`auth.js`) — no cross-file wiring risk from swapping it wholesale.

## Files deliberately left untouched

- **`auth.js`** — byte-identical between `baseline` and `design` (zero diff). The design made no visual changes to this file at all, so there was nothing to port. Left as pure production (auth flow, Idempotency-Key lifecycle, session state fix untouched).
- **`config.js`** — byte-identical across all three repos. Left as production.

## `app.js` — manual merge

This was the only file needing real reconciliation. `baseline→design` had 239 changed lines (visual/result-rendering); `baseline→production` had 231 changed lines (single-flight guard, XSS-safe rendering, URL validation, polling reentrancy guard — audit items P0-2 partial, P1-1). Design and production diverged from the same baseline independently, so a plain merge/patch wasn't viable — every hunk was reviewed by hand.

**What was necessary to display the new design (taken from design):**
- Removed `#btn-download` / `requestZipDownload()` (legacy Telegram-bot ZIP flow) — the new `index.html` no longer has this element; leaving the reference would have thrown on a `null` element during page init.
- Replaced the old `resultCount`/`resultSize` element refs and the dead, random-number `updateResultInfo()` with the new result-page element refs (`resultHeadline`, `resultSub`, `resultTags`, `resultClipGrid`, `resultActions`) and a real rendering pipeline driven by actual job data (mode label, clip count, duration, aspect) — no invented numbers.
- Added `progressForStatus()`/`setProgress()` to drive the new `#progress-bar` element from the real job status during polling (design added this; production's polling loop had no progress-bar wiring before).

**What was preserved/re-applied from production (not overwritten by design):**
- Single-flight job submission guard (`submitGenerationJob`, `jobSubmitInFlight`, button-busy state) — untouched.
- `isSafeResultUrl()` URL validation (https-only or same-origin, rejects `javascript:`/`data:`/malformed) — untouched, but **de-duplicated**: it was previously defined twice (once unused at top level from the design merge, once inside the polling IIFE); now a single definition, used by both the polling code and the new clip-grid renderer.
- Poll-loop reentrancy guard (`pollInFlight`) — untouched.
- Idempotency-Key flow, current auth flow, current API routes, error/retry handling — untouched (all live in `auth.js`, which wasn't modified).

**Where design and production's fixes actually conflicted, and how it was resolved:**

Design's own `app.js` *did* wire the real backend result into its new clip-grid (`renderSonyaResult()`), but it built the grid via `innerHTML` string concatenation with the raw, unvalidated backend URL — i.e. it reintroduced the exact XSS class production had already fixed (audit P1-1), just relocated to a new function. Production's old `showRealResult()`, meanwhile, safely rendered the result but into an ad-hoc floating `<div>` bolted onto `<body>`, visually disconnected from the redesigned result page.

Resolved by rebuilding the new result-rendering functions (`renderResultTags`, `renderResultClips`, `renderResultActions`) using **only** `createElement`/`textContent`/direct `.href` property assignment — never `innerHTML` — with every URL (per-clip and archive) passing through `isSafeResultUrl()` before it can reach a `.href`. `showRealResult()` now feeds validated clips into this same pipeline instead of building its own box. Net effect: the real completed-job result now renders inside the actual redesigned result page (grid, tags, headline) instead of an unstyled floating box, with the same security guarantees as before (arguably stronger, since design's own approach would have been a regression).

**Explicitly excluded from the merge:** design's `?page=`/`?clips=` debug-jump feature (lets a URL query param skip straight to any screen with fake demo data). This is a dev/preview convenience, not part of the rendered design, and the task instructions explicitly exclude "временные mock/preview режимы" from production.

## Test suite changes

Existing tests hard-asserted on the *old* implementation detail (`#sonya-real-result` floating box with an inline `<video>`), not just on the security property. Since the result UI intentionally changed, the tests were updated to target the new structure (`#result-clip-grid`, `#result-actions`) while keeping **identical or stronger** security assertions:
- `innerHTML` must stay `''` on the grid/actions containers (unchanged assertion, new target).
- `javascript:`/`data:` URLs must never reach any `href`/`src` in the result area (unchanged assertion).
- A rejected/missing URL still shows a safe, plain-text (`textContent`-only) message.
- A valid `https:` URL still produces a working, `target="_blank" rel="noopener"` link (previously asserted via `<video src>`+`<a href>`; now via the clip-card link, since the new design doesn't inline a `<video>` element — see "Known trade-off" below).

Also fixed a real gap in the test harness itself (`tests/frontend/dom_harness.mjs`): the lightweight DOM shim had no `document.createTextNode`, which the new tag-rendering code uses (a standard DOM API, fine in real browsers, just missing from the shim). Added a minimal `FakeTextNode`. Also updated `REQUIRED_IDS` to match the new markup's element IDs (dropped `btn-download`/`result-count`/`result-size`, added the five new result-page IDs).

**Known trade-off, flagged for a product decision:** the new clip-card design has no inline `<video>` preview — only a play-icon-styled card that links out (`target="_blank"`) to the validated URL, plus an explicit download link. Previously users could watch inline via the ad-hoc box's `<video>` tag. This matches the design's own approved markup (no video element in `sonya-design-impeccable`'s clip card either), so it was carried through as-is rather than inventing a UI element the design didn't include — but it is a minor functional regression in "watch without leaving the page" convenience, worth a follow-up decision (e.g. a lightbox player) if desired.

## Legal pages (P0-5 from the audit)

The audit flagged "visible TODO" placeholders in `legal/*.html`. Checked directly: the TODOs are inside an `<!-- -->` HTML comment at the top of each file (never rendered), and the `.legal-todo` CSS class defined in `_legal.css` is unused in any of the markup. So there is no user-visible TODO text — the actual page content (e.g. `privacy.html`) has real operator details (self-employed individual, INN, contact email, `sonya.group` domain). This is better than the audit's original finding, but **the TODO comments themselves still flag that legal sign-off hasn't happened** ("финально проверить текст с юристом перед запуском") — that's a legal/business item, not a code fix, and is unchanged by this integration.

## Media assets

- `space-bg.png` (96 KB) confirmed still unused in the new design (matches audit P2-1) — left in place, not deleted (out of scope for this task; flag for a separate cleanup).
- `bg-light.mp4` (15 MB) — already present and identical in production; no change.
- `hf_20260419_..._(1).mp4` (26 MB, dark theme + opencut background) — newly added, currently untracked in git. **This and `bg-light.mp4` are both good CDN candidates** (per the design repo's own `CDN_MIGRATION_PLAN.md`) rather than living in the git repo/VPS long-term; for now it's a plain file next to `index.html`, same as `bg-light.mp4` already was.

## What's VPS vs. future-CDN vs. future-S3

- **Stays on the VPS as static files (current state, no change in serving strategy):** `index.html`, `styles.css`, `sphere.js`, `opencut.html`, `app.js`, `auth.js`, `config.js`, `legal/*`, `payment/*`.
- **Candidates for CDN later (large, cacheable, rarely-changing binaries):** `bg-light.mp4`, `hf_20260419_..._(1).mp4`, and — if kept at all — `space-bg.png`. None of this was moved off the VPS as part of this task; flagging per your request, not acting on it.
- **S3/user-media (already the case, unaffected by this integration):** generated video results and any user-uploaded source files — these were already served via presigned S3 URLs through the backend, untouched here.

## Explicitly not touched

Backend, PostgreSQL/migrations, GPU/vast.ai lifecycle, S3 processing, payment backend, deployment/server config — no files outside the frontend static assets and `tests/frontend/` were modified. `sonya-cleaned/`, `design-preview.html`, `.claude/`, `.impeccable/`, and screenshots from the design repo were not copied.

## Test results (final state)

```
node --test tests/frontend/*.mjs
  tests 16, pass 16, fail 0

pytest -q
  82 passed, 4 skipped
```

## Not done (per instructions)

No commit, no push, no deploy. Working tree on `frontend-redesign-integration` has the changes above, ready for your review.
