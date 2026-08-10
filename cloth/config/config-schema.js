import { CONFIG_VERSION } from './default-config.js';

/**
 * Minimal shape + range validation. Used by preset import (Studio) and by
 * SonyaCloth.setConfig (public runtime) so a malformed config never reaches
 * the physics/material code.
 */

const range = (min, max) => (v) => typeof v === 'number' && Number.isFinite(v) && v >= min && v <= max;
const isString = (v) => typeof v === 'string';
const isBool = (v) => typeof v === 'boolean';
const isVec3 = (v) => Array.isArray(v) && v.length === 3 && v.every((n) => typeof n === 'number');
const isHexColor = (v) => isString(v) && /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(v);
const oneOf = (...options) => (v) => options.includes(v);

const SCHEMA = {
  texture: {
    url: isString,
    scale: range(0.05, 10),
    offsetX: range(-10, 10),
    offsetY: range(-10, 10),
    rotation: range(-360, 360),
    fit: oneOf('cover', 'contain', 'repeat'),
  },
  cloth: {
    width: range(0.2, 20),
    height: range(0.2, 20),
    segmentsX: range(4, 120),
    segmentsY: range(4, 120),
    stiffness: range(0, 1),
    damping: range(0, 0.6),
    gravity: range(0, 10),
    wind: range(0, 5),
    mass: range(0.05, 20),
    pins: (v) => Array.isArray(v) && v.every(isString),
    maxDisplacement: range(0.1, 50),
    settleGravity: range(0, 10),
  },
  material: {
    baseColor: isHexColor,
    secondaryColor: isHexColor,
    holoIntensity: range(0, 3),
    iridescence: range(0, 1),
    roughness: range(0, 1),
    metalness: range(0, 1),
    opacity: range(0, 1),
    glow: range(0, 2),
    lightIntensity: range(0, 4),
    clearcoat: range(0, 1),
    clearcoatRoughness: range(0, 1),
    sheen: range(0, 1),
  },
  lighting: {
    rimAColor: isHexColor,
    rimAIntensity: range(0, 3),
    rimBColor: isHexColor,
    rimBIntensity: range(0, 3),
    environmentIntensity: range(0, 3),
    exposure: range(0.1, 3),
  },
  scene: {
    cameraPosition: isVec3,
    clothPosition: isVec3,
    clothRotation: isVec3,
    transparentBackground: isBool,
    scale: range(0.1, 5),
    safeViewportBounds: (v) =>
      v && typeof v === 'object' && ['top', 'right', 'bottom', 'left'].every((k) => range(0, 0.9)(v[k])),
  },
  performance: {
    desktopSegments: range(4, 120),
    mobileSegments: range(4, 80),
    maxDevicePixelRatio: range(1, 3),
    reducedMotionBehavior: oneOf('static', 'slow'),
  },
};

/** @returns {{ valid: boolean, errors: string[] }} */
export function validateConfig(config) {
  const errors = [];
  if (!config || typeof config !== 'object') {
    return { valid: false, errors: ['config must be an object'] };
  }
  if (config.version !== CONFIG_VERSION) {
    errors.push(`unsupported config version: ${config.version} (expected ${CONFIG_VERSION})`);
  }
  for (const [group, fields] of Object.entries(SCHEMA)) {
    const value = config[group];
    if (!value || typeof value !== 'object') {
      errors.push(`missing config group "${group}"`);
      continue;
    }
    for (const [key, check] of Object.entries(fields)) {
      if (!(key in value)) {
        errors.push(`missing "${group}.${key}"`);
        continue;
      }
      if (!check(value[key])) {
        errors.push(`invalid "${group}.${key}": ${JSON.stringify(value[key])}`);
      }
    }
  }
  return { valid: errors.length === 0, errors };
}

/** Deep-merge a partial config (e.g. a preset) onto a full base config. */
export function mergeConfig(base, partial) {
  const out = structuredClone(base);
  for (const [group, fields] of Object.entries(partial || {})) {
    if (!out[group] || typeof out[group] !== 'object') {
      out[group] = fields;
      continue;
    }
    Object.assign(out[group], fields);
  }
  return out;
}
