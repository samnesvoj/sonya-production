/**
 * SONYA — Telegram Mini App runtime.
 *
 * Loaded only by /miniapp/index.html, before config.js/app.js/auth.js.
 * Sole responsibility: Telegram WebApp lifecycle + theming/viewport sync.
 * Never touches auth, job submission, or any other business logic — that
 * all lives in app.js/auth.js, shared unmodified with the plain site.
 *
 * Must be safe to load outside Telegram (direct browser hit on /miniapp/):
 * every Telegram API access is feature-detected and wrapped, so a missing
 * or malformed window.Telegram.WebApp never throws.
 */
(function () {
	'use strict';

	// Marks this page as the Telegram entry point regardless of whether the
	// SDK is actually present — telegram-overrides.css keys off this, and it
	// must apply even when /miniapp/ is opened by mistake in a plain browser.
	document.documentElement.setAttribute('data-runtime', 'telegram');

	// Pause the full-bleed background video while the WebView is backgrounded
	// (autoplaying <video> keeps decoding frames in a hidden tab/app unless
	// explicitly paused) and resume it on return. Independent of the
	// Telegram SDK — this script runs in <head>, before #cinema-video
	// exists, so the element is looked up lazily inside the handler rather
	// than captured up front.
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
	if (!webApp) return;

	var readyCalled = false;
	function initOnce() {
		if (readyCalled) return;
		readyCalled = true;
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
		try {
			if (!manualTheme() && webApp.colorScheme) {
				document.documentElement.setAttribute('data-theme', webApp.colorScheme);
			}
		} catch (e) {}
		applyThemeParams();
	}

	function applyViewportVars() {
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
		if (typeof webApp.onEvent === 'function') {
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
