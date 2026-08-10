import './studio/studio.css';
import { ClothStudio } from './studio/ClothStudio.js';
import { LocalDraftStore } from './config/LocalDraftStore.js';

// Admin/Studio-only code path. Never imported by public-entry.js, and
// Rollup builds it as a separate entry (dist/sonya-cloth-studio.js) — see
// vite.config.js — so a regular visitor's bundle can never pull this in.
document.body.classList.add('sonya-studio-body');

const canvasHost = document.getElementById('sonya-studio-canvas');
const panelHost = document.getElementById('sonya-studio-panel');

new ClothStudio({
  canvasHost,
  panelHost,
  configStore: new LocalDraftStore(),
});
