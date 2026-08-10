import type { HoloParams, ImagesState } from './scene.ts';

/**
 * SONYA-specific addition, not part of upstream holocloth. Gives the Studio
 * a "local draft" (this browser only) and a portable "SONYA preset" export/
 * import, without touching scene.ts/cloth.ts/holoMaterial.ts/App.tsx's core
 * DialKit wiring beyond the four new actions wired up in App.tsx.
 *
 * Exported presets never embed `blob:` URLs — images are exported as
 * separate PNG files, referenced from the JSON manifest by filename only.
 */

export const SONYA_PRESET_VERSION = 1;

export interface DecalMeta {
  u: number;
  v: number;
  scale: number;
  rotation: number;
}

export interface DraftImages {
  clothImage: HTMLImageElement | null;
  bumpImage: HTMLImageElement | null;
  decals: { img: HTMLImageElement; meta: DecalMeta }[];
}

const LS_KEY = 'sonya-holocloth:draft-params';
const LS_DECAL_META_KEY = 'sonya-holocloth:draft-decal-meta';
const DB_NAME = 'sonya-holocloth-drafts';
const DB_STORE = 'images';
const DB_KEY = 'draft-images';

interface StoredImages {
  clothImage: Blob | null;
  bumpImage: Blob | null;
  decals: Blob[];
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(DB_STORE);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function idbPut(key: string, value: unknown) {
  const db = await openDb();
  await new Promise<void>((resolve, reject) => {
    const tx = db.transaction(DB_STORE, 'readwrite');
    tx.objectStore(DB_STORE).put(value, key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
  db.close();
}

async function idbGet<T>(key: string): Promise<T | null> {
  const db = await openDb();
  const result = await new Promise<T | null>((resolve, reject) => {
    const tx = db.transaction(DB_STORE, 'readonly');
    const req = tx.objectStore(DB_STORE).get(key);
    req.onsuccess = () => resolve((req.result as T) ?? null);
    req.onerror = () => reject(req.error);
  });
  db.close();
  return result;
}

async function idbDelete(key: string) {
  const db = await openDb();
  await new Promise<void>((resolve, reject) => {
    const tx = db.transaction(DB_STORE, 'readwrite');
    tx.objectStore(DB_STORE).delete(key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
  db.close();
}

function imageToBlob(img: HTMLImageElement): Promise<Blob> {
  const w = img.naturalWidth || img.width;
  const h = img.naturalHeight || img.height;
  const canvas = document.createElement('canvas');
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext('2d')!;
  ctx.drawImage(img, 0, 0, w, h);
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error('toBlob failed'))), 'image/png');
  });
}

function blobToImage(blob: Blob): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(blob);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      resolve(img);
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error('image decode failed'));
    };
    img.src = url;
  });
}

function fileToImage(file: File): Promise<HTMLImageElement> {
  return blobToImage(file);
}

export function hasDraft(): boolean {
  try {
    return localStorage.getItem(LS_KEY) !== null;
  } catch {
    // Storage access can throw outright (Safari private browsing, a
    // locked-down embed) — treat that the same as "no draft" rather than
    // crashing whatever UI checked this.
    return false;
  }
}

export async function saveDraft(params: HoloParams, images: ImagesState): Promise<void> {
  // IndexedDB write happens FIRST, localStorage second: `restoreDraft()`
  // treats LS_KEY as "a draft exists". Writing it before the (async, more
  // failure-prone) IndexedDB put used to mean a rejected/interrupted
  // idbPut — tab closed mid-write, IndexedDB blocked, quota exceeded —
  // left LS_KEY/LS_DECAL_META_KEY pointing at a draft whose images were
  // never actually stored, so restoreDraft() paired fresh decal metadata
  // with a stale or missing image record. Committing the "a draft exists"
  // marker only after the image write actually succeeds makes a failure
  // here leave no draft at all, rather than a mismatched one.
  const stored: StoredImages = {
    clothImage: images.clothImage ? await imageToBlob(images.clothImage) : null,
    bumpImage: null, // set by caller via saveDraftBump — bump lives outside ImagesState (see App.tsx)
    decals: await Promise.all(images.decals.map((d) => imageToBlob(d.img))),
  };
  await idbPut(DB_KEY, stored);
  localStorage.setItem(LS_KEY, JSON.stringify(params));
  localStorage.setItem(
    LS_DECAL_META_KEY,
    JSON.stringify(images.decals.map((d) => ({ u: d.u, v: d.v, scale: d.scale, rotation: d.rotation }))),
  );
}

