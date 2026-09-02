// Teammate proposal inbox. A room-targeted draft opens the real conversation
// with the text already in the composer; the row has ✓ / ✕ so you can send or
// reject without the full-pane editor. Enter on a focused row sends. Identifier
// (new-chat) drafts still use the detail pane + confirm modal.

import { ROOMID_RE, api } from '../../shared/matrix/client.js';
import { $, el, sanitizeLine } from '../../shared/ui/el.js';
import { openConvo, sendConvoMessage, prefillComposer, setAfterSendHook } from '../../shared/ui/chat.js';
import { buildPlatBadge } from '../../shared/ui/rows.js';
import { sendCmd, validHandle } from '../../shared/ui/sources.js';
import { confirmModal } from '../../shared/ui/connections.js';
import { setProposalsViewHook, setDetailMode } from '../../shared/ui/nav.js';
import { feedRelTime } from '../../shared/ui/search.js';
import { S, feedModel } from '../../shared/state.js';

const HANDLED_KEY = 'com.jkali.proposals_handled';
let allProposals = [];     // everything parsed (pending + dismissed + non-actionable)
let selectedId = null;     // eventId shown in the right pane, or null
let showDismissed = false;
let showSent = false;
let kebabOpen = false;

function loadHandled() {
  try { return new Set(JSON.parse(localStorage.getItem(HANDLED_KEY) || '[]')); }
  catch (e) { return new Set(); }
}
function saveHandled(set) {
  try { localStorage.setItem(HANDLED_KEY, JSON.stringify([...set])); } catch (e) { /* ignore */ }
}
function markHandled(p) { const s = loadHandled(); s.add(p.eventId); saveHandled(s); }
function unmarkHandled(p) { const s = loadHandled(); s.delete(p.eventId); saveHandled(s); }

// Discriminated parse. EITHER an existing conversation (kind:'room', sent via the
// guarded sendConvoMessage path) OR a bare contact identifier with no
// conversation yet (kind:'identifier' — sent by starting a NEW iMessage chat
// through the gated start-chat capability). target_room wins if present; an
// identifier parse NEVER carries a targetRoom, so a person-targeted draft can
// never be redirected into a room send. Neither valid → dropped.
//
// S3 auto-send classification (direct-share-level plan D2.8/D2.9, wire
// contract pinned here for S3 to match): a normal draft's content can ALSO
// carry `"com.jkali.auto_sent": true` + `"sent_event_id"` (the uplink's
// non-actionable outcome record for a message it already sent directly) or
// `"com.jkali.send_ambiguous": true` (a post-dispatch failure whose outcome
// is unknown). Both flags come ONLY from event content — never from
// localStorage/HANDLED_KEY — so classification is identical on a fresh
// browser profile and an existing one. `autoSent`/`ambiguous` are mutually
// exclusive (autoSent wins if a malformed event somehow carried both) and
// mark the proposal non-actionable: never counted pending, never sendable
// with one click.
function parseProposal(e) {
  if (!e || e.type !== 'com.jkali.proposal' || !e.content) return null;
  const c = e.content;
  const body = c.body;
  if (typeof body !== 'string' || !body.trim()) return null;
  const ts = typeof c.origin_ts === 'number' ? c.origin_ts
      : (typeof e.origin_server_ts === 'number' ? e.origin_server_ts : 0);
  const template = c.template === true;
  const autoSent = c['com.jkali.auto_sent'] === true;
  const sentEventId = (autoSent && typeof c.sent_event_id === 'string' && c.sent_event_id) ? c.sent_event_id : null;
  const ambiguous = !autoSent && c['com.jkali.send_ambiguous'] === true;

  const room = c.target_room;
  if (typeof room === 'string' && room) {
    return { kind: 'room', eventId: e.event_id, targetRoom: room, body, template, ts, autoSent, sentEventId, ambiguous };
  }
  const identifier = typeof c.target_identifier === 'string' ? c.target_identifier.trim() : '';
  if (identifier && validHandle(identifier)) {
    const source = typeof c.target_source === 'string' ? c.target_source : '';
    const display = (typeof c.target_display === 'string' && c.target_display) ? c.target_display : identifier;
    return { kind: 'identifier', eventId: e.event_id, targetSource: source, targetIdentifier: identifier, targetDisplay: display, body, template, ts, autoSent, sentEventId, ambiguous };
  }
  return null;
}

