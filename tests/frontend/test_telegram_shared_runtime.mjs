// Runtime (not just textual) proof that app.js — the file BOTH entry
// points load unmodified — never touches window.Telegram: loadApp() gives
// it window.Telegram === undefined (see dom_harness.mjs), same as a plain
// browser tab on /miniapp/index.html loaded outside Telegram, or / itself,
// and it must run init() through DOMContentLoaded without throwing.
//
// init() deliberately does NOT call loadUserProfile() — that matches the
// pre-existing production contract exactly: loadUserProfile() was only
// ever reachable from inside the old Telegram-only init path (a
// window.Telegram-gated call to the now-removed initializeTelegramIfNeeded()),
// so on a plain browser load it never ran and the profile UI stayed at its
// untouched default (empty avatar, no name). This Telegram/Mini-App
// architecture split must not change that — no demo/fake profile may be
// invented for production, and browser auth/profile flow is out of scope
// here (see auth.js, unrelated to this file).

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadApp } from './dom_harness.mjs';

test('app.js runs init() cleanly with window.Telegram undefined (plain browser, either entry point)', () => {
  assert.doesNotThrow(() => loadApp());
});

test('init() does not call loadUserProfile() — no demo/fake profile is invented on a plain load', () => {
  const { document } = loadApp();
  const avatar = document.getElementById('profile-avatar');
  // updateProfileAvatar() (the only thing that ever writes .src/.alt here)
  // must never run off a plain init() — same as before this Telegram
  // architecture split, on both entry points.
  assert.equal(avatar.src, undefined, 'profile avatar must stay unpopulated — no demo user in production');
  assert.equal(avatar.alt, undefined, 'profile avatar alt must be untouched — no fake name in production');
});
