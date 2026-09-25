'use client';

import * as THREE from 'three';

/**
 * Jarvis's orb — his face.
 *
 * A real three.js scene rather than a hand-rolled shader: an icosahedron whose
 * vertices are displaced by domain-warped noise, plus an additive back-facing
 * halo so it reads as glowing rather than as a lit ball. `setState` crossfades
 * between parameter presets over ~600ms, so a transition never snaps, and any
 * state with no preset falls back to idle — an engine emitting something new
 * degrades quietly instead of breaking.
 *
 * `getLevel()` is polled once per frame. The caller decides what it means: mic
 * energy while listening, Jarvis's own voice while speaking.
 *
 * Ported from the interface this replaces, shaders and tuning intact. The
 * numbers here were arrived at by looking at the thing on a screen; they are
 * not worth re-deriving.
 */

export type OrbState =
  | 'idle'
  | 'listening'
  | 'thinking'
  | 'speaking'
  | 'hearing_speech'
  | 'tool_running'
  | 'interrupted';

interface Preset {
  noiseAmp: number;
  noiseSpeed: number;
  swirl: number;
  rippleAmp: number;
  rippleSpeed: number;
  breathe: number;
  colorA: [number, number, number];
  colorB: [number, number, number];
  emissive: number;
}

const STATE_PRESETS: Record<OrbState, Preset> = {
  idle: {
    noiseAmp: 0.05, noiseSpeed: 0.05, swirl: 0.6,
    rippleAmp: 0.0, rippleSpeed: 0.0, breathe: 1.0,
    colorA: [0.1, 0.13, 0.22], colorB: [0.2, 0.34, 0.55], emissive: 0.15,
  },
  listening: {
    noiseAmp: 0.1, noiseSpeed: 0.12, swirl: 0.8,
    rippleAmp: 0.12, rippleSpeed: 2.0, breathe: 1.4,
    colorA: [0.06, 0.16, 0.24], colorB: [0.3, 0.64, 1.0], emissive: 0.35,
  },
  thinking: {
    noiseAmp: 0.16, noiseSpeed: 0.35, swirl: 1.0,
    rippleAmp: 0.1, rippleSpeed: 1.0, breathe: 0.6,
    colorA: [0.16, 0.09, 0.22], colorB: [0.62, 0.42, 1.0], emissive: 0.45,
  },
  speaking: {
    noiseAmp: 0.08, noiseSpeed: 0.1, swirl: 0.7,
    rippleAmp: 0.24, rippleSpeed: 6.0, breathe: 1.0,
    colorA: [0.05, 0.16, 0.12], colorB: [0.42, 0.89, 0.64], emissive: 0.5,
  },
  /** Actively being spoken to, as opposed to mic-open-and-quiet. */
  hearing_speech: {
    noiseAmp: 0.13, noiseSpeed: 0.16, swirl: 0.9,
    rippleAmp: 0.2, rippleSpeed: 3.0, breathe: 1.6,
    colorA: [0.05, 0.18, 0.28], colorB: [0.25, 0.75, 1.0], emissive: 0.45,
  },
  /** A tool call is in flight: warmer, and pulsing on its own — there is no
   *  audio to react to mid-call. */
  tool_running: {
    noiseAmp: 0.14, noiseSpeed: 0.28, swirl: 1.4,
    rippleAmp: 0.18, rippleSpeed: 3.5, breathe: 0.8,
    colorA: [0.18, 0.12, 0.04], colorB: [0.95, 0.6, 0.15], emissive: 0.5,
  },
  /** A barge-in just happened — held briefly, then back to listening. */
  interrupted: {
    noiseAmp: 0.05, noiseSpeed: 0.05, swirl: 0.5,
    rippleAmp: 0.35, rippleSpeed: 8.0, breathe: 1.0,
    colorA: [0.2, 0.05, 0.05], colorB: [1.0, 0.35, 0.3], emissive: 0.7,
  },
};