// PURE: split a proposal list into the four rendered buckets. `autoSent` and
// `ambiguous` proposals are NEVER pending/dismissed — they classify the same
// way regardless of what `handled` contains (localStorage-independent, F5).
function partitionProposals(proposals, handled) {
  const h = (handled && typeof handled.has === 'function') ? handled : new Set();
  const pending = [];
  const dismissed = [];
  const ambiguous = [];
  const sent = [];
  for (const p of (Array.isArray(proposals) ? proposals : [])) {
    if (!p) continue;
    if (p.autoSent) { sent.push(p); continue; }
    if (p.ambiguous) { ambiguous.push(p); continue; }
    (h.has(p.eventId) ? dismissed : pending).push(p);
  }
  const byTsDesc = (a, b) => b.ts - a.ts;
  return {
    pending: pending.sort(byTsDesc),
    dismissed: dismissed.sort(byTsDesc),
    ambiguous: ambiguous.sort(byTsDesc),
    sent: sent.sort(byTsDesc),
  };
}

function pendingForRoom(proposals, handled, roomId) {
  if (typeof roomId !== 'string' || !roomId) return null;
  if (!Array.isArray(proposals) || !handled || typeof handled.has !== 'function') return null;
  let best = null;
  for (const p of proposals) {
    if (!p || p.kind !== 'room' || p.targetRoom !== roomId) continue;
    if (p.autoSent || p.ambiguous) continue;        // non-actionable, never a pending draft
    if (handled.has(p.eventId)) continue;
    if (!best || p.ts > best.ts) best = p;
  }
  return best;
}

function rowGesture(p, gesture) {
  if (!p) return { action: 'none' };
  if (gesture === 'reject') return { action: 'reject' };
  if (gesture === 'enter' || gesture === 'accept') return { action: 'send' };
  if (gesture === 'click') {
    if (p.kind === 'room') return { action: 'open', prefill: p.body };
    return { action: 'detail' };
  }
  return { action: 'none' };
}

function openWithDraft(roomId, body) {
  openConvo(roomId);
  queueMicrotask(() => {
    if (S.openRoomId === roomId) prefillComposer(body);
  });
}

// ---- proposals room discovery (cached; full /sync only on a real cache miss) --
const ROOMS_KEY = 'com.jkali.proposals_rooms';
let proposalsRoomIds = null;
let lastEmptyDiscovery = 0;

function loadCachedRooms() {
  try {
    const arr = JSON.parse(localStorage.getItem(ROOMS_KEY) || '[]');
    return (Array.isArray(arr) ? arr : []).filter((r) => typeof r === 'string' && ROOMID_RE.test(r));
  } catch (e) { return []; }
}
function saveCachedRooms(ids) {
  try { localStorage.setItem(ROOMS_KEY, JSON.stringify(ids)); } catch (e) { /* ignore */ }
}
async function verifyMarker(rid) {
  try {
    await api('GET', '/_matrix/client/v3/rooms/' + encodeURIComponent(rid) + '/state/com.jkali.proposals/');
    return true;
  } catch (e) { return false; }
}
async function discoverProposalsRooms() {
  const filter = encodeURIComponent(JSON.stringify({
    room: { timeline: { limit: 1, types: ['com.jkali.proposal'] },
      state: { types: ['com.jkali.proposals'], lazy_load_members: true }, account_data: { types: [] } },
    presence: { types: [] }, account_data: { types: [] },
  }));
  const data = await api('GET', '/_matrix/client/v3/sync?timeout=0&filter=' + filter);
  const join = (data.rooms && data.rooms.join) || {};
  const out = [];
  for (const rid of Object.keys(join)) {
    if (!ROOMID_RE.test(rid)) continue;
    const r = join[rid];
    const stateEvents = ((r.state && r.state.events) || []).concat((r.timeline && r.timeline.events) || []);
    if (stateEvents.some(e => e.type === 'com.jkali.proposals' && e.state_key === '')) out.push(rid);
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
    const data = await api('GET', '/_matrix/client/v3/rooms/' + encodeURIComponent(rid) + '/messages?dir=b&limit=100');
    for (const e of (Array.isArray(data.chunk) ? data.chunk : [])) {
      const p = parseProposal(e);
      if (p) out.push(p);
    }
  }
  return out;
}

