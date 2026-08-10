import './styles.css';
import { SonyaCloth } from './SonyaCloth.js';
import { PRESETS, SONYA_CHROME_LIGHT_OVERRIDES } from './config/default-config.js';
import { mergeConfig } from './config/config-schema.js';

/**
 * Vanilla-JS preview of the ported "SONYA Chrome" preset — no React, no
 * build step beyond the widget's own Vite dev server, using only the
 * public SonyaCloth API (mount/setConfig/setTexture) exactly as a real
 * host page would. Matches the approved processing-screen dressing
 * (title/status/progress bar, see chrome-preview.html's .proc-* rules,
 * copied from the real .app-container.v2 CSS) and the real site's top
 * chrome (.floating-controls--right icon buttons) instead of an ad-hoc
 * dev bar, so this reads as what production would actually show — not a
 * testing harness. The theme-toggle icon still swaps dark/light + the
 * matching brand texture live, for comparison against
 * widgets/holocloth-faithful/sonya-processing-chrome.html.
 */

const host = document.getElementById('sonya-cloth-root');
const chromeLightPartial = mergeConfig(PRESETS['SONYA Chrome'], SONYA_CHROME_LIGHT_OVERRIDES);

SonyaCloth.mount(host, { config: PRESETS['SONYA Chrome'] });

let theme = 'dark';
document.getElementById('theme-toggle').addEventListener('click', () => {
  theme = theme === 'dark' ? 'light' : 'dark';
  SonyaCloth.setConfig(theme === 'dark' ? PRESETS['SONYA Chrome'] : chromeLightPartial);
  document.body.dataset.theme = theme;
});

// Demo-only progress ticker so the bar reads as "alive" in this preview —
// the real page drives this off actual job status.
let progress = 6;
const bar = document.getElementById('proc-bar');
setInterval(() => {
  progress = progress >= 92 ? 18 : progress + Math.random() * 6;
  bar.style.width = `${progress}%`;
}, 900);
