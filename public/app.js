// Jarvis front-end shell: wires the UI (transcript, text box, mic button,
// settings, drawer navigation) to whichever voice engine is active. Engine A
// (Pipeline, any AI model) and Engine B (Live, Gemini only) are selected in
// settings. No build step — plain ES modules.

import { getSetting, setSetting } from './settings.js';
import { PipelineEngine } from './engines/pipeline-engine.js';
import { LiveEngine } from './engines/live-engine.js';
import { SECTIONS } from './nav.js';
import { navigate, initRouter, currentSectionId, refreshIfActive } from './router.js';
import { createOrb } from './orb.js';
import { Dictation } from './dictation.js';
// Plans and verdicts are documents, not speech — they render here in the
// transcript now that Planning and Content Analysis have no screens of their
// own. This module builds DOM nodes rather than setting innerHTML, which
// matters because the text was written by a model from pages it fetched.
import { markdownBlock, copyableBlock } from './screens/_markdown.js';
import { notify, ingest as ingestNotification, resync as resyncNotifications, setupNotifications } from './notifications.js';
// The memory review card (below) reuses the same plain form-field/armed-
// delete-button primitives every screens/*.js file already builds on, even
// though this card itself lives in the transcript, not a screen.
import { fieldTextarea, armedButton } from './screens/_helpers.js';

const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
// Two independent browser features, unsupported in different places —
// Firefox, for instance, has speech OUTPUT but not mic input. Only disable
// the specific half that's actually missing.
const MIC_SUPPORTED = Boolean(SpeechRecognition);
const SPEECH_OUTPUT_SUPPORTED = Boolean(window.speechSynthesis);

let engine = null;
let pushRecognition = null; // separate, single-utterance recognizer used only in push-to-talk mode
let orb = null; // the 3D orb — see orb.js. Reflects state, not the mic button (see setMicVisual).
let dictation = null; // the composer's own mic — see dictation.js. Separate from voice-control (engine).
let currentAssistantEl = null;
let interimEl = null;
let activeConfirmRow = null; // the Yes/No chips under a needs_confirmation reply, if any
let activeMonitorId = null; // whichever monitor the amber "Watching for" bar currently represents, if any
let turnStartTime = 0; // performance.now() when the current turn began, for the latency readout
let latencyMeasured = false;
let modelLabels = {}; // modelId -> label, for model_switch notices
// Files chosen but not yet sent. Each: {localId, name, size, id?, previewUrl?,
// status: 'uploading'|'ready'|'failed'}. `id` is the server's upload id and is
// what actually travels with the turn.
let pendingAttachments = [];
let nextLocalId = 1;

const TOOL_LABELS = {
  get_time: 'Checking the time…',
  open_website: 'Opening that…',
  open_app: 'Opening that…',
  web_search: 'Searching the web…',
  read_web_page: 'Reading that page…',
  get_weather: 'Checking the weather…',
  get_headlines: 'Checking the news…',
  schedule_task: 'Scheduling that…',
  list_tasks: 'Checking your schedule…',
  cancel_task: 'Updating that task…',
  configure_briefing: 'Updating your briefing…',
  remember_about_me: 'Noting that down…',
  open_section: 'Opening that…',
  control_computer: 'Controlling your computer…',
  share_content: 'Taking a look…',
  examine_content: 'Looking into that…',
  check_claim: 'Checking that against sources…',
  look_it_up: 'Looking that up…',
  start_project: 'Opening that up…',
  note_project_decision: 'Noting that down…',
  research_project: 'Looking into that…',
  write_project_plan: 'Writing that up…',
  write_build_prompts: 'Writing the build prompt…',
};

// ---------- Screen switching (the primitive public/router.js drives) ----------

function showScreen(id) {
  document.querySelectorAll('.screen').forEach((s) => s.classList.add('hidden'));
  document.getElementById(id).classList.remove('hidden');
}

// ---------- UI helpers ----------

/**
 * Drives both the orb (which carries all four states — idle/listening/
 * thinking/speaking) and the mic button (which reflects ONLY mute state,
 * never Jarvis's turn — it must never spin, interrupt, or otherwise react
 * to thinking/speaking; that used to be true here and was the bug: a mute
 * button has no business indicating, let alone affecting, "Jarvis is
 * thinking").
 *
 * `engine.muted` — not `state` — is the source of truth for the button now,
 * since mute is a persistent toggle independent of what Jarvis is currently
 * doing (see PipelineEngine/LiveEngine's setMuted()). Mute doesn't emit a
 * `'state'` event on its own, so callers that change it must call this
 * function again explicitly to repaint — see onMicButtonClick().
 */
function setMicVisual(state) {
  const btn = document.getElementById('mic-button');
  const statusLine = document.getElementById('status-line');

  const isListening = state === 'listening';
  const muted = Boolean(engine?.muted);
  // The slash shows for an explicit mute AND for the plain not-started-yet
  // idle case — both genuinely mean "the mic isn't capturing right now."
  const showMutedSlash = muted || (!isListening && state !== 'thinking' && state !== 'speaking');
  btn.classList.toggle('listening', isListening && !muted);
  btn.classList.toggle('muted', showMutedSlash);
  btn.setAttribute('aria-label', isListening && !muted ? 'Mute the microphone' : 'Unmute the microphone');

  if (muted && engine?.active) {
    statusLine.textContent = 'Muted — click the mic to unmute';
  } else if (state === 'listening') {
    statusLine.textContent = 'Listening…';
  } else if (state === 'thinking') {
    statusLine.textContent = 'Thinking…';
  } else if (state === 'speaking') {
    statusLine.textContent = 'Speaking…';
  } else {
    statusLine.textContent = micModeHint();
  }

  orb?.setState(state || 'idle');
}

function micModeHint() {
  if (!MIC_SUPPORTED) return 'Type to talk';
  return getSetting('micMode') === 'conversation'
    ? 'Click the mic to start a conversation, or type to talk'
    : 'Click the mic, press Space, or type to talk';
}

function setStatusLine(text) {
  document.getElementById('status-line').textContent = text;
}

function scrollToBottom() {
  const scroller = document.getElementById('conversation-scroll');
  scroller.scrollTop = scroller.scrollHeight;
}

function addBubble(role, text) {
  const el = document.createElement('div');
  el.className = `bubble ${role}`;
  el.textContent = text;
  document.getElementById('transcript').appendChild(el);
  scrollToBottom();
  return el;
}

/** A small centered notice (model switches, paused/failover notices, task-run pushes) — not part of the conversation. */
function addSystemNote(text) {
  clearConfirmRow();
  addBubble('system', text);
}

function showInterim(text) {
  if (!interimEl) {
    interimEl = document.createElement('div');
    interimEl.className = 'bubble user interim';
    document.getElementById('transcript').appendChild(interimEl);
  }
  interimEl.textContent = text;
  scrollToBottom();
}

function clearInterim() {
  if (interimEl) {
    interimEl.remove();
    interimEl = null;
  }
}

function clearConfirmRow() {
  if (activeConfirmRow) {
    activeConfirmRow.remove();
    activeConfirmRow = null;
  }
}

/** Yes/No chips for a needs_confirmation tool result — a tap does the same thing saying "yes"/"no" would. */
function showConfirmRow(summary) {
  clearConfirmRow();
  if (summary) setStatusLine(summary);

  const row = document.createElement('div');
  row.className = 'confirm-row';

  const yes = document.createElement('button');
  yes.type = 'button';
  yes.className = 'confirm-yes';
  yes.textContent = 'Yes, go ahead';
  yes.addEventListener('click', () => {
    clearConfirmRow();
    sendTypedOrPushText('Yes, go ahead.');
  });

  const no = document.createElement('button');
  no.type = 'button';
  no.className = 'confirm-no';
  no.textContent = 'No, cancel';
  no.addEventListener('click', () => {
    clearConfirmRow();
    sendTypedOrPushText('No, cancel that.');
  });

  row.append(yes, no);
  document.getElementById('transcript').appendChild(row);
  activeConfirmRow = row;
  scrollToBottom();
}

