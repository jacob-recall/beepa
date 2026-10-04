// Teammate manager-drafts (Triage Rail). There is NO inbox: a room-targeted
// draft renders as a GHOST in the open conversation's composer (#convo-ghost),
// and the chat list carries a draft stripe/pill via each feed record's `drafts`.
// A draft is PENDING only while draftPending(origin_ts, room.lastTs) holds
// (shared/model/attention.js, fail closed) — any later message from anyone
// retires it visibly (struck through, Restore). Person-targeted (new-chat)
// drafts surface as a pseudo-row at the top of the Home list and keep the
// detail pane + confirm modal + gated start-chat leg.

import { ROOMID_RE, api } from '../../shared/matrix/client.js';
import { $, el, sanitize, sanitizeLine } from '../../shared/ui/el.js';
import { openConvo, sendConvoMessage, prefillComposer, setAfterSendHook, setComposerGhostHook, convoSetStatus } from '../../shared/ui/chat.js';
import { buildPlatBadge } from '../../shared/ui/rows.js';
import { sendCmd, validHandle } from '../../shared/ui/sources.js';
import { confirmModal } from '../../shared/ui/connections.js';
import { setDetailMode } from '../../shared/ui/nav.js';
import { feedRelTime, scheduleFeedRender, setFeedRenderHook } from '../../shared/ui/search.js';
import { pendingDrafts, retiredDrafts } from '../../shared/model/attention.js';
import { setScheduleRooms, setScheduleChangeHook, ingestScheduleEvents, schedulesForRoom,
         attachScheduled, cancelSchedule, sendHeldNow, acceptProposalSchedule, initScheduleUI } from './scheduled.js';
import { S, feedModel } from '../../shared/state.js';

const HANDLED_KEY = 'com.jkali.proposals_handled';
let allProposals = [];     // everything parsed (pending + dismissed + non-actionable)

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
  // F17: the manager controls content.origin_ts; a far-future value must never
  // keep a ghost pending, so never let it exceed the local server's stamp.
  const serverTs = typeof e.origin_server_ts === 'number' ? e.origin_server_ts : 0;
  const claimed = typeof c.origin_ts === 'number' ? c.origin_ts : 0;
  const ts = (serverTs && claimed) ? Math.min(serverTs, claimed) : (serverTs || claimed);
  const template = c.template === true;
  const autoSent = c['com.jkali.auto_sent'] === true;
  const sentEventId = (autoSent && typeof c.sent_event_id === 'string' && c.sent_event_id) ? c.sent_event_id : null;
  const ambiguous = !autoSent && c['com.jkali.send_ambiguous'] === true;

  // Cosmetic: who suggested it (the uplink pins created_by to the server-stamped
  // manager; the app shows only the localpart). Never an authorization input.
  const createdBy = typeof c.created_by === 'string' ? c.created_by : '';
  // F7 manager-TIMED suggestion: the instant the manager asked it to go out.
  // Cosmetic for a share-level room (the ghost shows the time and offers
  // "Accept schedule", which writes the teammate's OWN scheduled_send); for a
  // direct room the daemon holds it and runs the twelve gates at that instant.
  const sendAt = (typeof c.send_at === 'number' && isFinite(c.send_at) && Math.floor(c.send_at) === c.send_at) ? c.send_at : 0;
  const room = c.target_room;
  if (typeof room === 'string' && room) {
    return { kind: 'room', eventId: e.event_id, targetRoom: room, body, template, ts, autoSent, sentEventId, ambiguous, createdBy, sendAt };
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
  const raw = [];
  const rooms = await proposalsRooms();
  setScheduleRooms(rooms);          // F7: the same rooms carry the schedule events
  for (const rid of rooms) {
    const data = await api('GET', '/_matrix/client/v3/rooms/' + encodeURIComponent(rid) + '/messages?dir=b&limit=100');
    for (const e of (Array.isArray(data.chunk) ? data.chunk : [])) {
      raw.push(e);
      const p = parseProposal(e);
      if (p) out.push(p);
    }
  }
  ingestScheduleEvents(raw);        // classification is from event content only
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
  if (ok) { markHandled(p); setDetailMode('empty'); afterHandled(); return; }
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
  setDetailMode('empty');
  afterHandled();
}

