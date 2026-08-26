import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.180.0/+esm';
import { RoomEnvironment } from 'https://cdn.jsdelivr.net/npm/three@0.180.0/examples/jsm/environments/RoomEnvironment.js/+esm';
import { ClothSim } from './clothPhysics.js';
import { createHoloMaterial, applyMaterialConfig } from './shaders/holoMaterial.js';
import { attachInteraction } from './interaction.js';
import { createDefaultConfig } from './config/default-config.js';
import { mergeConfig, validateConfig } from './config/config-schema.js';

/**
 * Scene/render-loop/lifecycle shell. Structurally follows holocloth's
 * `src/scene.ts` (`HoloApp`) for the renderer setup, resize handling via
 * ResizeObserver, and the dispose() teardown order — see
 * third_party/holocloth/NOTICE.md. OrbitControls and EffectComposer
 * (bloom/DOF/grain post-processing) stay dropped: SONYA wants a cheap,
 * always-on background widget (single `renderer.render()` call per frame),
 * not an authoring tool.
 *
 * The PMREM room-environment IBL that NOTICE.md originally said was
 * dropped is back (2026-08-10): the approved "SONYA Chrome" preset (see
 * default-config.js) needs a reflection source for its high-metalness/
 * low-roughness material to read as metal at all — without an env map a
 * metallic MeshPhysicalMaterial just goes dark. It's a one-time cost at
 * construction (PMREMGenerator.fromScene runs once, not per frame), so the
 * per-frame render loop is still a single `renderer.render()` call.
 * `lighting.environmentIntensity` defaults to 0, so any config that doesn't
 * opt in (i.e. everything except Chrome) renders exactly as before.
 *
 * Production port (2026-08-10): `three` is loaded from the pinned jsDelivr
 * `+esm` URL (same URL the RoomEnvironment addon's own bare `from 'three'`
 * resolves to internally) instead of the bare `'three'` specifier the
 * widgets/interactive-cloth prototype used under Vite — SONYA's production
 * frontend has no bundler/import map. Using the identical URL for both
 * guarantees a single shared three.js module instance (see
 * cloth-processing.js for why that matters). The `import.meta.env.DEV`
 * debug hook from the prototype was dropped — that's a Vite-only global,
 * undefined (and a TypeError) in a plain browser module.
 */