/** Yes/No chips for a control session's own mid-loop risky-action confirmation (control/session.js's awaitConfirmation) — same look as showConfirmRow, but answers POST /api/control/confirm instead of sending a chat message, since the session is paused inside a tool call, not waiting on the next user turn. */
function showControlConfirmRow(summary) {
  clearConfirmRow();
  if (summary) setStatusLine(`Waiting for your OK: ${summary}`);

  const row = document.createElement('div');
  row.className = 'confirm-row';

  const answer = async (approve) => {
    clearConfirmRow();
    try {
      await fetch('/api/control/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approve }),
      });
    } catch {
      // The banner/status line will settle back down via the next
      // control_status event either way.
    }
  };

  const yes = document.createElement('button');
  yes.type = 'button';
  yes.className = 'confirm-yes';
  yes.textContent = 'Yes, go ahead';
  yes.addEventListener('click', () => answer(true));

  const no = document.createElement('button');
  no.type = 'button';
  no.className = 'confirm-no';
  no.textContent = 'No, skip it';
  no.addEventListener('click', () => answer(false));

  row.append(yes, no);
  document.getElementById('transcript').appendChild(row);
  activeConfirmRow = row;
  scrollToBottom();
}

/**
 * Was the in-layout #banner (removed — see CLAUDE.md/handoff.md on why it
 * had to permanently reserve space near the mic/composer). Every former
 * showBanner() call site now fires a toast notification instead, via
 * notifications.js's notify() — same message, same action button (now a
 * nav.js section id rather than an onClick callback), but out of layout
 * flow entirely and kept in the notification panel/history afterward.
 */

// ---------- Computer-control banner (mirrors the desktop overlay — see control/overlay.ps1) ----------

function showControlBanner(step) {
  document.getElementById('control-banner-text').textContent = step || 'Jarvis is controlling your computer…';
  document.getElementById('control-banner').classList.remove('hidden');
}

function hideControlBanner() {
  document.getElementById('control-banner').classList.add('hidden');
}

function setupControlBanner() {
  document.getElementById('control-banner-stop').addEventListener('click', async () => {
    try {
      await fetch('/api/control/stop', { method: 'POST' });
    } catch {
      // The banner still hides locally below once the control_stopped event
      // (or the immediate control_status:false one hideOverlay() also sends)
      // comes back over SSE — a failed stop *request* shouldn't leave a
      // stale "still controlling" banner up if the server did stop anyway.
    }
  });
  // In case a control session was already running when this tab loaded
  // (e.g. the page was refreshed mid-task) — reflect the real state
  // immediately instead of waiting for the next status change.
  fetch('/api/control/status')
    .then((res) => res.json())
    .then((data) => {
      if (data?.active) showControlBanner(data.step);
    })
    .catch(() => {});
}

// ---------- Monitor banner ("Watching for…") — amber, the deliberately
// distinct twin of the red control banner above, so the two states are
// never confused with each other even when both are showing at once. ----------

function showMonitorBanner(monitorId, description) {
  activeMonitorId = monitorId;
  document.getElementById('monitor-banner-text').textContent = `Watching for: ${description || 'something'}…`;
  document.getElementById('monitor-banner').classList.remove('hidden');
}

function hideMonitorBanner() {
  activeMonitorId = null;
  document.getElementById('monitor-banner').classList.add('hidden');
}

function setupMonitorBanner() {
  document.getElementById('monitor-banner-stop').addEventListener('click', async () => {
    if (!activeMonitorId) {
      hideMonitorBanner(); // shouldn't happen, but never leave an un-clickable bar up
      return;
    }
    try {
      await fetch(`/api/monitors/${activeMonitorId}/stop`, { method: 'POST' });
    } catch {
      // The bar still hides via the monitor_stopped SSE event either way —
      // same reasoning as the control banner's own Stop button above.
    }
  });

  // In case a monitor was already active when this tab loaded (e.g. the
  // page was refreshed mid-watch) — reflect the real state immediately.
  fetch('/api/monitors')
    .then((res) => res.json())
    .then((data) => {
      const active = data?.monitors?.find((m) => m.status === 'watching');
      if (active) showMonitorBanner(active.id, active.description);
    })
    .catch(() => {});
}

// ---------- Observation dot ("Jarvis can see your screen") — a quieter,
// separate signal from the two banners above: lit for look_at_screen's
// one-off glance and a screen_looks_like monitor's ongoing visual watch,
// never for a control session (the red bar already covers "can see AND is
// acting"). See server/control/observation-bridge.js. ----------

function showObservationDot(reason) {
  const dot = document.getElementById('observation-dot');
  dot.title = reason || 'Jarvis can see your screen — click to stop';
  dot.classList.remove('hidden');
}

function hideObservationDot() {
  document.getElementById('observation-dot').classList.add('hidden');
}

function setupObservationDot() {
  document.getElementById('observation-dot').addEventListener('click', async () => {
    try {
      await fetch('/api/observation/stop', { method: 'POST' });
    } catch {
      // Harmless either way — a real stop still arrives via the
      // observation_status SSE event if the request itself landed; if
      // nothing was actually watching (e.g. mid a brief look_at_screen
      // glance) there was nothing to stop in the first place.
    }
  });

  // In case observation was already active when this tab loaded (e.g. the
  // page was refreshed mid-watch) — reflect the real state immediately.
  fetch('/api/observation/status')
    .then((res) => res.json())
    .then((data) => {
      if (data?.active) showObservationDot();
    })
    .catch(() => {});
}

/** Called right before a message (voice or text) is sent — renders the user's turn and a fresh reply bubble. */
function beginTurn(userText, attachments = []) {
  clearInterim();
  clearConfirmRow();

  // Show what was actually shared, not just the words — a picture sent with
  // no caption would otherwise appear as an empty bubble.
  const bubble = addBubble('user', '');
  for (const att of attachments) {
    if (att.previewUrl) {
      const img = document.createElement('img');
      img.className = 'bubble-image';
      img.src = att.previewUrl;
      img.alt = att.name;
      bubble.appendChild(img);
    } else {
      const line = document.createElement('div');
      line.className = 'chip-name';
      line.textContent = `📎 ${att.name}`;
      bubble.appendChild(line);
    }
  }
  if (userText) bubble.appendChild(document.createTextNode(userText));
  if (!userText && !attachments.length) bubble.textContent = userText;

  currentAssistantEl = addBubble('assistant', '');
  turnStartTime = performance.now();
  latencyMeasured = false;
  document.getElementById('latency-badge').classList.add('hidden');
  scrollToBottom();
}

/**
 * A document in the transcript — a plan, a fact-check verdict, a research
 * answer. Deliberately not a chat bubble: these are read, not spoken, and
 * server/prompt.js tells the model never to recite one out loud.
 */
function addDocumentCard(title, markdown, { badge, sources, copyText, copyLabel } = {}) {
  clearConfirmRow();
  const card = document.createElement('div');
  card.className = 'doc-card';

  const head = document.createElement('div');
  head.className = 'doc-card-head';
  head.appendChild(Object.assign(document.createElement('h3'), { textContent: title }));
  if (badge) {
    const b = document.createElement('span');
    b.className = `badge ${badge.className || ''}`.trim();
    b.textContent = badge.text;
    head.appendChild(b);
  }
  card.appendChild(head);

  if (markdown) card.appendChild(markdownBlock(markdown));

  if (sources?.length) {
    card.appendChild(Object.assign(document.createElement('p'), { className: 'hint', textContent: 'Checked against:' }));
    const list = document.createElement('ul');
    list.className = 'source-list';
    for (const s of sources) {
      const li = document.createElement('li');
      const a = document.createElement('a');
      a.href = s.url;
      a.textContent = s.title || s.url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      li.appendChild(a);
      list.appendChild(li);
    }
    card.appendChild(list);
  }

  // The handoff prompt is the actual deliverable of a plan — make taking it
  // one click, not a manual select-all of a long document.
  // copyableBlock is a full-width box (its own <pre> plus button), so it goes
  // straight into the card rather than into the inline actions row.
  if (copyText) card.appendChild(copyableBlock(copyText, { label: copyLabel || 'Copy' }));

  document.getElementById('transcript').appendChild(card);
  scrollToBottom();
  return card;
}

