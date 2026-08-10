import { useEffect, useRef } from 'react';
import { HoloApp, type HoloParams } from './scene.ts';

/**
 * SONYA addition, not upstream. Mounts the exact same `HoloApp` engine as
 * App.tsx/Studio — same scene.ts, cloth.ts, holoMaterial.ts, decals.ts,
 * textures.ts, dofPass.ts — with no DialKit panel, no file inputs, no
 * export/version UI. This is deliberately a thin wrapper, not a second
 * implementation: it only decides *what params/images to load*, then hands
 * everything else to HoloApp exactly like Studio does.
 *
 * It loads whatever was last published via Studio's "Export SONYA preset"
 * (see sonyaDraftStore.ts) — expected at /sonya-preset.json plus its PNGs,
 * all under public/. If that manifest isn't present yet (fresh checkout,
 * nothing exported), it falls back to upstream's own defaults so the page
 * still renders something instead of a blank screen.
 */

// Transcribed 1:1 from App.tsx's DialKit schema defaults (the first element
// of each [default, min, max, step] tuple, or the literal for plain fields)
// — this is the exact same "Holo" starting look upstream ships with.
const FALLBACK_PARAMS: HoloParams = {
  performance: 'High',
  physics: {
    viscosity: 0.6,
    stiffness: 1,
    iterations: 14,
    smoothing: 0.045,
    grabRadius: 0.27,
  },
  material: {
    preset: 'Holo',
    finish: 'Matte',
    baseColor: '#20242d',
    holoIntensity: 3.78,
    holoScale: 400,
    bandFreq: 1.1,
    saturation: 1,
    hueShift: 0.37,
    sparkle: 0.73,
    specTint: 0.33,
    iridescence: 0.81,
    roughness: 0.62,
    metalness: 1,
    clearcoat: 0.06,
    coatRoughness: 0.7,
    sheen: 0,
    bump: 3,
    bumpTiling: 3,
  },
  images: {
    edit: false,
    useImage: true,
    scale: 0.35,
    rotation: 0,
    opacity: 1,
    cornerRadius: 0,
  },
  render: {
    background: '#0b0c12',
    exposure: 0.5,
    environment: 0.73,
    bloom: 0.05,
    bloomThreshold: 1.41,
    noise: 0.345,
    toneMapping: 'Neutral',
    occlusion: true,
    occlusionStrength: 1,
    dof: false,
    dofAperture: 40,
    dofBlur: 0.04,
    dofRange: 0.3,
  },
};

const FALLBACK_CLOTH_IMAGE = '/holo-bg-2.jpg';
const FALLBACK_BUMP_IMAGE = '/bump-scratches.jpg';

interface PresetManifestAsset {
  role: 'clothImage' | 'bumpImage' | 'decal';
  index?: number;
  filename: string;
}

interface PresetManifest {
  version: number;
  params: HoloParams;
  decalMeta: { u: number; v: number; scale: number; rotation: number }[];
  assets: PresetManifestAsset[];
}

function loadImage(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error(`${url}: failed to load`));
    img.src = url;
  });
}

export default function PublicApp() {
  const hostRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!hostRef.current) return;
    const app = new HoloApp(hostRef.current);
    let cancelled = false;

    (async () => {
      let params = FALLBACK_PARAMS;
      let clothImageUrl = FALLBACK_CLOTH_IMAGE;
      let bumpImageUrl: string | null = FALLBACK_BUMP_IMAGE;
      let decalAssets: { url: string; meta: PresetManifest['decalMeta'][number] }[] = [];

      try {
        const res = await fetch('/sonya-preset.json');
        if (res.ok) {
          const manifest = (await res.json()) as PresetManifest;
          params = manifest.params;
          const clothAsset = manifest.assets.find((a) => a.role === 'clothImage');
          const bumpAsset = manifest.assets.find((a) => a.role === 'bumpImage');
          if (clothAsset) clothImageUrl = `/${clothAsset.filename}`;
          bumpImageUrl = bumpAsset ? `/${bumpAsset.filename}` : null;
          decalAssets = manifest.assets
            .filter((a) => a.role === 'decal' && a.index !== undefined)
            .map((a) => ({ url: `/${a.filename}`, meta: manifest.decalMeta[a.index!] }));
        }
      } catch (err) {
        console.warn('[sonya] no published preset yet, using upstream defaults', err);
      }

      const [clothImg, bumpImg, decalImgs] = await Promise.all([
        loadImage(clothImageUrl),
        bumpImageUrl ? loadImage(bumpImageUrl).catch(() => null) : Promise.resolve(null),
        Promise.all(decalAssets.map((d) => loadImage(d.url).then((img) => ({ img, meta: d.meta })))),
      ]);
      if (cancelled) return;

      if (bumpImg) app.setBumpMap(bumpImg);
      app.setClothImage(clothImg);
      if (decalImgs.length > 0) {
        app.restoreImages({ clothImage: clothImg, decals: decalImgs.map((d) => ({ img: d.img, ...d.meta })) });
      }
      app.applyParams(params);
      app.reveal();
    })();

    return () => {
      cancelled = true;
      app.dispose();
    };
  }, []);

  return <div id="canvas-host" ref={hostRef} />;
}
