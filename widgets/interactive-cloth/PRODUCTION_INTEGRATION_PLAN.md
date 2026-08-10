# Interactive Cloth — Production Integration Plan (Stage 2)

This document describes what stage 1 (this prototype) deliberately does
**not** implement: a shared, published config that all visitors see, and a
protected Studio for editing it. Nothing in this section is implemented —
it's a plan for review before any backend work starts.

## Runtime split

**Public runtime** (all visitors, e.g. the processing screen):
- Loads `dist/sonya-cloth.js` only. Never loads the Studio bundle.
- Fetches the currently published config via a **read-only** call:
  `GET /api/public/cloth-config`.
- Loads the cloth texture from a CDN URL returned in that config.
- Has no way to persist anything — `SonyaCloth.setConfig`/`setTexture` only
  change the local in-memory instance for that page view; there is no
  public write endpoint.

**Admin Studio** (SONYA operator only):
- Served from its own route, e.g. `/admin/cloth` — not linked from the
  public nav.
- **Not URL-secret-based.** A route being unlisted is not a security
  control; the backend must independently verify the request is from an
  authorized admin on every admin API call, exactly as it would if the URL
  were public knowledge. No `?admin=1` query flag, and no
  keyboard-shortcut-only gate — both are trivially bypassed by anyone who
  inspects the page.

## Auth for the Studio

- Reuse SONYA's existing passwordless auth (already in `auth.js`/backend) —
  no new login flow.
- Backend validates the authenticated session as usual, **then** checks the
  session's email against a server-side admin allowlist read from an
  environment variable (e.g. `CLOTH_STUDIO_ADMIN_EMAILS`, comma-separated).
  This is the actual access control — not the route, not a frontend flag.
- No admin password or secret is ever embedded in frontend JavaScript. The
  frontend only knows "am I authenticated," never "am I an admin" — that
  check happens server-side on every admin request.

## Future endpoints

| Endpoint | Access | Purpose |
|---|---|---|
| `GET /api/public/cloth-config` | public, read-only | Serves the currently published config + texture CDN URL. Cacheable. |
| `GET /api/admin/cloth-config` | admin-only | Same shape, but for editing in Studio (may include a draft, not just the published version). |
| `PUT /api/admin/cloth-config` | admin-only | Writes a new config, bumps `version`. |
| `POST /api/admin/cloth-texture` | admin-only | Uploads a new cloth image, returns its public CDN URL. |

Regular authenticated users (non-admin) get exactly the same access as
anonymous visitors on these routes: read-only via the public endpoint. The
admin endpoints reject anyone not on the allowlist, authenticated or not.

## Texture storage

- Upload target: the public `sonya-public-media` Timeweb bucket, served via
  `CDN-sonya-public-media` — **never** the private `sonya-media-prod`
  bucket, and never mixed into user-uploads storage (this image is public
  brand content, not user data).
- After upload, use a **versioned URL** (e.g.
  `.../cloth/2026-08-01-<hash>.png` or a `?v=<config.version>` query
  parameter) so the CDN and visitors' browsers don't serve a stale cached
  image after a republish.

## Storing the published config

Two options, compared (nothing implemented yet):

1. **PostgreSQL JSONB column** (recommended). A single row (or a small
   `cloth_config` table keyed by a fixed id) with a `config JSONB` column
   and a `version INTEGER`. Fits the existing FastAPI + PostgreSQL stack
   with zero new infrastructure; trivial to add an `updated_at`/`updated_by`
   audit trail; a `GET` is a single indexed row read, cheap enough to serve
   directly or behind a short CDN cache.
2. **Server-controlled versioned JSON file** (e.g. written to
   `sonya-public-media` alongside the texture, served straight from the
   CDN). Simpler to implement (no DB migration), but loses transactional
   writes, audit trail, and easy validation on write — the backend would
   still need to gate/validate writes through an authenticated endpoint
   rather than accepting raw file uploads, which mostly reproduces a
   database's job with worse tooling.

**Recommendation:** JSONB in Postgres — it's already the stack, gives free
versioning via the `version` column, and keeps validation server-side in
one place (reuse `config-schema.js`'s validation logic, ported or mirrored
in Python, on the `PUT` handler).

## "Publish" button flow (future Studio)

1. Operator uploads an image in Studio → `POST /api/admin/cloth-texture`
   → backend stores it in `sonya-public-media` → returns a versioned CDN
   URL.
2. Studio writes the full config (including that texture URL) via
   `PUT /api/admin/cloth-config`.
3. Backend validates the config server-side, increments `version`, persists
   it (JSONB row).
4. From that point, `GET /api/public/cloth-config` serves the new config to
   all visitors — no client-side deploy needed.
5. Regular visitors never gain write access; only the `PUT` path exists,
   and only admins can reach it.

## What Stage 1 already prepares for this

- `ConfigStore` (`src/config/ConfigStore.js`) is the interface Studio
  already codes against. Stage 2 only needs to add an `HttpConfigStore`
  implementing the same four methods (`load/save/uploadTexture/reset`)
  against the endpoints above — `ClothStudio.js` does not need to change.
- `config-schema.js`'s `validateConfig` is the same shape the backend
  should enforce on `PUT` — either port it to the backend's language or
  keep a matching schema server-side.
- The public/admin bundle split (`dist/sonya-cloth.js` vs
  `dist/sonya-cloth-studio.js`) already guarantees visitors never download
  Studio code, regardless of how `/admin/cloth` ends up being routed.
