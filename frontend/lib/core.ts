/**
 * Jarvis's core — the approved Home design's canvas (rays, drifting dots, rings,
 * ambient light, horizon arcs), drawn in plain 2D canvas.
 *
 * It replaced a three.js shader sphere: the design is flat and luminous rather
 * than a lit 3D object, and 2D canvas needs no WebGL context, no 600 KB library
 * and no fallback path. One animation loop draws the core and the two small
 * level meters that follow it (under the mic, and in the header's state pill),
 * so all three always agree.
 *
 * **It never re-renders React.** Everything it reads each frame comes through
 * `input()`, a function the caller backs with refs. The loop caps itself at ~30
 * frames a second and draws nothing while the canvas is off screen or the tab
 * is hidden — Home kept open in a background tab costs nothing.
 */
import type { JarvisState } from './jarvis-state';

export type Presence = 'engaged' | 'resting' | 'waking';
export type CoreLayout = 'desk' | 'tabletP' | 'phone' | 'phoneChat';

export interface CoreInput {
  state: JarvisState;
  presence: Presence;
  restLook: 'clock' | 'ember' | 'dim';
  activation: 'full' | 'quick' | 'instant';
  layout: CoreLayout;
  /** A real 0..1 level from the voice engine (mic while listening, output while
   *  speaking), or null to use the designed motion for the state. */
  level: number | null;
}

const RGB: Record<JarvisState, [number, number, number]> = {
  standby: [214, 222, 233],
  listening: [34, 211, 238],
  thinking: [167, 139, 250],
  processing: [245, 165, 36],
  speaking: [255, 59, 48],
};

/** How strongly each state drives each kind of motion: outward pulse, inward
 *  pull, swirl, and orbiting rings. */
const WEIGHTS: Record<JarvisState, { out: number; in: number; sw: number; orb: number }> = {
  standby: { out: 0.12, in: 0, sw: 0, orb: 0 },
  listening: { out: 0, in: 1, sw: 0, orb: 0 },
  thinking: { out: 0, in: 0, sw: 1, orb: 0 },
  processing: { out: 0, in: 0, sw: 0, orb: 1 },
  speaking: { out: 1, in: 0, sw: 0, orb: 0 },
};

const REST = {
  ember: { core: 0.2, amb: 0, bright: 0.55 },
  clock: { core: 0.24, amb: 0.1, bright: 0.6 },
  dim: { core: 0.78, amb: 0.35, bright: 0.5 },
};

export interface Core {
  /** A small level meter drawn by the same loop (null to detach). `gap` is
   *  the empty middle (where the mic sits), `bars` per side. */
  attachMeter(slot: 'wave' | 'pill', canvas: HTMLCanvasElement | null): void;
  destroy(): void;
}