// ---------- Memory review card (server/memory/*.js) ----------
//
// The in-chat approval UI for candidate memories Jarvis noticed quietly in
// the background — appears on its own when a checkpoint finds something
// (the 'memory_candidates_ready' SSE event) or on request (review_memories'
// ui_action). Every button here is a direct, model-free REST call — nothing
// about approving/editing/rejecting a suggestion should cost quota or wait
// on a model.

let memoryReviewCardEl = null;

function buildCandidateRow(candidate, onResolved) {
  const row = document.createElement('div');
  row.className = 'list-row';
  row.dataset.candidateId = candidate.id;

  const main = document.createElement('div');
  main.className = 'list-row-main';
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-title', textContent: candidate.text }));
  main.appendChild(Object.assign(document.createElement('span'), { className: 'list-row-sub', textContent: candidate.category }));
  row.appendChild(main);

  const actions = document.createElement('div');
  actions.className = 'list-row-actions';

  if (candidate.conflictWith) {
    // A conflict: show what's currently stored right alongside the new
    // suggestion, and let the user pick — never resolved automatically
    // (see server/memory/memory-store.js's resolveConflict()).
    const conflictNote = document.createElement('p');
    conflictNote.className = 'hint';
    conflictNote.textContent = candidate.conflictText
      ? `This conflicts with what's already remembered: "${candidate.conflictText}"`
      : 'This may conflict with something already remembered.';
    main.appendChild(conflictNote);

    const resolve = async (choice) => {
      row.querySelectorAll('button').forEach((b) => (b.disabled = true));
      await fetch(`/api/memories/candidates/${encodeURIComponent(candidate.id)}/resolve-conflict`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ choice }),
      });
      onResolved(row);
    };

    const updateBtn = document.createElement('button');
    updateBtn.type = 'button';
    updateBtn.className = 'btn btn-primary';
    updateBtn.textContent = 'Update the old one';
    updateBtn.addEventListener('click', () => resolve('update'));

    const bothBtn = document.createElement('button');
    bothBtn.type = 'button';
    bothBtn.className = 'btn';
    bothBtn.textContent = 'Keep both';
    bothBtn.addEventListener('click', () => resolve('keep_both'));

    const discardBtn = document.createElement('button');
    discardBtn.type = 'button';
    discardBtn.className = 'btn';
    discardBtn.textContent = 'Discard new';
    discardBtn.addEventListener('click', () => resolve('discard'));

    actions.append(updateBtn, bothBtn, discardBtn);
    row.appendChild(actions);
    return row;
  }

  const approve = async (edits) => {
    row.querySelectorAll('button').forEach((b) => (b.disabled = true));
    await fetch(`/api/memories/candidates/${encodeURIComponent(candidate.id)}/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ edits: edits || {} }),
    });
    onResolved(row);
  };

  const approveBtn = document.createElement('button');
  approveBtn.type = 'button';
  approveBtn.className = 'btn btn-primary';
  approveBtn.textContent = 'Approve';
  approveBtn.addEventListener('click', () => approve());

  const editBtn = document.createElement('button');
  editBtn.type = 'button';
  editBtn.className = 'btn';
  editBtn.textContent = 'Edit';
  // Swaps its own behavior on first click (open the field) vs second click
  // (save what's in it) via a single reassignable onclick, rather than
  // stacking addEventListener listeners for the same button.
  editBtn.onclick = () => {
    const editField = fieldTextarea('Edit before saving', '');
    editField.textarea.value = candidate.text;
    main.appendChild(editField.wrapper);
    editBtn.textContent = 'Save & Approve';
    editBtn.onclick = () => approve({ text: editField.textarea.value.trim() });
  };

  const rejectBtn = armedButton('Reject', 'Really reject?', async () => {
    row.querySelectorAll('button').forEach((b) => (b.disabled = true));
    await fetch(`/api/memories/candidates/${encodeURIComponent(candidate.id)}/reject`, { method: 'POST' });
    onResolved(row);
  });

  actions.append(approveBtn, editBtn, rejectBtn);
  row.appendChild(actions);
  return row;
}

function renderMemoryReviewCard(candidates) {
  if (memoryReviewCardEl) memoryReviewCardEl.remove();
  if (!candidates?.length) {
    memoryReviewCardEl = null;
    return;
  }

  clearConfirmRow();
  const card = document.createElement('div');
  card.className = 'doc-card';

  const head = document.createElement('div');
  head.className = 'doc-card-head';
  head.appendChild(
    Object.assign(document.createElement('h3'), {
      textContent: candidates.length === 1 ? 'A memory suggestion' : `${candidates.length} memory suggestions`,
    })
  );
  card.appendChild(head);
  card.appendChild(
    Object.assign(document.createElement('p'), {
      className: 'hint',
      textContent: 'Noticed quietly in the background — nothing here is remembered until you approve it.',
    })
  );

  function onResolved(row) {
    row.remove();
    const remaining = card.querySelectorAll('.list-row').length;
    if (!remaining) {
      card.remove();
      memoryReviewCardEl = null;
      return;
    }
    head.querySelector('h3').textContent = remaining === 1 ? 'A memory suggestion' : `${remaining} memory suggestions`;
  }

  for (const candidate of candidates) card.appendChild(buildCandidateRow(candidate, onResolved));

  document.getElementById('transcript').appendChild(card);
  scrollToBottom();
  memoryReviewCardEl = card;
}

/** Fetches whatever's currently pending and (re)shows the card — used both by the proactive SSE path (which already has the list) and the on-request path (review_memories' ui_action, which doesn't). */
async function showMemoryReviewCard(candidates) {
  if (candidates) {
    renderMemoryReviewCard(candidates);
    return;
  }
  try {
    const res = await fetch('/api/memories/candidates');
    const data = await res.json();
    renderMemoryReviewCard(data.candidates);
  } catch {
    // Best-effort — the model's spoken reply already said how many are
    // pending even if the card itself fails to load.
  }
}

// ---------- Chat History (persisted conversations — server/chat-store.js) ----------

/** Wipes the transcript and any turn-in-progress UI state tied to it — used before rehydrating a different conversation. */
function clearTranscript() {
  document.getElementById('transcript').innerHTML = '';
  currentAssistantEl = null;
  interimEl = null;
  activeConfirmRow = null;
  announcedJobs.clear();
}

/**
 * Rebuilds transcript bubbles from a persisted conversation's stored
 * messages (server/conversation.js's neutral shape, as returned by
 * GET /api/conversations/:id). Only user/assistant text and inline images
 * are restored — tool-call/tool-result messages are internal plumbing with
 * nothing to show, and document cards (plans, verdicts, research answers)
 * come from a separate live progress event, not the transcript itself, so
 * they don't reappear on reload; the text of the reply that announced them
 * does.
 */
function renderStoredMessages(messages) {
  for (const msg of messages || []) {
    if (msg.role === 'user') {
      const bubble = addBubble('user', '');
      for (const media of msg.media || []) {
        if (media.kind !== 'image' || !media.dataBase64) continue;
        const img = document.createElement('img');
        img.className = 'bubble-image';
        img.src = `data:${media.mimeType};base64,${media.dataBase64}`;
        bubble.appendChild(img);
      }
      if (msg.text) bubble.appendChild(document.createTextNode(msg.text));
    } else if (msg.role === 'assistant' && msg.text) {
      // Assistant messages with no text (a pure tool-call step) are skipped
      // — only the final spoken reply of each turn is worth showing again.
      addBubble('assistant', msg.text);
    }
  }
  scrollToBottom();
}

/** Fetches and renders whichever conversation id is passed — shared by startup and "open from Chat History". */
async function loadConversation(id) {
  const res = await fetch(`/api/conversations/${encodeURIComponent(id)}`);
  if (!res.ok) return;
  const data = await res.json();
  clearTranscript();
  renderStoredMessages(data.messages);
}

/** Restores whatever conversation was active when Jarvis last closed — this is what makes reopening Jarvis "just there" with no action needed. */
async function loadActiveConversation() {
  try {
    const res = await fetch('/api/conversations');
    if (!res.ok) return;
    const data = await res.json();
    if (data.activeId) await loadConversation(data.activeId);
  } catch {
    // Best-effort — a fresh/empty transcript is a harmless fallback if the
    // server isn't reachable yet at startup.
  }
}

function setupChatHistoryIntegration() {
  document.getElementById('new-chat-button').addEventListener('click', async () => {
    try {
      await fetch('/api/conversations', { method: 'POST' });
    } catch {
      // Even if persisting the new conversation failed, clearing the
      // visible transcript is still the right response to the click.
    }
    clearTranscript();
  });

  // Dispatched by screens/chat-history.js after it activates a conversation
  // server-side — this tab rehydrates it and switches back to the assistant
  // screen. See that file's comment for why this is an event, not an import.
  window.addEventListener('jarvis:conversation-activated', async (e) => {
    const id = e.detail?.id;
    if (id) await loadConversation(id);
    navigate('home');
  });
}

/** Measured once per turn, the moment Jarvis actually starts speaking — not guessed, not averaged. */
function maybeShowLatency(state) {
  if (state !== 'speaking' || latencyMeasured || !turnStartTime) return;
  latencyMeasured = true;
  const ms = Math.round(performance.now() - turnStartTime);
  const el = document.getElementById('latency-badge');
  el.textContent = `Replied in ${ms}ms`;
  el.classList.remove('hidden');
}

// ---------- Engine wiring ----------

function createEngine(type = getSetting('voiceEngine')) {
  const e =
    type === 'live'
      ? new LiveEngine()
      : new PipelineEngine({
          voiceOutput: SPEECH_OUTPUT_SUPPORTED ? getSetting('voiceOutput') : 'gemini',
          geminiVoice: getSetting('geminiVoice'),
          useAiTurnCheck: getSetting('useAiTurnCheck'),
        });

  // Both engines emit the same event set (see engines/voice-engine.js), so
  // the wiring below is identical regardless of which one is active.
  e.on('state', ({ state }) => {
    setMicVisual(state);
    maybeShowLatency(state);
  });

  e.on('transcript', ({ text, final }) => {
    if (final) {
      beginTurn(text);
    } else {
      showInterim(text);
    }
  });

  e.on('chunk', ({ text }) => {
    if (!currentAssistantEl) currentAssistantEl = addBubble('assistant', '');
    currentAssistantEl.textContent += text;
    scrollToBottom();
  });

  e.on('tool', ({ name }) => {
    setStatusLine(TOOL_LABELS[name] || 'Working on it…');
  });

  e.on('tool_result', (data) => {
    if (data.ui_action?.type === 'navigate') {
      navigate(data.ui_action.section);
    }
    if (data.ui_action?.type === 'memory_review') {
      showMemoryReviewCard();
    }
    if (data.needs_confirmation) {
      showConfirmRow(data.summary);
    }
  });

  // A model failed mid-reply and Jarvis is trying the next one — see
  // server/models/runner.js. Told plainly, as required, rather than left
  // to guess why the reply suddenly changed tone or restarted.
  e.on('model_switch', ({ from, to, reason }) => {
    const toLabel = modelLabels[to] || to;
    addSystemNote(`Switching from ${modelLabels[from] || from} to ${toLabel}${reason ? ` — ${reason}` : ''}…`);
  });

  e.on('restart', () => {
    // The partial reply just shown/spoken belonged to the model that
    // failed — clear it; a clean reply from the next model follows on the
    // same turn, not stitched onto this one.
    if (currentAssistantEl) currentAssistantEl.textContent = '';
  });

  e.on('paused', ({ reason }) => {
    if (currentAssistantEl && !currentAssistantEl.textContent) {
      currentAssistantEl.remove();
    }
    currentAssistantEl = null;
    notify({
      kind: 'voice',
      level: 'error',
      title: reason || "None of Jarvis's models could complete that.",
      action: { label: 'Manage models', section: 'models' },
    });
  });

  e.on('done', () => {
    if (currentAssistantEl && !currentAssistantEl.textContent) {
      currentAssistantEl.remove();
    }
    currentAssistantEl = null;
  });

  e.on('error', ({ message, code }) => {
    if (currentAssistantEl && !currentAssistantEl.textContent) {
      currentAssistantEl.remove();
    }
    currentAssistantEl = null;
    notify({
      kind: 'voice',
      level: 'error',
      title: message || 'Something went wrong.',
      action: code === 'NO_API_KEY' ? { label: 'Add a model', section: 'models' } : undefined,
    });
  });

  return e;
}

// ---------- Attachments ----------

function humanSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** The send button is disabled when there's nothing to send yet — recomputed on every attachment change and every keystroke (see setupAppScreen's textarea input listener). */
function updateSendButtonState() {
  const sendBtn = document.getElementById('send-button');
  if (!sendBtn) return;
  const textInput = document.getElementById('text-input');
  const hasText = Boolean(textInput?.value.trim());
  const hasReady = pendingAttachments.some((a) => a.status === 'ready');
  const hasPending = pendingAttachments.some((a) => a.status === 'uploading');
  sendBtn.disabled = !hasText && !hasReady && !hasPending;
}

/** The uppercase badge on a non-image tile — just the extension, so this needs no lookup table. */
function fileExtensionBadge(name) {
  const match = /\.([a-z0-9]+)$/i.exec(name || '');
  return match ? match[1].toUpperCase().slice(0, 4) : 'FILE';
}

function renderAttachmentChips() {
  const row = document.getElementById('attachment-chips');
  row.innerHTML = '';
  row.classList.toggle('hidden', pendingAttachments.length === 0);

  for (const att of pendingAttachments) {
    const chip = document.createElement('div');
    chip.className = `attachment-chip ${att.status === 'uploading' ? 'uploading' : ''} ${att.status === 'failed' ? 'failed' : ''}`.trim();
    chip.title = att.status === 'failed' ? att.error || 'could not be added' : `${att.name} (${humanSize(att.size)})`;

    if (att.previewUrl) {
      const thumb = document.createElement('img');
      thumb.className = 'chip-thumb';
      thumb.src = att.previewUrl;
      thumb.alt = att.name;
      chip.appendChild(thumb);
    } else {
      const title = document.createElement('span');
      title.className = 'chip-title';
      title.textContent = att.name;
      chip.appendChild(title);

      const badge = document.createElement('span');
      badge.className = 'chip-badge';
      badge.textContent =
        att.status === 'uploading' ? '…' : att.status === 'failed' ? 'failed' : fileExtensionBadge(att.name);
      chip.appendChild(badge);
    }

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'chip-remove';
    remove.setAttribute('aria-label', `Remove ${att.name}`);
    remove.textContent = '×';
    remove.addEventListener('click', () => {
      if (att.previewUrl) URL.revokeObjectURL(att.previewUrl);
      pendingAttachments = pendingAttachments.filter((a) => a.localId !== att.localId);
      renderAttachmentChips();
    });
    chip.appendChild(remove);

    row.appendChild(chip);
  }

  updateSendButtonState();
}

// Matches media.js's INLINE_MAX_BYTES on the server — the largest an image
// can be and still ride inside a message as base64 rather than needing a
// provider upload API. Normalizing to comfortably under this means a phone
// photo (routinely 4-8MB) never hits the "too large to send directly" wall.
const IMAGE_INLINE_MAX_BYTES = 3.5 * 1024 * 1024;
const HEIC_TIFF_RE = /\.(heic|heif|tiff?)$/i;
const ALREADY_FINE_IMAGE_TYPES = new Set(['image/jpeg', 'image/png']);

/**
 * Re-encodes an oversized or awkward-format picture to JPEG via canvas
 * before it ever leaves the browser — fixes big phone photos and converts
 * AVIF/WebP/BMP/ICO/SVG into something every model accepts, and costs less
 * quota than sending the original bytes.
 *
 * Chrome cannot decode HEIC or TIFF at all (no canvas/createImageBitmap
 * support) — those are reported plainly rather than silently uploaded as
 * something no model will be able to open either.
 *
 * Returns `{file, error}` — `file` is null when `error` is set, otherwise
 * always a File ready to upload (the original, unchanged, when it was
 * already a reasonable JPEG/PNG under the size cap and re-encoding would
 * just burn CPU for nothing).
 */
async function normalizeImageForUpload(file) {
  if (!file.type?.startsWith('image/')) return { file, error: null };

  if (HEIC_TIFF_RE.test(file.name || '')) {
    const ext = (/\.([a-z0-9]+)$/i.exec(file.name || '')?.[1] || 'that format').toUpperCase();
    return { file: null, error: `${ext} photos can't be opened here — save it as JPEG or PNG first.` };
  }

  if (ALREADY_FINE_IMAGE_TYPES.has(file.type) && file.size <= IMAGE_INLINE_MAX_BYTES) {
    return { file, error: null };
  }

  let bitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    // A format the browser genuinely can't decode (rare, beyond HEIC/TIFF
    // already caught above) — hand the original off and let the server's
    // own size/format checks give an honest answer rather than guessing here.
    return { file, error: null };
  }

  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  let scale = 1;
  let blob = null;

  // Re-encode as JPEG, shrinking in steps only if it's still over budget —
  // most photos fit on the first pass and never need to scale down at all.
  for (let attempt = 0; attempt < 5; attempt++) {
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.85));
    if (!blob || blob.size <= IMAGE_INLINE_MAX_BYTES) break;
    scale *= 0.7;
  }
  bitmap.close?.();

  if (!blob) return { file, error: null }; // encoding failed for some reason — fall back to the original
  const newName = (file.name || 'photo').replace(/\.[a-z0-9]+$/i, '') + '.jpg';
  return { file: new File([blob], newName, { type: 'image/jpeg' }), error: null };
}

