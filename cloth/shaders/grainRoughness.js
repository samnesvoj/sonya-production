import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.180.0/+esm';

/**
 * Adapted from holocloth's `src/textures.ts` (`makeGrainRoughnessMap`).
 * See third_party/holocloth/NOTICE.md. Unchanged besides module wrapping.
 */
function mulberry32(seed) {
  return () => {
    seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function makeGrainRoughnessMap(size = 256, seed = 4242) {
  const rand = mulberry32(seed);
  const canvas = document.createElement('canvas');
  canvas.width = size; canvas.height = size;
  const ctx = canvas.getContext('2d');
  const img = ctx.createImageData(size, size);
  const data = img.data;
  for (let i = 0; i < size * size; i++) {
    const v = Math.round(215 + (rand() - 0.5) * 70);
    data[i * 4] = v; data[i * 4 + 1] = v; data[i * 4 + 2] = v; data[i * 4 + 3] = 255;
  }
  ctx.putImageData(img, 0, 0);
  const tex = new THREE.CanvasTexture(canvas);
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.repeat.set(4, 4);
  tex.colorSpace = THREE.NoColorSpace;
  return tex;
}
