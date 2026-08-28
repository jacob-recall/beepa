// Relocated verbatim from hub/site/app.js (PLAN-MASTER-SYNC-IMPL P1.2).
// Shared ES module. Logic unchanged; only import/export + shared-state (S) access added.

import { ROOMID_RE, api } from '../matrix/client.js';
import { $, el, sanitizeLine, txn } from './el.js';
import { openConversation, setActiveNav, showSection, setDetailMode } from './nav.js';
import { renderMessageEvent } from './render.js';
import { buildPlatBadge, setActiveConvoRow } from './rows.js';
import { S, convoSeen, feedModel, runtime } from '../state.js';

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

// Open the native conversation view for a validated room (CV-6): same
// ROOMID_RE ∩ S.joinedSet gate openConversation uses. Loads recent history via
// /messages, renders through the shared renderer, then starts the room-scoped
// live watch.
async function openConvo(roomId) {
  if (!ROOMID_RE.test(roomId) || !S.joinedSet.has(roomId)) return;  // reject unvalidated ids
  stopConvoWatch();                                 // stop any prior room's watch first
  S.openRoomId = roomId;
  convoSeen.clear();
  convoSetStatus('');

  const rec = feedModel.get(roomId);
  const titleEl = $('convo-title');
  if (titleEl) titleEl.textContent = sanitizeLine((rec && rec.name) || roomId);
  const badge = $('convo-badge');
  if (badge) {
    const b = buildPlatBadge(rec && rec.sourceId); // derived from record sourceId only
    badge.className = b.className;
    badge.textContent = b.textContent;
  }
  // "Open in Element" reuses the exact validated openConversation path (CV-E1).
  const link = $('convo-element-link');
  if (link) link.onclick = (e) => { e.preventDefault(); openConversation(roomId); };

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

  try {
    const q = '/_matrix/client/v3/rooms/' + encodeURIComponent(roomId) + '/messages?dir=b&limit=50';
    const data = await api('GET', q);
    if (S.openRoomId === roomId) {                    // guard: user may have switched rooms mid-fetch
      const chunk = Array.isArray(data.chunk) ? data.chunk.slice().reverse() : [];  // b -> chronological
      for (const ev of chunk) renderMessageEvent(ev);
      if (box) box.scrollTop = box.scrollHeight;
    }
  } catch (e) {
    convoSetStatus('Could not load messages: ' + String(e.message || e));  // CV-R3: status, not a bubble
  }
  startConvoWatch(roomId);
}

// CV-I1 / CV-4: a THIRD independent long-poll, server-filtered to the open room
// only, plus a client guard that drops anything whose room != S.openRoomId. It
// appends ONLY to #convo-messages (via renderMessageEvent) and references none
// of the command/console symbols. 25s long-poll timeout + 3s error backoff.
async function startConvoWatch(roomId) {
  if (S.convoRunning) return;
  S.convoRunning = true;
  S.convoSince = null;
  const watchRoom = roomId;
  while (S.convoRunning && S.token && S.openRoomId === watchRoom) {
    try {
      const filter = encodeURIComponent(JSON.stringify({
        room: { rooms: [watchRoom], timeline: { limit: 20 }, state: { types: [] } },
        presence: { types: [] }, account_data: { types: [] },
      }));
      const q = '/_matrix/client/v3/sync?timeout=25000&filter=' + filter +
        (S.convoSince ? '&since=' + encodeURIComponent(S.convoSince) : '');
      const data = await api('GET', q);
      S.convoSince = data.next_batch;
      const join = (data.rooms && data.rooms.join) || {};
      const room = join[watchRoom];                 // read ONLY the open room's timeline
      if (room && room.timeline && Array.isArray(room.timeline.events) && S.openRoomId === watchRoom) {
        for (const ev of room.timeline.events) {
          if (S.openRoomId !== watchRoom) break;      // client guard: drop if the room changed
          renderMessageEvent(ev);
        }
        const box = $('convo-messages');
        if (box) box.scrollTop = box.scrollHeight;
      }
    } catch (e) {
      if (!S.token) { S.convoRunning = false; return; }
      await new Promise(r => setTimeout(r, 3000));
    }
  }
}
function stopConvoWatch() {
  S.convoRunning = false;
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
    return true;
  } catch (e) {
    convoSetStatus('Message failed to send: ' + String(e.message || e));  // CV-R3: status, not a bubble
    return false;
  }
}

export { convoSetStatus, openConvo, startConvoWatch, stopConvoWatch, sendConvoMessage };
