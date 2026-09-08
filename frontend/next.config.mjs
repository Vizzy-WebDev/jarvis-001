/** @type {import('next').NextConfig} */

// Jarvis runs as ONE process on ONE port: FastAPI serves the API and hands the
// browser these files itself. So the production build is a static export — there
// is no Node process in the deployed app at all, and the owner keeps a single
// window and a single desktop shortcut.
//
// `output: 'export'` is applied only for a production build. In development the
// app runs as a normal Next dev server (hot reload, fast refresh) and proxies
// /api to whichever backend is running, which is what lets the front end be
// built against the still-authoritative Node server before the Python one
// catches up. Rewrites are not supported alongside `export`, hence the switch.
const isProduction = process.env.NODE_ENV === 'production';

// Where /api goes during development. Defaults to the Node app's usual port so a
// contributor can point the new front end at the old backend with no setup.
const API_TARGET = process.env.JARVIS_API_TARGET || 'http://127.0.0.1:3000';

const nextConfig = {
  output: isProduction ? 'export' : undefined,
  reactStrictMode: true,
  // The export is served from the filesystem by FastAPI, so a trailing-slash
  // directory layout is what maps cleanly onto StaticFiles.
  trailingSlash: true,
  images: {
    // No image optimisation server exists in a static export.
    unoptimized: true,
  },
  ...(isProduction
    ? {}
    : {
        async rewrites() {
          return [{ source: '/api/:path*', destination: `${API_TARGET}/api/:path*` }];
        },
      }),
};

export default nextConfig;
