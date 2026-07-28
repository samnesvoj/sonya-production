/**
 * SONYA — runtime configuration.
 *
 * Loaded BEFORE auth.js / app.js so that the API client picks up the
 * production backend URL. Do not inline this in index.html — keeping it
 * as an external file makes future CSP hardening (drop 'unsafe-inline'
 * from script-src) trivially safe.
 *
 * Edit this file to point the frontend at a different backend.
 */
window.SONYA_API_BASE = '/api';