/** Bump map is tracked separately in App.tsx (not part of upstream ImagesState) — merge it in. */
export async function saveDraftBump(bumpImage: HTMLImageElement | null): Promise<void> {
  const existing = (await idbGet<StoredImages>(DB_KEY)) ?? { clothImage: null, bumpImage: null, decals: [] };
  existing.bumpImage = bumpImage ? await imageToBlob(bumpImage) : null;
  await idbPut(DB_KEY, existing);
}

export async function restoreDraft(): Promise<{ params: HoloParams; images: DraftImages } | null> {
  let raw: string | null;
  try {
    raw = localStorage.getItem(LS_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  let params: HoloParams;
  let decalMeta: DecalMeta[];
  try {
    params = JSON.parse(raw) as HoloParams;
    const decalMetaRaw = localStorage.getItem(LS_DECAL_META_KEY);
    decalMeta = decalMetaRaw ? JSON.parse(decalMetaRaw) : [];
  } catch {
    // Corrupted/foreign JSON under either key (a stale format from a
    // previous version, or manual tampering) — treat the same as "no
    // draft" instead of throwing out of a UI action's .then() handler
    // (see App.tsx's restoreDraft call site, which has no .catch()).
    return null;
  }
  const stored = (await idbGet<StoredImages>(DB_KEY)) ?? { clothImage: null, bumpImage: null, decals: [] };
  const clothImage = stored.clothImage ? await blobToImage(stored.clothImage) : null;
  const bumpImage = stored.bumpImage ? await blobToImage(stored.bumpImage) : null;
  const decals = await Promise.all(
    stored.decals.map(async (blob, i) => ({ img: await blobToImage(blob), meta: decalMeta[i] })),
  );
  return { params, images: { clothImage, bumpImage, decals } };
}

export async function clearDraft(): Promise<void> {
  localStorage.removeItem(LS_KEY);
  localStorage.removeItem(LS_DECAL_META_KEY);
  await idbDelete(DB_KEY).catch(() => {});
}

interface PresetManifestAsset {
  role: 'clothImage' | 'bumpImage' | 'decal';
  index?: number;
  filename: string;
}

interface PresetFile {
  version: number;
  exportedAt: string;
  params: HoloParams;
  decalMeta: DecalMeta[];
  assets: PresetManifestAsset[];
}

/**
 * Presents one download link per exported file so each download is its own
 * user gesture. Chrome (and other browsers) silently blocks automatic
 * downloads past the first one triggered from a single script — firing
 * several `a.click()` calls in a row loses everything after the first file.
 * A small on-page overlay with real links sidesteps that reliably.
 */
function presentDownloads(files: { blob: Blob; filename: string }[]) {
  const overlay = document.createElement('div');
  overlay.style.cssText =
    'position:fixed;inset:0;z-index:2147483647;background:rgba(5,5,8,0.82);' +
    'display:flex;align-items:center;justify-content:center;font-family:ui-sans-serif,system-ui,sans-serif;';
  const panel = document.createElement('div');
  panel.style.cssText =
    'background:#16171d;border:1px solid rgba(255,255,255,0.1);border-radius:12px;' +
    'padding:20px 24px;min-width:320px;max-width:90vw;color:#e8e9ee;';
  const title = document.createElement('div');
  title.textContent = 'SONYA preset exported — download each file';
  title.style.cssText = 'font-size:14px;font-weight:600;margin-bottom:4px;';
  const hint = document.createElement('div');
  hint.textContent = 'Browsers block multiple automatic downloads at once, so click each link below.';
  hint.style.cssText = 'font-size:11px;color:#9a9ca6;margin-bottom:14px;';
  panel.append(title, hint);

  // Tracked so "Done" can revoke whatever the user never clicked — a
  // second revokeObjectURL() on an already-revoked (clicked) URL is a
  // harmless no-op, so no need to track which ones were already freed.
  const urls: string[] = [];
  for (const { blob, filename } of files) {
    const url = URL.createObjectURL(blob);
    urls.push(url);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    link.textContent = `Download ${filename}`;
    link.style.cssText =
      'display:block;padding:8px 10px;margin:6px 0;border-radius:6px;' +
      'background:rgba(255,255,255,0.06);color:#7fd4ff;text-decoration:none;font-size:12px;';
    link.addEventListener('click', () => setTimeout(() => URL.revokeObjectURL(url), 2000));
    panel.appendChild(link);
  }

  const closeBtn = document.createElement('button');
  closeBtn.textContent = 'Done';
  closeBtn.style.cssText =
    'margin-top:12px;padding:6px 14px;border-radius:6px;border:1px solid rgba(255,255,255,0.15);' +
    'background:transparent;color:#e8e9ee;font-size:12px;cursor:pointer;';
  closeBtn.addEventListener('click', () => {
    // Frees any file the user never clicked — without this, dismissing the
    // overlay early leaked those blob URLs for the rest of the page's
    // lifetime (they're never referenced again once the overlay is gone).
    urls.forEach((url) => URL.revokeObjectURL(url));
    overlay.remove();
  });
  panel.appendChild(closeBtn);

  overlay.appendChild(panel);
  document.body.appendChild(overlay);
}

/**
 * Exports a JSON manifest (all HoloParams + decal transforms + an asset
 * list) plus one PNG file per image. The JSON never contains a `blob:`
 * URL — only filenames, which importPreset() matches against the image
 * files the operator re-selects. Every file is offered as its own download
 * link (see presentDownloads) rather than auto-triggered, since browsers
 * block bursts of automatic downloads.
 */
export async function exportPreset(params: HoloParams, images: ImagesState, bumpImage: HTMLImageElement | null) {
  const assets: PresetManifestAsset[] = [];
  const files: { blob: Blob; filename: string }[] = [];

  if (images.clothImage) {
    assets.push({ role: 'clothImage', filename: 'sonya-holocloth-cloth.png' });
    files.push({ blob: await imageToBlob(images.clothImage), filename: 'sonya-holocloth-cloth.png' });
  }
  if (bumpImage) {
    assets.push({ role: 'bumpImage', filename: 'sonya-holocloth-bump.png' });
    files.push({ blob: await imageToBlob(bumpImage), filename: 'sonya-holocloth-bump.png' });
  }
  for (let i = 0; i < images.decals.length; i++) {
    const filename = `sonya-holocloth-decal-${i}.png`;
    assets.push({ role: 'decal', index: i, filename });
    files.push({ blob: await imageToBlob(images.decals[i].img), filename });
  }

  const manifest: PresetFile = {
    version: SONYA_PRESET_VERSION,
    exportedAt: new Date().toISOString(),
    params,
    decalMeta: images.decals.map((d) => ({ u: d.u, v: d.v, scale: d.scale, rotation: d.rotation })),
    assets,
  };
  files.unshift({
    blob: new Blob([JSON.stringify(manifest, null, 2)], { type: 'application/json' }),
    filename: 'sonya-holocloth-preset.json',
  });

  presentDownloads(files);
}

export interface ImportedPreset {
  params: HoloParams;
  clothImage: HTMLImageElement | null;
  bumpImage: HTMLImageElement | null;
  decals: { img: HTMLImageElement; meta: DecalMeta }[];
}

/**
 * `files` should contain the exported `sonya-holocloth-preset.json` plus
 * however many of its referenced PNGs the operator wants to bring back
 * (missing ones are simply left empty, not an error).
 */
export async function importPreset(files: FileList | File[]): Promise<ImportedPreset> {
  const fileArray = Array.from(files);
  const jsonFile = fileArray.find((f) => f.name.endsWith('.json'));
  if (!jsonFile) throw new Error('No .json preset file selected');
  const manifest = JSON.parse(await jsonFile.text()) as PresetFile;
  if (manifest.version !== SONYA_PRESET_VERSION) {
    throw new Error(`Unsupported preset version: ${manifest.version}`);
  }

  const byName = new Map(fileArray.map((f) => [f.name, f]));
  let clothImage: HTMLImageElement | null = null;
  let bumpImage: HTMLImageElement | null = null;
  const decals: { img: HTMLImageElement; meta: DecalMeta }[] = [];

  for (const asset of manifest.assets) {
    const file = byName.get(asset.filename);
    if (!file) continue; // operator chose not to bring this image back
    if (asset.role === 'clothImage') clothImage = await fileToImage(file);
    else if (asset.role === 'bumpImage') bumpImage = await fileToImage(file);
    else if (asset.role === 'decal' && asset.index !== undefined) {
      decals[asset.index] = { img: await fileToImage(file), meta: manifest.decalMeta[asset.index] };
    }
  }

  return { params: manifest.params, clothImage, bumpImage, decals: decals.filter(Boolean) };
}
