import { SonyaCloth } from '../SonyaCloth.js';
import { createDefaultConfig, CONFIG_VERSION, PRESETS } from '../config/default-config.js';
import { validateConfig, mergeConfig } from '../config/config-schema.js';

/**
 * Vanilla-JS visual control panel for the cloth widget. Deliberately not
 * built on DialKit/React (holocloth's original panel library) — see
 * third_party/holocloth/NOTICE.md for why: SONYA's public API is a plain
 * ES-module (SonyaCloth.mount/...), and keeping the Studio dependency-free
 * means the public runtime bundle never has to know DialKit/React exist.
 * Every control here writes into the *same* config object shape that
 * SonyaCloth.setConfig consumes — there is no separate "studio-only" config.
 */
export class ClothStudio {
  constructor({ panelHost, canvasHost, configStore }) {
    this.panelHost = panelHost;
    this.canvasHost = canvasHost;
    this.configStore = configStore;
    this.config = createDefaultConfig();
    this.history = [];
    this.savedSnapshot = JSON.stringify(this.config);
    this.textureFile = null;

    SonyaCloth.mount(this.canvasHost, { config: this.config });
    this.render();
    this.refreshDraftState();
  }

  // ---- config plumbing -------------------------------------------------

  get(path) {
    return path.split('.').reduce((o, k) => o?.[k], this.config);
  }

  set(path, value, { record = true } = {}) {
    if (record) this.pushHistory();
    const parts = path.split('.');
    const last = parts.pop();
    const target = parts.reduce((o, k) => o[k], this.config);
    target[last] = value;
    const group = path.split('.')[0];
    SonyaCloth.setConfig({ [group]: this.config[group] });
    this.markDirty();
  }

  pushHistory() {
    this.history.push(JSON.stringify(this.config));
    if (this.history.length > 20) this.history.shift();
    this.updateUndoButton();
  }

  undo() {
    const prev = this.history.pop();
    if (!prev) return;
    this.config = JSON.parse(prev);
    SonyaCloth.setConfig(this.config);
    this.render();
    this.markDirty();
    this.updateUndoButton();
  }

  markDirty() {
    this.dirty = JSON.stringify(this.config) !== this.savedSnapshot;
    if (this.dirtyIndicator) {
      this.dirtyIndicator.textContent = this.dirty ? '● unsaved changes' : 'saved';
      this.dirtyIndicator.classList.toggle('is-dirty', this.dirty);
    }
  }

  resetGroup(group) {
    this.pushHistory();
    const fresh = createDefaultConfig();
    this.config[group] = fresh[group];
    SonyaCloth.setConfig({ [group]: this.config[group] });
    this.render();
    this.markDirty();
  }

  resetAll() {
    this.pushHistory();
    this.config = createDefaultConfig();
    SonyaCloth.setConfig(this.config);
    this.render();
    this.markDirty();
  }

  applyPreset(name) {
    this.pushHistory();
    this.config = mergeConfig(this.config, PRESETS[name]);
    SonyaCloth.setConfig(this.config);
    this.render();
    this.markDirty();
  }

  // ---- texture upload ----------------------------------------------------

  async handleFile(file) {
    if (!/^image\/(png|jpeg|jpg|webp)$/.test(file.type)) {
      this.showMessage(`Unsupported file type: ${file.type || 'unknown'}. Use PNG, JPG, or WebP.`);
      return;
    }
    this.textureFile = file;
    const url = URL.createObjectURL(file);
    this.pushHistory();
    this.config.texture.url = url;
    SonyaCloth.setTexture(url);
    this.markDirty();
    this.renderPreviewThumb(url);
  }

  // ---- local draft (browser-only) ----------------------------------------

  async saveDraft() {
    // configStore.save()/uploadTexture() can reject (e.g. localStorage
    // full, IndexedDB blocked in private browsing) — the click handler that
    // calls saveDraft() doesn't await it, so an uncaught rejection here
    // would surface only as a silent console error, not the usual
    // showMessage() feedback every other Studio action gives on failure.
    try {
      await this.configStore.save(this.config);
      if (this.textureFile) await this.configStore.uploadTexture(this.textureFile);
    } catch (err) {
      console.error('[ClothStudio] saveDraft failed', err);
      this.showMessage('Failed to save local draft — your browser storage may be full or unavailable.');
      return;
    }
    this.savedSnapshot = JSON.stringify(this.config);
    this.markDirty();
    this.showMessage('Saved as a local draft — only visible in this browser, not published to other visitors.');
    this.refreshDraftState();
  }