// ---- feed-record drafts (the ghost's data) -----------------------------------
// PURE: write each room's unhandled, room-targeted, actionable proposals onto
// its feed record as `drafts` (newest first) and clear rooms with none. Whether
// a draft is PENDING or RETIRED is decided at render time by attention.js's
// draftPending(draft.ts, rec.lastTs), so a phone reply that bumps lastTs
// retires the ghost on the next render with no extra bookkeeping.
function attachDrafts(proposals, handled, feed) {
  const byRoom = new Map();
  for (const p of (Array.isArray(proposals) ? proposals : [])) {
    if (!p || p.kind !== 'room' || p.autoSent || p.ambiguous) continue;
    if (handled && typeof handled.has === 'function' && handled.has(p.eventId)) continue;
    if (!byRoom.has(p.targetRoom)) byRoom.set(p.targetRoom, []);
    byRoom.get(p.targetRoom).push({ eventId: p.eventId, body: p.body, ts: p.ts, template: p.template, createdBy: p.createdBy || '', sendAt: p.sendAt || 0 });
  }
  let rooms = 0;
  for (const rec of feed.values()) {
    const list = byRoom.get(rec.id);
    rec.drafts = list ? list.sort((a, b) => b.ts - a.ts) : [];
    if (rec.drafts.length) rooms++;
  }
  return rooms;
}
function identifierDrafts(proposals, handled) {
  return (Array.isArray(proposals) ? proposals : [])
    .filter(p => p && p.kind === 'identifier' && !p.autoSent && !p.ambiguous && !(handled && handled.has(p.eventId)))
    .sort((a, b) => b.ts - a.ts);
}

// ---- ghost cluster IN the timeline ----------------------------------------------
// Suggestions render as a dashed, right-aligned cluster at the END of
// #convo-messages — where the teammate's own reply would appear — one bubble
// per pending suggestion with its own Send, plus "Send all in order". The
// cluster is a plain container appended after the bubbles; it is NEVER
// produced by renderMessageEvent (so the render whitelist / from_me gate are
// untouched) and carries no event_id/messageTs, so the renderer's ordering
// and 200-bubble cap ignore it. Re-appended (moved to the end) after every
// tail render through composerGhostHook.
let ghostRoom = null;
let ghostExpanded = false;   // "+N more" toggle (per open room)
const GHOST_SHOW = 3;
let sendAllBusy = false;

function whoSuggested(d) {
  const mx = d && typeof d.createdBy === 'string' ? d.createdBy : '';
  const lp = mx.startsWith('@') ? mx.slice(1, mx.indexOf(':') > 0 ? mx.indexOf(':') : undefined) : '';
  return lp ? sanitizeLine(lp) : 'your manager';
}

function ghostBubble(d, idx, total, roomId) {
  // F14: display and send the SAME string. sanitize() keeps newlines, strips
  // bidi/zero-width/control chars and clamps at 4000.
  const shown = sanitize(d.body);
  const g = el('div', 'ghost pending');
  g.appendChild(el('div', 'ghost-text', shown));
  const bar = el('div', 'ghost-bar');
  bar.appendChild(el('span', 'ghost-st', total > 1 ? (idx + 1) + ' of ' + total : (d.template ? 'template' : whoSuggested(d))));
  const edit = el('button', 'ghost-link', 'Edit'); edit.type = 'button';
  edit.addEventListener('click', (e) => { e.stopPropagation(); prefillComposer(shown); markHandled({ eventId: d.eventId }); afterHandled(); });
  const dismiss = el('button', 'ghost-link', 'Dismiss'); dismiss.type = 'button';
  dismiss.addEventListener('click', (e) => { e.stopPropagation(); markHandled({ eventId: d.eventId }); afterHandled(); });
  const send = el('button', 'ghost-send', 'Send'); send.type = 'button';
  send.addEventListener('click', async (e) => {
    e.stopPropagation();
    send.disabled = true;
    // Explicit target, the one guarded send path; `shown` is exactly what was read.
    const ok = await sendConvoMessage(roomId, shown, { fromProposal: d.eventId });
    send.disabled = false;
    if (!ok) return;
    markHandled({ eventId: d.eventId });
    // F16: siblings are NOT marked handled — the sent message bumps lastTs and
    // draftPending retires them visibly (struck through, Restore) on render.
    afterHandled();
  });
  bar.appendChild(edit); bar.appendChild(dismiss);
  if (d.sendAt) {
    // F7: the manager asked for a time. Accepting makes it the TEAMMATE'S own
    // scheduled send from then on — a separate event they authored, which is
    // the only thing the daemon's teammate queue will ever fire.
    bar.appendChild(el('span', 'ghost-st', 'for ' + clockTime(d.sendAt)));
    const take = el('button', 'ghost-send', 'Accept schedule'); take.type = 'button';
    take.addEventListener('click', (e) => { e.stopPropagation(); acceptProposalSchedule(d); });
    bar.appendChild(take);
  }
  bar.appendChild(send);
  g.appendChild(bar);
  return g;
}

