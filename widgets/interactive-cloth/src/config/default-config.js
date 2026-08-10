/**
 * Single source of truth for every tunable of the cloth widget.
 * Both the public runtime (SonyaCloth.setConfig) and the Studio operate on
 * this exact same shape — there is no separate "preview-only" config.
 */
export const CONFIG_VERSION = 1;

export function createDefaultConfig() {
  return {
    version: CONFIG_VERSION,

    texture: {
      url: '/default-cloth-texture.png',
      scale: 1,
      offsetX: 0,
      offsetY: 0,
      rotation: 0,
      fit: 'cover', // 'cover' | 'contain' | 'repeat'
    },

    cloth: {
      width: 3.4,
      height: 4.2,
      segmentsX: 40,
      segmentsY: 40,
      stiffness: 0.85,
      damping: 0.06,
      gravity: 1.4,
      wind: 0.25,
      mass: 1,
      pins: ['top-left', 'top-right'], // named pin presets, see clothPhysics.js
      maxDisplacement: 3.5,
      // 0 = no extra pre-fold pass (existing behaviour, unchanged). Chrome
      // sets this so it starts already draped from one-time gravity, then
      // runs live with cloth.gravity: 0 — see SonyaCloth.js constructor.
      settleGravity: 0,
    },

    material: {
      baseColor: '#c9403d',
      secondaryColor: '#2a1f7a',
      holoIntensity: 0.5,
      iridescence: 0.18,
      roughness: 0.55,
      metalness: 0.15,
      opacity: 1,
      glow: 0.12,
      lightIntensity: 1.1,
      // Added for the Chrome preset port from widgets/holocloth-faithful —
      // defaults match what createHoloMaterial() already hardcoded, so any
      // config that doesn't set these renders exactly as before.
      clearcoat: 0.12,
      clearcoatRoughness: 0.4,
      sheen: 0.1,
    },

    // Added for the Chrome preset port. Rim lights are off (intensity 0) by
    // default — zero visual change unless a preset (e.g. Chrome) turns them
    // on. environmentIntensity 0 means the PMREM room environment SonyaCloth
    // now always builds stays invisible until a preset raises it.
    lighting: {
      rimAColor: '#ffffff',
      rimAIntensity: 0,
      rimBColor: '#33363c',
      rimBIntensity: 0,
      environmentIntensity: 0,
      exposure: 1,
    },

    scene: {
      cameraPosition: [0, 0, 6.4],
      clothPosition: [0, 0, 0],
      clothRotation: [0, 0, 0],
      transparentBackground: true,
      scale: 1,
      safeViewportBounds: { top: 0.06, right: 0.06, bottom: 0.22, left: 0.06 },
    },

    performance: {
      desktopSegments: 40,
      mobileSegments: 18,
      maxDevicePixelRatio: 2,
      reducedMotionBehavior: 'static', // 'static' | 'slow'
    },
  };
}

export const PRESETS = {
  'SONYA Dark': {
    material: {
      baseColor: '#15131c',
      secondaryColor: '#5b3df0',
      holoIntensity: 0.75,
      iridescence: 0.65,
      roughness: 0.4,
      metalness: 0.35,
      opacity: 1,
      glow: 0.25,
      lightIntensity: 0.9,
    },
    scene: { transparentBackground: true },
  },
  'SONYA Light': {
    material: {
      baseColor: '#f2f1f7',
      secondaryColor: '#8ec9ff',
      holoIntensity: 0.35,
      iridescence: 0.3,
      roughness: 0.6,
      metalness: 0.1,
      opacity: 1,
      glow: 0.08,
      lightIntensity: 1.3,
    },
    scene: { transparentBackground: true },
  },
  Minimal: {
    material: {
      baseColor: '#2b2b2b',
      secondaryColor: '#2b2b2b',
      holoIntensity: 0,
      iridescence: 0,
      roughness: 0.85,
      metalness: 0,
      opacity: 1,
      glow: 0,
      lightIntensity: 1,
    },
    cloth: { wind: 0.08, gravity: 1.6 },
  },
  Holographic: {
    material: {
      baseColor: '#1a1a24',
      secondaryColor: '#ff8ad8',
      holoIntensity: 1.4,
      iridescence: 1,
      roughness: 0.2,
      metalness: 0.6,
      opacity: 1,
      glow: 0.5,
      lightIntensity: 1.2,
    },
    cloth: { wind: 0.4 },
  },
  // Approved processing-screen direction (2026-08-10), ported from
  // widgets/holocloth-faithful's Chrome variant — see
  // sonya-processing-chrome-main.tsx there for the source values this was
  // mapped from, and PRODUCTION_INTEGRATION_PLAN.md for how this preset is
  // meant to reach the real processing screen (not wired in yet).
  'SONYA Chrome': {
    texture: { url: '/sonya-cloth-dark.jpg', fit: 'cover', scale: 1, offsetX: 0, offsetY: 0, rotation: 0 },
    material: {
      baseColor: '#d8dde3',
      secondaryColor: '#e8ecf1',
      holoIntensity: 0,
      iridescence: 0,
      roughness: 0.16,
      metalness: 0.85,
      opacity: 1,
      glow: 0,
      lightIntensity: 0.9,
      clearcoat: 0.85,
      clearcoatRoughness: 0.06,
      sheen: 0,
    },
    lighting: {
      rimAColor: '#ffffff',
      rimAIntensity: 1.6,
      rimBColor: '#33363c',
      rimBIntensity: 0.6,
      environmentIntensity: 1.3,
      exposure: 0.6,
    },
    // cloth.width/height widened from the 3.4x4.2 portrait default so a
    // landscape viewport doesn't show black bars at the sides; scale/camera
    // tuned down from an earlier, too-zoomed-in pass to match the approved
    // reference's framing (whole sheet + its corners visible, not a
    // close-up of the weave).
    cloth: {
      width: 5.2,
      height: 4.4,
      // No pins, live gravity 0 — the approved reference is upstream
      // holocloth's zero-gravity "gel" cloth: it never sags, never falls,
      // only wind + the user's own grab move it. SONYA's hanging-curtain
      // default (pins + live gravity) kept slowly sinking over time — with
      // nothing pinning it and gravity applied every frame, it settles at
      // clampDisplacement's limit and stays there, i.e. "falls down".
      //
      // settleGravity runs once, before the first frame (see SonyaCloth.js
      // constructor's extra warmUp pass), to fold the sheet from its flat
      // start into a natural drape — same role upstream's hand-authored
      // bakedPose.ts plays, just generated live instead of pre-baked. Once
      // that pass ends, live gravity is 0, so the fold holds instead of
      // continuing to sink.
      pins: [],
      gravity: 0,
      settleGravity: 0.9,
      wind: 0.35,
      damping: 0.22,
      maxDisplacement: 3.2,
    },
    scene: { transparentBackground: true, scale: 1.7, cameraPosition: [0, 0, 7.4] },
  },
};

/** Light-theme companion for 'SONYA Chrome' — same material, brighter base/
 * texture/exposure. Not a full PRESETS entry (mergeConfig only does a
 * shallow per-group Object.assign, so partial overrides on top of 'SONYA
 * Chrome' would leave stale dark fields); apply as
 * mergeConfig(mergeConfig(base, PRESETS['SONYA Chrome']), SONYA_CHROME_LIGHT_OVERRIDES). */
export const SONYA_CHROME_LIGHT_OVERRIDES = {
  texture: { url: '/sonya-cloth-light.jpg' },
  material: { baseColor: '#eef1f4', secondaryColor: '#f5f7f9' },
  lighting: { exposure: 0.85, environmentIntensity: 1.2 },
};