// ---- helpers -----------------------------------------------------------------
function targetName(p) {
  if (p.kind === 'identifier') return sanitizeLine(p.targetDisplay || p.targetIdentifier);
  const rec = feedModel.get(p.targetRoom);
  return sanitizeLine((rec && rec.name) || p.targetRoom);
}
function sourceOf(p) {
  if (p.kind === 'identifier') return p.targetSource || '';
  const rec = feedModel.get(p.targetRoom);
  return (rec && rec.sourceId) || '';
}
function setDetailError(err, msg) {
  if (!err) return;
  err.textContent = msg || '';
  err.classList.toggle('hidden', !msg);
}
function pendingList() { return partitionProposals(allProposals, loadHandled()).pending; }
function dismissedList() { return partitionProposals(allProposals, loadHandled()).dismissed; }
// Non-actionable history: sent directly by the uplink (F5), never a draft.
function historyList() { return partitionProposals(allProposals, loadHandled()).sent; }
// Non-actionable, but needs a manual look — a post-dispatch failure whose
// outcome the uplink could not confirm (D2.9).
function ambiguousList() { return partitionProposals(allProposals, loadHandled()).ambiguous; }

// ---- send paths (UNCHANGED guards) -------------------------------------------
async function sendProposal(p, body, err, btn) {
  setDetailError(err, '');
  const text = (body || '').trim();
  if (!text) { setDetailError(err, 'Type a message before sending.'); return; }
  if (p.kind === 'identifier') { await sendIdentifierProposal(p, text, err, btn); return; }
  if (btn) btn.disabled = true;
  await openConvo(p.targetRoom);
  const ok = await sendConvoMessage(p.targetRoom, text);
  if (btn) btn.disabled = false;
  if (ok) { markHandled(p); afterHandled(p); return; }
  setDetailError(err, 'Could not send — conversation unavailable.');
}
// Person-targeted send leg — approving a draft aimed at a contact identifier with
// no existing conversation starts a NEW iMessage chat through the gated start-chat
// capability. NEVER auto-sends: explicit Send + a VERBATIM confirm modal. Goes
// ONLY through sendCmd(...,'start-chat ...') into the verified iMessage mgmt room.
async function sendIdentifierProposal(p, text, err, btn) {
  if (p.targetSource !== 'imessage') {
    setDetailError(err, 'Unsupported: only iMessage new-chat drafts can be sent from here.');
    return;
  }
  const handle = (p.targetIdentifier || '').trim();
  if (!validHandle(handle)) {
    setDetailError(err, 'Cannot send — the contact handle is not a valid phone number or email.');
    return;
  }
  const confirmed = await confirmModal('Start a NEW iMessage chat?', 'To: ' + handle + '\n\nMessage:\n' + text, false);
  if (!confirmed) return;
  if (btn) btn.disabled = true;
  try { await sendCmd('imessage', 'start-chat ' + handle + ' | ' + text); }
  finally { if (btn) btn.disabled = false; }
  markHandled(p);
  afterHandled(p);
}

// After send/reject: drop the draft from the list. Stay in the open chat if
// we just sent into one; identifier detail is the only pane we close.
let focusListAfterHandle = false;
function afterHandled(p) {
  if (selectedId === p.eventId) {
    selectedId = null;
    if (p.kind === 'identifier') setDetailMode('empty');
  }
  updateCount(pendingList().length);
  if (proposalsListShowing()) renderList();
  if (focusListAfterHandle) {
    focusListAfterHandle = false;
    queueMicrotask(() => {
      const btn = document.querySelector('.proposal-quick-yes');
      const row = btn && btn.closest('.convo');
      if (row) row.focus();
    });
  }
}