/**
 * Uploads a file the moment it's chosen, rather than at send time.
 *
 * Doing it up front means a large video is already on its way while the user
 * is still typing, and a failure is visible on the chip instead of appearing
 * as a mysteriously failed message later.
 */
async function addAttachment(file) {
  const att = {
    localId: nextLocalId++,
    name: file.name || 'file',
    size: file.size,
    status: 'uploading',
    previewUrl: null,
  };
  pendingAttachments.push(att);
  renderAttachmentChips();

  const { file: normalized, error: normalizeError } = await normalizeImageForUpload(file);
  if (normalizeError) {
    att.status = 'failed';
    att.error = normalizeError;
    renderAttachmentChips();
    return;
  }

  // The tile, the sent bubble, and the bytes actually uploaded must all be
  // the SAME picture — build the preview from whatever is really going out,
  // not the original file the user picked.
  att.name = normalized.name || att.name;
  att.size = normalized.size;
  if (normalized.type?.startsWith('image/')) att.previewUrl = URL.createObjectURL(normalized);
  renderAttachmentChips();

  try {
    const res = await fetch(`/api/uploads?name=${encodeURIComponent(att.name)}`, {
      method: 'POST',
      headers: { 'Content-Type': normalized.type || 'application/octet-stream' },
      body: normalized,
    });
    const data = await res.json();
    if (!data.ok) {
      att.status = 'failed';
      att.error = data.error || 'could not be added';
    } else {
      att.id = data.id;
      att.status = 'ready';
    }
  } catch {
    att.status = 'failed';
    att.error = 'upload failed';
  }
  renderAttachmentChips();
}

