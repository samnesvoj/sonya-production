/**
 * Verlet cloth simulation.
 *
 * Adapted from holocloth's `src/cloth.ts` (`ClothSim`) — see
 * third_party/holocloth/NOTICE.md for the full attribution and the list of
 * changes. The constraint topology, substep integrator shape, and the
 * grab/moveGrab/endGrab interaction model come from there. Gravity, wind,
 * pinned vertices, and the max-displacement clamp are new: upstream's cloth
 * is a zero-gravity "gel" that only reacts to direct dragging, while SONYA
 * wants a hanging cloth affected by gravity and ambient wind that a visitor
 * can grab, pull, and let go of.
 */

const SUBSTEP = 1 / 120;
const MAX_SUBSTEPS = 4;

export class ClothSim {
  /**
   * @param {number} width
   * @param {number} height
   * @param {number} segX
   * @param {number} segY
   * @param {string[]} pinNames named pin presets: 'top-left' | 'top-right' | 'top-edge' | 'all-corners'
   */
  constructor(width, height, segX, segY, pinNames = []) {
    this.width = width;
    this.height = height;
    this.segX = segX;
    this.segY = segY;
    this.cols = segX + 1;
    this.rows = segY + 1;
    this.count = this.cols * this.rows;

    this.positions = new Float32Array(this.count * 3);
    this.prev = new Float32Array(this.count * 3);
    this.pinned = new Uint8Array(this.count);

    this.windPhase = Math.random() * 1000;
    this.grab = null;
    this.accumulator = 0;

    this.initPositions();
    this.applyPins(pinNames);
    this.buildConstraints();
    this.buildNeighbors();
  }

  idx(x, y) {
    return y * this.cols + x;
  }

  initPositions() {
    const stepX = this.width / this.segX;
    const stepY = this.height / this.segY;
    let k = 0;
    for (let y = 0; y < this.rows; y++) {
      for (let x = 0; x < this.cols; x++) {
        this.positions[k] = (x - this.segX / 2) * stepX;
        this.positions[k + 1] = (this.segY / 2 - y) * stepY;
        this.positions[k + 2] = 0;
        k += 3;
      }
    }
    this.prev.set(this.positions);
  }

  applyPins(pinNames) {
    this.pinned.fill(0);
    const topLeft = this.idx(0, 0);
    const topRight = this.idx(this.cols - 1, 0);
    const bottomLeft = this.idx(0, this.rows - 1);
    const bottomRight = this.idx(this.cols - 1, this.rows - 1);
    for (const name of pinNames) {
      if (name === 'top-left') this.pinned[topLeft] = 1;
      else if (name === 'top-right') this.pinned[topRight] = 1;
      else if (name === 'top-edge') {
        for (let x = 0; x < this.cols; x++) this.pinned[this.idx(x, 0)] = 1;
      } else if (name === 'all-corners') {
        this.pinned[topLeft] = this.pinned[topRight] = this.pinned[bottomLeft] = this.pinned[bottomRight] = 1;
      }
    }
  }

  buildConstraints() {
    const a = [];
    const b = [];
    const mul = [];
    for (let y = 0; y < this.rows; y++) {
      for (let x = 0; x < this.cols; x++) {
        const i = this.idx(x, y);
        if (x + 1 < this.cols) { a.push(i); b.push(this.idx(x + 1, y)); mul.push(1.0); } // structural
        if (y + 1 < this.rows) { a.push(i); b.push(this.idx(x, y + 1)); mul.push(1.0); } // structural
        if (x + 1 < this.cols && y + 1 < this.rows) { // shear
          a.push(i); b.push(this.idx(x + 1, y + 1)); mul.push(0.85);
          a.push(this.idx(x + 1, y)); b.push(this.idx(x, y + 1)); mul.push(0.85);
        }
        if (x + 2 < this.cols) { a.push(i); b.push(this.idx(x + 2, y)); mul.push(0.35); } // bend
        if (y + 2 < this.rows) { a.push(i); b.push(this.idx(x, y + 2)); mul.push(0.35); } // bend
      }
    }
    this.cA = new Int32Array(a);
    this.cB = new Int32Array(b);
    this.cMul = new Float32Array(mul);
    this.cRest = new Float32Array(a.length);
    const p = this.positions;
    for (let c = 0; c < this.cA.length; c++) {
      const ia = this.cA[c] * 3, ib = this.cB[c] * 3;
      const dx = p[ib] - p[ia], dy = p[ib + 1] - p[ia + 1], dz = p[ib + 2] - p[ia + 2];
      this.cRest[c] = Math.hypot(dx, dy, dz);
    }
  }