// ---- list (left) -------------------------------------------------------------
// A small numbered dot badge overlaid on the Proposals tab (not "(N)" text).
function updateCount(n) {
  const btn = $('list-mode-proposals');
  if (!btn) return;
  btn.replaceChildren();
  btn.appendChild(document.createTextNode('Proposals'));
  if (n > 0) btn.appendChild(el('span', 'proposal-count-dot', String(n)));
}

function buildRow(p, dismissed) {
  const row = el('div', 'convo proposal-row' + (dismissed ? ' dismissed' : ''));
  row.dataset.proposalId = p.eventId;
  row.setAttribute('role', 'button');
  row.tabIndex = 0;
  if (selectedId === p.eventId) row.classList.add('active');
  row.appendChild(buildPlatBadge(sourceOf(p)));
  const meta = el('div', 'meta');
  meta.appendChild(el('div', 'title', targetName(p)));
  meta.appendChild(el('div', 'preview', sanitizeLine(p.body).replace(/\s+/g, ' ').slice(0, 90)));
  row.appendChild(meta);
  if (p.ts) row.appendChild(el('span', 'when', feedRelTime(p.ts)));
  row.appendChild(el('span', 'thread-badge', dismissed ? 'Dismissed' : 'Draft'));
  const open = () => select(p, dismissed);
  row.addEventListener('click', open);
  row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  if (!dismissed) attachQuickActions(row, p);
  return row;
}

// Non-actionable: "sent directly" history from the uplink's auto-send outcome
// record (F5). Never a quick-action row — clicking only opens the real
// conversation, never re-sends.
function buildHistoryRow(p) {
  const row = el('div', 'convo proposal-row proposal-history');
  row.setAttribute('role', 'button');
  row.tabIndex = 0;
  row.appendChild(buildPlatBadge(sourceOf(p)));
  const meta = el('div', 'meta');
  meta.appendChild(el('div', 'title', targetName(p)));
  meta.appendChild(el('div', 'preview', sanitizeLine(p.body).replace(/\s+/g, ' ').slice(0, 90)));
  row.appendChild(meta);
  if (p.ts) row.appendChild(el('span', 'when', feedRelTime(p.ts)));
  row.appendChild(el('span', 'thread-badge sent-directly', 'Sent directly'));
  const open = () => { if (p.kind === 'room') openConvo(p.targetRoom); };
  row.addEventListener('click', open);
  row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  return row;
}

// Non-actionable as a one-click send (D2.9 post-dispatch ambiguous failure) —
// distinct from both a pending draft and sent history: it may or may not
// already be out. The only affordance is the manual path, opening the real
// conversation so the teammate can check for themselves.
function buildAmbiguousRow(p) {
  const row = el('div', 'convo proposal-row proposal-ambiguous');
  row.setAttribute('role', 'button');
  row.tabIndex = 0;
  row.appendChild(buildPlatBadge(sourceOf(p)));
  const meta = el('div', 'meta');
  meta.appendChild(el('div', 'title', targetName(p)));
  meta.appendChild(el('div', 'preview', 'May already have been sent — check the conversation'));
  row.appendChild(meta);
  if (p.ts) row.appendChild(el('span', 'when', feedRelTime(p.ts)));
  row.appendChild(el('span', 'thread-badge ambiguous-badge', 'Check conversation'));
  const open = () => { if (p.kind === 'room') openConvo(p.targetRoom); };
  row.addEventListener('click', open);
  row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  return row;
}

