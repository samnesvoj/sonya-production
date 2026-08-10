import { useEffect, useRef, useState } from 'react';
import { HoloApp, type HoloParams } from './scene.ts';
import { mountAmbientBackground } from './sonyaAmbientBg.ts';
import './sonyaProcessingOverlay.css';

/**
 * SONYA addition. Generic version of SonyaProcessingApp.tsx, parameterized
 * by a design variant instead of hard-coding the main look — so alternate
 * treatments (matte / neon / chrome, see sonya-processing-*.tsx entries)
 * can be compared side by side on their own local pages without touching
 * the main file, which SONYA asked to keep exactly as-is as the baseline.
 */

export interface AccentLight {
  color: number;
  intensity: number;
}

export interface VariantConfig {
  label: string;
  clothScale: number;
  paramsDark: HoloParams;
  paramsLight: HoloParams;
  accentLights: { rimA: AccentLight; rimB: AccentLight };
  /** null = shader/material only, no photo draped on the cloth. */
  textureDark: string | null;
  textureLight: string | null;
  /** Bright/mirror-like materials can wash out the status line's low-alpha
   * text; adds a soft scrim behind the text block when set. */
  textScrim?: boolean;
}

type Theme = 'dark' | 'light';

function loadImage(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error(`${url}: failed to load`));
    img.src = url;
  });
}

export default function SonyaProcessingVariant({ config }: { config: VariantConfig }) {
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
    app.setClothScale(config.clothScale);
    app.setAccentLights(config.accentLights.rimA, config.accentLights.rimB);
    app.applyParams(config.paramsDark);
    const tex = config.textureDark;
    if (tex) {
      loadImage(tex).then((img) => {
        textureCache.current.dark = img;
        if (appRef.current === app) app.setClothImage(img);
        app.reveal();
      });
    } else {
      app.reveal();
    }
    return () => {
      appRef.current = null;
      app.dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    bgRef.current?.setTheme(theme);
    const app = appRef.current;
    if (!app) return;
    app.applyParams(theme === 'light' ? config.paramsLight : config.paramsDark);
    const url = theme === 'light' ? config.textureLight : config.textureDark;
    if (!url) return;
    const cached = textureCache.current[theme];
    if (cached) {
      app.setClothImage(cached);
    } else {
      loadImage(url).then((img) => {
        textureCache.current[theme] = img;
        if (appRef.current === app) app.setClothImage(img);
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [theme]);

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
      <div className={`sonya-proc-overlay${config.textScrim ? ' sonya-proc-overlay--scrim' : ''}`}>
        <div className="sonya-proc-title">Собираем монтаж</div>
        <div className="sonya-proc-status">Обрабатываем кадры</div>
        <div className="sonya-proc-progress">
          <div className="sonya-proc-bar" style={{ width: `${progress}%` }} />
        </div>
      </div>
      <div className="sonya-proc-controls">
        <span className="sonya-proc-label">{config.label}</span>
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
