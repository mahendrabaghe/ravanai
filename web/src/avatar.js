/*
 * RAVAN's face, on a Canvas 2D context.
 *
 * The geometry is not redrawn for the web: `assets/head.json` is exported from
 * core/avatar_mesh.py, so this is the same MediaPipe canonical face plus the
 * generated cranium and neck the desktop app animates, with the same jaw,
 * brow, lip and fade rigs. What changed is only the paint path — QPainter
 * becomes Canvas 2D, and the baked light map becomes a handful of broad
 * gradients re-anchored to the projected landmarks each frame.
 *
 * Everything that makes the character read as alive is kept: the idle sway,
 * saccades, blink timing, the jaw/lip rig and viseme-driven speech. The mobile
 * build must never substitute a static picture for this.
 */

const CAM_D = 4.6;
const JAW_PIVOT = [0.0, 0.06, -0.34];
const JAW_MAX = 0.115;      // radians at full amplitude
const BROW_LIFT = 0.14;     // head-half-heights at full lift

// (openness, width, closure), matching core/viseme.py before it is sent here.
const VISEME = {
  REST: [0.0, 0.0, 0.0],
  AA: [0.92, -0.05, 0.0],
  E: [0.52, 0.42, 0.0],
  I: [0.2, 0.62, 0.0],
  O: [0.55, -0.52, 0.0],
  U: [0.26, -0.74, 0.0],
  MBP: [0.0, 0.0, 1.0],
  FV: [0.1, 0.22, 0.55],
  S: [0.16, 0.42, 0.0],
  L: [0.36, 0.18, 0.0],
  TD: [0.28, 0.12, 0.0],
  K: [0.3, -0.04, 0.0],
  R: [0.28, -0.16, 0.0]
};

const TAU_OPEN = 0.022;
const TAU_SHUT = 0.012;
const TAU_REST = 0.11;
const TAU_WIDE = 0.03;

const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);
const rate = (dt, tau) => 1 - Math.exp(-dt / Math.max(0.001, tau));
const mix = (a, b, t) => a + (b - a) * t;

/** Monotone-chain convex hull of points [start, end) in xs/ys. */
function hullIndices(xs, ys, start, end, order, hull) {
  let count = 0;
  for (let i = start; i < end; i++) order[count++] = i;
  const sub = order.subarray(0, count);
  sub.sort((a, b) => (xs[a] - xs[b]) || (ys[a] - ys[b]));

  let k = 0;
  const cross = (o, a, b) =>
    (xs[a] - xs[o]) * (ys[b] - ys[o]) - (ys[a] - ys[o]) * (xs[b] - xs[o]);

  for (let n = 0; n < count; n++) {
    while (k >= 2 && cross(hull[k - 2], hull[k - 1], sub[n]) <= 0) k--;
    hull[k++] = sub[n];
  }
  for (let n = count - 2, lower = k + 1; n >= 0; n--) {
    while (k >= lower && cross(hull[k - 2], hull[k - 1], sub[n]) <= 0) k--;
    hull[k++] = sub[n];
  }
  return Math.max(0, k - 1);
}

export class Avatar {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d", { alpha: true });
    this.mesh = null;

    this.w = 0;
    this.h = 0;
    this.dpr = 1;
    this.r = 100;
    this.cx = 0;
    this.cy = 0;

    this._t = 0;
    this._last = 0;
    this._raf = 0;
    this._visible = true;
    this._running = false;

    this.state = "STANDBY";
    this.muted = false;

    // rig
    this.mouth = 0;
    this.wide = 0;
    this.brow = 0;
    this.browBias = 0;
    this.lids = 1;
    this.gaze = [0, 0];
    this.gazeTgt = [0, 0];
    this.gazeBias = [0, 0];
    this.biasTgt = [0, 0];
    this.biasAt = 0;
    this.gazeAt = 0;
    this.blink = 0;
    this.blinkAt = 2.4;
    this.expr = 0;
    this.exprTgt = 0;
    this.exprAt = 0;
    this.emph = 0;
    this.glow = 0;

    // speech schedule, in seconds
    this._speech = null;