/** Everything successfully uploaded and ready to travel with the next turn. */
function readyAttachments() {
  return pendingAttachments.filter((a) => a.status === 'ready' && a.id);
}

function clearAttachments() {
  // Object URLs are revoked only after the bubble that shows them is gone —
  // beginTurn() renders from these, so they're released on the next clear.
  pendingAttachments = [];
  renderAttachmentChips();
}

function setupAttachments() {
  const button = document.getElementById('attach-button');
  const input = document.getElementById('attach-input');
  const screen = document.getElementById('app-screen');

  button.addEventListener('click', () => input.click());
  input.addEventListener('change', () => {
    for (const file of input.files || []) addAttachment(file);
    input.value = ''; // so choosing the same file twice still fires `change`
  });

  // Drop anywhere on the assistant screen — no small target to aim at.
  let dragDepth = 0;
  screen.addEventListener('dragenter', (e) => {
    if (!e.dataTransfer?.types?.includes('Files')) return;
    e.preventDefault();
    dragDepth++;
    screen.classList.add('dragging');
  });
  screen.addEventListener('dragover', (e) => {
    if (e.dataTransfer?.types?.includes('Files')) e.preventDefault();
  });
  screen.addEventListener('dragleave', () => {
    // dragleave fires for every child element crossed, so count enters and
    // leaves rather than clearing on the first one.
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) screen.classList.remove('dragging');
  });
  screen.addEventListener('drop', (e) => {
    if (!e.dataTransfer?.files?.length) return;
    e.preventDefault();
    dragDepth = 0;
    screen.classList.remove('dragging');
    for (const file of e.dataTransfer.files) addAttachment(file);
  });

  // Paste a screenshot straight in — the fastest path for the commonest case.
  document.addEventListener('paste', (e) => {
    if (document.body.classList.contains('modal-open')) return;
    const files = [...(e.clipboardData?.items || [])]
      .filter((i) => i.kind === 'file')
      .map((i) => i.getAsFile())
      .filter(Boolean);
    if (!files.length) return;
    e.preventDefault();
    for (const file of files) addAttachment(file);
  });

  renderAttachmentChips();
}

function sendTypedOrPushText(text) {
  text = String(text || '').trim();
  const attachments = readyAttachments();
  // A picture with no caption is a normal thing to send.
  if (!text && !attachments.length) return;

  if (attachments.length && getSetting('voiceEngine') === 'live') {
    // Gemini Live is an audio-in/audio-out socket with no attachment path.
    // Say so rather than silently dropping the file the user just chose.
    notify({
      kind: 'system',
      level: 'warning',
      title: "Attachments need the Any-model voice engine — Gemini Live can't take files.",
      body: 'Change it in Settings.',
    });
    return;
  }

  // Sending while dictating sends what's currently in the composer and
  // stops dictation — it shouldn't keep appending to a message already gone.
  if (dictation?.active) dictation.stop();

  beginTurn(text, attachments);
  engine.sendText(text, { attachments: attachments.map((a) => a.id) });
  clearAttachments();

  const textInput = document.getElementById('text-input');
  if (textInput) textInput.style.height = ''; // collapse the auto-grown textarea back to one line
  updateSendButtonState();
}

// ---------- Push-to-talk mode (single utterance per click) ----------

function createPushRecognition() {
  const rec = new SpeechRecognition();
  rec.lang = 'en-US';
  rec.continuous = false;
  rec.interimResults = true;
  return rec;
}

function startPushListening() {
  pushRecognition = createPushRecognition();

  pushRecognition.onstart = () => setMicVisual('listening');

  pushRecognition.onresult = (event) => {
    let interim = '';
    let final = '';
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const t = event.results[i][0].transcript;
      if (event.results[i].isFinal) final += t;
      else interim += t;
    }
    if (interim) showInterim(interim);
    if (final) {
      clearInterim();
      sendTypedOrPushText(final.trim());
    }
  };

  pushRecognition.onerror = (event) => {
    clearInterim();
    if (event.error === 'no-speech' || event.error === 'aborted') {
      setMicVisual('idle');
      return;
    }
    if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
      notify({ kind: 'voice', level: 'warning', title: 'Microphone access was blocked. Allow the microphone and try again.' });
    } else if (event.error === 'network') {
      notify({ kind: 'voice', level: 'warning', title: 'Speech recognition needs an internet connection.' });
    } else if (event.error === 'audio-capture') {
      notify({ kind: 'voice', level: 'warning', title: "No microphone was found. Check that one's connected." });
    } else {
      // Was `Microphone problem: ${event.error}` — the Web Speech API's raw
      // error enum ('bad-grammar', 'language-not-supported', ...) shown
      // straight to the user, not plain language.
      notify({ kind: 'voice', level: 'warning', title: 'There was a problem with speech recognition. Try again in a moment.' });
    }
    setMicVisual('idle');
  };

  pushRecognition.onend = () => {
    if (engine.state === 'listening') setMicVisual('idle');
  };

  try {
    pushRecognition.start();
  } catch {
    setMicVisual('idle');
  }
}

