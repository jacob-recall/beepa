// Teammate proposal inbox — thread list (left) + detail (right).

import { ROOMID_RE, api } from '../../shared/matrix/client.js';
import { $, el, sanitizeLine } from '../../shared/ui/el.js';
import { openConvo, sendConvoMessage } from '../../shared/ui/chat.js';
import { sendCmd, validHandle } from '../../shared/ui/sources.js';
import { confirmModal } from '../../shared/ui/connections.js';
import { setProposalsViewHook, setDetailMode } from '../../shared/ui/nav.js';
import { feedRelTime } from '../../shared/ui/search.js';
import { S, feedModel } from '../../shared/state.js';

const HANDLED_KEY = 'com.jkali.proposals_handled';
let activeProposal = null;
let cachedProposals = [];

function loadHandled() {
  try { return new Set(JSON.parse(localStorage.getItem(HANDLED_KEY) || '[]')); }
  catch (e) { return new Set(); }
}
function saveHandled(set) {
  try { localStorage.setItem(HANDLED_KEY, JSON.stringify([...set])); } catch (e) { /* ignore */ }
}
function markHandled(p) { const s = loadHandled(); s.add(p.eventId); saveHandled(s); }

// Discriminated parse. A proposal is EITHER aimed at an existing conversation
// (kind:'room', the original behavior — sent via the guarded sendConvoMessage
// path) OR at a bare contact identifier with no conversation yet
// (kind:'identifier' — sent by starting a NEW iMessage chat through the gated
// start-chat capability). A proposal that is neither a valid target_room nor a
// valid target_identifier is dropped (null), exactly as before. The two shapes
// are mutually exclusive here: target_room wins if present, and an identifier
// parse NEVER carries a targetRoom key, so a person-targeted draft can never be
// mistaken for (or redirected into) a room send.
function parseProposal(e) {
  if (!e || e.type !== 'com.jkali.proposal' || !e.content) return null;
  const c = e.content;
  const body = c.body;
  if (typeof body !== 'string' || !body.trim()) return null;
  const ts = typeof c.origin_ts === 'number' ? c.origin_ts
      : (typeof e.origin_server_ts === 'number' ? e.origin_server_ts : 0);
  const template = c.template === true;

  const room = c.target_room;
  if (typeof room === 'string' && room) {
    return { kind: 'room', eventId: e.event_id, targetRoom: room, body, template, ts };
  }

  // Identifier-targeted: content carries target_identifier + target_source and
  // NO target_room. The handle is re-validated against the SC-7 regexes (E.164
  // OR strict email) at parse time — a malformed handle drops the whole
  // proposal; the send path re-validates again, and the daemon is authoritative.
  const identifier = typeof c.target_identifier === 'string' ? c.target_identifier.trim() : '';
  if (identifier && validHandle(identifier)) {
    const source = typeof c.target_source === 'string' ? c.target_source : '';
    const display = (typeof c.target_display === 'string' && c.target_display) ? c.target_display : identifier;
    return {
      kind: 'identifier',
      eventId: e.event_id,
      targetSource: source,
      targetIdentifier: identifier,
      targetDisplay: display,
      body, template, ts,
    };
  }
  return null;
}

// The proposals room id is discovered once (a full filtered /sync walks EVERY
// joined room server-side — ~10s on a real account) and then cached, in memory
// and, as a per-viewer convenience, in localStorage. The cache NEVER
// authorizes anything: every fetch re-verifies the room still carries the
// com.jkali.proposals state marker before reading it, so a stale or tampered
// cached id degrades to rediscovery, never to trusting an unmarked room.
const ROOMS_KEY = 'com.jkali.proposals_rooms';
let proposalsRoomIds = null;
let lastEmptyDiscovery = 0;