function buildTopBar(pendingN, dismissedN, sentN) {
  const bar = el('div', 'proposals-head');
  const left = el('div', 'proposals-head-left');
  left.appendChild(el('span', 'proposals-title', 'Proposals'));
  left.appendChild(el('span', 'proposals-count', String(pendingN)));
  bar.appendChild(left);

  const kebab = el('button', 'proposal-kebab');
  kebab.type = 'button';
  kebab.textContent = '⋮';                       // vertical ellipsis (kebab)
  kebab.title = 'More';
  kebab.addEventListener('click', (e) => { e.stopPropagation(); kebabOpen = !kebabOpen; renderList(); });
  bar.appendChild(kebab);

  if (kebabOpen) {
    const menu = el('div', 'proposal-kebab-menu');
    const toggle = el('button', 'proposal-kebab-item');
    toggle.type = 'button';
    toggle.textContent = (showDismissed ? 'Hide dismissed' : 'Show dismissed')
      + (dismissedN ? ' (' + dismissedN + ')' : '');
    toggle.addEventListener('click', (e) => { e.stopPropagation(); showDismissed = !showDismissed; kebabOpen = false; renderList(); });
    menu.appendChild(toggle);
    const sentToggle = el('button', 'proposal-kebab-item');
    sentToggle.type = 'button';
    sentToggle.textContent = (showSent ? 'Hide sent' : 'Show sent')
      + (sentN ? ' (' + sentN + ')' : '');
    sentToggle.addEventListener('click', (e) => { e.stopPropagation(); showSent = !showSent; kebabOpen = false; renderList(); });
    menu.appendChild(sentToggle);
    bar.appendChild(menu);
  }
  return bar;
}

function renderList() {
  const list = $('list-body');
  if (!list) return;
  const pending = pendingList();
  const dismissed = dismissedList();
  const ambiguous = ambiguousList();
  const sent = historyList();
  list.replaceChildren();
  list.appendChild(buildTopBar(pending.length, dismissed.length, sent.length));
  updateCount(pending.length);

  // Ambiguous rows need a manual look, so they always show, above pending —
  // never folded away behind a toggle the way sent/dismissed are.
  if (ambiguous.length) {
    list.appendChild(el('div', 'proposals-divider muted', 'Needs a manual check'));
    for (const p of ambiguous) list.appendChild(buildAmbiguousRow(p));
  }

  if (!pending.length && !ambiguous.length
    && !(showDismissed && dismissed.length) && !(showSent && sent.length)) {
    list.appendChild(el('p', 'list-empty', 'No proposals right now.'));
    return;
  }
  for (const p of pending) list.appendChild(buildRow(p, false));
  if (showDismissed && dismissed.length) {
    list.appendChild(el('div', 'proposals-divider muted', 'Dismissed'));
    for (const p of dismissed) list.appendChild(buildRow(p, true));
  }
  if (showSent && sent.length) {
    list.appendChild(el('div', 'proposals-divider muted', 'Sent directly'));
    for (const p of sent) list.appendChild(buildHistoryRow(p));
  }
}

// ---- detail (right, full) ----------------------------------------------------
function select(p, dismissed) {
  selectedId = p.eventId;
  const g = rowGesture(p, 'click');
  if (!dismissed && g.action === 'open') {
    openWithDraft(p.targetRoom, g.prefill);
  } else {
    setDetailMode('proposal');
    renderDetail(p, dismissed);
  }
  for (const r of document.querySelectorAll('.proposal-row')) {
    r.classList.toggle('active', r.dataset.proposalId === p.eventId);
  }
}

