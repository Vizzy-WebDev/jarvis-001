// A tiny pub/sub for server -> browser push over Server-Sent Events at
// /api/events — the first proactive channel in the app (until now the
// server only ever spoke when spoken to, via /api/chat/stream). A finished
// scheduled task, a ready briefing, or a background model switch all go out
// through here so the UI can react without polling.

const clients = new Set(); // Set<express.Response>

export function addClient(res) {
  clients.add(res);
}

export function removeClient(res) {
  clients.delete(res);
}

export function broadcast(event) {
  const payload = `data: ${JSON.stringify(event)}\n\n`;
  for (const res of clients) {
    try {
      res.write(payload);
    } catch {
      clients.delete(res);
    }
  }
}
