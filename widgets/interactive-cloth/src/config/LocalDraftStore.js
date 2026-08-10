import { ConfigStore } from './ConfigStore.js';
import { createDefaultConfig } from './default-config.js';

const LS_KEY = 'sonya-cloth:draft-config';
const DB_NAME = 'sonya-cloth-drafts';
const DB_STORE = 'textures';
const DB_KEY = 'draft-texture';

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      req.result.createObjectStore(DB_STORE);
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function idbPut(key, value) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(DB_STORE, 'readwrite');
    tx.objectStore(DB_STORE).put(value, key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  }).finally(() => db.close());
}

async function idbGet(key) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(DB_STORE, 'readonly');
    const req = tx.objectStore(DB_STORE).get(key);
    req.onsuccess = () => resolve(req.result ?? null);
    req.onerror = () => reject(req.error);
  }).finally(() => db.close());
}

async function idbDelete(key) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(DB_STORE, 'readwrite');
    tx.objectStore(DB_STORE).delete(key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  }).finally(() => db.close());
}

/**
 * Browser-local implementation of ConfigStore. Everything it saves lives
 * only in this browser's localStorage/IndexedDB — it is a personal draft,
 * never visible to other visitors and never written to the SONYA server.
 * A future HttpConfigStore implements the same interface for the real
 * shared/production config.
 */
export class LocalDraftStore extends ConfigStore {
  async load() {
    const raw = localStorage.getItem(LS_KEY);
    // JSON.parse throws on corrupted/foreign data (e.g. a stale format from
    // a previous config version, or manual localStorage tampering) — treat
    // that the same as "no draft" rather than rejecting load(), consistent
    // with the idbGet().catch(() => null) fallback below for the texture.
    let config = null;
    if (raw) {
      try {
        config = JSON.parse(raw);
      } catch {
        config = null;
      }
    }
    let textureUrl = null;
    const blob = await idbGet(DB_KEY).catch(() => null);
    if (blob instanceof Blob) textureUrl = URL.createObjectURL(blob);
    if (!config && !textureUrl) return null;
    return { config: config ?? createDefaultConfig(), textureUrl };
  }

  async save(config) {
    localStorage.setItem(LS_KEY, JSON.stringify(config));
  }

  async uploadTexture(file) {
    await idbPut(DB_KEY, file);
    return URL.createObjectURL(file);
  }

  async reset() {
    localStorage.removeItem(LS_KEY);
    await idbDelete(DB_KEY).catch(() => {});
  }

  hasDraft() {
    return localStorage.getItem(LS_KEY) !== null;
  }
}
