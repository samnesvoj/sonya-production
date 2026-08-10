import * as THREE from 'three';
import { makeGrainRoughnessMap } from './grainRoughness.js';

/**
 * Adapted from holocloth's `src/holoMaterial.ts` (`createHoloMaterial`).
 * See third_party/holocloth/NOTICE.md for full attribution. The
 * MeshPhysicalMaterial base, the onBeforeCompile injection points
 * (emissivemap_fragment / aomap_fragment), the flake-hash/HSV helpers, and
 * the rounded-corner SDF cutout are kept from upstream. The uniform surface
 * was simplified and remapped onto SONYA's smaller material config
 * (baseColor, secondaryColor, holoIntensity, iridescence, roughness,
 * metalness, opacity, glow, lightIntensity) instead of upstream's larger
 * DialKit-driven parameter set (bandFreq/saturation/hueShift/sparkle/
 * specTint/etc, each its own slider) — those are now derived from
 * secondaryColor + iridescence rather than exposed as separate controls.
 * Bump/normal-map support was dropped (not part of SONYA's material spec).
 */
export function createHoloMaterial(surfaceTexture) {
  const roughnessMap = makeGrainRoughnessMap();

  const material = new THREE.MeshPhysicalMaterial({
    color: new THREE.Color('#c9403d'),
    metalness: 0.15,
    roughness: 0.55,
    roughnessMap,
    clearcoat: 0.12,
    clearcoatRoughness: 0.4,
    sheen: 0.1,
    sheenColor: new THREE.Color('#ffffff'),
    iridescence: 0.18,
    iridescenceIOR: 1.35,
    iridescenceThicknessRange: [120, 480],
    transparent: true,
    side: THREE.DoubleSide,
  });

  const uniforms = {
    uHoloIntensity: { value: 0.6 },
    uHueShift: { value: 0.0 },
    uSaturation: { value: 0.8 },
    uBandFreq: { value: 2.4 },
    uSparkle: { value: 0.5 },
    uGlow: { value: 0.15 },
    uSurfaceMap: { value: surfaceTexture },
    uSurfaceOpacity: { value: 1.0 },
    uCavityAmount: { value: 0.5 },
  };

  material.alphaToCoverage = true;

  material.onBeforeCompile = (shader) => {
    Object.assign(shader.uniforms, uniforms);

    shader.vertexShader =
      'varying vec2 vHoloUv;\nattribute float aCavity;\nvarying float vCavity;\n' +
      shader.vertexShader.replace(
        '#include <uv_vertex>',
        '#include <uv_vertex>\n\tvHoloUv = uv;\n\tvCavity = aCavity;',
      );

    shader.fragmentShader =
      /* glsl */ `
      varying vec2 vHoloUv;
      uniform float uHoloIntensity;
      uniform float uHueShift;
      uniform float uSaturation;
      uniform float uBandFreq;
      uniform float uSparkle;
      uniform float uGlow;
      uniform sampler2D uSurfaceMap;
      uniform float uSurfaceOpacity;
      varying float vCavity;
      uniform float uCavityAmount;

      float holoHash(vec2 p) {
        vec3 p3 = fract(vec3(p.xyx) * 0.1031);
        p3 += dot(p3, p3.yzx + 33.33);
        return fract((p3.x + p3.y) * p3.z);
      }

      vec3 holoHsv2rgb(vec3 c) {
        vec3 rgb = clamp(abs(mod(c.x * 6.0 + vec3(0.0, 4.0, 2.0), 6.0) - 3.0) - 1.0, 0.0, 1.0);
        rgb = rgb * rgb * (3.0 - 2.0 * rgb);
        return c.z * mix(vec3(1.0), rgb, c.y);
      }
      ` +
      shader.fragmentShader
        .replace(
          '#include <emissivemap_fragment>',
          /* glsl */ `#include <emissivemap_fragment>
          {
            vec4 surf = texture2D(uSurfaceMap, vHoloUv);
            float surfA = surf.a * uSurfaceOpacity;
            diffuseColor.rgb = mix(diffuseColor.rgb, surf.rgb, surfA);

            vec3 hView = normalize(vViewPosition);
            float facing = clamp(abs(dot(normal, hView)), 0.0, 1.0);
            float fres = pow(1.0 - facing, 1.5);

            vec2 cellUv = vHoloUv * 90.0;
            vec2 cellId = floor(cellUv);
            float rnd = holoHash(cellId);
            float rnd2 = holoHash(cellId + 71.7);

            float hue = fract(uHueShift + facing * uBandFreq + rnd * 0.06);
            vec3 rainbow = holoHsv2rgb(vec3(hue, uSaturation, 1.0));
            float flake = 0.82 + 0.18 * rnd2;
            float gate = fract(rnd2 * 13.7 + facing * 6.0);
            float glint = smoothstep(1.0 - 0.012 * uSparkle, 1.0, gate) * 5.0;

            float energy = (0.22 + 0.78 * fres) * flake;
            float holoAO = 1.0 - uCavityAmount * vCavity * 0.8;
            totalEmissiveRadiance += rainbow * uHoloIntensity * (energy * 0.55 + glint * fres * 0.5) * holoAO;
            totalEmissiveRadiance += rainbow * uGlow * 0.5;
          }`,
        )
        .replace(
          '#include <aomap_fragment>',
          /* glsl */ `#include <aomap_fragment>
          {
            float cavityAO = 1.0 - uCavityAmount * vCavity;
            reflectedLight.indirectDiffuse *= cavityAO;
            #if defined( USE_CLEARCOAT )
              clearcoatSpecularIndirect *= cavityAO;
            #endif
            #if defined( USE_SHEEN )
              sheenSpecularIndirect *= cavityAO;
            #endif
          }`,
        );
  };

  return { material, uniforms };
}

/**
 * Apply SONYA's material config onto the holo material + its shader
 * uniforms. `secondaryColor`'s hue/saturation drive the holographic sweep
 * so operators get one intuitive color control instead of upstream's many
 * separate band/hue/saturation sliders.
 */
export function applyMaterialConfig(material, uniforms, materialConfig) {
  material.color.set(materialConfig.baseColor);
  material.roughness = materialConfig.roughness;
  material.metalness = materialConfig.metalness;
  material.iridescence = materialConfig.iridescence;
  material.opacity = materialConfig.opacity;
  // clearcoat/clearcoatRoughness/sheen: added for the Chrome preset port
  // (widgets/holocloth-faithful) — previously hardcoded at construction
  // time in createHoloMaterial() and never updated per-config.
  material.clearcoat = materialConfig.clearcoat;
  material.clearcoatRoughness = materialConfig.clearcoatRoughness;
  material.sheen = materialConfig.sheen;
  material.sheenColor.set(materialConfig.secondaryColor).lerp(new THREE.Color('#ffffff'), 0.5);

  const secondary = new THREE.Color(materialConfig.secondaryColor);
  const hsl = { h: 0, s: 0, l: 0 };
  secondary.getHSL(hsl);

  uniforms.uHoloIntensity.value = materialConfig.holoIntensity;
  uniforms.uHueShift.value = hsl.h;
  uniforms.uSaturation.value = Math.max(0.4, hsl.s);
  uniforms.uSparkle.value = 0.3 + materialConfig.iridescence * 0.7;
  uniforms.uGlow.value = materialConfig.glow;
  uniforms.uSurfaceOpacity.value = materialConfig.opacity;
}
