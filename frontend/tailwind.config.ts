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
      // Every colour is a CSS variable holding RGB channels (globals.css), so
      // the Appearance settings can re-theme the whole app — Dark, Light, the
      // person's own colours — by rewriting variables, never class names. The
      // channel form keeps Tailwind's opacity modifiers (`bg-accent/10`) working.
      colors: {
        ink: {
          DEFAULT: 'rgb(var(--ink) / <alpha-value>)',
          strong: 'rgb(var(--ink-strong) / <alpha-value>)',
          soft: 'rgb(var(--ink-soft) / <alpha-value>)',
          muted: 'rgb(var(--ink-muted) / <alpha-value>)',
          faint: 'rgb(var(--ink-faint) / <alpha-value>)',
        },
        surface: {
          DEFAULT: 'rgb(var(--bg) / <alpha-value>)',
          raised: 'rgb(var(--raised) / <alpha-value>)',
          // Floating panels are translucent: they sit ON TOP of the stage.
          panel: 'rgb(var(--panel) / 0.8)',
          overlay: 'rgb(var(--bg) / 0.7)',
          border: 'rgb(var(--line) / 0.16)',
          'border-strong': 'rgb(var(--line) / 0.26)',
        },
        line: 'rgb(var(--line) / <alpha-value>)',
        accent: {
          DEFAULT: 'rgb(var(--accent) / <alpha-value>)',
          hover: 'rgb(var(--accent-hover) / <alpha-value>)',
        },
        state: {
          danger: 'rgb(var(--danger) / <alpha-value>)',
          warn: 'rgb(var(--warn) / <alpha-value>)',
          ok: 'rgb(var(--ok) / <alpha-value>)',
        },
        badge: {
          red: 'rgb(var(--badge-red) / <alpha-value>)',
          amber: 'rgb(var(--badge-amber) / <alpha-value>)',
          blue: 'rgb(var(--badge-blue) / <alpha-value>)',
        },
        bubble: {
          user: 'rgb(var(--bubble-user) / 0.28)',
          assistant: 'rgb(var(--panel) / 0.7)',
        },
        // Jarvis's states. Fixed by design: never themeable, so a colour always
        // means the same thing (only Standby deepens in light mode).
        orb: {
          standby: 'rgb(var(--orb-standby) / <alpha-value>)',
          listening: '#22d3ee',
          thinking: '#a78bfa',
          processing: '#f5a524',
          speaking: '#ff3b30',
        },
      },
      fontFamily: {
        sans: ['var(--font-geist-sans)', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['var(--font-geist-mono)', 'ui-monospace', 'monospace'],
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
        modal: '0 40px 90px -20px #000',
        tab: '0 18px 40px -18px rgb(0 0 0 / 0.9), inset 0 -1px 0 rgb(170 210 240 / 0.12)',
        focus: '0 0 0 3px rgb(var(--accent) / 0.28)',
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