  buildNeighbors() {
    this.neighbors = new Int32Array(this.count * 4).fill(-1);
    for (let y = 0; y < this.rows; y++) {
      for (let x = 0; x < this.cols; x++) {
        const i = this.idx(x, y) * 4;
        this.neighbors[i + 0] = x > 0 ? this.idx(x - 1, y) : -1;
        this.neighbors[i + 1] = x + 1 < this.cols ? this.idx(x + 1, y) : -1;
        this.neighbors[i + 2] = y > 0 ? this.idx(x, y - 1) : -1;
        this.neighbors[i + 3] = y + 1 < this.rows ? this.idx(x, y + 1) : -1;
      }
    }
  }

  reset() {
    this.initPositions();
    this.grab = null;
    this.accumulator = 0;
  }

  /** Begin a grab around a world-space point {x,y,z}. Returns false if nothing near. */
  startGrab(point, radius) {
    const p = this.positions;
    const indices = [];
    const weights = [];
    const offsets = [];
    let best = Infinity;
    for (let i = 0; i < this.count; i++) {
      if (this.pinned[i]) continue;
      const dx = p[i * 3] - point.x, dy = p[i * 3 + 1] - point.y, dz = p[i * 3 + 2] - point.z;
      const d = Math.sqrt(dx * dx + dy * dy + dz * dz);
      best = Math.min(best, d);
      if (d > radius) continue;
      const t = 1 - d / radius;
      const w = t * t * (3 - 2 * t);
      indices.push(i);
      weights.push(w);
      offsets.push(dx, dy, dz);
    }
    if (indices.length === 0 || best > radius) return false;
    this.grab = { indices, weights, offsets: new Float32Array(offsets), target: { x: point.x, y: point.y, z: point.z } };
    return true;
  }

  moveGrab(target) {
    if (this.grab) {
      this.grab.target.x = target.x;
      this.grab.target.y = target.y;
      this.grab.target.z = target.z;
    }
  }

  endGrab() {
    this.grab = null;
  }

  get isGrabbing() {
    return this.grab !== null;
  }

  /**
   * @param {number} dt seconds since last step
   * @param {{ stiffness: number, damping: number, gravity: number, wind: number, maxDisplacement: number }} params
   * @param {number} elapsed total elapsed seconds (drives the wind gust)
   */
  step(dt, params, elapsed) {
    this.accumulator += Math.min(dt, 0.05);
    let steps = 0;
    while (this.accumulator >= SUBSTEP && steps < MAX_SUBSTEPS) {
      this.substep(params, elapsed);
      this.accumulator -= SUBSTEP;
      steps++;
    }
    if (steps === MAX_SUBSTEPS) this.accumulator = 0;
  }

  substep(params, elapsed) {
    const p = this.positions;
    const prev = this.prev;
    const n = this.count;
    const damp = Math.pow(1 - Math.min(Math.max(params.damping, 0), 0.99), SUBSTEP * 60);

    // wind: a slow directional gust plus a faster small wobble, so the
    // cloth doesn't look like it's just falling under gravity alone
    const gust = Math.sin(elapsed * 0.6 + this.windPhase) * 0.7 + Math.sin(elapsed * 2.3 + this.windPhase * 1.7) * 0.3;
    const windX = params.wind * gust;
    const windZ = params.wind * Math.cos(elapsed * 0.5 + this.windPhase) * 0.5;
    const gAccel = params.gravity * SUBSTEP * SUBSTEP;
    const wxAccel = windX * SUBSTEP * SUBSTEP;
    const wzAccel = windZ * SUBSTEP * SUBSTEP;

    for (let i = 0; i < n; i++) {
      if (this.pinned[i]) continue;
      const ix = i * 3, iy = ix + 1, iz = ix + 2;
      const vx = (p[ix] - prev[ix]) * damp;
      const vy = (p[iy] - prev[iy]) * damp;
      const vz = (p[iz] - prev[iz]) * damp;
      prev[ix] = p[ix]; prev[iy] = p[iy]; prev[iz] = p[iz];
      p[ix] += vx + wxAccel;
      p[iy] += vy - gAccel;
      p[iz] += vz + wzAccel;
    }

    const iters = 6;
    const stiff = params.stiffness;
    const cA = this.cA, cB = this.cB, cRest = this.cRest, cMul = this.cMul;
    const nc = cA.length;
    for (let it = 0; it < iters; it++) {
      for (let c = 0; c < nc; c++) {
        const ia = cA[c], ib = cB[c];
        const ia3 = ia * 3, ib3 = ib * 3;
        const dx = p[ib3] - p[ia3], dy = p[ib3 + 1] - p[ia3 + 1], dz = p[ib3 + 2] - p[ia3 + 2];
        const d = Math.sqrt(dx * dx + dy * dy + dz * dz);
        if (d < 1e-9) continue;
        const diff = ((d - cRest[c]) / d) * 0.5 * stiff * cMul[c];
        const ox = dx * diff, oy = dy * diff, oz = dz * diff;
        const pinnedA = this.pinned[ia], pinnedB = this.pinned[ib];
        if (!pinnedA) { p[ia3] += ox; p[ia3 + 1] += oy; p[ia3 + 2] += oz; }
        if (!pinnedB) { p[ib3] -= ox; p[ib3 + 1] -= oy; p[ib3 + 2] -= oz; }
      }
      this.applyGrab();
    }

    this.clampDisplacement(params.maxDisplacement);
  }