// ---------- Mic button (behavior depends on mic mode) ----------

function onMicButtonClick() {
  if (!MIC_SUPPORTED) return;
  // Chrome only reliably runs one SpeechRecognition session at a time —
  // starting/resuming voice control always wins over composer dictation.
  if (dictation?.active) {
    dictation.stop();
    addSystemNote('Dictation stopped while you talk to Jarvis.');
  }
  const mode = getSetting('micMode');

  if (mode === 'push') {
    if (engine.state === 'listening') {
      pushRecognition?.stop();
      return;
    }
    if (engine.state === 'thinking') return;
    if (engine.state === 'speaking') {
      engine.interrupt();
    }
    startPushListening();
    return;
  }

  // Conversation mode: the mic button is a pure Mute/Unmute toggle once a
  // session is running — it must never touch Jarvis's current turn
  // (thinking/speaking), only whether he can hear you. This deliberately
  // replaces the old "click while speaking = barge-in" shortcut: those two
  // meanings can't coexist on one click once mute has to never interrupt.
  // Voice-triggered barge-in (talking over him — the mic-energy sampler) is
  // unaffected and still works exactly as before.
  if (!engine.active) {
    engine.start();
  } else {
    engine.setMuted(!engine.muted);
  }
  setMicVisual(engine.state); // mute doesn't fire its own 'state' event — repaint explicitly
}

// ---------- Navigation drawer ----------

function buildDrawer() {
  const drawer = document.getElementById('drawer');
  drawer.innerHTML = '';
  const groups = new Map();
  for (const section of SECTIONS) {
    if (!groups.has(section.group)) groups.set(section.group, []);
    groups.get(section.group).push(section);
  }
  for (const [group, items] of groups) {
    const heading = document.createElement('div');
    heading.className = 'drawer-group';
    heading.textContent = group;
    drawer.appendChild(heading);
    for (const item of items) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'drawer-item';
      btn.dataset.section = item.id;
      btn.textContent = item.label;
      btn.addEventListener('click', () => {
        closeDrawer();
        navigate(item.id);
      });
      drawer.appendChild(btn);
    }
  }
}

function openDrawer() {
  document.getElementById('drawer').classList.add('open');
  document.getElementById('scrim').classList.add('open');
}

function closeDrawer() {
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('scrim').classList.remove('open');
}

function highlightActiveDrawerItem(sectionId) {
  document.querySelectorAll('.drawer-item').forEach((el) => {
    el.classList.toggle('active', el.dataset.section === sectionId);
  });
}

function setupDrawer() {
  buildDrawer();
  // Both the home screen's hamburger and the generic-section header's
  // hamburger open the same drawer — otherwise there's no way to jump
  // between sections without going back to the assistant screen first.
  for (const id of ['menu-toggle', 'generic-menu-toggle']) {
    document.getElementById(id).addEventListener('click', (e) => {
      e.stopPropagation();
      openDrawer();
    });
  }
  document.getElementById('scrim').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeDrawer();
  });
  document.getElementById('generic-back').addEventListener('click', () => navigate('home'));

  initRouter({
    showScreen,
    afterNavigate: (sectionId) => highlightActiveDrawerItem(sectionId),
  });
}

// ---------- Model picker (compact — full management lives on the Model Settings screen) ----------

async function populateModelPicker() {
  const select = document.getElementById('provider-select');
  try {
    const [modelsRes, prefsRes] = await Promise.all([fetch('/api/models'), fetch('/api/prefs')]);
    const modelsData = await modelsRes.json();
    const prefs = await prefsRes.json();

    modelLabels = {};
    select.innerHTML = '';
    const autoOpt = document.createElement('option');
    autoOpt.value = '';
    autoOpt.textContent = 'Auto (recommended)';
    select.appendChild(autoOpt);

    for (const m of modelsData.models || []) {
      modelLabels[m.id] = m.label;
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.enabled && m.ready ? m.label : `${m.label} (not ready)`;
      select.appendChild(opt);
    }

    select.value = prefs.autoSelect ? '' : prefs.manualModelId || '';

    const hasReadyModel = (modelsData.models || []).some((m) => m.enabled && m.ready);
    document.getElementById('setup-nudge').classList.toggle('hidden', hasReadyModel);
    return { modelsData, prefs };
  } catch {
    return null;
  }
}

// ---------- Settings panel ----------

async function populateGeminiVoices() {
  const select = document.getElementById('gemini-voice-select');
  try {
    const res = await fetch('/api/voices');
    const data = await res.json();
    select.innerHTML = '';
    for (const voice of data.voices || []) {
      const opt = document.createElement('option');
      opt.value = voice.name;
      opt.textContent = `${voice.name} — ${voice.description}`;
      select.appendChild(opt);
    }
    select.value = getSetting('geminiVoice');
  } catch {
    // Leave the dropdown empty — voice output will fall back to the default.
  }
}

// Gemini Live always speaks with its own voice and can't run through a
// different AI model — hide the model picker, but keep the model row's
// note visible so users know why.
function applyVoiceEngineVisibility(engineType) {
  const isLive = engineType === 'live';
  document.getElementById('voice-output-row').classList.toggle('hidden', isLive);
  document.getElementById('gemini-voice-row').classList.toggle('hidden', isLive || getSetting('voiceOutput') !== 'gemini');
  document.getElementById('model-row').classList.toggle('hidden', isLive);
  document.getElementById('live-engine-note').classList.toggle('hidden', !isLive);
}

function setupSettingsPanel() {
  const toggleBtn = document.getElementById('settings-toggle');
  const panel = document.getElementById('settings-panel');
  const speakToggle = document.getElementById('speak-replies-toggle');
  const noteEl = document.getElementById('voice-support-note');
  const voiceEngineSelect = document.getElementById('voice-engine-select');
  const micModeSelect = document.getElementById('mic-mode-select');
  const voiceOutputSelect = document.getElementById('voice-output-select');
  const geminiVoiceRow = document.getElementById('gemini-voice-row');
  const geminiVoiceSelect = document.getElementById('gemini-voice-select');
  const aiTurnCheckToggle = document.getElementById('ai-turn-check-toggle');
  const providerSelect = document.getElementById('provider-select');
  const providerErrorEl = document.getElementById('provider-error');

  speakToggle.checked = getSetting('speakReplies');
  voiceEngineSelect.value = getSetting('voiceEngine');
  micModeSelect.value = getSetting('micMode');
  voiceOutputSelect.value = SPEECH_OUTPUT_SUPPORTED ? getSetting('voiceOutput') : 'gemini';
  aiTurnCheckToggle.checked = getSetting('useAiTurnCheck');
  geminiVoiceRow.classList.toggle('hidden', voiceOutputSelect.value !== 'gemini');
  applyVoiceEngineVisibility(voiceEngineSelect.value);

  if (!MIC_SUPPORTED) {
    micModeSelect.disabled = true;
  }
  if (!SPEECH_OUTPUT_SUPPORTED) {
    speakToggle.checked = false;
    speakToggle.disabled = true;
    voiceOutputSelect.value = 'gemini';
    voiceOutputSelect.disabled = true;
  }
  if (!MIC_SUPPORTED || !SPEECH_OUTPUT_SUPPORTED) {
    noteEl.textContent = !MIC_SUPPORTED
      ? 'Voice input needs Chrome or Edge — typing still works here.'
      : "Voice output isn't available in this browser — replies will show as text only.";
    noteEl.classList.remove('hidden');
  }

  populateGeminiVoices();
  populateModelPicker();

  toggleBtn.addEventListener('click', () => panel.classList.toggle('hidden'));

  document.getElementById('setup-nudge-btn').addEventListener('click', (e) => {
    e.stopPropagation(); // don't let this bubble to the document click-outside-closes-settings listener
    navigate('models');
  });

  document.addEventListener('click', (e) => {
    if (panel.classList.contains('hidden')) return;
    if (panel.contains(e.target) || toggleBtn.contains(e.target)) return;
    panel.classList.add('hidden');
  });

  speakToggle.addEventListener('change', () => {
    setSetting('speakReplies', speakToggle.checked);
    if (!speakToggle.checked && SPEECH_OUTPUT_SUPPORTED) window.speechSynthesis.cancel();
  });

  micModeSelect.addEventListener('change', () => {
    if (engine.active) engine.stop();
    pushRecognition?.stop();
    setSetting('micMode', micModeSelect.value);
    setMicVisual('idle');
  });

  voiceEngineSelect.addEventListener('change', () => {
    if (engine.active) engine.stop();
    pushRecognition?.stop();
    currentAssistantEl = null;
    clearInterim();
    setSetting('voiceEngine', voiceEngineSelect.value);
    applyVoiceEngineVisibility(voiceEngineSelect.value);
    engine = createEngine(voiceEngineSelect.value);
    setMicVisual('idle');
  });

  voiceOutputSelect.addEventListener('change', () => {
    setSetting('voiceOutput', voiceOutputSelect.value);
    geminiVoiceRow.classList.toggle('hidden', voiceOutputSelect.value !== 'gemini');
    engine.updateOptions({ voiceOutput: voiceOutputSelect.value });
  });

  geminiVoiceSelect.addEventListener('change', () => {
    setSetting('geminiVoice', geminiVoiceSelect.value);
    engine.updateOptions({ geminiVoice: geminiVoiceSelect.value });
  });

  aiTurnCheckToggle.addEventListener('change', () => {
    setSetting('useAiTurnCheck', aiTurnCheckToggle.checked);
    engine.updateOptions({ useAiTurnCheck: aiTurnCheckToggle.checked });
  });

  providerSelect.addEventListener('change', async () => {
    const value = providerSelect.value; // '' = Auto, otherwise a specific model id
    providerErrorEl.classList.add('hidden');
    try {
      await fetch('/api/prefs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(value ? { autoSelect: false, manualModelId: value } : { autoSelect: true }),
      });
    } catch {
      providerErrorEl.textContent = 'Could not reach the Jarvis server.';
      providerErrorEl.classList.remove('hidden');
    }
  });
}

