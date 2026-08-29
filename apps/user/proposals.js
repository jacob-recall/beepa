// Teammate proposal inbox — thread list (left) + detail (right).

import { ROOMID_RE, api } from '../../shared/matrix/client.js';
import { $, el, sanitizeLine } from '../../shared/ui/el.js';
import { openConvo, sendConvoMessage } from '../../shared/ui/chat.js';
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

function parseProposal(e) {
  if (!e || e.type !== 'com.jkali.proposal' || !e.content) return null;
  const c = e.content;
  const target = c.target_room;
  const body = c.body;
  if (typeof target !== 'string' || !target) return null;
  if (typeof body !== 'string' || !body.trim()) return null;
  return {
    eventId: e.event_id,
    targetRoom: target,
    body: body,
    template: c.template === true,
    ts: typeof c.origin_ts === 'number' ? c.origin_ts
        : (typeof e.origin_server_ts === 'number' ? e.origin_server_ts : 0),
  };
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
  const rec = feedModel.get(p.targetRoom);
  return sanitizeLine((rec && rec.name) || p.targetRoom);
}

function setDetailError(err, msg) {
  if (!err) return;
  err.textContent = msg || '';
  err.classList.toggle('hidden', !msg);
}

function buildThreadRow(p) {
  const rec = feedModel.get(p.targetRoom);
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
  if (btn) btn.disabled = true;
  await openConvo(p.targetRoom);
  const ok = await sendConvoMessage(p.targetRoom, text);
  if (btn) btn.disabled = false;
  if (ok) { markHandled(p); renderProposalsView(); return; }
  setDetailError(err, 'Could not send — conversation unavailable.');
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
  to.appendChild(el('span', 'proposal-to-name', targetName(p)));
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
  const editBtn = el('button', 'proposal-btn', 'Open in chat');
  editBtn.type = 'button';
  editBtn.addEventListener('click', () => prefillProposal(p, ta.value, err));
  const dropBtn = el('button', 'proposal-btn proposal-dismiss', 'Dismiss');
  dropBtn.type = 'button';
  dropBtn.addEventListener('click', () => { markHandled(p); renderProposalsView(); });
  actions.appendChild(sendBtn);
  actions.appendChild(editBtn);
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

export { initProposalsUI, renderProposalsView };