// Local wall-clock time for a scheduled instant (textContent only).
function clockTime(ms) {
  try { return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }
  catch (e) { return ''; }
}

// F7: the scheduled-send cluster, rendered in the SAME ghost container as the
// manager suggestions. State comes from scheduled.js's pure classification over
// event content — never from localStorage.
function scheduleGhosts(wrap, roomId) {
  for (const s of schedulesForRoom(roomId)) {
    if (s.state === 'cancelled') continue;
    if ((s.state === 'sent' || s.state === 'refused' || s.state === 'ambiguous')
        && Date.now() - (s.outcomeTs || 0) > 6 * 60 * 60 * 1000) continue;   // transient
    const g = el('div', 'ghost ' + (s.state === 'scheduled' ? 'scheduled' : s.state === 'held' ? 'held' : 'retired'));
    g.appendChild(el('div', 'ghost-text', sanitize(s.body)));
    const bar = el('div', 'ghost-bar');
    const label = {
      scheduled: 'Scheduled for ' + clockTime(s.sendAt),
      held: 'Held: ' + heldReason(s.reason),
      sent: 'sent at ' + clockTime(s.sendAt),
      refused: 'Not sent: ' + heldReason(s.reason),
      ambiguous: 'May already have been sent — check above before replying',
    }[s.state] || s.state;
    bar.appendChild(el('span', 'ghost-st', label));
    if (s.state === 'held') {
      const now = el('button', 'ghost-link', 'Send now'); now.type = 'button';
      now.addEventListener('click', async (e) => { e.stopPropagation(); now.disabled = true; await sendHeldNow(s); now.disabled = false; });
      bar.appendChild(now);
    }
    if (s.state === 'scheduled' || s.state === 'held') {
      const stop = el('button', 'ghost-link', 'Cancel'); stop.type = 'button';
      stop.addEventListener('click', (e) => { e.stopPropagation(); cancelSchedule(s.eventId); });
      bar.appendChild(stop);
    }
    g.appendChild(bar);
    wrap.appendChild(g);
  }
}

// The daemon's reason strings, in the teammate's words. An unknown reason is
// shown verbatim rather than hidden — a refusal nobody can read is a silent one.
function heldReason(reason) {
  return {
    superseded: 'the conversation moved on',
    late: 'the sync service was not running at that time',
    cap: 'too many automatic messages in this conversation this hour',
    sanitize: 'the message could not be sent as written',
    cancel_unreadable: 'the cancel state could not be read',
    target: 'that conversation is not a schedulable chat',
    horizon: 'the time was too far ahead',
    send_at: 'the time was not valid',
    interrupted: 'the sync service was interrupted mid-send',
    already_handled: 'it was already handled another way',
  }[reason] || (reason || 'unknown');
}

// Send every pending ghost in order (oldest first), one at a time, stopping on
// the first failure and saying which. Each send is the same guarded call.
async function sendAllInOrder(roomId, pending) {
  if (sendAllBusy) return;
  sendAllBusy = true;
  try {
    const ordered = pending.slice().sort((a, b) => a.ts - b.ts);
    for (let i = 0; i < ordered.length; i++) {
      const d = ordered[i];
      const ok = await sendConvoMessage(roomId, sanitize(d.body), { fromProposal: d.eventId });
      if (!ok) { convoSetStatus('Stopped at suggestion ' + (i + 1) + ' of ' + ordered.length + ' — it did not send.'); break; }
      markHandled({ eventId: d.eventId });
    }
  } finally { sendAllBusy = false; afterHandled(); }
}