// ---------- Proactive server -> browser notices (scheduled task runs, ...) ----------

// The Planning Partner and Content Analysis do their work on the server —
// in the background for anything that takes real time, straight away for a
// quick lookup — and announce it over SSE (`project_progress` /
// `content_progress`). There is no page to send you to any more — the
// result belongs in the conversation you were already having, which is the
// whole point of the change. A finished plan, prompt, finding, or verdict
// renders as a document card right in the transcript.

// Only act on a job once per finished state — the server can push several
// events for one job (one per step), and a note per step would be noise.
const announcedJobs = new Set();

async function handleJobProgress(data) {
  const key = `${data.id}:${data.status}:${data.step || ''}`;
  if (announcedJobs.has(key)) return;
  announcedJobs.add(key);

  const label = data.title || 'that';

  if (data.status === 'failed') {
    addSystemNote(`⚠ ${label}: ${data.error || "couldn't be finished"}`);
    // A later step can fail while an earlier one still produced something
    // worth keeping — e.g. the plan is written but the build prompt wasn't.
    if (data.document) addDocumentCard(label, data.document);
    return;
  }

  // 'started' (a project notepad opened), 'noted' (a decision recorded) and
  // 'shared' (content's free glance) are deliberately silent — no model call
  // happened, and the live reply already covers what just happened. Showing
  // a note for each would be exactly the noise a state-machine UI used to make.
  if (data.status === 'started' || data.status === 'noted' || data.status === 'shared') return;

  if (data.status === 'ready') {
    // Several build prompts, in sequence — one card each, with the
    // recommended order called out separately.
    if (data.step === 'prompts' && data.prompts?.length) {
      addPromptCards(label, data);
      return;
    }
    if (data.document) {
      addDocumentCard(label, data.document, {
        badge: data.verdict ? { text: data.verdict, className: VERDICT_CLASS[data.verdict] || '' } : undefined,
        sources: data.sources,
      });
      if (data.lookedAgain) addSystemNote(`(Looked at "${label}" again to answer that.)`);
      return;
    }
  }

  if (data.status === 'working') {
    addSystemNote(`Working on "${label}"…`);
  }
}

/** One card per build prompt — a project can need several, in sequence, for a large idea; most need only one. */
function addPromptCards(title, { prompts, promptOrder, target }) {
  const multiple = prompts.length > 1;
  for (const p of prompts) {
    const heading = multiple ? `${title} — Prompt ${p.n} of ${prompts.length}: ${p.title}` : `${title}${target ? ` — for ${target}` : ''}`;
    addDocumentCard(heading, `\`\`\`\n${p.text}\n\`\`\``, { copyText: p.text, copyLabel: 'Copy this prompt' });
  }
  if (multiple && promptOrder) addSystemNote(`Recommended order: ${promptOrder}`);
}

const VERDICT_CLASS = {
  'checks out': 'good',
  'partly true': 'warn',
  misleading: 'warn',
  false: 'bad',
  "can't tell": '',
};

function connectEvents() {
  try {
    const es = new EventSource('/api/events');
    es.onmessage = (ev) => {
      let data;
      try {
        data = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (data.type === 'control_status') {
        if (data.active) showControlBanner(data.step);
        else hideControlBanner();
        return;
      }
      if (data.type === 'control_confirm_needed') {
        showControlConfirmRow(data.summary);
        return;
      }
      if (data.type === 'control_stopped') {
        hideControlBanner();
        return;
      }
      if (data.type === 'notification') {
        ingestNotification(data.notification);
        return;
      }
      if (data.type === 'model_health') {
        // A model went unhealthy or recovered (server/models/health.js) —
        // the Model Settings screen's badges are a point-in-time snapshot
        // otherwise, so this is what keeps one open there live instead of
        // requiring a reload/Test/Check-all to see the current state.
        refreshIfActive('models');
        return;
      }
      if (data.type === 'notifications_changed') {
        // A read/read-all/delete/clear-all made through the full history
        // screen — resync so the header bell's badge doesn't drift out of
        // sync with it (see notifications.js's resync() doc comment).
        resyncNotifications();
        return;
      }
      if (data.type === 'observation_status') {
        if (data.active) showObservationDot(data.reason);
        else hideObservationDot();
        return;
      }
      if (data.type === 'monitor_started') {
        showMonitorBanner(data.monitorId, data.description);
        return;
      }
      if (data.type === 'monitor_stopped') {
        // Guard: only clear the bar if it's showing THIS monitor. Two
        // watches can run at once (the store allows it, the bar can only
        // show one) — without this, stopping monitor B could wrongly blank
        // the bar while monitor A is still the one actually shown.
        if (!activeMonitorId || data.monitorId === activeMonitorId) hideMonitorBanner();
        return;
      }
      if (data.type === 'monitor_triggered') {
        if (!activeMonitorId || data.monitorId === activeMonitorId) hideMonitorBanner();
        if (data.onTrigger?.mode === 'act') {
          addSystemNote(`Noticed: ${data.description}. Working on the next step…`);
        } else {
          addSystemNote(`Noticed: ${data.description}.`);
        }
        return;
      }
      if (data.type === 'monitor_action_done') {
        addSystemNote(data.ok !== false ? data.text || `Finished: ${data.description}.` : `Couldn't finish "${data.description}": ${data.text}`);
        return;
      }
      if (data.type === 'monitor_expired') {
        if (!activeMonitorId || data.monitorId === activeMonitorId) hideMonitorBanner();
        return;
      }
      if (data.type === 'project_progress' || data.type === 'content_progress') {
        handleJobProgress(data);
        return;
      }
      if (data.type === 'memory_candidates_ready') {
        // Proactive checkpoint result (server/memory/memory-review.js) —
        // appears on its own, no request needed. If a card is already
        // showing (unlikely — checkpoints are one-at-a-time) this simply
        // replaces it with the fuller, current list.
        showMemoryReviewCard(data.candidates);
        return;
      }
      // connector_status and task_run used to add a transcript system note
      // directly here — both now arrive as a separate 'notification' event
      // (handled above), pushed by the server at the same broadcast() call
      // site (see server.js / scheduler.js), so nothing left to do for
      // either type itself.
    };
  } catch {
    // Proactive notices are a nice-to-have — silently skip if EventSource isn't available.
  }
}

// ---------- App screen ----------

/**
 * Grows the textarea to fit its content, up to #text-input's own
 * max-height (style.css's --composer-text-max) — read from the computed
 * style, not hardcoded here, so style.css stays the one source of truth for
 * the cap (a previous version hardcoded 200px here while the CSS cap lived
 * separately, which is exactly how the two drifted out of sync before).
 * Past that height CSS's own overflow-y:auto takes over and this textarea
 * scrolls internally — it is a SIBLING of #composer-dock's other growable
 * region, .attachment-chips, not nested inside a wrapper-level scroll, so
 * the two can never produce nested/double scrollbars. This is what keeps
 * #composer-dock's own total height bounded by construction, which in turn
 * is what keeps the control row (paperclip/mic/send) always inside
 * #conversation-rail — see #composer-dock's comment in style.css. The
 * composer lives under #conversation-rail, fully decoupled from
 * #stage/#orb-stage/#mic-cluster — growth here can never touch the orb or
 * mic at all.
 */
function autoGrowTextarea(el) {
  el.style.height = 'auto';
  const max = parseFloat(getComputedStyle(el).maxHeight);
  el.style.height = `${Number.isFinite(max) ? Math.min(el.scrollHeight, max) : el.scrollHeight}px`;
}

function setupOrb() {
  const canvas = document.getElementById('orb-canvas');
  const fallbackEl = document.getElementById('orb-fallback');
  orb = createOrb(canvas, {
    fallbackEl,
    // The orb reacts to whatever's actually making sound right now: the
    // dictation mic while dictating, Jarvis's own voice while speaking,
    // otherwise the voice-control mic — except while muted, where it stays
    // still, since Jarvis genuinely isn't hearing you.
    getLevel: () => {
      if (dictation?.active) return dictation.getLevel();
      if (engine?.state === 'speaking') return engine.getOutputLevel?.() ?? 0;
      if (engine?.muted) return 0;
      return engine?.getMicLevel?.() ?? 0;
    },
  });
  orb.start();

  // No sense burning GPU on a hidden canvas — pause while backgrounded or
  // while looking at a different section (Models, Tasks, ...).
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) orb.stop();
    else if (!document.getElementById('app-screen').classList.contains('hidden')) orb.start();
  });
  new MutationObserver(() => {
    const visible = !document.getElementById('app-screen').classList.contains('hidden');
    if (visible && !document.hidden) orb.start();
    else orb.stop();
  }).observe(document.getElementById('app-screen'), { attributes: true, attributeFilter: ['class'] });
}