const VERTEX_SHADER = `
  uniform float uTime;
  uniform float uLevel;
  uniform float uNoiseAmp;
  uniform float uNoiseSpeed;
  uniform float uSwirl;
  uniform float uRippleAmp;
  uniform float uRippleSpeed;
  uniform float uBreathe;

  varying vec3 vNormal;
  varying float vDisp;

  float hash(vec3 p) {
    p = fract(p * 0.3183099 + 0.1);
    p *= 17.0;
    return fract(p.x * p.y * p.z * (p.x + p.y + p.z));
  }
  float noise(vec3 x) {
    vec3 i = floor(x);
    vec3 f = fract(x);
    f = f * f * (3.0 - 2.0 * f);
    return mix(
      mix(mix(hash(i + vec3(0,0,0)), hash(i + vec3(1,0,0)), f.x),
          mix(hash(i + vec3(0,1,0)), hash(i + vec3(1,1,0)), f.x), f.y),
      mix(mix(hash(i + vec3(0,0,1)), hash(i + vec3(1,0,1)), f.x),
          mix(hash(i + vec3(0,1,1)), hash(i + vec3(1,1,1)), f.x), f.y),
      f.z);
  }
  float fbm(vec3 p) {
    float v = 0.0;
    float a = 0.5;
    for (int i = 0; i < 4; i++) {
      v += a * noise(p);
      p *= 2.02;
      a *= 0.5;
    }
    return v;
  }
  float warpedNoise(vec3 p, float t) {
    vec3 q = vec3(fbm(p + vec3(0.0, 0.0, t * 0.3)),
                  fbm(p + vec3(5.2, 1.3, t * 0.25)),
                  fbm(p + vec3(3.1, 4.7, t * 0.2)));
    return fbm(p + uSwirl * q);
  }

  void main() {
    float t = uTime;
    vec3 dir = normalize(position);

    float n = warpedNoise(dir * 2.2 + t * uNoiseSpeed, t) - 0.5;
    float ripple = sin(t * uRippleSpeed - length(position) * 4.0) * uRippleAmp * (0.3 + uLevel * 0.7);
    float breathe = 1.0 + uBreathe * sin(t * 0.6) * 0.03;

    float disp = n * uNoiseAmp + ripple;
    vDisp = disp;
    vec3 displaced = position * breathe + dir * disp;

    vNormal = normalize(normalMatrix * normal);
    gl_Position = projectionMatrix * modelViewMatrix * vec4(displaced, 1.0);
  }
`;

const FRAGMENT_SHADER = `
  uniform vec3 uColorA;
  uniform vec3 uColorB;
  uniform float uEmissive;
  uniform float uLevel;

  varying vec3 vNormal;
  varying float vDisp;

  void main() {
    vec3 n = normalize(vNormal);
    vec3 viewDir = vec3(0.0, 0.0, 1.0);
    float fresnel = pow(1.0 - max(dot(n, viewDir), 0.0), 2.2);
    float diff = max(dot(n, normalize(vec3(0.5, 0.7, 0.9))), 0.0);

    vec3 base = mix(uColorA, uColorB, diff * 0.6 + 0.2 + vDisp * 0.4);
    vec3 col = base * (0.35 + diff * 0.5);
    col += fresnel * uColorB * 0.85;
    col += uColorB * uEmissive * (0.15 + uLevel * 0.35);

    gl_FragColor = vec4(col, 1.0);
  }
`;

const GLOW_VERTEX_SHADER = `
  varying vec3 vNormal;
  void main() {
    vNormal = normalize(normalMatrix * normal);
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`;

const GLOW_FRAGMENT_SHADER = `
  uniform vec3 uColorB;
  uniform float uEmissive;
  uniform float uLevel;
  varying vec3 vNormal;
  void main() {
    float fresnel = pow(1.0 - max(dot(normalize(vNormal), vec3(0.0, 0.0, 1.0)), 0.0), 3.0);
    gl_FragColor = vec4(uColorB, fresnel * (0.25 + uEmissive * 0.3 + uLevel * 0.25));
  }
`;

