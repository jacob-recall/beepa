// Relocated verbatim from hub/site/app.js (PLAN-MASTER-SYNC-IMPL P1.2).
// Shared ES module. Logic unchanged; only import/export + shared-state (S) access added.

import { ROOMID_RE, api } from '../matrix/client.js';
import { $, el, sanitizeLine, txn } from './el.js';
import { setActiveNav, showSection, setDetailMode } from './nav.js';
import { convoResolveContent, renderMessageEvent } from './render.js';
import { buildPlatBadge, setActiveConvoRow } from './rows.js';
import { S, convoSeen, feedModel, runtime } from '../state.js';

// Module-local per-conversation message cache (LRU by room, module-local like
// convoSeen): roomId -> chronological array of raw, renderable message
// EVENTS — the exact objects handed to renderMessageEvent, never a rendered
// or derived form, so every render still goes through the render whitelist +
// from_me anti-spoof gate in render.js. Lets openConvo paint instantly from
// memory instead of always blocking on a fresh /messages fetch. Bounded so it
// cannot grow unboundedly across a long session: at most CACHE_MAX_EVENTS
// events kept per room, and only the CACHE_MAX_ROOMS most recently touched
// rooms are kept at all (oldest evicted first).
const convoCache = new Map();
const CACHE_MAX_ROOMS = 20;
const CACHE_MAX_EVENTS = 60;
let cacheSession = null;
let convoEpoch = 0;
let activeWatch = null;

// Only cache events that actually resolve to a renderable message (mirrors
// what renderMessageEvent would keep) — no point spending cache slots on
// reactions/redactions/state events that render nothing.
function isCacheable(ev) {
  try { return !!convoResolveContent(ev); } catch (e) { return false; }
}

// Read + LRU-touch (moves roomId to most-recently-used).
function cacheGet(roomId) {
  const v = convoCache.get(roomId);
  if (v) { convoCache.delete(roomId); convoCache.set(roomId, v); }
  return v;
}

// Merge new events into a room's cache entry (deduped by event_id, existing
// order preserved, new ones appended), cap per-room size, then evict the
// least-recently-used room(s) if over the room cap.
function cacheAppend(roomId, events) {
  if (!Array.isArray(events) || !events.length) return;
  let arr = convoCache.get(roomId) || [];
  convoCache.delete(roomId);
  const seen = new Set();
  for (const e of arr) { if (e && typeof e.event_id === 'string') seen.add(e.event_id); }
  for (const ev of events) {
    const eid = ev && typeof ev.event_id === 'string' ? ev.event_id : null;
    if (eid) { if (seen.has(eid)) continue; seen.add(eid); }
    arr.push(ev);
  }
  if (arr.length > CACHE_MAX_EVENTS) arr = arr.slice(arr.length - CACHE_MAX_EVENTS);
  convoCache.set(roomId, arr);                      // re-insert => most-recently-used
  while (convoCache.size > CACHE_MAX_ROOMS) {
    const oldestKey = convoCache.keys().next().value;  // Map preserves insertion order
    if (oldestKey === undefined) break;
    convoCache.delete(oldestKey);
  }
}

// CV-R3: hub errors/status go to a SEPARATE, distinctly-styled region — NEVER a
// message bubble in #convo-messages (anti-phishing). Created lazily since the
// layout scaffold has no dedicated status node.
function convoSetStatus(text) {
  const pane = $('msgr-convo');                       // was #view-convo (removed in the two-pane merge)
  if (!pane) return;
  let s = $('convo-status');
  if (!s) {
    s = el('div', 'convo-status');
    s.id = 'convo-status';
    const compose = $('convo-compose');
    if (compose && compose.parentNode) compose.parentNode.insertBefore(s, compose);
    else pane.appendChild(s);
  }
  s.textContent = text || '';
  s.classList.toggle('hidden', !text);
}

