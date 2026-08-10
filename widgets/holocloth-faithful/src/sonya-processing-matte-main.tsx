import { createRoot } from 'react-dom/client';
import SonyaProcessingVariant, { type VariantConfig } from './SonyaProcessingVariant.tsx';
import type { HoloParams } from './scene.ts';

// Matte: soft, understated, low shine — folds read from fabric weave and
// occlusion rather than specular streaks. Same brand photos as main, but
// the material response is tuned all the way down so they stop reading as
// "wet foil" and start reading as dry, worn leather/satin.
const paramsDark: HoloParams = {
  performance: 'High',
  physics: { viscosity: 0.6, stiffness: 1, iterations: 14, smoothing: 0.045, grabRadius: 0.27 },
  material: {
    preset: 'Black Cloth',
    finish: 'Matte',
    baseColor: '#242426',
    holoIntensity: 0.05,
    holoScale: 8,
    bandFreq: 0.2,
    saturation: 0,
    hueShift: 0,
    sparkle: 0,
    specTint: 0.25,
    iridescence: 0,
    roughness: 0.88,
    metalness: 0.12,
    clearcoat: 0.04,
    coatRoughness: 0.9,
    sheen: 0.06,
    bump: 0.6,
    bumpTiling: 5,
  },
  images: { edit: false, useImage: true, scale: 1, rotation: 0, opacity: 1, cornerRadius: 0 },
  render: {
    background: '#0d0d0e',
    exposure: 0.5,
    environment: 0.35,
    bloom: 0,
    bloomThreshold: 1.4,
    noise: 0,
    toneMapping: 'Neutral',
    occlusion: true,
    occlusionStrength: 1.2,
    dof: false,
    dofAperture: 40,
    dofBlur: 0.04,
    dofRange: 0.3,
  },
};

const paramsLight: HoloParams = {
  ...paramsDark,
  material: { ...paramsDark.material, baseColor: '#e6e6e2', specTint: 0.15, roughness: 0.9, metalness: 0.08 },
  render: { ...paramsDark.render, background: '#eceae5', exposure: 0.85, environment: 0.3 },
};

const config: VariantConfig = {
  label: 'Матовый',
  clothScale: 1.9,
  paramsDark,
  paramsLight,
  accentLights: {
    rimA: { color: 0xd6d6d6, intensity: 0.55 },
    rimB: { color: 0x8a8a8a, intensity: 0.35 },
  },
  textureDark: '/sonya-cloth-dark.jpg',
  textureLight: '/sonya-cloth-light.jpg',
};

createRoot(document.getElementById('root')!).render(<SonyaProcessingVariant config={config} />);
