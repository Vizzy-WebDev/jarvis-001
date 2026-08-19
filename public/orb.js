// Jarvis's 3D orb — his face. Built on vendored three.js (see
// vendor/three/README.md — the one deliberate external dependency in this
// front-end, chosen specifically for this) rather than a hand-rolled WebGL
// shader: a real scene graph, camera and lighting for a sphere whose surface
// is displaced by noise in a custom vertex shader (ShaderMaterial), lit with
// three's own lighting model on top. Falls back to a CSS gradient orb
// (driven by the same state classes) if WebGL isn't available or three
// fails to initialize — see .orb-fallback in style.css.
//
// setState('idle'|'listening'|'thinking'|'speaking') crossfades between four
// small parameter presets over ~600ms so transitions never snap. getLevel()
// is polled once per frame (0..1, smoothed here) — the caller decides what it
// means (mic energy while listening/dictating, Jarvis's own voice while
// speaking) via app.js's getLevel callback.

import * as THREE from './vendor/three/three.module.min.js';

// rippleAmp on listening/speaking raised from the shipped 0.05/0.14 —
// tuned alongside mapLevel() below (see its own comment): with raw mic/
// voice RMS actually reaching a usable 0..1 range now, instead of sitting
// near-zero, the ripple term needed real amplitude to make that response
// visible rather than just technically present. Every other parameter
// (noiseAmp/noiseSpeed/swirl/breathe/colours), and thinking/idle's own
// rippleAmp, are unchanged — those are what differentiate the four states
// and weren't the thing reported as invisible.
const STATE_PRESETS = {
  idle: {
    noiseAmp: 0.05, noiseSpeed: 0.05, swirl: 0.6,
    rippleAmp: 0.0, rippleSpeed: 0.0, breathe: 1.0,
    colorA: [0.10, 0.13, 0.22], colorB: [0.20, 0.34, 0.55], emissive: 0.15,
  },
  listening: {
    noiseAmp: 0.10, noiseSpeed: 0.12, swirl: 0.8,
    rippleAmp: 0.12, rippleSpeed: 2.0, breathe: 1.4,
    colorA: [0.06, 0.16, 0.24], colorB: [0.30, 0.64, 1.0], emissive: 0.35,
  },
  thinking: {
    noiseAmp: 0.16, noiseSpeed: 0.35, swirl: 2.2,
    rippleAmp: 0.02, rippleSpeed: 1.0, breathe: 0.6,
    colorA: [0.16, 0.09, 0.22], colorB: [0.62, 0.42, 1.0], emissive: 0.45,
  },
  speaking: {
    noiseAmp: 0.08, noiseSpeed: 0.10, swirl: 0.7,
    rippleAmp: 0.24, rippleSpeed: 6.0, breathe: 1.0,
    colorA: [0.05, 0.16, 0.12], colorB: [0.42, 0.89, 0.64], emissive: 0.5,
  },
};

// Vertex displacement (domain-warped fBm, same idea as a raymarched surface
// but applied to a real IcosahedronGeometry's vertices) plus an outward
// ripple driven by the live audio level — this is what makes "thinking"
// swirl internally rather than spinning the mesh, and "speaking" pulse with
// Jarvis's actual voice.
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

  // Cheap value noise / fbm — enough for an organic, non-repeating surface.
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

function lerp(a, b, t) {
  return a + (b - a) * t;
}

// getLevel() callers hand back raw RMS (see turn-detector.js's MicLevelMonitor
// and engines/*.js's getMicLevel/getOutputLevel) — real speech sits in
// roughly the 0.012-0.19 range, nowhere near the 0..1 the shader's uLevel
// actually needs, so without this the ripple term (see VERTEX_SHADER's
// `0.3 + uLevel*0.7`) barely moved and the orb read as "responsive" only in
// theory. NOISE_FLOOR is below typical residual mic/room noise (so silence
// reads as a flat 0, not a low hum); LEVEL_GAIN stretches the rest of that
// range up to 1.
const NOISE_FLOOR = 0.012;
const LEVEL_GAIN = 0.18;
function mapLevel(raw) {
  return Math.max(0, Math.min(1, (raw - NOISE_FLOOR) / LEVEL_GAIN));
}

function lerpColor(a, b, t) {
  return [lerp(a[0], b[0], t), lerp(a[1], b[1], t), lerp(a[2], b[2], t)];
}

function toVec3(arr) {
  return new THREE.Vector3(arr[0], arr[1], arr[2]);
}

/**
 * Creates the orb. `canvas` gets the three.js render; `fallbackEl` is shown
 * instead (with the same four state classes) if WebGL/three.js isn't
 * available. `getLevel()` is polled every frame — return 0..1.
 */
