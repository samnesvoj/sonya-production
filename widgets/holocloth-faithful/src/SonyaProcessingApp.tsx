import { useEffect, useRef, useState } from 'react';
import { HoloApp, type HoloParams } from './scene.ts';
import { mountAmbientBackground } from './sonyaAmbientBg.ts';
import './sonyaProcessingOverlay.css';

/**
 * SONYA addition, not upstream. Full-viewport composite for the processing
 * screen pitch: an ambient canvas backdrop (sonyaAmbientBg.ts — animated,
 * themed dark/light, with a light dose of the real site's cinema-overlay
 * tint/gradient/vignette layered on top) behind a transparent, full-screen,
 * enlarged HoloApp cloth (HoloApp.setLiveTransparent + setClothScale), with
 * the real processing-screen title/status/progress bar on top
 * (sonyaProcessingOverlay.css — copied from styles.css's .app-container.v2
 * rules).
 *
 * Cloth texture: SONYA's own two brand photos (public/sonya-cloth-light.jpg,
 * public/sonya-cloth-dark.jpg), swapped by theme.
 */

const CLOTH_SCALE = 1.9;

// "Black Cloth" preset (App.tsx PRESET_VALUES) with the SONYA deltas from
// the design-pitch spec table: cooler/glassier surface, brand-exact
// background, grain and bloom pulled down to match the video backdrop's
// own (grain-free, near-flat) treatment.
const SONYA_PARAMS_DARK: HoloParams = {
  performance: 'High',
  physics: {
    viscosity: 0.6,
    stiffness: 1,
    iterations: 14,
    smoothing: 0.045,
    grabRadius: 0.27,
  },
  material: {
    preset: 'Black Cloth',
    finish: 'Satin',
    baseColor: '#0a0a0e',
    holoIntensity: 0.15,
    holoScale: 8,
    bandFreq: 0.2,
    saturation: 0,
    hueShift: 0,
    sparkle: 0,
    specTint: 0.82,
    iridescence: 0,
    roughness: 0.38,
    metalness: 0.58,
    clearcoat: 0.55,
    coatRoughness: 0.12,
    sheen: 0.14,
    bump: 0.4,
    bumpTiling: 5,
  },
  images: {
    edit: false,
    useImage: true,
    scale: 1,
    rotation: 0,
    opacity: 1,
    cornerRadius: 0,
  },
  render: {
    background: '#060608',
    exposure: 0.5,
    environment: 0.73,
    bloom: 0.02,
    bloomThreshold: 1.41,
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

// Light theme: muted white/grey (not the old warm-cream v1 tokens) per
// SONYA's "бело-серую, приглушенное" direction, to match the ambient
// background's own neutral-grey palette.
const SONYA_PARAMS_LIGHT: HoloParams = {
  ...SONYA_PARAMS_DARK,
  material: {
    ...SONYA_PARAMS_DARK.material,
    baseColor: '#e4e5e8',
    specTint: 0.4,
    roughness: 0.42,
    metalness: 0.22,
    clearcoat: 0.4,
    coatRoughness: 0.18,
    sheen: 0.18,
  },
  render: {
    ...SONYA_PARAMS_DARK.render,
    background: '#eceeef',
    exposure: 0.95,
  },
};

const TEXTURE_BY_THEME = {
  dark: '/sonya-cloth-dark.jpg',
  light: '/sonya-cloth-light.jpg',
} as const;

type Theme = 'dark' | 'light';

function loadImage(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error(`${url}: failed to load`));
    img.src = url;
  });
}

export default function SonyaProcessingApp() {
  const bgHostRef = useRef<HTMLDivElement>(null);
  const clothHostRef = useRef<HTMLDivElement>(null);
  const appRef = useRef<HoloApp | null>(null);
  const bgRef = useRef<ReturnType<typeof mountAmbientBackground> | null>(null);
  const textureCache = useRef<Partial<Record<Theme, HTMLImageElement>>>({});
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [progress, setProgress] = useState(6);
  const [theme, setTheme] = useState<Theme>('dark');

  useEffect(() => {
    if (!bgHostRef.current) return;
    const bg = mountAmbientBackground(bgHostRef.current);
    bgRef.current = bg;
    return () => bg.destroy();
  }, []);

  useEffect(() => {
    if (!clothHostRef.current) return;
    const app = new HoloApp(clothHostRef.current);
    appRef.current = app;
    app.setLiveTransparent(true);
    app.setClothScale(CLOTH_SCALE);
    app.applyParams(SONYA_PARAMS_DARK);
    loadImage(TEXTURE_BY_THEME.dark).then((img) => {
      textureCache.current.dark = img;
      if (appRef.current === app) app.setClothImage(img);
      app.reveal();
    });
    return () => {
      appRef.current = null;
      app.dispose();
    };
  }, []);

  // Theme change: swap material, video-less ambient palette, and the
  // matching brand texture (cached after first load).
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    bgRef.current?.setTheme(theme);
    const app = appRef.current;
    if (!app) return;
    app.applyParams(theme === 'light' ? SONYA_PARAMS_LIGHT : SONYA_PARAMS_DARK);
    const cached = textureCache.current[theme];
    if (cached) {
      app.setClothImage(cached);
    } else {
      loadImage(TEXTURE_BY_THEME[theme]).then((img) => {
        textureCache.current[theme] = img;
        if (appRef.current === app) app.setClothImage(img);
      });
    }
  }, [theme]);

  // Demo-only progress ticker so the bar/ticks read as "alive" in this
  // preview — the real page drives this off actual job status.
  useEffect(() => {
    const id = setInterval(() => {
      setProgress((p) => (p >= 92 ? 18 : p + Math.random() * 6));
    }, 900);
    return () => clearInterval(id);
  }, []);

  function onPickTexture(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    const img = new Image();
    img.onload = () => {
      appRef.current?.setClothImage(img);
      textureCache.current[theme] = img;
      URL.revokeObjectURL(img.src);
    };
    img.src = URL.createObjectURL(file);
    e.target.value = '';
  }

  return (
    <>
      <div ref={bgHostRef} style={{ position: 'fixed', inset: 0, zIndex: 0 }} />
      <div ref={clothHostRef} style={{ position: 'fixed', inset: 0, zIndex: 1 }} />
      <div className="sonya-proc-overlay">
        <div className="sonya-proc-title">Собираем монтаж</div>
        <div className="sonya-proc-status">Обрабатываем кадры</div>
        <div className="sonya-proc-progress">
          <div className="sonya-proc-bar" style={{ width: `${progress}%` }} />
        </div>
      </div>
      <div className="sonya-proc-controls">
        <button
          type="button"
          className="sonya-proc-btn"
          onClick={() => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))}
        >
          Тема: {theme === 'dark' ? 'тёмная' : 'светлая'}
        </button>
        <button type="button" className="sonya-proc-btn" onClick={() => fileInputRef.current?.click()}>
          Текстура ткани…
        </button>
        <input
          ref={fileInputRef}
          type="file"
          accept="image/*"
          style={{ display: 'none' }}
          onChange={onPickTexture}
        />
      </div>
    </>
  );
}
