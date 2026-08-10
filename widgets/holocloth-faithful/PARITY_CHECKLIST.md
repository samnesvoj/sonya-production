# Parity Checklist — holocloth-faithful vs upstream

Upstream: https://github.com/dmitrykurash/holocloth
Live reference: https://holocloth.vercel.app/
Commit copied from: `a6078ba698a53cbf18be870cea555c1971f84103` (2026-07-22, "README: one-line clone-and-run command"), MIT license, © 2026 Dmitry Kurash.

This is a **direct copy** of the upstream source (`src/scene.ts`, `cloth.ts`, `holoMaterial.ts`, `decals.ts`, `textures.ts`, `dofPass.ts`, `bakedPose.ts`, `App.tsx`, `main.tsx`, `index.html`, `public/*`, `package.json`, `vite.config.ts`, `tsconfig.json` — byte-identical unless listed below), not a reimplementation. React, TypeScript, Vite, Three.js, and DialKit are all kept exactly as upstream uses them.

## Files kept byte-identical

`src/scene.ts`, `src/cloth.ts`, `src/holoMaterial.ts`, `src/decals.ts`, `src/textures.ts`, `src/dofPass.ts`, `src/bakedPose.ts`, `src/main.tsx`, `tsconfig.json`, `LICENSE`, `public/holo-bg.jpg`, `public/holo-bg-2.jpg`, `public/bump-scratches.jpg`.

## Files changed, and exactly what changed

| File | Change | Why |
|---|---|---|
| `index.html` → `studio.html` | Renamed only. Content untouched (still loads `/src/main.tsx`, still renders the original `<App/>`). | Task requires a `studio.html` entry point. |
| `vite.config.ts` | Added `build.rollupOptions.input` with two entries (`studio.html`, `public.html`). Nothing else touched — same plugin, same dev port. | Multi-page build so Public and Studio ship as separate HTML/JS outputs. |
| `package.json` | `name` changed to `sonya-holocloth-faithful` (was `"holocloth"`, a private local name, not published); added `dev:studio`/`dev:public` convenience scripts. Dependencies list is untouched. | Avoid confusion with the actual upstream package name; no behavior change. |
| `src/App.tsx` | Added: 1 import line (`sonyaDraftStore.ts`), 2 new refs (`sonyaPresetInputRef`, `bumpImageRef`), 4 new DialKit actions (`saveDraft`, `restoreDraft`, `exportSonyaPreset`, `importSonyaPreset`) appended after the existing `poke` action, their branches in `onAction`, one new hidden `<input>` for multi-file preset import, and 3 one-line additions to keep `bumpImageRef` in sync wherever `setBumpMap`/upload/remove already run. **No existing line was removed or altered in meaning** — see `git diff` for the exact hunks. | Only way to add SONYA save/export without a parallel UI; every new control routes through the same `HoloApp`/DialKit state the original controls already use. |

## New files (SONYA-only, zero upstream code)

- `public.html`, `src/public-main.tsx`, `src/PublicApp.tsx` — Public entry. Mounts the same `HoloApp` class, no DialKit, no panel.
- `src/sonyaDraftStore.ts` — local draft (localStorage/IndexedDB) + JSON preset export/import. Does not modify simulation, material, or rendering code.
- `PARITY_CHECKLIST.md` (this file).

## What was explicitly NOT done

- No gravity, no pinning, no "curtain" behavior — the cloth is exactly upstream's zero-gravity, gel-damped `ClothSim`.
- No OrbitControls removal — camera orbit/zoom/pan is upstream's untouched `OrbitControls` wiring in `scene.ts`.
- No EffectComposer/bloom/DOF/grain removal — `scene.ts`'s post-processing chain (`RenderPass` → `MacroDofPass` → `UnrealBloomPass` → `OutputPass` → grain `ShaderPass`) is untouched.
- No baked-pose removal — `bakedPose.ts` still seeds the initial drape.
- No shader rewrite — `holoMaterial.ts`'s `onBeforeCompile` GLSL is untouched.
- No DialKit/React removal from Studio.

## Parity table

| Original feature | Works locally | Difference | Reason |
|---|---|---|---|
| Cloth grab (pointerdown + raycast) | Yes | None | Unmodified `scene.ts` |
| Throw / release, momentum | Yes | None | Unmodified `cloth.ts` |
| Zero-gravity / gel floating behavior | Yes | None | Unmodified `ClothSim` (no gravity term exists) |
| Wrinkle settling / damping | Yes | None | Unmodified |
| Baked starting pose | Yes | None | `bakedPose.ts` unmodified |
| Camera orbit (drag empty space) | Yes | None | Unmodified `OrbitControls` |
| Camera pan (Space+drag) | Yes | None | Unmodified |
| Camera zoom (wheel) | Yes | None | Unmodified |
| Upload cloth image | Yes | None | Unmodified `App.tsx` upload path |
| Upload decal image | Yes | None | Unmodified |
| Holo foil / rainbow diffraction / sparkle | Yes | None | Unmodified `holoMaterial.ts` |
| Chrome preset | Yes | None | Unmodified `PRESET_VALUES.Chrome` |
| Black Cloth preset | Yes | None | Unmodified `PRESET_VALUES['Black Cloth']` |
| Bump maps (upload + procedural weave) | Yes | None | Unmodified `textures.ts` |
| Bloom | Yes | None | Unmodified `UnrealBloomPass` |
| Depth of field | Yes | None | Unmodified `dofPass.ts` |
| Focus picking (click cloth) | Yes | None | Unmodified `startPickFocus`/`clearPickFocus` |
| Film grain | Yes | None | Unmodified `GrainShader`/`grainPass` in `scene.ts`'s composer chain |
| Versions (DialKit in-memory) | Yes | None | Unmodified `DialStore` wiring |
| PNG export (with/without background) | Yes | None | Unmodified `exportPNG()` |
| Transparent export | Yes | None | Same code path as above |
| Resize | Yes | None | Unmodified `ResizeObserver` |
| Mobile pointer interaction | Not yet verified on a physical touch device | Pointer Events API is unmodified so it should behave identically to upstream, but this environment could only exercise mouse + synthetic pointer events | Sandbox has no physical touch device |
| SONYA: Save/Restore local draft | Yes (new) | Additive — not in upstream | New requirement |
| SONYA: Export/Import portable preset | Yes (new) | Additive — not in upstream; images exported as separate PNGs, JSON never contains a `blob:` URL | New requirement |
| Public mode without panel | Yes (new) | Additive — loads the same `HoloApp` with no DialKit mounted | New requirement |

## Known gaps / follow-ups

- `public.html` needs an exported `sonya-preset.json` (+ its PNGs) copied into `public/` to show a *published* look; until then it falls back to upstream's own default "Holo" preset + `holo-bg-2.jpg`/`bump-scratches.jpg`, which is why it renders something sensible out of the box.
- Mobile/touch has not been physically verified in this sandbox (no touch hardware) — the code path is identical to upstream's, which is known to support pointer events.