  applyGrab() {
    const g = this.grab;
    if (!g) return;
    const p = this.positions;
    for (let k = 0; k < g.indices.length; k++) {
      const i = g.indices[k] * 3;
      const w = g.weights[k];
      const tx = g.target.x + g.offsets[k * 3];
      const ty = g.target.y + g.offsets[k * 3 + 1];
      const tz = g.target.z + g.offsets[k * 3 + 2];
      p[i] += (tx - p[i]) * w;
      p[i + 1] += (ty - p[i + 1]) * w;
      p[i + 2] += (tz - p[i + 2]) * w;
    }
  }

  /**
   * Keep a thrown cloth from drifting off-screen forever: clamp how far
   * any vertex may sit from its rest (flat, unpinned) position.
   */
  clampDisplacement(maxDisplacement) {
    if (!(maxDisplacement > 0)) return;
    const p = this.positions;
    const stepX = this.width / this.segX;
    const stepY = this.height / this.segY;
    for (let y = 0; y < this.rows; y++) {
      for (let x = 0; x < this.cols; x++) {
        const i = this.idx(x, y);
        if (this.pinned[i]) continue;
        const i3 = i * 3;
        const rx = (x - this.segX / 2) * stepX;
        const ry = (this.segY / 2 - y) * stepY;
        const dx = p[i3] - rx, dy = p[i3 + 1] - ry, dz = p[i3 + 2];
        const d = Math.sqrt(dx * dx + dy * dy + dz * dz);
        if (d > maxDisplacement) {
          const s = maxDisplacement / d;
          p[i3] = rx + dx * s;
          p[i3 + 1] = ry + dy * s;
          p[i3 + 2] = dz * s;
        }
      }
    }
  }

  /** Per-vertex cavity term for ambient occlusion in the holo shader. */
  computeCavity(normals, out, gain = 6) {
    const p = this.positions;
    const nb = this.neighbors;
    const n = this.count;
    const invStep = 1 / Math.min(this.width / this.segX, this.height / this.segY);
    if (!this._cavityScratch || this._cavityScratch.length < n) this._cavityScratch = new Float32Array(n);
    const tmp = this._cavityScratch;
    for (let i = 0; i < n; i++) {
      let ax = 0, ay = 0, az = 0, cnt = 0;
      for (let j = 0; j < 4; j++) {
        const ni = nb[i * 4 + j];
        if (ni < 0) continue;
        ax += p[ni * 3]; ay += p[ni * 3 + 1]; az += p[ni * 3 + 2];
        cnt++;
      }
      if (cnt === 0) { tmp[i] = 0; continue; }
      const inv = 1 / cnt;
      const lx = ax * inv - p[i * 3], ly = ay * inv - p[i * 3 + 1], lz = az * inv - p[i * 3 + 2];
      const c = (lx * normals[i * 3] + ly * normals[i * 3 + 1] + lz * normals[i * 3 + 2]) * invStep;
      tmp[i] = Math.min(1, Math.max(0, c * gain));
    }
    for (let i = 0; i < n; i++) {
      let sum = 0, cnt = 0;
      for (let j = 0; j < 4; j++) {
        const ni = nb[i * 4 + j];
        if (ni < 0) continue;
        sum += tmp[ni];
        cnt++;
      }
      out[i] = cnt > 0 ? tmp[i] * 0.5 + (sum / cnt) * 0.5 : tmp[i];
    }
  }

  /** Run a few silent physics steps so the cloth is already draped before first paint. */
  warmUp(params, steps = 90) {
    for (let i = 0; i < steps; i++) this.step(1 / 60, params, i / 60);
  }
}