class ClothInstance {
  constructor(host, options) {
    this.host = host;
    const merged = mergeConfig(createDefaultConfig(), options.config || {});
    // setConfig() (below) validates every later config change and rejects
    // an invalid one outright — mount()'s initial config skipped that
    // entirely, so a malformed options.config (typo'd color, NaN/
    // out-of-range numeric field) flowed straight into buildCloth()/
    // applyMaterial()/the physics loop unchecked, unlike the identical
    // mistake caught by a later setConfig() call. There's no caller to
    // return `false` to here (mount() doesn't refuse to construct), so an
    // invalid initial config falls back to the known-good default instead
    // of running with broken material/physics values.
    const { valid, errors } = validateConfig(merged);
    if (!valid) {
      console.warn('[SonyaCloth] mount() received an invalid config, falling back to defaults:', errors);
      this.config = createDefaultConfig();
    } else {
      this.config = merged;
    }
    this.disposed = false;
    this.manuallyPaused = false;
    this.hiddenPaused = false;
    this.elapsed = 0;
    this.reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;

    const width = host.clientWidth || window.innerWidth;
    const height = host.clientHeight || window.innerHeight;

    let renderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'low-power' });
    } catch {
      renderer = null;
    }
    if (!renderer || !renderer.getContext()) {
      this.webglUnavailable = true;
      host.innerHTML = '';
      const fallback = document.createElement('div');
      fallback.className = 'sonya-cloth-fallback';
      if (this.config.texture.url) {
        fallback.style.backgroundImage = `url(${this.config.texture.url})`;
      }
      host.appendChild(fallback);
      this.fallbackEl = fallback;
      return;
    }

    this.renderer = renderer;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, this.config.performance.maxDevicePixelRatio));
    renderer.setSize(width, height);
    renderer.setClearColor(0x000000, 0);
    // NeutralToneMapping: needed for the Chrome preset's metal highlights to
    // roll off smoothly instead of hard-clipping (three's default
    // NoToneMapping). Global to the renderer, so this affects every config,
    // not just Chrome — a mild, industry-standard curve, low risk for the
    // existing presets too.
    renderer.toneMapping = THREE.NeutralToneMapping;
    renderer.toneMappingExposure = this.config.lighting.exposure;
    host.appendChild(renderer.domElement);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 100);
    this.camera.position.set(...this.config.scene.cameraPosition);
    this.camera.lookAt(0, 0, 0);

    const key = new THREE.DirectionalLight(0xffffff, 1.1);
    key.position.set(1.5, 3, 4);
    const fill = new THREE.AmbientLight(0xffffff, 0.6);
    // Silver-key / graphite-fill rim pair, ported from holocloth-faithful's
    // Chrome variant (same positions as its rimA/rimB). Off by default
    // (rimAIntensity/rimBIntensity = 0 in default-config.js).
    const rimA = new THREE.DirectionalLight(new THREE.Color(this.config.lighting.rimAColor), this.config.lighting.rimAIntensity);
    rimA.position.set(-4, 2.5, -3);
    const rimB = new THREE.DirectionalLight(new THREE.Color(this.config.lighting.rimBColor), this.config.lighting.rimBIntensity);
    rimB.position.set(4.5, -1.5, -2.5);
    this.scene.add(key, fill, rimA, rimB);
    this.keyLight = key;
    this.fillLight = fill;
    this.rimA = rimA;
    this.rimB = rimB;

    // One-time PMREM room environment for metal reflections — see the
    // class-level comment for why this came back. `environmentIntensity`
    // (via material.envMapIntensity in applyMaterial()) keeps it invisible
    // for every config except Chrome.
    const pmrem = new THREE.PMREMGenerator(renderer);
    this.scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    pmrem.dispose();

    this.textureLoader = new THREE.TextureLoader();
    this.currentTexture = null;
    const holo = createHoloMaterial(this._placeholderTexture());
    this.material = holo.material;
    this.uniforms = holo.uniforms;

    this.clothMesh = new THREE.Mesh(undefined, this.material);
    this.clothMesh.frustumCulled = false;
    this.clothMesh.position.set(...this.config.scene.clothPosition);
    this.clothMesh.rotation.set(...this.config.scene.clothRotation);
    this.scene.add(this.clothMesh);

    this.buildCloth();
    this.applyMaterial();
    this.applySceneSettings();
    this.loadTexture(this.config.texture.url);

    // One-time pre-fold, before the live loop starts: run the physics with
    // settleGravity (instead of the live cloth.gravity, which may be 0) so
    // configs like Chrome — no pins, no live gravity, so nothing pulls it
    // out of shape over time — still start already draped instead of dead
    // flat. No-op for any config that leaves settleGravity at 0 (the
    // default), so existing presets render exactly as before.
    if (this.config.cloth.settleGravity) {
      this.sim.warmUp({ ...this.config.cloth, gravity: this.config.cloth.settleGravity });
      this.syncGeometry();
    }

    this.interaction = attachInteraction({
      renderer: this.renderer,
      camera: this.camera,
      clothMesh: this.clothMesh,
      sim: this.sim,
      getGrabRadius: () => Math.max(this.config.cloth.width, this.config.cloth.height) * 0.2,
    });

    this.resizeObserver = new ResizeObserver(() => this.onResize());
    this.resizeObserver.observe(host);

    this.onVisibilityChange = () => {
      this.hiddenPaused = document.hidden;
      this.syncRunning();
    };
    document.addEventListener('visibilitychange', this.onVisibilityChange);

    this.clock = new THREE.Clock();
    this.running = false;
    this.syncRunning();

    if (this.reducedMotion && this.config.performance.reducedMotionBehavior === 'static') {
      this.sim.warmUp({ ...this.config.cloth });
      this.syncGeometry();
      this.renderOnce();
    }
  }

  _placeholderTexture() {
    const tex = new THREE.Texture();
    tex.colorSpace = THREE.SRGBColorSpace;
    return tex;
  }

  buildCloth() {
    const c = this.config.cloth;
    const isMobile = window.matchMedia?.('(pointer: coarse)').matches ?? false;
    const segBudget = isMobile ? this.config.performance.mobileSegments : this.config.performance.desktopSegments;
    const segX = Math.max(4, Math.min(c.segmentsX, segBudget));
    const segY = Math.max(4, Math.min(c.segmentsY, segBudget));
    this.sim = new ClothSim(c.width, c.height, segX, segY, c.pins);
    const geo = new THREE.PlaneGeometry(c.width, c.height, segX, segY);
    const posAttr = new THREE.BufferAttribute(this.sim.positions, 3);
    posAttr.setUsage(THREE.DynamicDrawUsage);
    geo.setAttribute('position', posAttr);
    this.cavityAttr = new THREE.BufferAttribute(new Float32Array(this.sim.count), 1);
    this.cavityAttr.setUsage(THREE.DynamicDrawUsage);
    geo.setAttribute('aCavity', this.cavityAttr);
    geo.computeVertexNormals();
    const old = this.clothMesh.geometry;
    this.clothMesh.geometry = geo;
    if (old) old.dispose();
  }

  syncGeometry() {
    this.clothMesh.geometry.attributes.position.needsUpdate = true;
    this.clothMesh.geometry.computeVertexNormals();
  }

  applyMaterial() {
    applyMaterialConfig(this.material, this.uniforms, this.config.material);
    this.keyLight.intensity = this.config.material.lightIntensity;

    const l = this.config.lighting;
    this.material.envMapIntensity = l.environmentIntensity;
    this.renderer.toneMappingExposure = l.exposure;
    this.rimA.color.set(l.rimAColor);
    this.rimA.intensity = l.rimAIntensity;
    this.rimB.color.set(l.rimBColor);
    this.rimB.intensity = l.rimBIntensity;
  }

  applySceneSettings() {
    const s = this.config.scene;
    this.camera.position.set(...s.cameraPosition);
    this.camera.lookAt(0, 0, 0);
    this.clothMesh.position.set(...s.clothPosition);
    this.clothMesh.rotation.set(...s.clothRotation);
    this.clothMesh.scale.setScalar(s.scale);
    this.renderer.setClearColor(0x000000, s.transparentBackground ? 0 : 1);
  }

  loadTexture(url) {
    if (!url) return;
    this.textureLoader.load(url, (tex) => {
      if (this.disposed) { tex.dispose(); return; }
      tex.colorSpace = THREE.SRGBColorSpace;
      this.applyTextureFit(tex);
      const old = this.currentTexture;
      this.currentTexture = tex;
      this.uniforms.uSurfaceMap.value = tex;
      if (old) old.dispose();
    });
  }

  applyTextureFit(tex) {
    const t = this.config.texture;
    tex.center.set(0.5, 0.5);
    tex.rotation = (t.rotation * Math.PI) / 180;
    if (t.fit === 'repeat') {
      tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
      tex.repeat.set(t.scale, t.scale);
      tex.offset.set(t.offsetX, t.offsetY);
      return;
    }
    // cover/contain: account for image aspect vs cloth aspect, like CSS background-size
    const img = tex.image;
    const clothAspect = this.config.cloth.width / this.config.cloth.height;
    const imgAspect = img ? img.width / img.height : clothAspect;
    let rx = 1, ry = 1;
    if (t.fit === 'cover') {
      if (imgAspect > clothAspect) { rx = clothAspect / imgAspect; ry = 1; }
      else { rx = 1; ry = imgAspect / clothAspect; }
    } else {
      if (imgAspect > clothAspect) { rx = 1; ry = imgAspect / clothAspect; }
      else { rx = clothAspect / imgAspect; ry = 1; }
    }
    tex.wrapS = tex.wrapT = THREE.ClampToEdgeWrapping;
    tex.repeat.set(rx / t.scale, ry / t.scale);
    tex.offset.set(t.offsetX + (1 - rx / t.scale) / 2, t.offsetY + (1 - ry / t.scale) / 2);
  }

  onResize() {
    const width = this.host.clientWidth || window.innerWidth;
    const height = this.host.clientHeight || window.innerHeight;
    if (width === 0 || height === 0) return;
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(width, height);
  }

  syncRunning() {
    const shouldRun = !this.manuallyPaused && !this.hiddenPaused;
    if (shouldRun === this.running) return;
    this.running = shouldRun;
    if (shouldRun) {
      this.clock.start();
      this.renderer.setAnimationLoop(this.tick);
    } else {
      this.renderer.setAnimationLoop(null);
    }
  }

  tick = () => {
    if (this.disposed) return;
    let dt = this.clock.getDelta();
    // 'static' short-circuits to a single warmed-up frame and never starts
    // this loop at all (see syncRunning()/the constructor's warmUp branch)
    // — 'slow' was a valid schema/Studio option with no matching runtime
    // behavior here, so picking it did nothing: full-speed animation,
    // identical to no reduced-motion preference at all.
    if (this.reducedMotion && this.config.performance.reducedMotionBehavior === 'slow') dt *= 0.35;
    this.elapsed += dt;
    this.sim.step(dt, this.config.cloth, this.elapsed);
    this.syncGeometry();
    this.sim.computeCavity(this.clothMesh.geometry.attributes.normal.array, this.cavityAttr.array);
    this.cavityAttr.needsUpdate = true;
    this.renderer.render(this.scene, this.camera);
  };

  renderOnce() {
    this.renderer?.render(this.scene, this.camera);
  }

  setTexture(url) {
    if (this.webglUnavailable) {
      this.config.texture.url = url;
      if (this.fallbackEl) this.fallbackEl.style.backgroundImage = `url(${url})`;
      return;
    }
    this.config.texture.url = url;
    this.loadTexture(url);
  }

  setConfig(partial) {
    if (this.webglUnavailable) {
      // No renderer/scene to reconfigure in the fallback path — still merge
      // + validate so a later setTexture()/inspection sees a consistent
      // config, same contract as the WebGL path's return value.
      const next = mergeConfig(this.config, partial);
      const { valid, errors } = validateConfig(next);
      if (!valid) {
        console.warn('[SonyaCloth] setConfig rejected invalid config:', errors);
        return false;
      }
      const textureChanged = next.texture.url !== this.config.texture.url;
      this.config = next;
      if (textureChanged) this.setTexture(next.texture.url);
      return true;
    }
    const next = mergeConfig(this.config, partial);
    const { valid, errors } = validateConfig(next);
    if (!valid) {
      console.warn('[SonyaCloth] setConfig rejected invalid config:', errors);
      return false;
    }
    const clothShapeChanged =
      next.cloth.width !== this.config.cloth.width ||
      next.cloth.height !== this.config.cloth.height ||
      next.cloth.segmentsX !== this.config.cloth.segmentsX ||
      next.cloth.segmentsY !== this.config.cloth.segmentsY ||
      JSON.stringify(next.cloth.pins) !== JSON.stringify(this.config.cloth.pins);
    const textureChanged = next.texture.url !== this.config.texture.url;
    const textureFitChanged = JSON.stringify(next.texture) !== JSON.stringify(this.config.texture);
    this.config = next;
    if (clothShapeChanged) this.buildCloth();
    this.applyMaterial();
    this.applySceneSettings();
    if (textureChanged) this.loadTexture(next.texture.url);
    else if (textureFitChanged && this.currentTexture) this.applyTextureFit(this.currentTexture);
    if (this.reducedMotion && this.config.performance.reducedMotionBehavior === 'static') this.renderOnce();
    return true;
  }

  pause() {
    if (this.webglUnavailable) return;
    this.manuallyPaused = true;
    this.syncRunning();
  }

  resume() {
    if (this.webglUnavailable) return;
    this.manuallyPaused = false;
    this.syncRunning();
  }

  dispose() {
    this.disposed = true;
    if (this.webglUnavailable) {
      this.fallbackEl?.remove();
      return;
    }
    this.renderer.setAnimationLoop(null);
    this.resizeObserver.disconnect();
    document.removeEventListener('visibilitychange', this.onVisibilityChange);
    this.interaction.dispose();
    this.clothMesh.geometry.dispose();
    this.material.dispose();
    if (this.currentTexture) this.currentTexture.dispose();
    this.material.roughnessMap?.dispose();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}

let activeInstance = null;
let activeHost = null;

export const SonyaCloth = {
  /**
   * @param {HTMLElement} element
   * @param {{ config?: object }} [options]
   */
  mount(element, options = {}) {
    if (!element) throw new Error('[SonyaCloth] mount() requires a host element');
    if (activeInstance) {
      console.warn('[SonyaCloth] already mounted — call destroy() before mounting again');
      return;
    }
    if (element.dataset.sonyaClothMounted === '1') {
      console.warn('[SonyaCloth] this element is already marked as mounted');
      return;
    }
    element.dataset.sonyaClothMounted = '1';
    activeHost = element;
    activeInstance = new ClothInstance(element, options);
  },

  setTexture(url) {
    activeInstance?.setTexture(url);
  },

  setConfig(config) {
    return activeInstance?.setConfig(config) ?? false;
  },

  pause() {
    activeInstance?.pause();
  },

  resume() {
    activeInstance?.resume();
  },

  destroy() {
    if (!activeInstance) return;
    activeInstance.dispose();
    if (activeHost) delete activeHost.dataset.sonyaClothMounted;
    activeInstance = null;
    activeHost = null;
  },
};