export function createCore(canvas: HTMLCanvasElement, input: () => CoreInput): Core {
  const ctx = canvas.getContext('2d');
  const R = Math.random;
  const meters: { wave: HTMLCanvasElement | null; pill: HTMLCanvasElement | null } = { wave: null, pill: null };

  const rays = Array.from({ length: 460 }, () => ({ a: R() * 6.2832, len: 0.18 + Math.pow(R(), 0.7) * 0.82, ph: R() * 6.28, w: R() }));
  const dots = Array.from({ length: 520 }, () => ({ a: R() * 6.2832, f: R(), sp: 0.04 + R() * 0.12, w: R(), j: R() }));
  const specks = Array.from({ length: 70 }, () => ({ x: R(), y: R(), r: 0.6 + Math.pow(R(), 3) * 4, ph: R() * 6.28, sp: 0.3 + R() * 1.2 }));
  const edge = (): [number, number] => {
    const k = Math.floor(R() * 4), u = R();
    return k === 0 ? [u, 0] : k === 1 ? [1, u] : k === 2 ? [u, 1] : [0, u];
  };
  const lines = Array.from({ length: 8 }, () => ({ p: edge(), q: [0.2 + R() * 0.6, 0.2 + R() * 0.6] as [number, number], ph: R(), sp: 0.04 + R() * 0.06 }));
  const bokeh = [[0.04, 0.95, 0.16], [0.98, 0.1, 0.12], [0.01, 0.38, 0.08], [0.95, 0.9, 0.13], [0.32, 0.02, 0.06], [0.7, 0.99, 0.07]]
    .map(([x, y, r]) => ({ x: x!, y: y!, r: r!, ph: R() * 6.28 }));

  // Film grain, made once.
  const grain = document.createElement('canvas');
  grain.width = grain.height = 160;
  const gx = grain.getContext('2d');
  if (gx) {
    const id = gx.createImageData(160, 160);
    for (let i = 0; i < id.data.length; i += 4) {
      const v = R() * 255;
      id.data[i] = id.data[i + 1] = id.data[i + 2] = v;
      id.data[i + 3] = 255;
    }
    gx.putImageData(id, 0, 0);
  }
  let pattern: CanvasPattern | null = null;

  const first = input();
  let rgb = [...RGB[first.state]];
  let lvl = 0.1;
  let A = first.presence === 'resting' ? 0 : 1;
  const w = { ...WEIGHTS[first.state] };
  let rotA = 0;
  let last = 0;
  let visible = true;
  let cxv: number | null = null, cyv = 0, Rv = 0, lastW = 0;

  const io = new IntersectionObserver((entries) => { visible = entries[0]?.isIntersecting ?? true; }, { threshold: 0.02 });
  io.observe(canvas);

  const fit = (c: HTMLCanvasElement, dpr: number): [number, number] => {
    const W = Math.round(c.clientWidth * dpr), H = Math.round(c.clientHeight * dpr);
    if (W && H && (c.width !== W || c.height !== H)) { c.width = W; c.height = H; }
    return [W, H];
  };

  const bars = (c: HTMLCanvasElement | null, s: number, e: number, col: (a: number) => string,
                gap: number, n: number, mh: number) => {
    if (!c) return;
    const dpr = Math.min(1.5, window.devicePixelRatio || 1), [W, H] = fit(c, dpr);
    if (!W || !H) return;
    const x = c.getContext('2d');
    if (!x) return;
    x.clearRect(0, 0, W, H);
    const cx = W / 2, cy = H / 2, g0 = gap * dpr, step = (W / 2 - g0) / n, bw = Math.max(1, step * 0.42), p = new Path2D();
    for (let i = 0; i < n; i++) {
      const t = i / n, env = gap ? Math.pow(1 - t, 1.2) : 1;
      const a = (0.05 + e * 1.15 * env * (0.3 + 0.7 * Math.abs(Math.sin(i * 0.9 + s * 9) * Math.cos(i * 0.37 - s * 5)))) * H * 0.5 * mh;
      const h = Math.min(H * 0.5, Math.max(dpr, a));
      p.rect(cx - g0 - i * step - bw, cy - h, bw, h * 2);
      p.rect(cx + g0 + i * step, cy - h, bw, h * 2);
    }
    const lg = x.createLinearGradient(0, 0, W, 0);
    lg.addColorStop(0, col(0));
    lg.addColorStop(0.35, col(0.9));
    lg.addColorStop(0.65, col(0.9));
    lg.addColorStop(1, col(0));
    x.fillStyle = gap ? lg : col(0.95);
    x.fill(p);
  };

  const draw = (s: number, dt: number) => {
    if (!ctx) return;
    const dpr = Math.min(1.25, window.devicePixelRatio || 1), [W, H] = fit(canvas, dpr);
    if (!W || !H) return;
    const inp = input();
    const P = inp.presence, st = P === 'resting' ? 'standby' : P === 'waking' ? 'listening' : inp.state;
    const target = RGB[st];
    const RS = REST[inp.restLook] ?? REST.clock;
    const DUR = inp.activation === 'quick' ? 0.8 : inp.activation === 'instant' ? 0.05 : 2.6;
    A = Math.max(0, Math.min(1, A + (P === 'resting' ? -dt / 1.4 : dt / DUR)));
    const sg = (a0: number, b0: number) => {
      const t = Math.max(0, Math.min(1, (A - a0) / (b0 - a0)));
      return t * t * (3 - 2 * t);
    };
    const coreK = RS.core + (1 - RS.core) * sg(0.06, 0.5), amb = RS.amb + (1 - RS.amb) * sg(0.3, 0.9);
    const hzK = RS.amb > 0.2 ? 1 : sg(0.18, 0.6), coreA = RS.bright + (1 - RS.bright) * sg(0.04, 0.35);
    rgb = rgb.map((v, i) => v + (target[i]! - v) * 0.07);
    const [r, g, b] = rgb.map((v) => Math.round(v)) as [number, number, number];
    const col = (a: number) => `rgba(${r},${g},${b},${a < 0 ? 0 : a > 1 ? 1 : a})`;

    // The level: the engine's real one when it has one, otherwise the designed
    // motion for the state.
    const voice = Math.abs(Math.sin(s * 3.1) * Math.sin(s * 1.3 + 1)) * 0.8 + 0.2 * Math.abs(Math.sin(s * 11.7));
    const designed = {
      standby: 0.1 + 0.03 * Math.sin(s * 0.8),
      listening: 0.22 + 0.5 * voice,
      thinking: 0.3 + 0.06 * Math.sin(s * 2.2),
      processing: 0.36,
      speaking: 0.45 + 0.5 * Math.abs(Math.sin(s * 6.2) * Math.cos(s * 2.3)),
    }[st];
    const real = inp.level;
    const tg = real == null || (st !== 'listening' && st !== 'speaking')
      ? designed
      : (st === 'listening' ? 0.22 : 0.45) + Math.min(1, real * 2.2) * 0.5;
    const br = 0.05 + 0.035 * Math.sin(s * 0.9), tg2 = A < 1 ? br + (tg - br) * sg(0.1, 0.55) : tg;
    lvl += (tg2 - lvl) * 0.14;
    const e = lvl;
    const wt = WEIGHTS[st];
    (Object.keys(w) as (keyof typeof w)[]).forEach((k) => { w[k] += (wt[k] - w[k]) * 0.05; });
    rotA += dt * (0.02 + w.sw * 0.45 + w.orb * 0.08);

    const x = ctx;
    x.setTransform(1, 0, 0, 1, 0, 0);
    x.clearRect(0, 0, W, H);
    const tcx = W / 2;
    let tcy: number, tR: number;
    if (inp.layout === 'phoneChat') { tcy = 34 * dpr; tR = 24 * dpr; }
    else if (inp.layout === 'phone') { tcy = H * 0.4; tR = Math.min(W * 0.95, H * 0.5) * 0.5; }
    else if (inp.layout === 'tabletP') {
      const top = 120, bot = H / dpr - 596;
      tcy = ((top + bot) / 2) * dpr;
      tR = Math.min(W * 0.7, (bot - top) * dpr) * 0.5;
    } else {
      tcy = ((64 + (H / dpr - 150)) / 2) * dpr;
      tR = Math.min(W * 0.5, (H / dpr - 200) * dpr) * 0.5;
    }
    if (cxv == null || lastW !== W) { cxv = tcx; cyv = tcy; Rv = tR; lastW = W; }
    cxv += (tcx - cxv) * 0.14;
    cyv += (tcy - cyv) * 0.14;
    Rv += (tR - Rv) * 0.14;
    const cx = cxv, cy = cyv, Rr = Rv * coreK;

    // Ambient light and the horizon.
    x.globalAlpha = amb;
    let gr = x.createRadialGradient(cx, cy, 0, cx, cy, Math.max(W, H) * 0.7);
    gr.addColorStop(0, col(0.1 + e * 0.08));
    gr.addColorStop(0.5, col(0.025));
    gr.addColorStop(1, 'rgba(0,0,0,0)');
    x.fillStyle = gr;
    x.fillRect(0, 0, W, H);
    const hz = x.createLinearGradient(0, 0, W, 0);
    hz.addColorStop(0, col(0));
    hz.addColorStop(0.5, col(0.42));
    hz.addColorStop(1, col(0));
    x.strokeStyle = hz;
    x.lineWidth = 1.4 * dpr;
    x.beginPath();
    x.ellipse(W / 2, H * 1.62, W * 0.78, H * 0.8, 0, Math.PI * (1.5 - 0.4 * hzK), Math.PI * (1.5 + 0.4 * hzK));
    x.stroke();
    x.lineWidth = 1 * dpr;
    x.globalAlpha = 0.45 * amb;
    x.beginPath();
    x.ellipse(W / 2, H * 1.7, W * 0.95, H * 0.86, 0, Math.PI * 1.08, Math.PI * 1.92);
    x.stroke();
    x.globalAlpha = 0.22 * amb;
    x.beginPath();
    x.ellipse(W / 2, H * 1.8, W * 1.1, H * 0.93, 0, Math.PI * 1.06, Math.PI * 1.94);
    x.stroke();
    x.globalAlpha = amb;
    x.save();
    x.translate(cx, H * 0.82);
    x.scale(1, 0.16);
    const fg = x.createRadialGradient(0, 0, 0, 0, 0, W * 0.3);
    fg.addColorStop(0, col(0.16 + e * 0.12));
    fg.addColorStop(1, col(0));
    x.fillStyle = fg;
    x.beginPath();
    x.arc(0, 0, W * 0.3, 0, 6.2832);
    x.fill();
    x.restore();
    for (const l of lines) {
      const x0 = l.p[0] * W, y0 = l.p[1] * H, x1 = l.q[0] * W, y1 = l.q[1] * H;
      const lg = x.createLinearGradient(x0, y0, x1, y1);
      lg.addColorStop(0, col(0.16));
      lg.addColorStop(1, col(0));
      x.strokeStyle = lg;
      x.lineWidth = 0.8 * dpr;
      x.beginPath();
      x.moveTo(x0, y0);
      x.lineTo(x1, y1);
      x.stroke();
      const t = (s * l.sp + l.ph) % 1;
      x.fillStyle = col(0.75 * (1 - t));
      x.beginPath();
      x.arc(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, 1.6 * dpr, 0, 6.2832);
      x.fill();
    }
    x.globalCompositeOperation = 'lighter';
    x.globalAlpha = amb;
    for (const k of bokeh) {
      const rr = k.r * Math.max(W, H), bx = k.x * W, by = k.y * H;
      const bg = x.createRadialGradient(bx, by, 0, bx, by, rr);
      bg.addColorStop(0, col(0.14 + 0.05 * Math.sin(s * 0.4 + k.ph)));
      bg.addColorStop(1, col(0));
      x.fillStyle = bg;
      x.fillRect(bx - rr, by - rr, rr * 2, rr * 2);
    }
    for (const k of specks) {
      const a = (0.12 + 0.3 * (0.5 + 0.5 * Math.sin(s * k.sp + k.ph))) * (k.r > 2.5 ? 0.5 : 1);
      x.fillStyle = col(a);
      x.beginPath();
      x.arc(k.x * W, k.y * H, k.r * dpr, 0, 6.2832);
      x.fill();
      if (k.r > 2) {
        x.fillStyle = col(a * 0.15);
        x.beginPath();
        x.arc(k.x * W, k.y * H, k.r * 4 * dpr, 0, 6.2832);
        x.fill();
      }
    }

    // The core itself.
    x.globalAlpha = coreA;
    gr = x.createRadialGradient(cx, cy, 0, cx, cy, Rr * 1.1);
    gr.addColorStop(0, col(0.35 + e * 0.3));
    gr.addColorStop(0.35, col(0.1 + e * 0.1));
    gr.addColorStop(1, col(0));
    x.fillStyle = gr;
    x.beginPath();
    x.arc(cx, cy, Rr * 1.1, 0, 6.2832);
    x.fill();
    const rg = x.createRadialGradient(cx, cy, 0, cx, cy, Rr);
    const hc = `rgba(${Math.round(r + (255 - r) * 0.55)},${Math.round(g + (255 - g) * 0.55)},${Math.round(b + (255 - b) * 0.55)},`;
    rg.addColorStop(0, 'rgba(255,255,255,1)');
    rg.addColorStop(0.07, hc + '0.95)');
    rg.addColorStop(0.2, col(0.9));
    rg.addColorStop(0.6, col(0.38));
    rg.addColorStop(1, col(0));
    const tgr = x.createRadialGradient(cx, cy, 0, cx, cy, Rr * 1.25);
    tgr.addColorStop(0, hc + '1)');
    tgr.addColorStop(0.5, col(0.8));
    tgr.addColorStop(1, col(0));
    const rayPath = new Path2D(), bright = new Path2D(), tips = new Path2D(), sw = w.sw, r0 = Rr * 0.03;
    for (const q of rays) {
      const mod = 1 + (w.out + w.in) * e * 0.35 * Math.sin(s * (4 + q.w * 6) + q.ph);
      const L = Rr * q.len * (0.5 + e * 0.55) * mod * (1 - w.orb * 0.25);
      const a = q.a + rotA, Pth = q.w > 0.9 ? bright : rayPath;
      Pth.moveTo(cx + Math.cos(a) * r0, cy + Math.sin(a) * r0);
      let ex: number, ey: number;
      if (sw > 0.02) {
        const ca = a + sw * 0.45 * q.len, ea = a + sw * 0.9 * q.len;
        ex = cx + Math.cos(ea) * L;
        ey = cy + Math.sin(ea) * L;
        Pth.quadraticCurveTo(cx + Math.cos(ca) * L * 0.55, cy + Math.sin(ca) * L * 0.55, ex, ey);
      } else {
        ex = cx + Math.cos(a) * L;
        ey = cy + Math.sin(a) * L;
        Pth.lineTo(ex, ey);
      }
      if (q.w > 0.55) {
        const ts = (0.5 + (q.w - 0.55) * 2.4) * dpr;
        tips.moveTo(ex + ts, ey);
        tips.arc(ex, ey, ts, 0, 6.2832);
      }
    }
    x.strokeStyle = rg;
    x.lineWidth = 0.7 * dpr;
    x.stroke(rayPath);
    x.lineWidth = 1.5 * dpr;
    x.stroke(bright);
    x.fillStyle = tgr;
    x.fill(tips);
    const speed = w.out * (0.3 + e * 1.8) - w.in * (0.25 + e * 1.4) + 0.08, dp = new Path2D(), hot = new Path2D();
    for (const d of dots) {
      d.f += dt * d.sp * speed;
      if (d.f > 1) d.f -= 1;
      if (d.f < 0) d.f += 1;
      d.a += dt * (sw * 0.8 * (1.2 - d.f) + w.orb * (0.6 + d.j * 0.6));
      const free = Rr * (0.04 + Math.pow(d.f, 1.2) * 0.96) * (0.6 + e * 0.5);
      const ring = Rr * (0.5 + d.j * 0.1 + (d.w > 0.5 ? 0.26 : 0)), rr = free + (ring - free) * w.orb;
      const px = cx + Math.cos(d.a + rotA) * rr, py = cy + Math.sin(d.a + rotA) * rr, sz = (0.6 + d.w * 1.3) * dpr;
      const Pth = d.w > 0.92 ? hot : dp;
      Pth.moveTo(px + sz, py);
      Pth.arc(px, py, sz, 0, 6.2832);
    }
    x.fillStyle = rg;
    x.fill(dp);
    x.fillStyle = 'rgba(255,246,242,0.85)';
    x.fill(hot);
    x.lineCap = 'round';
    if (w.orb > 0.02) {
      for (let k = 0; k < 2; k++) {
        const rr = Rr * (0.55 + k * 0.28), a0 = s * (1.4 - k * 0.6) * (k ? -1 : 1);
        x.lineWidth = 1.6 * dpr;
        x.strokeStyle = col(0.7 * w.orb);
        x.beginPath();
        x.arc(cx, cy, rr, a0, a0 + 1.1);
        x.stroke();
        x.lineWidth = 1 * dpr;
        x.strokeStyle = col(0.12 * w.orb);
        x.beginPath();
        x.arc(cx, cy, rr, 0, 6.2832);
        x.stroke();
      }
    }
    if (w.in > 0.02) {
      for (let k = 0; k < 3; k++) {
        const ph = 1 - ((s * 0.5 + k / 3) % 1);
        x.lineWidth = 1 * dpr;
        x.strokeStyle = col(0.3 * w.in * (1 - ph));
        x.beginPath();
        x.arc(cx, cy, Rr * (0.2 + ph * 0.85), 0, 6.2832);
        x.stroke();
      }
    }
    const wo = Math.max(0, w.out - 0.12);
    if (wo > 0.02) {
      for (let k = 0; k < 2; k++) {
        const ph = (s * 0.7 + k / 2) % 1;
        x.lineWidth = 1.2 * dpr;
        x.strokeStyle = col(0.32 * wo * (1 - ph));
        x.beginPath();
        x.arc(cx, cy, Rr * (0.25 + ph * 0.9), 0, 6.2832);
        x.stroke();
      }
    }
    const cr = Rr * 0.13 * (1 + e * 0.6);
    gr = x.createRadialGradient(cx, cy, 0, cx, cy, cr);
    gr.addColorStop(0, 'rgba(255,255,255,1)');
    gr.addColorStop(0.3, 'rgba(255,250,248,0.9)');
    gr.addColorStop(0.6, col(0.6));
    gr.addColorStop(1, col(0));
    x.fillStyle = gr;
    x.beginPath();
    x.arc(cx, cy, cr, 0, 6.2832);
    x.fill();
    // Waking: two shock rings and a flash.
    if (P === 'waking') {
      x.globalAlpha = 1;
      for (const [a0, a1, k] of [[0.05, 0.42, 1], [0.13, 0.56, 0.55]] as const) {
        const p = (A - a0) / (a1 - a0);
        if (p > 0 && p < 1) {
          x.lineWidth = (2.6 - p * 1.6) * dpr;
          x.strokeStyle = col(0.8 * k * (1 - p));
          x.beginPath();
          x.arc(cx, cy, Rv * (0.12 + p * 2.8), 0, 6.2832);
          x.stroke();
        }
      }
      const f = 1 - Math.abs(A - 0.1) / 0.1;
      if (f > 0) {
        const fg2 = x.createRadialGradient(cx, cy, 0, cx, cy, Rv * 1.5);
        fg2.addColorStop(0, `rgba(255,255,255,${0.6 * f})`);
        fg2.addColorStop(1, col(0));
        x.fillStyle = fg2;
        x.fillRect(0, 0, W, H);
      }
    }
    x.globalAlpha = 1;
    x.globalCompositeOperation = 'source-over';
    gr = x.createRadialGradient(W / 2, H / 2, Math.min(W, H) * 0.35, W / 2, H / 2, Math.max(W, H) * 0.75);
    gr.addColorStop(0, 'rgba(0,0,0,0)');
    gr.addColorStop(1, `rgba(0,0,0,${0.6 + 0.3 * (1 - amb)})`);
    x.fillStyle = gr;
    x.fillRect(0, 0, W, H);
    if (gx) {
      pattern = pattern ?? x.createPattern(grain, 'repeat');
      if (pattern) {
        x.globalAlpha = 0.05;
        x.fillStyle = pattern;
        x.fillRect(0, 0, W, H);
        x.globalAlpha = 1;
      }
    }
    bars(meters.wave, s, e, col, 50, 34, 0.9);
    bars(meters.pill, s, e, col, 0, 7, 1);
  };

  let raf = 0;
  const loop = (t: number) => {
    raf = requestAnimationFrame(loop);
    if (!visible || document.hidden || t - last < 32) return;
    const dt = Math.min(0.1, (t - (last || t)) / 1000);
    last = t;
    draw(t / 1000, dt);
  };
  raf = requestAnimationFrame(loop);

  return {
    attachMeter(slot, c) { meters[slot] = c; },
    destroy() {
      cancelAnimationFrame(raf);
      io.disconnect();
    },
  };
}
