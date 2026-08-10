import { createRoot } from 'react-dom/client';
import SonyaProcessingVariant, { type VariantConfig } from './SonyaProcessingVariant.tsx';
import type { HoloParams } from './scene.ts';

// Chrome — SONYA's pick, now with her brand photos draped on. Dialed back
// from the original photo-less mirror pass (roughness .03/metalness 1,
// which would have flattened the SONYA print into an unreadable reflection)
// just enough for the embossed logo/weave to stay legible, while keeping
// it clearly more reflective than the "main" variant — still the same
// silver-key/graphite-fill rim pair and strong environment IBL that sold
// the metal in the photo-less pass.
const paramsDark: HoloParams = {
  performance: 'High',
  physics: { viscosity: 0.6, stiffness: 1, iterations: 14, smoothing: 0.045, grabRadius: 0.27 },
  material: {
    preset: 'Chrome',
    finish: 'Glossy',
    baseColor: '#d8dde3',
    holoIntensity: 0,
    holoScale: 8,
    bandFreq: 0.2,
    saturation: 0,
    hueShift: 0,
    sparkle: 0.15,
    specTint: 0,
    iridescence: 0,
    roughness: 0.16,
    metalness: 0.85,
    clearcoat: 0.85,
    coatRoughness: 0.06,
    sheen: 0,
    bump: 0.15,
    bumpTiling: 3,
  },
  images: { edit: false, useImage: true, scale: 1, rotation: 0, opacity: 1, cornerRadius: 0 },
  render: {
    background: '#050505',
    exposure: 0.6,
    environment: 1.3,
    bloom: 0.05,
    bloomThreshold: 1.3,
    noise: 0,
    toneMapping: 'Neutral',
    occlusion: true,
    occlusionStrength: 0.8,
    dof: false,
    dofAperture: 40,
    dofBlur: 0.04,
    dofRange: 0.3,
  },
};

const paramsLight: HoloParams = {
  ...paramsDark,
  material: { ...paramsDark.material, baseColor: '#eef1f4' },
  render: { ...paramsDark.render, background: '#e9ebee', exposure: 0.85, environment: 1.2 },
};

const config: VariantConfig = {
  label: 'Хром',
  clothScale: 1.9,
  paramsDark,
  paramsLight,
  accentLights: {
    rimA: { color: 0xffffff, intensity: 1.6 },
    rimB: { color: 0x33363c, intensity: 0.6 },
  },
  textureDark: '/sonya-cloth-dark.jpg',
  textureLight: '/sonya-cloth-light.jpg',
  textScrim: true,
};

createRoot(document.getElementById('root')!).render(<SonyaProcessingVariant config={config} />);
