import type { Config } from 'tailwindcss';

// The visual language, in one place.
//
// The STRUCTURE of this interface is carried over exactly from the app it
// replaces — the header, the drawer, the centred orb, the conversation
// floating at the right, the composer beneath it. The LOOK is not: this is a
// deliberately quieter, deeper palette with softer edges and real translucency,
// which is what lets the conversation panel read as floating over the stage
// rather than sitting in a box next to it.
//
// Colours are tokens, never hex literals in a component: a screen that reaches
// for `#171a21` is a screen that will not follow the next change.
const config: Config = {
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}', './lib/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: {
          DEFAULT: '#e9ecf1',
          muted: '#98a0ae',
          faint: '#6a7280',
        },
        surface: {
          // The page itself. Deeper than the original's #0f1115 so the orb's
          // own light is the brightest thing on screen.
          DEFAULT: '#0b0d11',
          raised: '#141821',
          // Floating panels are translucent: they sit ON TOP of the stage, and
          // saying so in the material is more honest than drawing a hard box.
          panel: 'rgb(20 24 32 / 0.78)',
          overlay: 'rgb(8 10 14 / 0.72)',
          border: 'rgb(255 255 255 / 0.08)',
          'border-strong': 'rgb(255 255 255 / 0.14)',
        },
        accent: {
          DEFAULT: '#5b9cff',
          soft: '#2f4d7d',
          glow: 'rgb(91 156 255 / 0.28)',
        },
        state: {
          danger: '#ff6b6b',
          warn: '#f2b45c',
          ok: '#6be3a3',
        },
        bubble: {
          user: 'rgb(91 156 255 / 0.14)',
          assistant: 'rgb(255 255 255 / 0.05)',
        },
      },
      borderRadius: {
        sm: '10px',
        DEFAULT: '14px',
        lg: '18px',
        xl: '22px',
        pill: '999px',
      },
      boxShadow: {
        panel: '0 24px 60px -20px rgb(0 0 0 / 0.65)',
        focus: '0 0 0 3px rgb(91 156 255 / 0.28)',
      },
      transitionTimingFunction: {
        out: 'cubic-bezier(0.22, 0.61, 0.36, 1)',
      },
      keyframes: {
        'fade-up': {
          from: { opacity: '0', transform: 'translateY(6px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
        breathe: {
          '0%, 100%': { transform: 'scale(1)' },
          '50%': { transform: 'scale(1.04)' },
        },
      },
      animation: {
        'fade-up': 'fade-up 220ms cubic-bezier(0.22, 0.61, 0.36, 1)',
        breathe: 'breathe 4s ease-in-out infinite',
      },
    },
  },
  plugins: [],
};

export default config;