export function createOrb(canvas, { fallbackEl, getLevel } = {}) {
  let usingFallback = false;
  let renderer = null;
  let scene = null;
  let camera = null;
  let material = null;
  let glowMesh = null;

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
    // z was 3.2 — at that distance the glow halo (radius 1.35, below) is
    // wider than the camera's own visible frustum at the sphere's plane, so
    // it got clipped by the canvas edge into a visible rounded-square box
    // (not a container around the orb — the clipped halo WAS the box).
    // Pulled back to 3.8 to give the halo room to fade out inside the
    // frame; #orb-canvas's CSS size (below) is enlarged to compensate so
    // the sphere itself keeps its original on-screen diameter.
    camera.position.set(0, 0, 3.8);

    // NOTE: IcosahedronGeometry's 2nd argument is subdivision "detail", not a
    // segment count — each +1 roughly quadruples the face count (20*4^detail
    // faces). detail:5 is ~20,480 faces, plenty smooth for a displaced
    // sphere at this size; anything much higher can hang the tab.
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
        uColorA: { value: toVec3(STATE_PRESETS.idle.colorA) },
        uColorB: { value: toVec3(STATE_PRESETS.idle.colorB) },
        uEmissive: { value: STATE_PRESETS.idle.emissive },
      },
      vertexShader: VERTEX_SHADER,
      fragmentShader: FRAGMENT_SHADER,
    });
    const mesh = new THREE.Mesh(geometry, material);
    scene.add(mesh);

    // A soft outer halo — a slightly larger, additive, back-facing sphere —
    // so the orb reads as glowing rather than a flat lit ball. Lower detail
    // than the main mesh — it's a blurry fresnel glow, not displaced, so it
    // doesn't need many faces. See the main geometry's comment above on why
    // this argument is dangerous to raise.
    const glowGeometry = new THREE.IcosahedronGeometry(1.35, 3);
    const glowMaterial = new THREE.ShaderMaterial({
      uniforms: material.uniforms,
      vertexShader: `
        varying vec3 vNormal;
        void main() {
          vNormal = normalize(normalMatrix * normal);
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }
      `,
      fragmentShader: `
        uniform vec3 uColorB;
        uniform float uEmissive;
        uniform float uLevel;
        varying vec3 vNormal;
        void main() {
          float fresnel = pow(1.0 - max(dot(normalize(vNormal), vec3(0.0, 0.0, 1.0)), 0.0), 3.0);
          gl_FragColor = vec4(uColorB, fresnel * (0.25 + uEmissive * 0.3 + uLevel * 0.25));
        }
      `,
      transparent: true,
      side: THREE.BackSide,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    glowMesh = new THREE.Mesh(glowGeometry, glowMaterial);
    scene.add(glowMesh);
  } catch (err) {
    console.warn('[orb] three.js/WebGL unavailable, using CSS fallback:', err);
    renderer = null;
    usingFallback = true;
  }

  if (canvas) canvas.classList.toggle('hidden', usingFallback);
  if (fallbackEl) fallbackEl.classList.toggle('hidden', !usingFallback);

  let currentState = 'idle';
  let from = { ...STATE_PRESETS.idle };
  let to = { ...STATE_PRESETS.idle };
  let mixStart = 0;
  const MIX_MS = 600;

  let smoothedLevel = 0;
  let running = false;
  let rafId = null;
  let startTime = 0;
  let resizeObserver = null;

  function currentPreset(nowMs) {
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
    if (!renderer || !canvas) return;
    const rect = canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(rect.width));
    const h = Math.max(1, Math.round(rect.height));
    renderer.setPixelRatio(dpr);
    renderer.setSize(w, h, false);
    camera.aspect = w / h || 1;
    camera.updateProjectionMatrix();
  }

  function frame(nowMs) {
    if (!running) return;
    rafId = requestAnimationFrame(frame);

    const rawLevel = mapLevel(getLevel ? getLevel() || 0 : 0);
    // Exponential follower: fast attack, slower release, so it swells with
    // real speech rather than jittering frame to frame.
    const attack = 0.35;
    const release = 0.08;
    const k = rawLevel > smoothedLevel ? attack : release;
    smoothedLevel += (rawLevel - smoothedLevel) * k;

    if (usingFallback) return; // CSS animation drives the fallback; nothing to render

    const preset = currentPreset(nowMs);
    const t = (nowMs - startTime) / 1000;
    const u = material.uniforms;
    u.uTime.value = t;
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

  function setState(state) {
    const preset = STATE_PRESETS[state] || STATE_PRESETS.idle;
    if (state === currentState && mixStart === 0) return;
    from = currentPreset(performance.now());
    to = { ...preset };
    mixStart = performance.now();
    currentState = state;

    if (usingFallback && fallbackEl) {
      fallbackEl.classList.remove('idle', 'listening', 'thinking', 'speaking');
      fallbackEl.classList.add(state);
    }
  }

  function start() {
    if (running) return;
    running = true;
    startTime = performance.now();
    if (canvas && !resizeObserver && 'ResizeObserver' in window) {
      resizeObserver = new ResizeObserver(() => resize());
      resizeObserver.observe(canvas);
    }
    resize();
    rafId = requestAnimationFrame(frame);
  }

  function stop() {
    running = false;
    if (rafId) cancelAnimationFrame(rafId);
    rafId = null;
  }

  function destroy() {
    stop();
    if (resizeObserver) resizeObserver.disconnect();
    resizeObserver = null;
    if (renderer) renderer.dispose();
  }

  setState('idle');

  return { setState, start, stop, destroy };
}