// Open the native conversation view for a validated room (CV-6): the
// ROOMID_RE ∩ S.joinedSet gate re-applied at open time. Loads recent history via
// /messages, renders through the shared renderer, then starts the room-scoped
// live watch.
async function openConvo(roomId) {
  if (!ROOMID_RE.test(roomId) || !S.joinedSet.has(roomId)) return;  // reject unvalidated ids
  stopConvoWatch();                                 // stop any prior room's watch first
  const epoch = convoEpoch;
  const session = S.token;
  if (cacheSession !== session) { convoCache.clear(); cacheSession = session; }
  const current = () => epoch === convoEpoch && S.token === session && S.openRoomId === roomId;
  S.openRoomId = roomId;
  convoSeen.clear();
  convoSetStatus('');
  const input = $('convo-input');
  if (input) input.value = '';                      // never carry a draft into a different room

  const rec = feedModel.get(roomId);
  const titleEl = $('convo-title');
  if (titleEl) titleEl.textContent = sanitizeLine((rec && rec.name) || roomId);
  const badge = $('convo-badge');
  if (badge) {
    const b = buildPlatBadge(rec && rec.sourceId); // derived from record sourceId only
    badge.className = b.className;
    badge.textContent = b.textContent;
  }
  const box = $('convo-messages');
  if (box) box.replaceChildren();                   // #convo-messages holds ONLY bubbles (CV-R3)
  // Layout/nav only: reveal the Home messenger and open the right-hand chat pane.
  // Show the same Home section the Home nav shows, WITHOUT re-rendering the list
  // (which would drop the active highlight); then reveal #msgr-convo by removing
  // the no-selection class and mark the clicked row active.
  showSection('view-workspace');
  setActiveNav('home');
  setDetailMode('chat');
  const convoPane = $('msgr-convo');
  if (convoPane) convoPane.classList.remove('no-selection');
  setActiveConvoRow(roomId);

  // Perf: paint instantly from the module-local cache (if we have one for
  // this room) BEFORE the network fetch below, through the SAME
  // renderMessageEvent gate as everything else — no bypass of the render
  // whitelist / from_me check. convoSeen was just cleared above, so this is
  // the first pass through it for this open and every cached event renders.
  const cached = cacheGet(roomId);
  if (cached && cached.length) {
    for (const ev of cached) renderMessageEvent(ev);
    if (box) box.scrollTop = box.scrollHeight;
  }

  try {
    const q = '/_matrix/client/v3/rooms/' + encodeURIComponent(roomId) + '/messages?dir=b&limit=50';
    const data = await api('GET', q);
    if (!current()) return;
    const chunk = Array.isArray(data.chunk) ? data.chunk.slice().reverse() : [];  // b -> chronological
    cacheAppend(roomId, chunk.filter(isCacheable));   // keep the cache warm regardless of the guard below
    if (S.openRoomId === roomId) {                    // guard: user may have switched rooms mid-fetch
      // renderMessageEvent dedups via convoSeen against whatever the cache
      // pass above already rendered, so this only paints what's new.
      for (const ev of chunk) renderMessageEvent(ev);
      if (box) box.scrollTop = box.scrollHeight;
    }
  } catch (e) {
    if (current()) convoSetStatus('Could not load messages: ' + String(e.message || e));
  }
  if (current()) startConvoWatch(roomId);
}

// CV-I1 / CV-4: a THIRD independent long-poll, server-filtered to the open room
// only, plus a client guard that drops anything whose room != S.openRoomId. It
// appends ONLY to #convo-messages (via renderMessageEvent) and references none
// of the command/console symbols. 25s long-poll timeout + 3s error backoff.
async function startConvoWatch(roomId) {
  if (!S.token || S.openRoomId !== roomId || activeWatch) return;
  const owner = { room: roomId, token: S.token, epoch: convoEpoch, since: null };
  activeWatch = owner;
  const current = () => activeWatch === owner && convoEpoch === owner.epoch &&
    S.token === owner.token && S.openRoomId === owner.room;
  S.convoRunning = true;
  S.convoSince = null;
  const watchRoom = roomId;
  try {
  while (current()) {
    try {
      const filter = encodeURIComponent(JSON.stringify({
        room: { rooms: [watchRoom], timeline: { limit: 20 }, state: { types: [] } },
        presence: { types: [] }, account_data: { types: [] },
      }));
      const q = '/_matrix/client/v3/sync?timeout=25000&filter=' + filter +
        (owner.since ? '&since=' + encodeURIComponent(owner.since) : '');
      const data = await api('GET', q);
      if (!current()) return;
      owner.since = data.next_batch;
      S.convoSince = owner.since;
      const join = (data.rooms && data.rooms.join) || {};
      const room = join[watchRoom];                 // read ONLY the open room's timeline
      if (room && room.timeline && Array.isArray(room.timeline.events) && S.openRoomId === watchRoom) {
        const toCache = [];
        for (const ev of room.timeline.events) {
          if (S.openRoomId !== watchRoom) break;      // client guard: drop if the room changed
          renderMessageEvent(ev);
          if (isCacheable(ev)) toCache.push(ev);
        }
        if (toCache.length) cacheAppend(watchRoom, toCache);  // keep the cache warm while the room is open
        const box = $('convo-messages');
        if (box) box.scrollTop = box.scrollHeight;
      }
    } catch (e) {
      if (!current()) return;
      await new Promise(r => setTimeout(r, 3000));
    }
  }
  } finally {
    if (activeWatch === owner) { activeWatch = null; S.convoRunning = false; }
  }
}
function stopConvoWatch() {
  convoEpoch += 1;
  activeWatch = null;
  S.convoRunning = false;
  S.convoSince = null;
  S.openRoomId = null;
}

