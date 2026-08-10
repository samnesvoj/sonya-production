# Third-Party Notice: holocloth

## Source

- Repository: https://github.com/dmitrykurash/holocloth
- Author: Dmitry Kurash
- License: MIT (see `LICENSE` in this directory — verbatim copy of the upstream `LICENSE` file)
- Copyright: (c) 2026 Dmitry Kurash

This SONYA widget (`widgets/interactive-cloth/`) adapts small parts of holocloth's
source. The MIT license and this notice are kept alongside the adapted code as
required by the license terms.

## What was adapted from holocloth

| Upstream file | Adapted into | What was kept |
|---|---|---|
| `src/cloth.ts` (`ClothSim`) | `src/clothPhysics.js` | Verlet position/previous-position integration, structural/shear/bend constraint topology and iterative relaxation solver, `startGrab`/`moveGrab`/`endGrab` grab-radius interaction model, per-vertex cavity (ambient occlusion) computation. |
| `src/holoMaterial.ts` (`createHoloMaterial`) | `src/shaders/holoMaterial.js` | `MeshPhysicalMaterial` + `onBeforeCompile`-injected GLSL for the iridescent diffraction-band look, the cavity-AO fragment hookup, and the rounded-corner cutout SDF. |
| `src/textures.ts` (`makeGrainRoughnessMap`) | `src/shaders/grainRoughness.js` | Small deterministic procedural canvas-noise roughness map generator. |
| `src/scene.ts` (`HoloApp`) | `src/interaction.js`, `src/SonyaCloth.js` | The pointer-raycast grab/drag/release lifecycle (pointerdown/move/up, pointer capture, drag-plane projection), `ResizeObserver`-based resize handling, and the `dispose()` teardown pattern (remove listeners, dispose geometry/material/texture/renderer). |

## Substantial changes made for SONYA

- **Physics model changed from zero-gravity to gravity-driven.** Upstream `ClothSim`
  is a zero-gravity "gel" — all motion comes from user interaction and heavy
  damping. SONYA's spec calls for a hanging/draped cloth affected by
  configurable `gravity` and `wind`, so `clothPhysics.js` adds gravity and a
  time-varying wind acceleration term to the substep integrator, adds pinned
  vertex support (fixed points, e.g. the top edge, read from config) so the
  cloth can hang, and adds a maximum-displacement clamp so a thrown cloth
  settles back into view instead of drifting off-screen forever.
- **No baked initial pose.** Upstream seeds the grid from `bakedPose.ts`, a
  hand-captured drape snapshot. SONYA's cloth instead starts flat/pinned and
  is warmed up under gravity for a few physics steps before first reveal —
  simpler and avoids carrying over baked sample data.
- **Rewritten from React + DialKit to vanilla JS / ES modules.** Upstream's
  UI (`App.tsx`, `main.tsx`) is a React app using the `dialkit` control-panel
  library. SONYA's project is intentionally React-free vanilla JS end to end,
  and the requested public API (`SonyaCloth.mount/setTexture/setConfig/pause/
  resume/destroy`) is a plain ES-module API, not a React component. The
  entire UI layer — both the public runtime and the Studio configurator
  (`src/studio/ClothStudio.js`) — was written from scratch in vanilla JS/CSS;
  none of `App.tsx`, `main.tsx`, or the `dialkit`/`react`/`react-dom`/`motion`
  dependencies were used.
- **New config system.** A single versioned config object + JSON-schema-style
  validation (`src/config/`), a `ConfigStore` interface, and a
  `LocalDraftStore` implementation did not exist upstream — added to support
  SONYA's Studio → future production publish workflow.

## What was dropped entirely (not ported)

- `src/decals.ts` — multi-image decal/sticker compositing (SONYA drapes a
  single operator-selected picture, not composable stickers).
- `src/dofPass.ts` — custom depth-of-field post-processing pass.
- `src/bakedPose.ts` — baked initial drape pose data.
- `OrbitControls`, `EffectComposer`/`UnrealBloomPass`/film-grain post pass,
  `RoomEnvironment` PMREM image-based lighting — dropped to keep the public
  runtime cheap (no heavy post-processing, single `renderer.render()` call
  per frame) per SONYA's performance requirements.
- The `dialkit`, `react`, `react-dom`, and `motion` npm dependencies — not
  used anywhere in this widget.

None of holocloth's code or assets were copied into SONYA's main frontend
(`index.html`, `app.js`, `auth.js`, `config.js`, `styles.css`) or backend —
the adaptation is entirely contained in `widgets/interactive-cloth/`.
