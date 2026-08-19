// Bundled "Official Connectors" directory. Rewritten from scratch as part of
// the connector-catalog rebuild, treating MCP's whole mechanism (this file
// included) as a genuine peer next to the new API/CLI mechanisms rather than
// pre-existing code left untouched — the loading logic is trivial, so what
// actually changed here is intent, not behavior.
//
// Plain fs.readFileSync + JSON.parse (not an import assertion), read once at
// startup since this is a bundled file, not something that changes while the
// server runs — same pattern as server/skill-catalog/index.js.
//
// Each entry is `{id, label, icon, description, connectFlow}`. Every entry's
// `connectFlow` already knows which of the three mechanisms it needs (today,
// every catalog entry happens to be MCP — API/CLI catalog entries are a
// natural future extension, not a structural limitation) so browsing the
// catalog never has to ask; clicking Connect on any entry runs the exact
// same connect flow a Custom Connector's own details would, just pre-filled.
// No "ready"/"needs setup" distinction is ever shown — every entry gets the
// same plain Connect button.
//
// **Catalog honesty rule**: an entry is only added here once its real
// endpoint has been verified live (not just recalled from a docs page or
// blog post) — see CLAUDE.md's "App Control connectors" section for the full
// verification record. Notion is `oauth_dcr` (genuinely one-click — Dynamic
// Client Registration confirmed live). GitHub, Slack, Google Drive, and
// Gmail are `oauth_guided` — their real endpoints are verified, but none of
// the four supports DCR (confirmed live: no usable `registration_endpoint`
// in their real OAuth metadata) — each needs the user to create their own
// app/Client ID in that service's own console first, walked through by that
// entry's own `connectFlow.guide.steps`. Figma was deliberately NOT added —
// verified live that its real MCP server only accepts pre-approved catalog
// clients (VS Code, Cursor, Claude Code), rejecting DCR and personal access
// tokens alike for anyone else. A user can still connect anything else right
// now via "Add custom connector".

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CATALOG_FILE_PATH = path.join(__dirname, 'catalog.json');

function readCatalogFile() {
  try {
    const raw = fs.readFileSync(CATALOG_FILE_PATH, 'utf8');
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed.entries) ? parsed.entries : [];
  } catch (err) {
    console.warn('[connectors] Could not load catalog.json:', err.message);
    return [];
  }
}

const catalogEntries = readCatalogFile();

export function listCatalog() {
  return catalogEntries;
}

export function getCatalogEntry(id) {
  return catalogEntries.find((entry) => entry.id === id) || null;
}