// CV-S1 / CV-2: send text to a validated room, re-validating that room at send
// time (ROOMID_RE ∩ feedModel ∩ S.joinedSet, and never any management room).
// Fresh txn(); body length clamped. Optimistic echo deduped against the server
// echo by transaction_id. Returns true only when the server accepted the send.
//
// V2 proposal approve/send (PLAN §2 v2 / §7): the manager can PROPOSE a message
// but MUST NEVER cause an external send — only the teammate does, and only
// through THIS one guarded path. So an approved proposal calls the SAME function
// with an EXPLICIT (targetRoom, bodyOverride): the target then runs through the
// identical guard below. A passed target NEVER falls back to S.openRoomId, so an
// invalid or management-room target is rejected here, never silently redirected
// to whatever chat happens to be open. No second send path exists.
async function sendConvoMessage(targetRoom, bodyOverride) {
  const input = $('convo-input');
  if (!input) return false;
  const hasTarget = (typeof targetRoom === 'string' && !!targetRoom);
  const fromComposer = (typeof bodyOverride !== 'string');
  const raw = fromComposer ? (typeof input.value === 'string' ? input.value : '') : bodyOverride;
  const text = raw.trim();
  if (!text) return false;
  // A passed target is used verbatim; only the no-target case reads the open room.
  const roomId = hasTarget ? targetRoom : S.openRoomId;
  // CV-S1: no stale-room trust — re-validate the room at the moment of send.
  if (!roomId || !ROOMID_RE.test(roomId) || !feedModel.has(roomId) || !S.joinedSet.has(roomId)) {
    convoSetStatus('Cannot send: this conversation is not available.');
    return false;
  }
  // Defense-in-depth: never send into a bridge management room from this surface.
  if (roomId === runtime.whatsapp.mgmtRoomId ||
      roomId === runtime.imessage.mgmtRoomId ||
      roomId === runtime.gmessages.mgmtRoomId ||
      roomId === runtime.instagram.mgmtRoomId ||
      roomId === runtime.linkedin.mgmtRoomId ||
      roomId === runtime.twitter.mgmtRoomId) {
    convoSetStatus('Cannot send a message here.');
    return false;
  }
  const body = text.slice(0, 8000);                 // clamp length
  const t = txn();                                  // fresh random transaction id
  if (fromComposer) input.value = '';               // clear only the composer we read from
  convoSetStatus('');
  // Optimistic echo through the SAME shared renderer; deduped vs the server echo
  // by 'txn:'+t (renderMessageEvent). No event_id yet -> keyed by txn only.
  renderMessageEvent({
    type: 'm.room.message', sender: S.userId,
    content: { msgtype: 'm.text', body },
    origin_server_ts: Date.now(), unsigned: { transaction_id: t },
  });
  const box = $('convo-messages');
  if (box) box.scrollTop = box.scrollHeight;
  try {
    await api('PUT', '/_matrix/client/v3/rooms/' + encodeURIComponent(roomId) +
      '/send/m.room.message/' + encodeURIComponent(t), { msgtype: 'm.text', body });
    if (fromComposer && afterSendHook) {
      try { afterSendHook(roomId, body); } catch (e) { /* app hook must not break send */ }
    }
    return true;
  } catch (e) {
    convoSetStatus('Message failed to send: ' + String(e.message || e));  // CV-R3: status, not a bubble
    return false;
  }
}

// Optional app hook after a successful *composer* send (not an explicit-target
// send). apps/user uses this to mark a pending proposal handled once the
// teammate hits the normal Send/Enter path. Shared code never imports apps/.
let afterSendHook = null;
function setAfterSendHook(fn) { afterSendHook = typeof fn === 'function' ? fn : null; }

function prefillComposer(text) {
  const input = $('convo-input');
  if (!input) return;
  input.value = typeof text === 'string' ? text : '';
  input.focus();
}

export { convoSetStatus, openConvo, startConvoWatch, stopConvoWatch, sendConvoMessage, prefillComposer, setAfterSendHook };
