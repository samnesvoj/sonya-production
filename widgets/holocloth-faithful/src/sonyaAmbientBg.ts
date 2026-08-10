/**
 * SONYA addition, not upstream. Fixed full-viewport canvas layer that sits
 * BEHIND the transparent cloth canvas (see HoloApp.setLiveTransparent) — a
 * slow drifting glow so the void around/between the dragged fabric isn't
 * flat. Vanilla canvas 2D, no Three.js, so it never shares a GL context
 * with the cloth.
 *
 * Two palettes (dark / light), plus a light dose of the real production
 * cinema-overlay treatment (`.cinema-stage.cinema-stage--global
 * .cinema-overlay--*` in /styles.css — tint/gradient/vignette) layered on
 * top at a fraction of its shipped strength, per SONYA's request to nod at
 * that look without pulling in the actual video file.
 */

type Theme = 'dark' | 'light';

const PALETTES: Record<
  Theme,
  { base: string; vignetteA: string; vignetteB: string; blobs: [string, string][] }
> = {
  dark: {
    base: '#060608',
    vignetteA: 'rgba(20,20,26,0.5)',
    vignetteB: 'rgba(0,0,0,0.55)',
    blobs: [
      ['rgba(214,222,235,0.10)', 'rgba(214,222,235,0.03)'],
      ['rgba(255,255,255,0.09)', 'rgba(255,255,255,0.0)'],
      ['rgba(255,255,255,0.07)', 'rgba(255,255,255,0.0)'],
    ],
  },
  // Muted white/grey — deliberately neutral (not the old warm-cream v1
  // tokens) per SONYA's "бело-серую, приглушенное" direction.
  light: {
    base: '#eceeef',
    vignetteA: 'rgba(255,255,255,0.4)',
    vignetteB: 'rgba(178,181,186,0.35)',
    blobs: [
      ['rgba(255,255,255,0.55)', 'rgba(255,255,255,0.0)'],
      ['rgba(200,203,208,0.28)', 'rgba(200,203,208,0.0)'],
      ['rgba(210,213,218,0.22)', 'rgba(210,213,218,0.0)'],
    ],
  },
};

export function mountAmbientBackground(host: HTMLElement): {
  destroy: () => void;
  setTheme: (theme: Theme) => void;
} {
  const canvas = document.createElement('canvas');
  canvas.style.cssText = 'position:fixed;inset:0;display:block;';
  host.appendChild(canvas);
  const ctx = canvas.getContext('2d')!;
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  let theme: Theme = 'dark';
  let w = 0;
  let h = 0;
  let raf = 0;

  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = window.innerWidth;
    h = window.innerHeight;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  window.addEventListener('resize', resize);
  resize();

  function blob(t: number, seed: number, cx: number, cy: number, rx: number, ry: number, a: string, b: string) {
    const wob = Math.sin(t * 0.6 + seed) * 0.08 + Math.cos(t * 0.37 + seed * 1.7) * 0.05;
    const x = cx + Math.sin(t * 0.25 + seed) * w * 0.06;
    const y = cy + Math.cos(t * 0.2 + seed * 1.3) * h * 0.05;
    const g = ctx.createRadialGradient(x, y, 0, x, y, Math.max(rx, ry) * (1 + wob));
    g.addColorStop(0, a);
    g.addColorStop(0.55, b);
    g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = g;
    ctx.beginPath();
    ctx.ellipse(x, y, rx * (1 + wob * 0.5), ry * (1 - wob * 0.3), t * 0.05 + seed, 0, Math.PI * 2);
    ctx.fill();
  }

  // A strong dose (~80% of shipped strength) of the real
  // .cinema-overlay--tint/--gradient/--vignette treatment, tinted per theme
  // instead of the production version's fixed dark rgba(6,6,8,...) — that
  // fixed dark tint only makes sense over a dark/desaturated video plate,
  // not over a muted-white ambient layer.
  function cinemaOverlay() {
    const tintColor = theme === 'dark' ? '6,6,8' : '255,255,255';
    const tint = ctx.createLinearGradient(0, 0, 0, h);
    tint.addColorStop(0, `rgba(${tintColor},0.04)`);
    tint.addColorStop(1, `rgba(${tintColor},0.15)`);
    ctx.fillStyle = tint;
    ctx.fillRect(0, 0, w, h);

    const grad = ctx.createRadialGradient(w / 2, h * 0.4, 0, w / 2, h * 0.4, h * 0.9);
    grad.addColorStop(0, 'rgba(0,0,0,0)');
    grad.addColorStop(1, `rgba(${tintColor},0.16)`);
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, w, h);

    const vig = ctx.createRadialGradient(w / 2, h / 2, h * 0.6, w / 2, h / 2, h);
    vig.addColorStop(0, 'rgba(0,0,0,0)');
    vig.addColorStop(1, theme === 'dark' ? 'rgba(0,0,0,0.3)' : 'rgba(0,0,0,0.18)');
    ctx.fillStyle = vig;
    ctx.fillRect(0, 0, w, h);
  }

  function draw(t: number) {
    const p = PALETTES[theme];
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = p.base;
    ctx.fillRect(0, 0, w, h);

    const vg = ctx.createRadialGradient(w / 2, h / 2, h * 0.15, w / 2, h / 2, h * 0.75);
    vg.addColorStop(0, p.vignetteA);
    vg.addColorStop(1, p.vignetteB);
    ctx.fillStyle = vg;
    ctx.fillRect(0, 0, w, h);

    const cx = w / 2;
    const cy = h * 0.46;
    blob(t, 0.0, cx, cy, w * 0.34, h * 0.34, p.blobs[0][0], p.blobs[0][1]);
    blob(t, 2.1, cx - w * 0.1, cy + h * 0.06, w * 0.22, h * 0.2, p.blobs[1][0], p.blobs[1][1]);
    blob(t, 4.4, cx + w * 0.12, cy - h * 0.05, w * 0.18, h * 0.16, p.blobs[2][0], p.blobs[2][1]);

    cinemaOverlay();
  }

  if (reduced) {
    draw(0);
  } else {
    const loop = (ts: number) => {
      draw(ts / 1000);
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
  }

  return {
    setTheme: (t: Theme) => {
      theme = t;
      if (reduced) draw(0);
    },
    destroy: () => {
      cancelAnimationFrame(raf);
      window.removeEventListener('resize', resize);
      canvas.remove();
    },
  };
}
