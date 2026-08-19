# Vendored three.js

Version: **0.185.1**, pulled from the official npm package (`npm pack three@0.185.1`),
`build/three.module.min.js` — the minified ES module build, used unmodified.

**Both `three.module.min.js` AND `three.core.min.js` are required.** Since three.js's
r150-ish build split, the `.module.min.js` entry point is a thin file whose own internal
`import` statements pull the bulk of the library from a sibling `three.core.min.js` — vendoring
only the first file looked complete (it's the one that exports everything, and `node --check`
happily parses it standalone) but silently 404'd in the browser at actual module-resolution
time, which surfaced as `orb.js` failing to load with a content-free "Failed to fetch
dynamically imported module" error and, because `app.js` imports `orb.js` at the top level,
took the ENTIRE app down (nothing in `app.js` runs until all its static imports resolve) with
no console error pointing at the real cause — diagnosed by checking actual network requests,
which showed a 503 for `three.core.min.js` specifically. If either file is ever missing after
an update, expect this same silent-whole-app failure.

This is deliberately vendored as a static file rather than an npm dependency: the rest of
this project's front-end has no build step and no browser-side packages (see the project's
CLAUDE.md), and this is the one deliberate exception, chosen for the 3D orb specifically.
`public/orb.js` imports it via a plain relative ES module import
(`import * as THREE from './vendor/three/three.module.min.js'`) — no bundler, no import
map, nothing else needed; three.core.min.js is resolved automatically since it sits next to
three.module.min.js in this same folder.

To update: `npm pack three@<version>` somewhere outside this repo, extract
`package/build/three.module.min.js`, `package/build/three.core.min.js`, and `package/LICENSE`,
and replace all three files here.