function renderGhost(roomId) {
  const box = $('convo-messages');
  const legacy = $('convo-ghost');
  if (legacy) legacy.classList.add('hidden');
  if (!box) return;
  if (roomId !== ghostRoom) { ghostRoom = roomId; ghostExpanded = false; }
  const old = box.querySelector('.ghosts');
  if (old) old.remove();
  if (S.openRoomId !== roomId) return;
  const rec = feedModel.get(roomId);
  if (!rec) return;
  const pendingNewestFirst = pendingDrafts(rec.drafts, rec.lastTs);
  const pending = pendingNewestFirst.slice().sort((a, b) => a.ts - b.ts);     // oldest first = send order
  const retired = retiredDrafts(rec.drafts, rec.lastTs, Date.now());
  const ambiguous = allProposals.filter(p => p && p.kind === 'room' && p.ambiguous && p.targetRoom === roomId);
  const scheduled = schedulesForRoom(roomId).filter(s => s.state !== 'cancelled');
  if (!pending.length && !retired.length && !ambiguous.length && !scheduled.length) return;
  const wrap = el('div', 'ghosts');
  scheduleGhosts(wrap, roomId);
  for (const a of ambiguous.slice(0, 1)) {
    const g = el('div', 'ghost ambiguous');
    g.appendChild(el('div', 'ghost-text', sanitize(a.body)));
    g.appendChild(el('div', 'ghost-bar', 'May already have been sent (' + feedRelTime(a.ts) + ' ago) — check above before replying'));
    wrap.appendChild(g);
  }
  if (pending.length) {
    const cap = el('div', 'ghosts-cap');
    const newest = pendingNewestFirst[0];
    cap.appendChild(el('span', '', whoSuggested(newest) + ' suggested ' + (pending.length === 1 ? 'a reply' : pending.length + ' messages') + ' · ' + feedRelTime(newest.ts) + ' ago'));
    if (pending.length > 1) {
      const all = el('button', 'ghost-link', 'Send all in order'); all.type = 'button';
      all.addEventListener('click', (e) => { e.stopPropagation(); sendAllInOrder(roomId, pending); });
      const none = el('button', 'ghost-link', 'Dismiss all'); none.type = 'button';
      none.addEventListener('click', (e) => { e.stopPropagation(); for (const d of pending) markHandled({ eventId: d.eventId }); afterHandled(); });
      cap.appendChild(all); cap.appendChild(none);
    }
    wrap.appendChild(cap);
    const shown = (ghostExpanded || pending.length <= GHOST_SHOW) ? pending : pending.slice(pending.length - GHOST_SHOW);
    if (shown.length < pending.length) {
      const more = el('button', 'ghost-link ghost-more', '+' + (pending.length - shown.length) + ' more'); more.type = 'button';
      more.addEventListener('click', (e) => { e.stopPropagation(); ghostExpanded = true; renderGhost(roomId); });
      wrap.appendChild(more);
    }
    shown.forEach((d) => wrap.appendChild(ghostBubble(d, pending.indexOf(d), pending.length, roomId)));
  }
  for (const d of retired.slice(0, 2)) {
    const g = el('div', 'ghost retired');
    g.appendChild(el('div', 'ghost-text', sanitize(d.body)));
    const bar = el('div', 'ghost-bar');
    bar.appendChild(el('span', 'ghost-st', whoSuggested(d) + ' suggested ' + feedRelTime(d.ts) + ' ago · the thread moved on'));
    const restore = el('button', 'ghost-link', 'Restore'); restore.type = 'button';
    restore.addEventListener('click', (e) => { e.stopPropagation(); prefillComposer(sanitize(d.body)); markHandled({ eventId: d.eventId }); afterHandled(); });
    bar.appendChild(restore);
    g.appendChild(bar);
    wrap.appendChild(g);
  }
  box.appendChild(wrap);
  box.scrollTop = box.scrollHeight;
}

function afterHandled() {
  attachDrafts(allProposals, loadHandled(), feedModel);
  attachScheduled(feedModel);
  scheduleFeedRender();
  if (S.openRoomId) renderGhost(S.openRoomId);
  renderIdentifierRows();
}