function loadCachedRooms() {
  try {
    const arr = JSON.parse(localStorage.getItem(ROOMS_KEY) || '[]');
    return (Array.isArray(arr) ? arr : [])
      .filter((r) => typeof r === 'string' && ROOMID_RE.test(r));
  } catch (e) { return []; }
}
function saveCachedRooms(ids) {
  try { localStorage.setItem(ROOMS_KEY, JSON.stringify(ids)); } catch (e) { /* ignore */ }
}

async function verifyMarker(rid) {
  try {
    await api('GET', '/_matrix/client/v3/rooms/' + encodeURIComponent(rid)
      + '/state/com.jkali.proposals/');
    return true;
  } catch (e) { return false; }
}

// Slow path: the full-sync walk, run only when no cached id verifies. Rate-
// limited when it comes back empty (no proposals room exists yet — e.g. the
// uplink has not connected to a master), so the 10s poll does not re-pay the
// full walk continuously.
async function discoverProposalsRooms() {
  const filter = encodeURIComponent(JSON.stringify({
    room: {
      timeline: { limit: 1, types: ['com.jkali.proposal'] },
      state: { types: ['com.jkali.proposals'], lazy_load_members: true },
      account_data: { types: [] },
    },
    presence: { types: [] }, account_data: { types: [] },
  }));
  const data = await api('GET', '/_matrix/client/v3/sync?timeout=0&filter=' + filter);
  const join = (data.rooms && data.rooms.join) || {};
  const out = [];
  for (const rid of Object.keys(join)) {
    if (!ROOMID_RE.test(rid)) continue;
    const r = join[rid];
    const stateEvents = ((r.state && r.state.events) || [])
      .concat((r.timeline && r.timeline.events) || []);
    if (stateEvents.some(e => e.type === 'com.jkali.proposals' && e.state_key === '')) {
      out.push(rid);
    }
  }
  return out;
}

async function proposalsRooms() {
  const cached = proposalsRoomIds !== null ? proposalsRoomIds : loadCachedRooms();
  const ok = [];
  for (const rid of cached) if (await verifyMarker(rid)) ok.push(rid);
  if (!ok.length) {
    if (Date.now() - lastEmptyDiscovery < 60000) return [];
    for (const rid of await discoverProposalsRooms()) ok.push(rid);
    if (!ok.length) lastEmptyDiscovery = Date.now();
  }
  proposalsRoomIds = ok;
  saveCachedRooms(ok);
  return ok;
}

async function fetchProposals() {
  const out = [];
  for (const rid of await proposalsRooms()) {
    const data = await api('GET', '/_matrix/client/v3/rooms/' + encodeURIComponent(rid)
      + '/messages?dir=b&limit=100');
    for (const e of (Array.isArray(data.chunk) ? data.chunk : [])) {
      const p = parseProposal(e);
      if (p) out.push(p);
    }
  }
  return out;
}

function targetName(p) {
  if (p.kind === 'identifier') return sanitizeLine(p.targetDisplay || p.targetIdentifier);
  const rec = feedModel.get(p.targetRoom);
  return sanitizeLine((rec && rec.name) || p.targetRoom);
}

function setDetailError(err, msg) {
  if (!err) return;
  err.textContent = msg || '';
  err.classList.toggle('hidden', !msg);
}

function buildThreadRow(p) {
  const name = targetName(p);
  const preview = sanitizeLine(p.body).replace(/\s+/g, ' ').slice(0, 80);
  const row = el('div', 'convo thread-row');
  row.dataset.proposalId = p.eventId;
  row.setAttribute('role', 'button');
  row.tabIndex = 0;
  row.appendChild(el('div', 'avatar', 'P'));
  const meta = el('div', 'meta');
  meta.appendChild(el('div', 'title', name));
  meta.appendChild(el('div', 'preview', preview));
  row.appendChild(meta);
  if (p.ts) row.appendChild(el('span', 'when', feedRelTime(p.ts)));
  row.appendChild(el('span', 'thread-badge', 'Draft'));
  const open = () => selectProposal(p);
  row.addEventListener('click', open);
  row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  return row;
}

function markActiveThread(eventId) {
  for (const row of document.querySelectorAll('.thread-row')) {
    row.classList.toggle('active', row.dataset.proposalId === eventId);
  }
}

