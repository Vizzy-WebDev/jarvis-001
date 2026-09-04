import type { Config } from 'tailwindcss';

// The palette is carried over from public/style.css rather than invented, so the
// port is a change of technology and not a silent redesign. Named tokens exist
// so the eventual screen-by-screen migration can reference the same colours the
// hand-written CSS did instead of scattering hex values through components.
const config: Config = {
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}', './lib/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: {
          DEFAULT: '#e8e8ea',
          muted: '#9a9aa2',
          faint: '#6b6b73',
        },
        surface: {
          DEFAULT: '#101014',
          raised: '#18181d',
          border: '#2a2a32',
        },
        accent: {
          DEFAULT: '#6ea8fe',
          soft: '#3a5a8c',
        },
      },
    },
  },
  plugins: [],
};

export default config;