// ---- person-targeted drafts: a row at the top of the chat list -----------------
// No inbox: a "start a new chat" suggestion has no room, so it surfaces as a
// pseudo-row above the list (draft stripe) that opens the existing detail pane.
function renderIdentifierRows() {
  const list = $('list-body');
  if (!list) return;
  for (const old of list.querySelectorAll('.convo.identifier-draft')) old.remove();
  if (S.activeNavKey !== 'home') return;
  const rows = identifierDrafts(allProposals, loadHandled());
  for (const p of rows.reverse()) {
    const row = el('div', 'convo identifier-draft');
    row.setAttribute('role', 'button'); row.tabIndex = 0;
    const stripe = el('div', 'stripe'); stripe.appendChild(el('i', 'seg draft')); row.appendChild(stripe);
    row.appendChild(buildPlatBadge(p.targetSource));
    const meta = el('div', 'meta');
    meta.appendChild(el('div', 'title', 'New chat suggested: ' + sanitizeLine(p.targetDisplay || p.targetIdentifier)));
    meta.appendChild(el('div', 'preview draft', 'Draft: ' + sanitizeLine(p.body)));
    row.appendChild(meta);
    const open = () => { setDetailMode('proposal'); renderDetail(p); };
    row.addEventListener('click', open);
    row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
    list.insertBefore(row, list.firstChild);
  }
}

// Detail pane for a person-targeted draft (unchanged send leg: confirm + sendCmd start-chat).
function renderDetail(p) {
  const host = $('proposal-detail-body');
  if (!host) return;
  host.replaceChildren();
  const title = $('proposal-detail-title');
  if (title) title.textContent = targetName(p);
  const to = el('div', 'proposal-to');
  to.appendChild(buildPlatBadge(sourceOf(p)));
  to.appendChild(el('span', 'proposal-to-name', targetName(p)));
  to.appendChild(el('span', 'muted', ' — starts a NEW iMessage chat'));
  host.appendChild(to);
  host.appendChild(el('p', 'muted proposal-note', 'Suggested by your manager — not sent yet.'));
  const ta = el('textarea', 'proposal-body-full'); ta.value = sanitize(p.body); host.appendChild(ta);
  const err = el('div', 'proposal-card-error hidden'); host.appendChild(err);
  const actions = el('div', 'proposal-actions');
  const sendBtn = el('button', 'proposal-btn proposal-send primary', 'Send'); sendBtn.type = 'button';
  sendBtn.addEventListener('click', () => sendProposal(p, ta.value, err, sendBtn));
  const rejectBtn = el('button', 'proposal-btn proposal-dismiss', 'Reject'); rejectBtn.type = 'button';
  rejectBtn.addEventListener('click', () => { markHandled(p); setDetailMode('empty'); afterHandled(); });
  actions.appendChild(sendBtn); actions.appendChild(rejectBtn);
  host.appendChild(actions);
  const back = $('proposal-back');
  if (back && !back.dataset.wired) { back.dataset.wired = '1'; back.addEventListener('click', () => setDetailMode('empty')); }
}

// ---- refresh ----------------------------------------------------------------------
async function refresh() {
  let proposals;
  try { proposals = await fetchProposals(); } catch (e) { return; }
  // fetchProposals also re-ingested the schedule events, so always re-run the
  // attach/render pass: a schedule change moves nothing in `proposals`.
  allProposals = proposals;
  afterHandled();
}
let pollTimer = null;

function initProposalsUI() {
  setComposerGhostHook(renderGhost);
  // F7: a schedule written/cancelled here re-attaches the row indicators and
  // repaints the open room's cluster, same as a draft change does.
  setScheduleChangeHook(() => {
    attachScheduled(feedModel);
    scheduleFeedRender();
    if (S.openRoomId) renderGhost(S.openRoomId);
  });
  try { initScheduleUI(); } catch (e) { /* the clock button stays unwired on error */ }
  setFeedRenderHook(() => { renderIdentifierRows(); if (S.openRoomId) renderGhost(S.openRoomId); });
  setAfterSendHook((roomId) => {
    // A reply typed by the teammate retires every pending draft for the room.
    const rec = feedModel.get(roomId);
    for (const d of pendingDrafts((rec && rec.drafts) || [], (rec && rec.lastTs) || 0)) markHandled({ eventId: d.eventId });
    afterHandled();
  });
  if (!pollTimer) pollTimer = setInterval(() => { refresh().catch(() => {}); }, 10000);
  refresh().catch(() => {});
}

export { initProposalsUI, parseProposal, partitionProposals, pendingForRoom, rowGesture, attachDrafts, identifierDrafts };