async function sendProposal(p, body, err, btn) {
  setDetailError(err, '');
  const text = (body || '').trim();
  if (!text) { setDetailError(err, 'Type a message before sending.'); return; }

  if (p.kind === 'identifier') { await sendIdentifierProposal(p, text, err, btn); return; }

  // kind === 'room' — unchanged: the ONLY conversation send path.
  if (btn) btn.disabled = true;
  await openConvo(p.targetRoom);
  const ok = await sendConvoMessage(p.targetRoom, text);
  if (btn) btn.disabled = false;
  if (ok) { markHandled(p); renderProposalsView(); return; }
  setDetailError(err, 'Could not send — conversation unavailable.');
}

// Person-targeted send leg: approving a draft aimed at a contact identifier with
// no existing conversation starts a NEW iMessage chat through the already-
// approved, gated start-chat capability. NEVER auto-sends: this only runs from
// an explicit Send click, and it additionally requires the teammate to confirm a
// VERBATIM modal showing the exact handle + message. The identifier send goes
// ONLY through sendCmd(...,'start-chat ...') into the C-1 unconditionally-
// verified iMessage mgmt room — no portal send, no bypass. The client re-
// validates the handle (SC-7 regex) as a UX pre-check; the daemon is the
// authoritative gate and enforces the rate caps. The handle and body are never
// logged or echoed by this function.
async function sendIdentifierProposal(p, text, err, btn) {
  // Only iMessage supports start-chat today.
  if (p.targetSource !== 'imessage') {
    setDetailError(err, 'Unsupported: only iMessage new-chat drafts can be sent from here.');
    return;
  }
  const handle = (p.targetIdentifier || '').trim();
  // Client-side SC-7 re-validation (E.164 phone OR strict email). UX pre-check.
  if (!validHandle(handle)) {
    setDetailError(err, 'Cannot send — the contact handle is not a valid phone number or email.');
    return;
  }
  // Verbatim confirm: show the EXACT handle + message that will be sent, so
  // confirm-equals-send. textContent-only inside confirmModal.
  const confirmed = await confirmModal(
    'Start a NEW iMessage chat?',
    'To: ' + handle + '\n\nMessage:\n' + text,
    false);
  if (!confirmed) return;

  if (btn) btn.disabled = true;
  try {
    await sendCmd('imessage', 'start-chat ' + handle + ' | ' + text);
  } finally {
    if (btn) btn.disabled = false;
  }
  markHandled(p);
  renderProposalsView();
}

async function prefillProposal(p, body, err) {
  setDetailError(err, '');
  await openConvo(p.targetRoom);
  if (S.openRoomId !== p.targetRoom) {
    setDetailError(err, 'Conversation not available.');
    return;
  }
  const input = $('convo-input');
  if (input) { input.value = body || ''; input.focus(); }
}