  async restoreDraft() {
    const draft = await this.configStore.load();
    if (!draft) {
      this.showMessage('No local draft found in this browser.');
      return;
    }
    this.pushHistory();
    this.config = draft.config;
    if (draft.textureUrl) this.config.texture.url = draft.textureUrl;
    SonyaCloth.setConfig(this.config);
    if (draft.textureUrl) SonyaCloth.setTexture(draft.textureUrl);
    this.savedSnapshot = JSON.stringify(this.config);
    this.render();
    this.markDirty();
    this.showMessage('Restored the local draft from this browser.');
  }

  async clearDraft() {
    await this.configStore.reset();
    this.refreshDraftState();
    this.showMessage('Local draft cleared.');
  }

  async refreshDraftState() {
    const has = this.configStore.hasDraft?.() ?? false;
    if (this.draftStatusEl) {
      this.draftStatusEl.textContent = has
        ? 'A local draft exists in this browser.'
        : 'No local draft saved yet.';
    }
  }

  // ---- export / import preset --------------------------------------------

  exportPreset() {
    const blob = new Blob([JSON.stringify(this.config, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'sonya-cloth-preset.json';
    a.click();
    URL.revokeObjectURL(url);
  }

  async importPreset(file) {
    const text = await file.text();
    let parsed;
    try {
      parsed = JSON.parse(text);
    } catch {
      this.showMessage('Import failed: not valid JSON.');
      return;
    }
    const { valid, errors } = validateConfig(parsed);
    if (!valid) {
      this.showMessage(`Import rejected — preset does not match the schema:\n${errors.join('\n')}`);
      return;
    }
    this.pushHistory();
    this.config = parsed;
    SonyaCloth.setConfig(this.config);
    this.render();
    this.markDirty();
    this.showMessage(`Imported preset (schema version ${parsed.version}).`);
  }

  showMessage(text) {
    if (!this.messageEl) return;
    this.messageEl.textContent = text;
    clearTimeout(this._msgTimer);
    this._msgTimer = setTimeout(() => { this.messageEl.textContent = ''; }, 5000);
  }

  updateUndoButton() {
    if (this.undoButton) this.undoButton.disabled = this.history.length === 0;
  }

  renderPreviewThumb(url) {
    if (this.thumbEl) this.thumbEl.style.backgroundImage = `url(${url})`;
  }

  // ---- render --------------------------------------------------------------

  render() {
    this.panelHost.innerHTML = '';
    const root = el('div', 'cloth-studio');

    root.appendChild(this.renderHeader());
    root.appendChild(this.renderUploadSection());
    root.appendChild(this.renderPresets());
    root.appendChild(this.renderGroup('Texture', 'texture', [
      slider('scale', 'Scale', 0.1, 5, 0.01),
      slider('offsetX', 'Offset X', -2, 2, 0.01),
      slider('offsetY', 'Offset Y', -2, 2, 0.01),
      slider('rotation', 'Rotation °', -180, 180, 1),
      select('fit', 'Fit', ['cover', 'contain', 'repeat']),
    ]));
    root.appendChild(this.renderGroup('Cloth', 'cloth', [
      slider('width', 'Width', 0.5, 8, 0.1),
      slider('height', 'Height', 0.5, 8, 0.1),
      slider('segmentsX', 'Segments X', 4, 80, 1),
      slider('segmentsY', 'Segments Y', 4, 80, 1),
      slider('stiffness', 'Stiffness', 0, 1, 0.01),
      slider('damping', 'Damping', 0, 0.6, 0.005),
      slider('gravity', 'Gravity', 0, 6, 0.05),
      slider('wind', 'Wind', 0, 3, 0.02),
      slider('mass', 'Mass', 0.1, 5, 0.05),
      slider('maxDisplacement', 'Max displacement', 0.5, 12, 0.1),
      pinsControl(),
    ]));
    root.appendChild(this.renderGroup('Material', 'material', [
      color('baseColor', 'Base color'),
      color('secondaryColor', 'Secondary color'),
      slider('holoIntensity', 'Holo intensity', 0, 3, 0.01),
      slider('iridescence', 'Iridescence', 0, 1, 0.01),
      slider('roughness', 'Roughness', 0, 1, 0.01),
      slider('metalness', 'Metalness', 0, 1, 0.01),
      slider('opacity', 'Opacity', 0, 1, 0.01),
      slider('glow', 'Glow', 0, 2, 0.01),
      slider('lightIntensity', 'Light intensity', 0, 4, 0.05),
    ]));
    root.appendChild(this.renderGroup('Scene', 'scene', [
      slider('scale', 'Scale', 0.2, 3, 0.01),
      checkbox('transparentBackground', 'Transparent background'),
    ]));
    root.appendChild(this.renderGroup('Performance', 'performance', [
      slider('desktopSegments', 'Desktop segments', 8, 80, 1),
      slider('mobileSegments', 'Mobile segments', 6, 40, 1),
      slider('maxDevicePixelRatio', 'Max device pixel ratio', 1, 3, 0.1),
      select('reducedMotionBehavior', 'Reduced-motion behavior', ['static', 'slow']),
    ]));
    root.appendChild(this.renderPresetActions());
    root.appendChild(this.renderFooter());

    this.panelHost.appendChild(root);
    this.updateUndoButton();
  }

  renderHeader() {
    const header = el('div', 'cloth-studio__header');
    const title = el('h1', 'cloth-studio__title');
    title.textContent = 'SONYA Interactive Cloth — Studio';
    const subtitle = el('p', 'cloth-studio__subtitle');
    subtitle.textContent = 'Local configurator. Nothing here is visible to real visitors yet — see PRODUCTION_INTEGRATION_PLAN.md.';
    this.messageEl = el('div', 'cloth-studio__message');
    header.append(title, subtitle, this.messageEl);
    return header;
  }

  renderUploadSection() {
    const section = el('div', 'cloth-studio__section');
    section.appendChild(el('h2', 'cloth-studio__section-title', 'Cloth image'));
    const dropzone = el('div', 'cloth-studio__dropzone');
    this.thumbEl = el('div', 'cloth-studio__thumb');
    this.thumbEl.style.backgroundImage = `url(${this.config.texture.url})`;
    const hint = el('p', 'cloth-studio__hint', 'Drag & drop a PNG / JPG / WebP here, or');
    const button = el('button', 'cloth-studio__button', 'Choose file…');
    const input = el('input');
    input.type = 'file';
    input.accept = 'image/png,image/jpeg,image/webp';
    input.style.display = 'none';
    input.addEventListener('change', () => {
      if (input.files[0]) this.handleFile(input.files[0]);
      input.value = '';
    });
    button.addEventListener('click', () => input.click());
    dropzone.addEventListener('dragover', (e) => { e.preventDefault(); dropzone.classList.add('is-dragover'); });
    dropzone.addEventListener('dragleave', () => dropzone.classList.remove('is-dragover'));
    dropzone.addEventListener('drop', (e) => {
      e.preventDefault();
      dropzone.classList.remove('is-dragover');
      const file = e.dataTransfer.files[0];
      if (file) this.handleFile(file);
    });
    dropzone.append(this.thumbEl, hint, button, input);
    section.appendChild(dropzone);
    return section;
  }

  renderPresets() {
    const section = el('div', 'cloth-studio__section');
    section.appendChild(el('h2', 'cloth-studio__section-title', 'Starter presets'));
    const row = el('div', 'cloth-studio__preset-row');
    for (const name of Object.keys(PRESETS)) {
      const btn = el('button', 'cloth-studio__button', name);
      btn.addEventListener('click', () => this.applyPreset(name));
      row.appendChild(btn);
    }
    section.appendChild(row);
    return section;
  }

  renderGroup(title, groupKey, controls) {
    const section = el('div', 'cloth-studio__section');
    const head = el('div', 'cloth-studio__group-head');
    head.appendChild(el('h2', 'cloth-studio__section-title', title));
    const resetBtn = el('button', 'cloth-studio__link-button', 'Reset group');
    resetBtn.addEventListener('click', () => this.resetGroup(groupKey));
    head.appendChild(resetBtn);
    section.appendChild(head);

    for (const spec of controls) {
      section.appendChild(this.renderControl(groupKey, spec));
    }
    return section;
  }

  renderControl(groupKey, spec) {
    const path = `${groupKey}.${spec.key}`;
    const row = el('label', 'cloth-studio__row');
    const labelEl = el('span', 'cloth-studio__label', spec.label);
    row.appendChild(labelEl);

    if (spec.type === 'pins') {
      const wrap = el('div', 'cloth-studio__pins');
      for (const name of ['top-left', 'top-right', 'top-edge', 'all-corners']) {
        const cb = el('label', 'cloth-studio__pin-option');
        const input = document.createElement('input');
        input.type = 'checkbox';
        input.checked = this.config.cloth.pins.includes(name);
        input.addEventListener('change', () => {
          const pins = new Set(this.config.cloth.pins);
          if (input.checked) pins.add(name); else pins.delete(name);
          this.set('cloth.pins', [...pins]);
        });
        cb.append(input, document.createTextNode(' ' + name));
        wrap.appendChild(cb);
      }
      row.appendChild(wrap);
      return row;
    }

    if (spec.type === 'select') {
      const value = this.get(path);
      const sel = document.createElement('select');
      for (const opt of spec.options) {
        const o = document.createElement('option');
        o.value = opt; o.textContent = opt;
        if (opt === value) o.selected = true;
        sel.appendChild(o);
      }
      sel.addEventListener('change', () => this.set(path, sel.value));
      row.appendChild(sel);
      return row;
    }

    if (spec.type === 'color') {
      const value = this.get(path);
      const input = document.createElement('input');
      input.type = 'color';
      input.value = value;
      input.addEventListener('input', () => this.set(path, input.value, { record: false }));
      input.addEventListener('change', () => this.set(path, input.value));
      row.appendChild(input);
      return row;
    }

    if (spec.type === 'checkbox') {
      const value = this.get(path);
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.checked = !!value;
      input.addEventListener('change', () => this.set(path, input.checked));
      row.appendChild(input);
      return row;
    }

    // slider (default)
    const value = this.get(path);
    const input = document.createElement('input');
    input.type = 'range';
    input.min = spec.min; input.max = spec.max; input.step = spec.step;
    input.value = value;
    const readout = el('span', 'cloth-studio__readout', formatNumber(value));
    input.addEventListener('input', () => {
      readout.textContent = formatNumber(Number(input.value));
      this.set(path, Number(input.value), { record: false });
    });
    input.addEventListener('change', () => this.pushHistory());
    row.append(input, readout);
    return row;
  }

  renderPresetActions() {
    const section = el('div', 'cloth-studio__section cloth-studio__actions');
    section.appendChild(el('h2', 'cloth-studio__section-title', 'Preset file'));
    const row = el('div', 'cloth-studio__preset-row');

    const exportBtn = el('button', 'cloth-studio__button', 'Export preset (.json)');
    exportBtn.addEventListener('click', () => this.exportPreset());

    const importBtn = el('button', 'cloth-studio__button', 'Import preset…');
    const importInput = document.createElement('input');
    importInput.type = 'file';
    importInput.accept = 'application/json';
    importInput.style.display = 'none';
    importInput.addEventListener('change', () => {
      if (importInput.files[0]) this.importPreset(importInput.files[0]);
      importInput.value = '';
    });
    importBtn.addEventListener('click', () => importInput.click());

    row.append(exportBtn, importBtn, importInput);
    section.appendChild(row);
    return section;
  }

  renderFooter() {
    const section = el('div', 'cloth-studio__section cloth-studio__actions');
    section.appendChild(el('h2', 'cloth-studio__section-title', 'Local draft (this browser only)'));
    this.draftStatusEl = el('p', 'cloth-studio__hint', '');

    const row = el('div', 'cloth-studio__preset-row');
    const saveBtn = el('button', 'cloth-studio__button', 'Save local draft');
    saveBtn.addEventListener('click', () => this.saveDraft());
    const restoreBtn = el('button', 'cloth-studio__button', 'Restore local draft');
    restoreBtn.addEventListener('click', () => this.restoreDraft());
    const clearBtn = el('button', 'cloth-studio__button', 'Clear local draft');
    clearBtn.addEventListener('click', () => this.clearDraft());

    this.undoButton = el('button', 'cloth-studio__button', 'Undo last change');
    this.undoButton.addEventListener('click', () => this.undo());

    const resetAllBtn = el('button', 'cloth-studio__button cloth-studio__button--danger', 'Reset all');
    resetAllBtn.addEventListener('click', () => this.resetAll());

    this.dirtyIndicator = el('span', 'cloth-studio__dirty', 'saved');

    row.append(saveBtn, restoreBtn, clearBtn, this.undoButton, resetAllBtn, this.dirtyIndicator);
    section.append(this.draftStatusEl, row);
    return section;
  }
}

function el(tagName, className, text) {
  const node = document.createElement(tagName);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function slider(key, label, min, max, step) {
  return { type: 'slider', key, label, min, max, step };
}
function select(key, label, options) {
  return { type: 'select', key, label, options };
}
function color(key, label) {
  return { type: 'color', key, label };
}
function checkbox(key, label) {
  return { type: 'checkbox', key, label };
}
function pinsControl() {
  return { type: 'pins', key: 'pins', label: 'Pinned points' };
}
function formatNumber(n) {
  return Number.isInteger(n) ? String(n) : n.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
}

export const STUDIO_CONFIG_VERSION = CONFIG_VERSION;
