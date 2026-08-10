/**
 * NOT loaded by the real SONYA app. This file documents the intended
 * lifecycle for wiring the built `dist/sonya-cloth.js` bundle into
 * SONYA's existing vanilla app.js/index.html — for review only, stage 2.
 *
 * Real anchor points in the current app (unchanged by this prototype):
 *   - index.html:555   <section class="page" id="page-processing">
 *   - app.js:890        showPage('processing') is called when a generation starts
 *   - app.js:891        simulateProcessing() drives the existing progress UI
 *   - app.js:1361-1396  state → label/progress mapping used by polling
 *
 * None of those files are touched by this prototype. This example shows
 * the calls SONYA would make, once the widget is approved for real use.
 */

// import { SonyaCloth } from './dist/sonya-cloth.js'; // served from SONYA's own static assets or CDN

let clothMounted = false;
let publicConfig = null;

/** Lazy-load the bundle only when the visitor actually reaches the processing screen. */
async function loadClothModuleOnce() {
  if (loadClothModuleOnce._mod) return loadClothModuleOnce._mod;
  const mod = await import('./dist/sonya-cloth.js');
  loadClothModuleOnce._mod = mod;
  return mod;
}

/** Called once, the first time a job enters the processing screen this session. */
async function mountClothOnProcessingScreen() {
  if (clothMounted) return; // never a second canvas / second rAF loop
  const { SonyaCloth } = await loadClothModuleOnce();

  const host = document.getElementById('processing-cloth-host'); // a dedicated element inside #page-processing
  if (!host) return;

  // Stage 2: fetch from GET /api/public/cloth-config (read-only, cached by
  // CDN). Stage 1 prototype has no such endpoint yet — this is the shape
  // it would return.
  publicConfig = publicConfig || (await fetchPublicClothConfig());

  SonyaCloth.mount(host, { config: publicConfig });
  clothMounted = true;

  // The cloth must never sit on top of the status text/progress bar that
  // already lives in #page-processing — safeViewportBounds in the config
  // (see src/config/default-config.js) reserves that margin.
}

async function fetchPublicClothConfig() {
  // Placeholder for stage 2: GET /api/public/cloth-config
  // Returns { config, textureUrl } signed/served off the public CDN.
  throw new Error('not implemented in this prototype — see PRODUCTION_INTEGRATION_PLAN.md');
}

/** Hook into SONYA's existing showPage('processing') call site (app.js:890). */
function onEnterProcessingScreen() {
  mountClothOnProcessingScreen();
  SonyaCloth?.resume?.();
}

/** Hook into wherever app.js transitions to the result screen. */
function onEnterResultScreen() {
  destroyClothIfMounted();
}

/** Hook into job cancellation. */
function onJobCancelled() {
  destroyClothIfMounted();
}

function destroyClothIfMounted() {
  if (!clothMounted) return;
  loadClothModuleOnce._mod?.SonyaCloth.destroy();
  clothMounted = false;
}

// Standard page-visibility pause/resume — independent of navigating between
// SONYA's own screens. SonyaCloth already auto-pauses on document.hidden
// internally (see src/SonyaCloth.js), but the explicit calls below are the
// same lifecycle a host app would use for its own reasons (e.g. pausing
// while a modal dialog is open on top of the processing screen).
document.addEventListener('visibilitychange', () => {
  if (!clothMounted) return;
  const { SonyaCloth } = loadClothModuleOnce._mod || {};
  if (!SonyaCloth) return;
  if (document.hidden) SonyaCloth.pause();
  else SonyaCloth.resume();
});

/**
 * Re-entering the processing screen for a *second* job in the same session
 * must not create a second canvas or a second animation loop. Because
 * `mountClothOnProcessingScreen` short-circuits on `clothMounted`, and
 * `SonyaCloth.mount` itself refuses a second mount (see SonyaCloth.js),
 * either guard is sufficient on its own — both exist for defense in depth.
 *
 * The cloth's own render loop is fully independent of `app.js`'s generation
 * polling (`fetch`/`setTimeout` based) — nothing here shares a timer, and
 * SonyaCloth never touches app.js's DOM outside its own host element, so it
 * cannot block or interfere with polling.
 */

export {
  onEnterProcessingScreen,
  onEnterResultScreen,
  onJobCancelled,
};