function renderProposalDetail(p) {
  const host = $('proposal-detail-body');
  if (!host) return;
  host.replaceChildren();

  const note = el('p', 'muted proposal-detail-note',
    p.template ? 'Template suggestion — review before sending.' : 'Suggested by your manager — not sent yet.');
  host.appendChild(note);

  const to = el('div', 'proposal-to');
  to.appendChild(el('span', 'proposal-to-label', 'To'));
  if (p.kind === 'identifier') {
    // "To: <display> (<handle>) — starts a NEW iMessage chat" (textContent only).
    to.appendChild(el('span', 'proposal-to-name',
      sanitizeLine(p.targetDisplay || p.targetIdentifier)
      + ' (' + sanitizeLine(p.targetIdentifier) + ')'));
    to.appendChild(el('span', 'proposal-to-new muted', ' — starts a NEW iMessage chat'));
  } else {
    to.appendChild(el('span', 'proposal-to-name', targetName(p)));
  }
  host.appendChild(to);

  const ta = el('textarea', 'proposal-body');
  ta.value = p.body;
  ta.rows = 6;
  host.appendChild(ta);

  const err = el('div', 'proposal-card-error hidden');
  host.appendChild(err);

  const actions = el('div', 'proposal-actions');
  const sendBtn = el('button', 'proposal-btn proposal-send primary', 'Send');
  sendBtn.type = 'button';
  sendBtn.addEventListener('click', () => sendProposal(p, ta.value, err, sendBtn));
  actions.appendChild(sendBtn);
  // "Open in chat" prefills an EXISTING conversation's composer — only meaningful
  // for a room-targeted draft; an identifier draft has no conversation yet.
  if (p.kind === 'room') {
    const editBtn = el('button', 'proposal-btn', 'Open in chat');
    editBtn.type = 'button';
    editBtn.addEventListener('click', () => prefillProposal(p, ta.value, err));
    actions.appendChild(editBtn);
  }
  const dropBtn = el('button', 'proposal-btn proposal-dismiss', 'Dismiss');
  dropBtn.type = 'button';
  dropBtn.addEventListener('click', () => { markHandled(p); renderProposalsView(); });
  actions.appendChild(dropBtn);
  host.appendChild(actions);

  $('proposal-detail-title').textContent = targetName(p);
}

function selectProposal(p) {
  activeProposal = p;
  markActiveThread(p.eventId);
  setDetailMode('proposal');
  renderProposalDetail(p);
}

function wireProposalNav() {
  const back = $('proposal-back');
  if (back && !back.dataset.wired) {
    back.dataset.wired = '1';
    back.addEventListener('click', () => {
      activeProposal = null;
      markActiveThread(null);
      setDetailMode('empty');
    });
  }
}

async function renderProposalsView() {
  wireProposalNav();
  const list = $('list-body');
  if (!list) return;
  list.replaceChildren();
  list.appendChild(el('p', 'muted', 'Loading…'));

  let proposals;
  try { proposals = await fetchProposals(); }
  catch (e) {
    list.replaceChildren();
    list.appendChild(el('p', 'error', 'Could not load: ' + String(e.message || e)));
    return;
  }

  const handled = loadHandled();
  cachedProposals = proposals.filter(p => !handled.has(p.eventId));
  cachedProposals.sort((a, b) => b.ts - a.ts);

  list.replaceChildren();
  if (!cachedProposals.length) {
    list.appendChild(el('p', 'list-empty', 'No suggestions right now.'));
    setDetailMode('empty');
    return;
  }
  for (const p of cachedProposals) list.appendChild(buildThreadRow(p));
  if (activeProposal && cachedProposals.some(p => p.eventId === activeProposal.eventId)) {
    selectProposal(activeProposal);
  } else {
    activeProposal = null;
    setDetailMode('empty');
  }
}

// Live refresh: new proposals appear without leaving and re-entering the view.
// Guards: only while the proposals LIST is actually showing (the shared
// #list-body belongs to whichever view is active), and never while a draft is
// open — a re-render rebuilds the detail pane and would destroy typed edits.
// Re-renders only when the visible set actually changed.
let pollTimer = null;

function proposalsListShowing() {
  const btn = $('list-mode-proposals');
  return !!(btn && btn.classList.contains('active') && btn.offsetParent !== null);
}

async function refreshIfChanged() {
  if (!proposalsListShowing() || activeProposal) return;
  let proposals;
  try { proposals = await fetchProposals(); } catch (e) { return; }
  const handled = loadHandled();
  const fresh = proposals.filter(p => !handled.has(p.eventId))
    .map(p => p.eventId).sort().join(',');
  const cur = cachedProposals.map(p => p.eventId).sort().join(',');
  if (fresh !== cur) await renderProposalsView();
}

function initProposalsUI() {
  setProposalsViewHook(renderProposalsView);
  if (!pollTimer) {
    pollTimer = setInterval(() => { refreshIfChanged().catch(() => {}); }, 10000);
  }
}

export { initProposalsUI, renderProposalsView, parseProposal };