function setupComposer() {
  const textForm = document.getElementById('text-form');
  const textInput = document.getElementById('text-input');
  const dictationBtn = document.getElementById('composer-mic-button');

  // #composer-dock's own :focus-within (style.css) only lights up its own
  // half of the shared card — #conversation-rail is a SIBLING, not an
  // ancestor of the textarea/buttons, so CSS alone can't reach backward to
  // highlight it too. Without this, focusing the composer would visibly cut
  // the "one card" look in half right at the seam, at exactly the moment
  // (typing) it matters most. focusin/focusout (unlike focus/blur) bubble,
  // so one listener here catches focus landing on the textarea OR any of
  // the attach/dictate/send buttons.
  const composerDock = document.getElementById('composer-dock');
  const conversationRail = document.getElementById('conversation-rail');
  composerDock.addEventListener('focusin', () => conversationRail.classList.add('composer-focused'));
  composerDock.addEventListener('focusout', () => conversationRail.classList.remove('composer-focused'));

  textInput.addEventListener('input', () => {
    autoGrowTextarea(textInput);
    updateSendButtonState();
  });

  // A <textarea> in a <form> doesn't submit on Enter the way a single-line
  // <input> did — wire it explicitly. Shift+Enter still inserts a newline.
  textInput.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.shiftKey || e.isComposing) return;
    e.preventDefault();
    textForm.requestSubmit();
  });

  textForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = textInput.value;
    textInput.value = '';
    sendTypedOrPushText(text);
  });

  updateSendButtonState();

  if (!MIC_SUPPORTED) {
    dictationBtn.classList.add('hidden');
    return;
  }

  dictation = new Dictation({
    onText: (text) => {
      textInput.value = text;
      autoGrowTextarea(textInput);
      updateSendButtonState();
      // Keep the caret (and the visible scroll position) at the end as
      // words keep arriving, so a long dictation doesn't scroll away from
      // what's currently being typed.
      textInput.setSelectionRange(text.length, text.length);
    },
    onState: (state) => {
      dictationBtn.classList.toggle('dictating', state === 'listening');
      dictationBtn.setAttribute('aria-label', state === 'listening' ? 'Stop dictation' : 'Dictate a message');
    },
  });

  dictationBtn.addEventListener('click', () => {
    if (dictation.active) {
      dictation.stop();
      return;
    }
    // Chrome only reliably runs one SpeechRecognition session at a time —
    // dictating always pauses Jarvis's own voice-control session first.
    if (engine.active) {
      engine.stop();
      addSystemNote("Jarvis's voice control paused while you dictate.");
    }
    pushRecognition?.stop();
    dictation.start(textInput.value.trim());
  });

  // Stop-without-sending: lets you back out of a dictation without either
  // sending it or having to manually clear the text.
  textInput.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && dictation.active) dictation.stop();
  });
}

function setupAppScreen() {
  engine = createEngine();
  setupOrb();

  const micButton = document.getElementById('mic-button');
  micButton.addEventListener('click', onMicButtonClick);

  if (!MIC_SUPPORTED) {
    micButton.disabled = true;
    micButton.title = 'Voice input needs Chrome or Edge';
    document.getElementById('mic-hint').classList.add('hidden');
    document.getElementById('text-input').focus();
  } else {
    document.getElementById('mic-hint').textContent =
      getSetting('micMode') === 'conversation' ? 'Talk freely once listening starts' : 'Space bar also works';
  }

  setupAttachments();
  setupComposer();

  // The Skills screen's "Create with Jarvis" / "Try in chat" / "Edit with
  // Jarvis" actions dispatch this rather than importing app.js directly
  // (public/screens/*.js are dynamically imported BY router.js, so a static
  // import back into app.js risks a module-graph cycle — this event is the
  // same loosely-coupled shape server-driven ui_action navigation already
  // uses). Mirrors Claude's own "Create with Claude": not a separate
  // screen, just a composer pre-fill into ordinary conversation.
  window.addEventListener('jarvis:start-skill-chat', (e) => {
    navigate('home');
    const textInput = document.getElementById('text-input');
    if (!textInput) return;
    textInput.value = e.detail?.text || '';
    autoGrowTextarea(textInput);
    updateSendButtonState();
    textInput.focus();
  });

  if (MIC_SUPPORTED) {
    document.addEventListener('keydown', (e) => {
      if (e.code !== 'Space') return;
      // A modal popup's Cancel/Create/checkbox controls need Space to work
      // as a normal activation key, not have it hijacked into toggling the
      // mic underneath — SELECT/BUTTON weren't guarded before (only text
      // inputs were), and modal-open on <body> covers every control inside
      // a popup regardless of which element currently has focus.
      if (document.body.classList.contains('modal-open')) return;
      const tag = document.activeElement && document.activeElement.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'BUTTON') return;
      if (e.repeat) return;
      e.preventDefault();
      onMicButtonClick();
    });
  }

  setMicVisual('idle');
  setupSettingsPanel();
}

// ---------- Startup ----------

async function init() {
  setupDrawer();
  setupAppScreen();
  setupChatHistoryIntegration();
  setupControlBanner();
  setupMonitorBanner();
  setupObservationDot();
  await setupNotifications();
  await navigate(currentSectionId(), { pushHash: false });
  await loadActiveConversation();
  connectEvents();

  const data = await populateModelPicker();
  if (!data) {
    // notify() itself falls back to a local-only toast when the POST to
    // persist it also fails — exactly what happens here, since the server
    // being unreachable is the whole reason this fires.
    notify({ kind: 'system', level: 'error', title: 'Could not reach the Jarvis server. Is the window running it still open?' });
  }
}

init();
