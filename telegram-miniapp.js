/**
 * SONYA — Telegram Mini App runtime.
 *
 * Loaded only by /miniapp/index.html, before config.js/app.js/auth.js.
 * Sole responsibility: Telegram WebApp lifecycle + theming/viewport sync.
 * Never touches auth, job submission, or any other business logic — that
 * all lives in app.js/auth.js, shared unmodified with the plain site.
 *
 * Must be safe to load outside Telegram (direct browser hit on /miniapp/
 * in Chrome/Safari): every Telegram API access is feature-detected and
 * wrapped, so a missing or malformed window.Telegram.WebApp never throws.
 *
 * IMPORTANT: the official telegram-web-app.js SDK creates
 * window.Telegram.WebApp UNCONDITIONALLY, even when the page is opened as
 * a plain link outside Telegram — its mere presence is not proof of a real
 * Telegram launch. Only webApp.initData (non-empty) or one of Telegram's
 * own tgWebApp* launch params in the URL prove that. Everything below —
 * data-runtime="telegram" (which telegram-overrides.css keys off), ready(),
 * expand(), and theme/viewport sync — only ever runs after that check.
 */
(function () {
	'use strict';

	// Pause the full-bleed background video while the WebView is backgrounded
	// (autoplaying <video> keeps decoding frames in a hidden tab/app unless
	// explicitly paused) and resume it on return. Independent of whether this
	// is a real Telegram launch — harmless, generically useful for this page,
	// doesn't touch any Telegram API or Telegram-only visuals. This script
	// runs in <head>, before #cinema-video exists, so the element is looked
	// up lazily inside the handler rather than captured up front.
	document.addEventListener('visibilitychange', function () {
		var video = document.getElementById('cinema-video');
		if (!video) return;
		try {
			if (document.hidden) {
				video.pause();
			} else {
				video.play().catch(function () {});
			}
		} catch (e) {}
	});

	var webApp;
	try {
		webApp = window.Telegram && window.Telegram.WebApp;
	} catch (e) {
		webApp = null;
	}

	// Telegram appends these to the launch URL (query string on some
	// clients, hash fragment on others) — their presence is evidence of a
	// real Telegram launch independent of whether the SDK object loaded.
	var TG_LAUNCH_PARAM_RE = /(?:^|[?&#])tgWebApp(?:Data|Version|Platform|ThemeParams|StartParam)=/;

	function hasTelegramLaunchParams() {
		var haystack = '';
		try {
			haystack += window.location.search || '';
		} catch (e) {}
		try {
			haystack += window.location.hash || '';
		} catch (e) {}
		return TG_LAUNCH_PARAM_RE.test(haystack);
	}

	function isRealTelegramLaunch() {
		if (webApp) {
			try {
				if (typeof webApp.initData === 'string' && webApp.initData.length > 0) {
					return true;
				}
			} catch (e) {}
		}
		try {
			return hasTelegramLaunchParams();
		} catch (e) {
			return false;
		}
	}

	// Plain browser hit on /miniapp/ (Chrome, Safari, a dev preview, someone
	// pasting the link outside Telegram) — even with the SDK loaded and
	// window.Telegram.WebApp present, this is NOT a real Telegram launch.
	// Stop here: no data-runtime, no Telegram CSS, no Telegram API calls,
	// no console errors. The page renders with the plain-site browser style.
	if (!isRealTelegramLaunch()) return;

	// Marks this page as running inside a confirmed Telegram launch —
	// telegram-overrides.css and app.js's theme logic key off this.
	document.documentElement.setAttribute('data-runtime', 'telegram');

	var readyCalled = false;
	function initOnce() {
		if (readyCalled) return;
		readyCalled = true;
		if (!webApp) return;
		try {
			if (typeof webApp.ready === 'function') webApp.ready();
		} catch (e) {}
		try {
			if (typeof webApp.expand === 'function') webApp.expand();
		} catch (e) {}
	}

	function manualTheme() {
		try {
			return localStorage.getItem('theme');
		} catch (e) {
			return null;
		}
	}

	function applyThemeParams() {
		if (!webApp) return;
		try {
			var params = webApp.themeParams || {};
			var root = document.documentElement.style;
			Object.keys(params).forEach(function (key) {
				var cssName = '--tg-theme-' + key.replace(/_/g, '-');
				root.setProperty(cssName, params[key]);
			});
		} catch (e) {}
	}

	// A manual in-app theme choice (toggleTheme() in app.js, persisted to
	// localStorage['theme']) always wins over Telegram's colorScheme —
	// Telegram only supplies the *default* when the user never picked one.
	function applyColorScheme() {
		if (webApp) {
			try {
				if (!manualTheme() && webApp.colorScheme) {
					document.documentElement.setAttribute('data-theme', webApp.colorScheme);
				}
			} catch (e) {}
		}
		applyThemeParams();
	}

	function applyViewportVars() {
		if (!webApp) return;
		try {
			var root = document.documentElement.style;
			if (typeof webApp.viewportHeight === 'number') {
				root.setProperty('--tg-viewport-height', webApp.viewportHeight + 'px');
			}
			if (typeof webApp.viewportStableHeight === 'number') {
				root.setProperty('--tg-viewport-stable-height', webApp.viewportStableHeight + 'px');
			}
		} catch (e) {}
	}

	initOnce();
	applyColorScheme();
	applyViewportVars();

	try {
		if (webApp && typeof webApp.onEvent === 'function') {
			webApp.onEvent('themeChanged', applyColorScheme);
			webApp.onEvent('viewportChanged', applyViewportVars);
		}
	} catch (e) {}

	// NOTE: this file intentionally never reads Telegram's raw user/session
	// payload — that data is unsigned and client-controlled, so it must
	// never be treated as an authenticated identity. Real auth is the
	// email-code session cookie handled by auth.js, identical on both
	// entry points.
})();