    this._onResize = () => this.resize();
    this._onVisibility = () => {
      this._visible = !document.hidden;
      if (this._visible) {
        this._last = 0;
        if (this._running) this._kick();
      }
    };
    window.addEventListener("resize", this._onResize, { passive: true });
    document.addEventListener("visibilitychange", this._onVisibility);
  }

  async load(url = "assets/head.json") {
    const res = await fetch(url, { cache: "force-cache" });
    if (!res.ok) throw new Error(`face mesh ${res.status}`);
    const data = await res.json();

    const n = data.v.length;
    this.mesh = {
      count: n,
      nHead: data.nhead,
      v0: Float32Array.from(data.v.flat()),
      n0: Float32Array.from(data.n.flat()),
      f: Uint16Array.from(data.f.flat()),
      e: Uint16Array.from(data.e.flat()),
      jawW: Float32Array.from(data.jaw),
      browW: Float32Array.from(data.brow),
      lipW: Float32Array.from(data.lips),
      fade: Float32Array.from(data.fade),
      grp: Uint8Array.from(data.grp),
      lipC: Float32Array.from(data.lipc),
      lm: Object.fromEntries(
        Object.entries(data.lm).map(([k, v]) => [k, Int32Array.from(v)])
      )
    };
    this.mesh.v = new Float32Array(n * 3);
    this.mesh.n = new Float32Array(n * 3);
    this.mesh.px = new Float32Array(n);
    this.mesh.py = new Float32Array(n);
    this.mesh.order = new Int32Array(n);
    this.mesh.hullA = new Int32Array(n);
    this.mesh.hullB = new Int32Array(n);

    // The eye rings ordered along their edge; the canonical model's rings are
    // already sequential, which lets the lid line be stroked as a loop.
    this.resize();
    return this;
  }

  resize() {
    if (!this.mesh) return;
    const box = this.canvas.parentElement?.getBoundingClientRect();
    const cssW = Math.max(1, Math.floor(box?.width || this.canvas.clientWidth || 1));
    const cssH = Math.max(1, Math.floor(box?.height || this.canvas.clientHeight || 1));
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    if (cssW === this.w && cssH === this.h && dpr === this.dpr) return;

    this.w = cssW;
    this.h = cssH;
    this.dpr = dpr;
    this.canvas.width = Math.round(cssW * dpr);
    this.canvas.height = Math.round(cssH * dpr);
    this.canvas.style.width = `${cssW}px`;
    this.canvas.style.height = `${cssH}px`;

    this.r = Math.min(cssW * 0.38, cssH * 0.42);
    this.cx = cssW * 0.5;
    this.cy = cssH * 0.44;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  start() {
    if (this._running) return;
    this._running = true;
    this._last = 0;
    this._kick();
  }

  stop() {
    this._running = false;
    if (this._raf) cancelAnimationFrame(this._raf);
    this._raf = 0;
  }

  _kick() {
    if (this._raf) return;
    this._raf = requestAnimationFrame((ts) => this._frame(ts));
  }

  _frame(ts) {
    this._raf = 0;
    if (!this._running) return;
    if (!this._visible) return;
    const dt = this._last ? clamp((ts - this._last) / 1000, 0.001, 0.1) : 0.016;
    this._last = ts;
    this.step(dt);
    this.paint();
    this._kick();
  }

  setVisible(visible) {
    const next = Boolean(visible) && !document.hidden;
    if (this._visible === next) return;
    this._visible = next;
    if (next) {
      this._last = 0;
      this.resize();
      if (this._running) this._kick();
    } else {
      if (this._raf) {
        cancelAnimationFrame(this._raf);
        this._raf = 0;
      }
    }
  }

  setState(state) {
    this.state = String(state || "STANDBY").toUpperCase();
  }

  setMuted(muted) {
    this.muted = Boolean(muted);
    if (this.muted) this.stopSpeech();
  }

  // ── speech ────────────────────────────────────────────────────────────────

  /**
   * @param {Array<[string, number]>} visemes  [viseme, duration-weight] pairs
   * @param {number} durationSec  how long the voice is expected to speak
   */
  beginSpeech(visemes, durationSec) {
    const items = Array.isArray(visemes) ? visemes.filter((v) => VISEME[v?.[0]]) : [];
    const total = items.reduce((sum, v) => sum + Math.max(0.1, Number(v[1]) || 1), 0);
    const seconds = Math.max(0.6, Number(durationSec) || 1);
    this._speech = {
      items,
      total: total || 1,
      step: clamp(seconds / (total || 1), 0.035, 0.17),
      elapsed: 0,
      index: 0
    };
    if (!items.length) this._speech.fallback = true;
  }

  endSpeech() {
    if (!this._speech) return;
    this._speech.done = true;
  }

  stopSpeech() {
    this._speech = null;
  }

  get speaking() {
    return Boolean(this._speech && !this._speech.done);
  }

  _advanceSpeech(dt) {
    const sp = this._speech;
    if (!sp) return [0, 0];
    sp.elapsed += dt;
    if (sp.fallback) {
      // No viseme schedule (text in a script the viseme engine skips). Keep a
      // syllable-shaped mouth rather than a frozen one.
      const o = 0.3 + 0.45 * Math.abs(Math.sin(sp.elapsed * 9.5));
      return [o, 0.15 * Math.sin(sp.elapsed * 4.1)];
    }
    let guard = 24;
    while (sp.index < sp.items.length && guard-- > 0) {
      const [name, weight] = sp.items[sp.index];
      const span = Math.max(0.035, weight * sp.step);
      if (sp.elapsed < span) {
        const [o, w, closure] = VISEME[name] || VISEME.REST;
        return [o * (1 - closure), w];
      }
      sp.elapsed -= span;
      sp.index += 1;
    }
    sp.done = true;
    return [0, 0];
  }

  // ── animation ─────────────────────────────────────────────────────────────

  step(dt) {
    dt = clamp(dt, 0.001, 0.1);
    this._t += dt;
    const t = this._t;
    const live = this.speaking && !this.muted;

    // Idle sway, integrated so its phase cannot jump when speech starts.
    const speed = (this.muted ? 0.55 : 1) * (live ? 1.25 : 1);
    this._sway = (this._sway || 0) + dt * speed;
    const s = this._sway;

    // ── mouth ──
    let openT = 0;
    let wideT = 0;
    if (live) {
      [openT, wideT] = this._advanceSpeech(dt);
    } else if (this._speech?.done) {
      this._speech = null;
    }
    const shift = openT > this.mouth ? TAU_OPEN : live ? TAU_SHUT : TAU_REST;
    this.mouth += (openT - this.mouth) * rate(dt, shift);
    if (this.mouth < 0.002) this.mouth = 0;
    this.wide += (wideT - this.wide) * rate(dt, TAU_WIDE);

    // ── expression ──
    this.emph += (this.mouth - this.emph) * rate(dt, this.mouth > this.emph ? 0.055 : 0.32);
    if (live) {
      if (t >= this.exprAt) {
        this.exprTgt = Math.random() * 1.35 - 0.35;
        this.exprAt = t + 1.1 + Math.random() * 2;
      }
    } else {
      this.exprTgt = 0;
      this.exprAt = t + 0.8;
    }
    this.expr += (this.exprTgt - this.expr) * 0.075;

    const browT = clamp(0.55 * this.mouth + 0.6 * this.expr + this.browBias, -0.4, 1.2);
    this.brow += (browT - this.brow) * 0.2;

    // ── state drives the face ──
    const st = this.state;
    const thinking = st === "THINKING" || st === "PROCESSING";
    const asleep = st === "STANDBY" || st === "OFFLINE" || st === "SLEEPING";

    if (thinking) {
      if (t >= this.biasAt) {
        this.biasTgt = [
          (Math.random() < 0.5 ? -1 : 1) * (0.45 + Math.random() * 0.35),
          0.25 + Math.random() * 0.3
        ];
        this.biasAt = t + 1.4 + Math.random() * 1.6;
      }
      this.browBias += (-0.28 - this.browBias) * 0.06;
      this.lids += (0.94 - this.lids) * 0.08;
    } else if (asleep) {
      this.biasTgt = [0, -0.2];
      this.browBias += (-0.05 - this.browBias) * 0.06;
      this.lids += (0.55 - this.lids) * 0.08;
    } else {
      this.biasTgt = [0, 0];
      this.browBias += ((st === "LISTENING" ? 0.1 : 0) - this.browBias) * 0.06;
      this.lids += ((st === "LISTENING" ? 1.04 : 0.98) - this.lids) * 0.08;
    }
    for (let i = 0; i < 2; i++) {
      this.gazeBias[i] += (this.biasTgt[i] - this.gazeBias[i]) * 0.06;
    }

    // Saccades: quick jumps, more frequent while speaking, slower while thinking.
    if (t >= this.gazeAt) {
      const reach = live ? 0.9 : thinking ? 0.35 : 0.55;
      this.gazeTgt = [
        (Math.random() * 2 - 1) * reach,
        (Math.random() * 2 - 1) * reach * 0.55
      ];
      this.gazeAt = t + (live ? 0.55 + Math.random() * 1.7
        : thinking ? 1.8 + Math.random() * 2.4
        : 1.3 + Math.random() * 2.8);
    }
    for (let i = 0; i < 2; i++) {
      this.gaze[i] += (clamp(this.gazeTgt[i] + this.gazeBias[i], -1, 1) - this.gaze[i]) * 0.3;
    }

    // ── blink ──
    if (this.blink > 0) {
      this.blink = Math.max(0, this.blink - dt * 8.5);
    } else if (t >= this.blinkAt) {
      if (asleep) this.blinkAt = t + 6;
      else {
        this.blink = 1;
        this.blinkAt = t + (thinking ? 5.5 : 3.4) + Math.random() * 3.1;
      }
    }

    this.glow += ((this.muted ? 0 : this.mouth) - this.glow) * 0.16;
  }

  // ── posing ────────────────────────────────────────────────────────────────

  _pose() {
    const m = this.mesh;
    const n = m.count;
    const v0 = m.v0;
    const v = m.v;
    const jawA = this.mouth * JAW_MAX;
    const useJaw = this.mouth > 0.004;
    const useBrow = Math.abs(this.brow) > 0.004;
    const useLip = Math.abs(this.wide) > 0.01 && this.mouth > 0;
    const [lcx, lcy] = m.lipC;

    const cj = Math.cos(jawA);
    const sj = Math.sin(jawA);
    const lipK = this.wide * this.mouth;
    const [px, py, pz] = JAW_PIVOT;

    const sway = this._sway || 0;
    const yaw = 0.26 * Math.sin(sway * 0.31) + 0.09 * Math.sin(sway * 0.73 + 1.3)
      + 0.018 * Math.sin(this._t * 1.7) * this.emph;
    const pitch = 0.06 * Math.sin(sway * 0.23 + 0.7) + 0.024 * Math.sin(sway * 0.61)
      - this.emph * 0.028;
    this._yaw = yaw;
    this._pitch = pitch;

    const cy_ = Math.cos(yaw);
    const sy_ = Math.sin(yaw);
    const cp = Math.cos(pitch);
    const sp = Math.sin(pitch);

    for (let i = 0, j = 0; i < n; i++, j += 3) {
      let x = v0[j];
      let y = v0[j + 1];
      let z = v0[j + 2];

      if (useBrow) y += m.browW[i] * this.brow * BROW_LIFT;

      if (useLip) {
        const k = m.lipW[i] * lipK;
        x += k * (x - lcx) * 0.55;
        y += k * (y - lcy) * 0.3;
        z -= k * 0.055;
      }

      if (useJaw) {
        const a = m.jawW[i] * jawA;
        if (a !== 0) {
          const ca = Math.cos(a);
          const sa = Math.sin(a);
          const dy = y - py;
          const dz = z - pz;
          y = py + dy * ca - dz * sa;
          z = pz + dy * sa + dz * ca;
        }
      }

      // Rotate head-local → view.
      const x1 = cy_ * x + sy_ * z;
      const y1 = sp * sy_ * x + cp * y - sp * cy_ * z;
      const z1 = -cp * sy_ * x + sp * y + cp * cy_ * z;
      v[j] = x1;
      v[j + 1] = y1;
      v[j + 2] = z1;

      const nx = m.n0[j];
      const ny = m.n0[j + 1];
      const nz = m.n0[j + 2];
      m.n[j] = cy_ * nx + sy_ * nz;
      m.n[j + 1] = sp * sy_ * nx + cp * ny - sp * cy_ * nz;
      m.n[j + 2] = -cp * sy_ * nx + sp * ny + cp * cy_ * nz;
    }

    // Project.
    const r = this.r;
    const cx = this.cx;
    const cy = this.cy;
    const px_ = m.px;
    const py_ = m.py;
    for (let i = 0, j = 0; i < n; i++, j += 3) {
      const w = Math.max(0.35, CAM_D - v[j + 2]);
      const k = (CAM_D / w) * r;
      px_[i] = cx + v[j] * k;
      py_[i] = cy - v[j + 1] * k;
    }
  }

  _hull(start, end, dest) {
    const m = this.mesh;
    return hullIndices(m.px, m.py, start, end, m.order, dest);
  }

  _buildHullPath(count, hull) {
    const p = new Path2D();
    const px = this.mesh.px;
    const py = this.mesh.py;
    for (let k = 0; k < count; k++) {
      const i = hull[k];
      if (k === 0) p.moveTo(px[i], py[i]);
      else p.lineTo(px[i], py[i]);
    }
    p.closePath();
    return p;
  }

  _ringMean(idx) {
    const px = this.mesh.px;
    const py = this.mesh.py;
    let x = 0;
    let y = 0;
    for (let k = 0; k < idx.length; k++) {
      x += px[idx[k]];
      y += py[idx[k]];
    }
    return [x / idx.length, y / idx.length];
  }

  // ── painting ──────────────────────────────────────────────────────────────

  paint() {
    if (!this.mesh) return;
    const ctx = this.ctx;
    const w = this.w;
    const h = this.h;
    if (!w || !h) return;
    this._pose();

    ctx.clearRect(0, 0, w, h);

    const r = this.r;
    const cx = this.cx;
    const cy = this.cy;
    const amp = this.glow;

    // Aura.
    const aura = ctx.createRadialGradient(cx, cy, 0, cx, cy, r * 1.95);
    aura.addColorStop(0, `rgba(0, 212, 255, ${0.16 + 0.12 * amp})`);
    aura.addColorStop(0.38, `rgba(0, 212, 255, ${0.08 + 0.06 * amp})`);
    aura.addColorStop(1, "rgba(0, 212, 255, 0)");
    ctx.fillStyle = aura;
    ctx.beginPath();
    ctx.arc(cx, cy, r * 1.95, 0, Math.PI * 2);
    ctx.fill();

    const nHead = this.mesh.nHead;
    const nAll = this.mesh.count;

    const headCount = this._hull(0, nHead, this.mesh.hullA);
    const headP = this._buildHullPath(headCount, this.mesh.hullA);
    const neckCount = this._hull(nHead, nAll, this.mesh.hullB);
    const neckP = this._buildHullPath(neckCount, this.mesh.hullB);

    // Neck: fades towards the bottom instead of needing shoulders.
    const neckGrad = ctx.createLinearGradient(0, cy + r * 0.35, 0, cy + r * 1.45);
    neckGrad.addColorStop(0, "rgba(30, 96, 116, 0.88)");
    neckGrad.addColorStop(0.45, "rgba(12, 48, 62, 0.66)");
    neckGrad.addColorStop(1, "rgba(3, 16, 24, 0)");
    ctx.fillStyle = neckGrad;
    ctx.fill(neckP);

    // ── head base ──
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    const px = this.mesh.px;
    const py = this.mesh.py;
    for (let i = 0; i < nHead; i++) {
      if (px[i] < minX) minX = px[i];
      if (px[i] > maxX) maxX = px[i];
      if (py[i] < minY) minY = py[i];
      if (py[i] > maxY) maxY = py[i];
    }
    const bw = Math.max(1, maxX - minX);
    const bh = Math.max(1, maxY - minY);

    ctx.save();
    ctx.clip(headP);

    const base = ctx.createRadialGradient(
      minX + bw * 0.36, minY + bh * 0.28, r * 0.05,
      minX + bw * 0.5, minY + bh * 0.52, Math.max(bw, bh) * 0.72
    );
    base.addColorStop(0, "rgba(128, 208, 222, 0.95)");
    base.addColorStop(0.38, "rgba(52, 124, 144, 0.96)");
    base.addColorStop(0.72, "rgba(16, 54, 70, 0.97)");
    base.addColorStop(1, "rgba(3, 18, 27, 0.98)");
    ctx.fillStyle = base;
    ctx.fillRect(minX - 2, minY - 2, bw + 4, bh + 4);

    // Directional volume: a broad shadow on the viewer's right and under the
    // jaw. Without it the smooth base reads as a painted egg, however many
    // small highlights sit on top.
    const side = ctx.createLinearGradient(minX, 0, maxX, 0);
    side.addColorStop(0, "rgba(214, 255, 255, 0.10)");
    side.addColorStop(0.34, "rgba(214, 255, 255, 0)");
    side.addColorStop(0.66, "rgba(0, 10, 18, 0)");
    side.addColorStop(1, "rgba(0, 10, 18, 0.46)");
    ctx.fillStyle = side;
    ctx.fillRect(minX, minY, bw, bh);

    const depth = ctx.createLinearGradient(0, minY, 0, maxY);
    depth.addColorStop(0, "rgba(0, 10, 18, 0)");
    depth.addColorStop(0.72, "rgba(0, 10, 18, 0)");
    depth.addColorStop(1, "rgba(0, 8, 16, 0.42)");
    ctx.fillStyle = depth;
    ctx.fillRect(minX, minY, bw, bh);

    // Broad light and shade, anchored to the real landmarks.
    const lm = this.mesh.lm;
    const [exl, ey] = this._ringMean(lm.eye_l);
    const [exr] = this._ringMean(lm.eye_r);
    const [lx, ly] = this._ringMean(lm.lips_out);
    const [bx, by] = this._ringMean(lm.brow_l);

    const glow = (x, y, rx, ry, colour, alpha) => {
      if (rx <= 0 || ry <= 0) return;
      ctx.save();
      ctx.translate(x, y);
      ctx.scale(1, ry / rx);
      const g = ctx.createRadialGradient(0, 0, 0, 0, 0, rx);
      g.addColorStop(0, colour.replace("ALPHA", alpha.toFixed(3)));
      g.addColorStop(0.55, colour.replace("ALPHA", (alpha * 0.34).toFixed(3)));
      g.addColorStop(1, colour.replace("ALPHA", "0"));
      ctx.fillStyle = g;
      ctx.fillRect(-rx, -rx, rx * 2, rx * 2);
      ctx.restore();
    };

    const sheen = "rgba(206, 250, 255, ALPHA)";
    const shade = "rgba(0, 10, 18, ALPHA)";
    const eyeMid = (exl + exr) * 0.5;

    // Ambient: down the viewer's right and under the jaw.
    glow(eyeMid + bw * 0.20, ey + bh * 0.22, bw * 0.17, bh * 0.28, shade, 0.46);
    glow(eyeMid, ly + bh * 0.11, bw * 0.22, bh * 0.10, shade, 0.40);
    glow(eyeMid, minY + bh * 0.98, bw * 0.16, bh * 0.10, shade, 0.34);

    // Key light: forehead, left cheek, nose ridge, chin.
    glow(eyeMid - bw * 0.03, minY + bh * 0.13, bw * 0.17, bh * 0.18, sheen, 0.30);
    glow(eyeMid - bw * 0.11, ey + bh * 0.17, bw * 0.10, bh * 0.13, sheen, 0.28);
    glow(eyeMid - bw * 0.002, ey + bh * 0.10, bw * 0.012, bh * 0.13, "rgba(240, 255, 255, ALPHA)", 0.32);
    glow(eyeMid, ly + bh * 0.16, bw * 0.06, bh * 0.05, sheen, 0.22);

    // Nose: side shadows are asymmetric on purpose.
    const noseY = ey + (ly - ey) * 0.55;
    glow(eyeMid - bw * 0.02, noseY, bw * 0.018, bh * 0.06, shade, 0.22);
    glow(eyeMid + bw * 0.02, noseY, bw * 0.018, bh * 0.065, shade, 0.36);
    glow(eyeMid, ly - bh * 0.05, bw * 0.055, bh * 0.02, shade, 0.42);
    glow(eyeMid - bw * 0.03, ly - bh * 0.04, bw * 0.015, bh * 0.012, "rgba(240, 255, 255, ALPHA)", 0.24);
    glow(eyeMid + bw * 0.03, ly - bh * 0.04, bw * 0.014, bh * 0.011, "rgba(240, 255, 255, ALPHA)", 0.18);

    // Eye sockets.
    glow(exl, ey - bh * 0.025, bw * 0.075, bh * 0.038, shade, 0.28);
    glow(exr, ey - bh * 0.025, bw * 0.075, bh * 0.038, shade, 0.30);

    // Brow ridge and cheekbones.
    glow(bx, by - bh * 0.012, bw * 0.105, bh * 0.024, sheen, 0.20);
    glow(exl, ey + bh * 0.13, bw * 0.105, bh * 0.065, sheen, 0.18);
    glow(exr, ey + bh * 0.13, bw * 0.095, bh * 0.06, sheen, 0.13);
    glow(exl, ey + bh * 0.27, bw * 0.11, bh * 0.07, shade, 0.18);
    glow(exr, ey + bh * 0.27, bw * 0.10, bh * 0.07, shade, 0.24);

    // Scan sweep — the holographic tell.
    const sweepY = cy - r * (0.2 + 0.5 * Math.sin(this._t * 0.43));
    const band = ctx.createLinearGradient(0, sweepY - r * 0.16, 0, sweepY + r * 0.16);
    band.addColorStop(0, "rgba(0, 224, 255, 0)");
    band.addColorStop(0.5, `rgba(0, 240, 255, ${0.10 + 0.06 * amp})`);
    band.addColorStop(1, "rgba(0, 224, 255, 0)");
    ctx.fillStyle = band;
    ctx.fillRect(minX, sweepY - r * 0.16, bw, r * 0.32);

    ctx.restore();

    // Silhouette rim.
    ctx.strokeStyle = "rgba(120, 235, 255, 0.30)";
    ctx.lineWidth = 1.1;
    ctx.stroke(headP);

    this._paintWire(ctx, amp);
    this._paintFeatures(ctx, amp);
  }

  _paintWire(ctx, amp) {
    const m = this.mesh;
    const normals = m.n;
    const edge = m.e;
    const px = m.px;
    const py = m.py;
    const fade = m.fade;
    const nHead = m.nHead;
    const sweep = this.cy - this.r * (0.2 + 0.5 * Math.sin(this._t * 0.43));
    const scanWorld = (this.cy - sweep) / Math.max(1, this.r);
    const buckets = [[], [], [], []];

    for (let k = 0; k < edge.length; k += 2) {
      const a = edge[k];
      const b = edge[k + 1];
      const normalZ = (normals[a * 3 + 2] + normals[b * 3 + 2]) * 0.5;
      if (normalZ <= -0.05) continue;
      // View-space y is close enough to head-local y for the sweep and the
      // chin fade; both are broad bands, not precise masks.
      const vertsY = (m.v[a * 3 + 1] + m.v[b * 3 + 1]) * 0.5;
      let alpha = 0.032 + 0.052 * Math.pow(Math.abs(normalZ), 1.2);
      alpha += 0.045 * Math.exp(-Math.pow((vertsY - scanWorld) / 0.16, 2));
      alpha *= (fade[a] + fade[b]) * 0.5;
      alpha *= 0.8 + 0.45 * amp;
      alpha *= 0.3 + 0.7 * clamp((vertsY + 0.55) / 0.45, 0, 1);
      if (a >= nHead && b >= nHead) alpha *= 0.15;
      if (alpha < 0.018) continue;
      const bucket = Math.min(3, Math.max(0, Math.floor((alpha / 0.13) * 4)));
      buckets[bucket].push(px[a], py[a], px[b], py[b]);
    }

    for (let b = 0; b < 4; b++) {
      const seg = buckets[b];
      if (!seg.length) continue;
      const alpha = 0.042 + b * 0.040;
      ctx.beginPath();
      for (let i = 0; i < seg.length; i += 4) {
        ctx.moveTo(seg[i], seg[i + 1]);
        ctx.lineTo(seg[i + 2], seg[i + 3]);
      }
      ctx.strokeStyle = `rgba(214, 252, 255, ${alpha})`;
      ctx.lineWidth = 0.9;
      ctx.stroke();
    }
  }

  _paintFeatures(ctx, amp) {
    const m = this.mesh;
    const px = m.px;
    const py = m.py;
    const lm = m.lm;
    const face = Math.max(0, Math.cos(this._yaw) * Math.cos(this._pitch)) ** 2;
    if (face < 0.02) return;

    const vis = (1 - this.blink) * this.lids;

    // ── eyes ──
    for (const key of ["eye_l", "eye_r"]) {
      const idx = lm[key];
      let midY = 0;
      let midX = 0;
      for (const i of idx) {
        midY += py[i];
        midX += px[i];
      }
      midY /= idx.length;
      midX /= idx.length;

      // The measured rings trace the closed lids. Opening them by half again
      // is what turns a line into an eye at HUD size; the position and shape
      // still come from the real landmarks, and the blink compresses this
      // same ring back down.
      const ptsX = [];
      const ptsY = [];
      for (const i of idx) {
        ptsX.push(midX + (px[i] - midX) * 1.06);
        ptsY.push(midY + (py[i] - midY) * 1.5 * Math.max(0.05, vis));
      }

      ctx.beginPath();
      ctx.moveTo(ptsX[0], ptsY[0]);
      for (let k = 1; k < ptsX.length; k++) ctx.lineTo(ptsX[k], ptsY[k]);
      ctx.closePath();

      ctx.fillStyle = "rgba(2, 20, 30, 0.62)";
      ctx.fill();
      ctx.strokeStyle = `rgba(0, 212, 255, ${(0.60 * face).toFixed(3)})`;
      ctx.lineWidth = 1.2;
      ctx.stroke();

      if (vis > 0.3) {
        let bx0 = Infinity;
        let bx1 = -Infinity;
        let by0 = Infinity;
        let by1 = -Infinity;
        for (let k = 0; k < ptsX.length; k++) {
          bx0 = Math.min(bx0, ptsX[k]);
          bx1 = Math.max(bx1, ptsX[k]);
          by0 = Math.min(by0, ptsY[k]);
          by1 = Math.max(by1, ptsY[k]);
        }
        const ewx = Math.max(1, bx1 - bx0);
        const ewy = Math.max(1, by1 - by0);
        const gx = (bx0 + bx1) / 2 + this.gaze[0] * ewx * 0.16;
        const gy = (by0 + by1) / 2 + this.gaze[1] * ewy * 0.2;
        const rad = Math.max(2.2, Math.min(ewy * 0.95, ewx * 0.26));

        ctx.save();
        ctx.beginPath();
        ctx.moveTo(ptsX[0], ptsY[0]);
        for (let k = 1; k < ptsX.length; k++) ctx.lineTo(ptsX[k], ptsY[k]);
        ctx.closePath();
        ctx.clip();

        // A pale sclera keeps the eye from reading as a dark slit at HUD size.
        ctx.fillStyle = `rgba(176, 236, 248, ${0.42 * face * vis})`;
        ctx.fillRect(bx0, by0, ewx, ewy);

        ctx.fillStyle = `rgba(126, 168, 255, ${(0.72 + 0.2 * amp) * face * vis})`;
        ctx.beginPath();
        ctx.ellipse(gx, gy, rad, Math.max(1.4, rad * Math.max(0.25, vis)), 0, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = `rgba(3, 8, 22, ${0.92 * face * vis})`;
        ctx.beginPath();
        ctx.arc(gx, gy, Math.max(1, rad * 0.42), 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = `rgba(230, 255, 255, ${0.8 * face * vis})`;
        ctx.beginPath();
        ctx.arc(gx - rad * 0.32, gy - rad * 0.34, Math.max(0.6, rad * 0.18), 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
      }
    }

    // ── brows ──
    ctx.strokeStyle = `rgba(8, 44, 58, ${0.62 * face})`;
    ctx.lineWidth = 1.6;
    ctx.lineCap = "round";
    for (const key of ["brow_l", "brow_r"]) {
      const idx = lm[key];
      ctx.beginPath();
      for (let k = 0; k < idx.length; k++) {
        const i = idx[k];
        if (k === 0) ctx.moveTo(px[i], py[i]);
        else ctx.lineTo(px[i], py[i]);
      }
      ctx.stroke();
    }

    // ── mouth ──
    const outer = lm.lips_out;
    const inner = lm.lips_in;
    const outerPoly = new Path2D();
    for (let k = 0; k < outer.length; k++) {
      const i = outer[k];
      if (k === 0) outerPoly.moveTo(px[i], py[i]);
      else outerPoly.lineTo(px[i], py[i]);
    }
    outerPoly.closePath();

    const innerPoly = new Path2D();
    for (let k = 0; k < inner.length; k++) {
      const i = inner[k];
      if (k === 0) innerPoly.moveTo(px[i], py[i]);
      else innerPoly.lineTo(px[i], py[i]);
    }
    innerPoly.closePath();

    let iy0 = Infinity;
    let iy1 = -Infinity;
    for (const i of inner) {
      iy0 = Math.min(iy0, py[i]);
      iy1 = Math.max(iy1, py[i]);
    }
    const openH = Math.max(1, iy1 - iy0);

    ctx.fillStyle = `rgba(186, 116, 150, ${0.30 * face})`;
    ctx.fill(outerPoly);

    if (this.mouth > 0.02) {
      ctx.fillStyle = `rgba(2, 12, 20, ${(0.55 + 0.3 * this.mouth) * face})`;
      ctx.fill(innerPoly);

      // Upper teeth: one bright strip under the upper lip.
      const up = [];
      for (let k = 10; k < inner.length; k++) up.push(inner[k]);
      up.push(inner[0]);
      const th = openH * 0.3;
      ctx.beginPath();
      for (let k = 0; k < up.length; k++) {
        const i = up[k];
        if (k === 0) ctx.moveTo(px[i], py[i]);
        else ctx.lineTo(px[i], py[i]);
      }
      for (let k = up.length - 1; k >= 0; k--) {
        const i = up[k];
        ctx.lineTo(px[i], py[i] + th);
      }
      ctx.closePath();
      ctx.fillStyle = `rgba(196, 245, 250, ${(0.40 + 0.2 * this.mouth) * face})`;
      ctx.fill();
    }

    ctx.strokeStyle = `rgba(198, 132, 162, ${(0.55 + 0.2 * this.mouth) * face})`;
    ctx.lineWidth = 1.2;
    ctx.stroke(innerPoly);
    ctx.strokeStyle = `rgba(190, 124, 158, ${0.32 * face})`;
    ctx.lineWidth = 1.0;
    ctx.stroke(outerPoly);
  }
}
