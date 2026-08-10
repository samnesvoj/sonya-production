import { SonyaCloth } from './SonyaCloth.js';
import { PRESETS, SONYA_CHROME_LIGHT_OVERRIDES } from './config/default-config.js';
import { mergeConfig } from './config/config-schema.js';

/**
 * SONYA — Chrome processing-screen cloth backdrop.
 *
 * Approved design (2026-08-10): PRESETS['SONYA Chrome'] in
 * cloth/config/default-config.js, ported byte-for-byte from
 * widgets/holocloth-faithful's sonya-processing-chrome-main.tsx via
 * widgets/interactive-cloth (see PR #2). Values are not re-tuned here.
 *
 * Lazy-mounts when #page-processing becomes active, destroyed the instant
 * the user navigates away — same MutationObserver-driven show/hide pattern
 * sphere.js already used for this exact page (the effect this replaces;
 * sphere.js itself is untouched and stays inert under the existing
 * `.sonya-sphere-wrap { display: none }` v2 rule). Mount/destroy (not just
 * start/stop) because SonyaCloth owns a real WebGL context per mount, and
 * the generation job polling in app.js is completely independent of this
 * file — nothing here touches app.js, #progress-bar, or #processing-status.
 */
(function () {
	const host = document.getElementById('processing-cloth-host');
	const page = document.getElementById('page-processing');
	if (!host || !page) return;

	const chromeDark = PRESETS['SONYA Chrome'];
	const chromeLight = mergeConfig(chromeDark, SONYA_CHROME_LIGHT_OVERRIDES);

	function currentTheme() {
		return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
	}

	function presetForTheme(theme) {
		return theme === 'light' ? chromeLight : chromeDark;
	}

	let mounted = false;

	// The cloth canvas must stay pointer-events:auto (only while mounted —
	// see the .is-interactive CSS toggle below) so drag/grab works, but a
	// full-viewport WebGL canvas was found (manual testing) to swallow wheel
	// scrolling even with touch-action left open, once it becomes its own
	// GPU compositor layer — a page below the fold (short viewports, e.g.
	// mobile) would then be unscrollable while the cursor sits anywhere on
	// the processing screen. Forward wheel deltas to the real scroll
	// container explicitly instead of relying on native chain-scrolling.
	function onWheel(e) {
		document.scrollingElement.scrollTop += e.deltaY;
	}

	function mount() {
		if (mounted) return;
		mounted = true;
		host.classList.add('is-interactive');
		host.addEventListener('wheel', onWheel, { passive: true });
		SonyaCloth.mount(host, { config: presetForTheme(currentTheme()) });
	}

	function destroy() {
		if (!mounted) return;
		mounted = false;
		host.classList.remove('is-interactive');
		host.removeEventListener('wheel', onWheel);
		SonyaCloth.destroy();
	}

	function sync() {
		if (page.classList.contains('active')) mount();
		else destroy();
	}

	// Live dark/light swap while the user is on the processing screen —
	// SonyaCloth.setConfig() re-applies material + the theme-matched brand
	// texture (sonya-cloth-dark.jpg / sonya-cloth-light.jpg) in one call.
	const themeObserver = new MutationObserver(() => {
		if (!mounted) return;
		SonyaCloth.setConfig(presetForTheme(currentTheme()));
	});
	themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

	// Observe processing-page activation/deactivation — mirrors sphere.js's
	// own MutationObserver on the same #page-processing class attribute.
	const pageObserver = new MutationObserver(sync);
	pageObserver.observe(page, { attributes: true, attributeFilter: ['class'] });

	sync();
})();