const MIX_MS = 600;
/** Raw RMS from a microphone sits around 0.012–0.19, nowhere near the 0..1 the
 *  shader wants. Below the floor is silence; the gain stretches the rest. */
const NOISE_FLOOR = 0.012;
const LEVEL_GAIN = 0.18;

const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
const lerpColor = (
  a: [number, number, number],
  b: [number, number, number],
  t: number,
): [number, number, number] => [lerp(a[0], b[0], t), lerp(a[1], b[1], t), lerp(a[2], b[2], t)];
const mapLevel = (raw: number) =>
  Math.max(0, Math.min(1, (raw - NOISE_FLOOR) / LEVEL_GAIN));

export interface Orb {
  setState: (state: OrbState) => void;
  start: () => void;
  stop: () => void;
  destroy: () => void;
  /** False when WebGL was unavailable and the caller should show the fallback. */
  readonly rendering: boolean;
}

export function createOrb(
  canvas: HTMLCanvasElement,
  options: { getLevel?: () => number; onFallback?: (state: OrbState) => void } = {},
): Orb {
  const { getLevel, onFallback } = options;

  let renderer: THREE.WebGLRenderer | null = null;
  let scene: THREE.Scene | null = null;
  let camera: THREE.PerspectiveCamera | null = null;
  let material: THREE.ShaderMaterial | null = null;
  let usingFallback = false;

  try {
    renderer = new THREE.WebGLRenderer({
      canvas,
      alpha: true,
      antialias: true,
      powerPreference: 'low-power',
    });
    renderer.setClearColor(0x000000, 0);

    scene = new THREE.Scene();
    camera = new THREE.PerspectiveCamera(45, 1, 0.1, 10);
    // Far enough back that the halo fades out inside the frame; any closer and
    // it is clipped by the canvas edge into a visible rounded square.
    camera.position.set(0, 0, 3.8);

    // Detail 5 is ~20,480 faces. Each +1 roughly quadruples that, and much
    // higher can hang the tab.
    const geometry = new THREE.IcosahedronGeometry(1, 5);
    material = new THREE.ShaderMaterial({
      uniforms: {
        uTime: { value: 0 },
        uLevel: { value: 0 },
        uNoiseAmp: { value: STATE_PRESETS.idle.noiseAmp },
        uNoiseSpeed: { value: STATE_PRESETS.idle.noiseSpeed },
        uSwirl: { value: STATE_PRESETS.idle.swirl },
        uRippleAmp: { value: STATE_PRESETS.idle.rippleAmp },
        uRippleSpeed: { value: STATE_PRESETS.idle.rippleSpeed },
        uBreathe: { value: STATE_PRESETS.idle.breathe },
        uColorA: { value: new THREE.Vector3(...STATE_PRESETS.idle.colorA) },
        uColorB: { value: new THREE.Vector3(...STATE_PRESETS.idle.colorB) },
        uEmissive: { value: STATE_PRESETS.idle.emissive },
      },
      vertexShader: VERTEX_SHADER,
      fragmentShader: FRAGMENT_SHADER,
    });
    scene.add(new THREE.Mesh(geometry, material));

    const glow = new THREE.ShaderMaterial({
      uniforms: material.uniforms,
      vertexShader: GLOW_VERTEX_SHADER,
      fragmentShader: GLOW_FRAGMENT_SHADER,
      transparent: true,
      side: THREE.BackSide,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    scene.add(new THREE.Mesh(new THREE.IcosahedronGeometry(1.35, 3), glow));
  } catch (err) {
    console.warn('[orb] WebGL unavailable, falling back to CSS:', err);
    renderer = null;
    usingFallback = true;
  }

  let currentState: OrbState = 'idle';
  let from: Preset = { ...STATE_PRESETS.idle };
  let to: Preset = { ...STATE_PRESETS.idle };
  let mixStart = 0;
  let smoothedLevel = 0;
  let running = false;
  let rafId = 0;
  let startTime = 0;
  let resizeObserver: ResizeObserver | null = null;

  function currentPreset(nowMs: number): Preset {
    const t = mixStart ? Math.min(1, (nowMs - mixStart) / MIX_MS) : 1;
    return {
      noiseAmp: lerp(from.noiseAmp, to.noiseAmp, t),
      noiseSpeed: lerp(from.noiseSpeed, to.noiseSpeed, t),
      swirl: lerp(from.swirl, to.swirl, t),
      rippleAmp: lerp(from.rippleAmp, to.rippleAmp, t),
      rippleSpeed: lerp(from.rippleSpeed, to.rippleSpeed, t),
      breathe: lerp(from.breathe, to.breathe, t),
      colorA: lerpColor(from.colorA, to.colorA, t),
      colorB: lerpColor(from.colorB, to.colorB, t),
      emissive: lerp(from.emissive, to.emissive, t),
    };
  }

  function resize() {
    if (!renderer || !camera) return;
    const rect = canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, Math.round(rect.width));
    const height = Math.max(1, Math.round(rect.height));
    renderer.setPixelRatio(dpr);
    renderer.setSize(width, height, false);
    camera.aspect = width / height || 1;
    camera.updateProjectionMatrix();
  }

  function frame(nowMs: number) {
    if (!running) return;
    rafId = requestAnimationFrame(frame);

    const raw = mapLevel(getLevel?.() ?? 0);
    // Fast attack, slower release: it swells with real speech instead of
    // jittering frame to frame.
    const k = raw > smoothedLevel ? 0.35 : 0.08;
    smoothedLevel += (raw - smoothedLevel) * k;

    if (!renderer || !scene || !camera || !material) return;

    const preset = currentPreset(nowMs);
    // three types `uniforms` as a possibly-sparse record. These are the ones
    // this material was built with, named once here rather than asserted at
    // every line below.
    const u = material.uniforms as {
      uTime: { value: number };
      uLevel: { value: number };
      uNoiseAmp: { value: number };
      uNoiseSpeed: { value: number };
      uSwirl: { value: number };
      uRippleAmp: { value: number };
      uRippleSpeed: { value: number };
      uBreathe: { value: number };
      uColorA: { value: THREE.Vector3 };
      uColorB: { value: THREE.Vector3 };
      uEmissive: { value: number };
    };
    u.uTime.value = (nowMs - startTime) / 1000;
    u.uLevel.value = smoothedLevel;
    u.uNoiseAmp.value = preset.noiseAmp;
    u.uNoiseSpeed.value = preset.noiseSpeed;
    u.uSwirl.value = preset.swirl;
    u.uRippleAmp.value = preset.rippleAmp;
    u.uRippleSpeed.value = preset.rippleSpeed;
    u.uBreathe.value = preset.breathe;
    u.uColorA.value.set(...preset.colorA);
    u.uColorB.value.set(...preset.colorB);
    u.uEmissive.value = preset.emissive;

    renderer.render(scene, camera);
  }

  function setState(state: OrbState) {
    const preset = STATE_PRESETS[state] ?? STATE_PRESETS.idle;
    if (state === currentState && mixStart === 0) return;
    from = currentPreset(performance.now());
    to = { ...preset };
    mixStart = performance.now();
    currentState = state;
    if (usingFallback) onFallback?.(state);
  }

  return {
    setState,
    get rendering() {
      return !usingFallback;
    },
    start() {
      if (running) return;
      running = true;
      startTime = performance.now();
      if (!resizeObserver && typeof ResizeObserver !== 'undefined') {
        resizeObserver = new ResizeObserver(() => resize());
        resizeObserver.observe(canvas);
      }
      resize();
      rafId = requestAnimationFrame(frame);
    },
    stop() {
      running = false;
      if (rafId) cancelAnimationFrame(rafId);
      rafId = 0;
    },
    destroy() {
      running = false;
      if (rafId) cancelAnimationFrame(rafId);
      resizeObserver?.disconnect();
      resizeObserver = null;
      renderer?.dispose();
    },
  };
}