function renderDetail(p, dismissed) {
  const host = $('proposal-detail-body');
  if (!host) return;
  host.replaceChildren();
  const title = $('proposal-detail-title');
  if (title) title.textContent = targetName(p);

  const to = el('div', 'proposal-to');
  to.appendChild(buildPlatBadge(sourceOf(p)));
  to.appendChild(el('span', 'proposal-to-name', targetName(p)));
  if (p.kind === 'identifier') to.appendChild(el('span', 'muted', ' — starts a NEW iMessage chat'));
  host.appendChild(to);
  host.appendChild(el('p', 'muted proposal-note',
    p.template ? 'Template suggestion — review before sending.' : 'Suggested by your manager — not sent yet.'));

  // Full-height, inline-editable message so the whole draft is visible.
  const ta = el('textarea', 'proposal-body-full');
  ta.value = p.body;
  host.appendChild(ta);

  const err = el('div', 'proposal-card-error hidden');
  host.appendChild(err);

  const actions = el('div', 'proposal-actions');
  if (dismissed) {
    const restore = el('button', 'proposal-btn primary', 'Restore');
    restore.type = 'button';
    restore.addEventListener('click', () => { unmarkHandled(p); select(p, false); renderList(); });
    actions.appendChild(restore);
  } else {
    const sendBtn = el('button', 'proposal-btn proposal-send primary', 'Send');
    sendBtn.type = 'button';
    sendBtn.addEventListener('click', () => sendProposal(p, ta.value, err, sendBtn));
    actions.appendChild(sendBtn);

    if (p.kind === 'room') {
      const openBtn = el('button', 'proposal-btn', 'Open conversation');
      openBtn.type = 'button';
      openBtn.addEventListener('click', () => openConvo(p.targetRoom));
      actions.appendChild(openBtn);
    }
    const rejectBtn = el('button', 'proposal-btn proposal-dismiss', 'Reject');
    rejectBtn.type = 'button';
    rejectBtn.addEventListener('click', () => { markHandled(p); afterHandled(p); });
    actions.appendChild(rejectBtn);
  }
  host.appendChild(actions);
}

function wireProposalBack() {
  const back = $('proposal-back');
  if (back && !back.dataset.wired) {
    back.dataset.wired = '1';
    back.addEventListener('click', () => { selectedId = null; setDetailMode('empty'); renderList(); });
  }
}

// ---- entry + refresh ---------------------------------------------------------
function renderProposalsView() {
  wireProposalBack();
  if (!selectedId) setDetailMode('empty');
  renderList();                       // instant paint from memory (empty-by-default)
  refresh().catch(() => {});          // background; never blocks first paint
}

async function refresh() {
  let proposals;
  try { proposals = await fetchProposals(); } catch (e) { return; }
  const sig = (arr) => arr.map((p) => p.eventId).sort().join(',');
  if (sig(proposals) === sig(allProposals)) return;
  allProposals = proposals;
  updateCount(pendingList().length);
  // If the open draft vanished (sent/removed elsewhere), clear the right pane.
  if (selectedId && !allProposals.some((p) => p.eventId === selectedId)) {
    selectedId = null;
    if (!S.openRoomId) setDetailMode('empty');
  }
  if (proposalsListShowing()) renderList();
}

let pollTimer = null;
function proposalsListShowing() {
  const btn = $('list-mode-proposals');
  return !!(btn && btn.classList.contains('active') && btn.offsetParent !== null);
}
function attachQuickActions(row, p) {
  const wrap = el('span', 'proposal-quick');
  const yes = el('button', 'proposal-quick-yes', '✓');
  yes.type = 'button';
  yes.title = 'Send';
  yes.setAttribute('aria-label', 'Send suggestion');
  const no = el('button', 'proposal-quick-no', '✕');
  no.type = 'button';
  no.title = 'Reject';
  no.setAttribute('aria-label', 'Reject suggestion');
  const stop = (e) => e.stopPropagation();
  yes.addEventListener('click', (e) => {
    stop(e);
    focusListAfterHandle = true;
    sendProposal(p, p.body, null, yes);
  });
  no.addEventListener('click', (e) => {
    stop(e);
    focusListAfterHandle = true;
    markHandled(p);
    afterHandled(p);
  });
  wrap.appendChild(yes);
  wrap.appendChild(no);
  wrap.addEventListener('click', stop);
  row.appendChild(wrap);

  row.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.target !== row) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    focusListAfterHandle = true;
    sendProposal(p, p.body, null, null);
  }, true);
}

function initProposalsUI() {
  setProposalsViewHook(renderProposalsView);
  setAfterSendHook((roomId) => {
    const p = pendingForRoom(allProposals, loadHandled(), roomId);
    if (!p) return;
    markHandled(p);
    afterHandled(p);
  });
  if (!pollTimer) {
    pollTimer = setInterval(() => { refresh().catch(() => {}); }, 10000);
  }
  refresh().catch(() => {});
}

export { initProposalsUI, renderProposalsView, parseProposal, partitionProposals, pendingForRoom, rowGesture };
