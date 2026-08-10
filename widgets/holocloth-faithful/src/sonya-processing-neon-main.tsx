import { createRoot } from 'react-dom/client';
import SonyaProcessingVariant, { type VariantConfig } from './SonyaProcessingVariant.tsx';
import type { HoloParams } from './scene.ts';

// Neon: the opposite direction from main — full rainbow diffraction, hot
// bloom, electric cyan/magenta rims. No photo texture here on purpose: a
// flat print would sit under and mute the shader; this variant is about
// the raw holographic material itself.
const paramsDark: HoloParams = {
  performance: 'High',
  physics: { viscosity: 0.6, stiffness: 1, iterations: 14, smoothing: 0.045, grabRadius: 0.27 },
  material: {
    preset: 'Holo',
    finish: 'Glossy',
    baseColor: '#07060d',
    holoIntensity: 2.6,
    holoScale: 220,
    bandFreq: 1.6,
    saturation: 1,
    hueShift: 0.55,
    sparkle: 1.3,
    specTint: 0.5,
    iridescence: 1,
    roughness: 0.15,
    metalness: 0.9,
    clearcoat: 0.7,
    coatRoughness: 0.05,
    sheen: 0.3,
    bump: 1.4,
    bumpTiling: 4,
  },
  images: { edit: false, useImage: false, scale: 0.35, rotation: 0, opacity: 1, cornerRadius: 0 },
  render: {
    background: '#050308',
    exposure: 0.65,
    environment: 0.4,
    bloom: 0.75,
    bloomThreshold: 0.55,
    noise: 0,
    toneMapping: 'Neutral',
    occlusion: true,
    occlusionStrength: 1,
    dof: false,
    dofAperture: 40,
    dofBlur: 0.04,
    dofRange: 0.3,
  },
};

// Neon rarely reads well on a light ground, so light theme here is a
// pastel/opal cousin (softer saturation, gentler bloom) rather than a true
// inversion — still comparable as "the light mode of this variant".
const paramsLight: HoloParams = {
  ...paramsDark,
  material: {
    ...paramsDark.material,
    baseColor: '#f1eef8',
    holoIntensity: 1.2,
    saturation: 0.7,
    sparkle: 0.8,
    iridescence: 0.8,
    roughness: 0.25,
    metalness: 0.5,
    clearcoat: 0.5,
    coatRoughness: 0.1,
    sheen: 0.2,
    bump: 0.8,
  },
  render: { ...paramsDark.render, background: '#f4f2fa', exposure: 0.85, bloom: 0.3, bloomThreshold: 0.9 },
};

const config: VariantConfig = {
  label: 'Неоновый',
  clothScale: 1.9,
  paramsDark,
  paramsLight,
  accentLights: {
    rimA: { color: 0x00eaff, intensity: 1.5 },
    rimB: { color: 0xd400ff, intensity: 1.3 },
  },
  textureDark: null,
  textureLight: null,
};

createRoot(document.getElementById('root')!).render(<SonyaProcessingVariant config={config} />);
