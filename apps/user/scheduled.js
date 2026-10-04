// Teammate self-schedule (roadmap F7). The browser's ONLY job here is to
// record an INTENT: a `com.jkali.scheduled_send` event in the teammate's own
// local proposals room (the room the uplink created and already polls). It
// never sends anything itself and it never fires a schedule — the daemon does
// that, behind its own gates (agents/uplink/CLAUDE.md, S-1..S-11). Cancelling
// is a STATE event whose state_key is the scheduled event's id, which is what
// makes the daemon's fire-time point-read a single, unambiguous question.
//
// CLASSIFICATION IS FROM EVENT CONTENT ONLY — `com.jkali.scheduled_send` +
// `com.jkali.scheduled_cancel` state + the daemon's `com.jkali.scheduled_outcome`
// records. Nothing here reads localStorage for state, so a fresh browser
// profile and a poisoned one classify identically.
//
// Everything that decides something is a pure function (`classifySchedules`,
// `scheduleRefusal`) so tests/unit/scheduled_send.test.js can hold it still
// without a DOM.

import { ROOMID_RE, HS, api } from '../../shared/matrix/client.js';
import { $, el, sanitize } from '../../shared/ui/el.js';
import { sendConvoMessage, convoSetStatus } from '../../shared/ui/chat.js';
import { S, feedModel, runtime } from '../../shared/state.js';

const SEND_TYPE = 'com.jkali.scheduled_send';
const CANCEL_TYPE = 'com.jkali.scheduled_cancel';
const OUTCOME_TYPE = 'com.jkali.scheduled_outcome';
const MIN_LEAD_MS = 60 * 1000;             // min now+1min
const MAX_LEAD_MS = 30 * 24 * 60 * 60 * 1000;  // max now+30d (= the daemon's S-5 horizon)
const SKEW_MAX_MS = 5 * 60 * 1000;         // UX guard only — see scheduleRefusal
const EVENT_ID_RE = /^\$[A-Za-z0-9._~:+/=-]{1,255}$/;

// ---- pure classification ----------------------------------------------------
// `events` is the raw /messages chunk of the proposals room(s). Returns one
// item per scheduled_send, newest first:
//   state 'scheduled' — armed, still waiting (no outcome, no cancel)
//   state 'held'      — the daemon could not fire it and says why (Send now / Cancel)
//   state 'sent' | 'refused' | 'ambiguous' | 'cancelled' — terminal
// A cancel STATE event and an outcome record both come from the room; the
// newest outcome wins over a cancel, because the daemon writes its outcome
// after resolving the cancel itself.
function classifySchedules(events) {
  const items = new Map();
  const cancelled = new Map();   // scheduled event id -> cancel ts
  const outcomes = new Map();    // scheduled event id -> newest outcome content
  for (const e of (Array.isArray(events) ? events : [])) {
    if (!e || !e.content || typeof e.type !== 'string') continue;
    const c = e.content;
    if (e.type === SEND_TYPE) {
      const id = e.event_id;
      const room = c.target_room;
      const body = c.body;
      const sendAt = c.send_at;
      if (typeof id !== 'string' || !id) continue;
      if (typeof room !== 'string' || !room) continue;
      if (typeof body !== 'string' || !body.trim()) continue;
      if (typeof sendAt !== 'number' || !isFinite(sendAt) || Math.floor(sendAt) !== sendAt) continue;
      items.set(id, {
        eventId: id, targetRoom: room, body, sendAt,
        ts: typeof e.origin_server_ts === 'number' ? e.origin_server_ts : 0,
        fromProposal: typeof c.from_proposal === 'string' ? c.from_proposal : '',
      });
    } else if (e.type === CANCEL_TYPE && typeof e.state_key === 'string' && e.state_key) {
      cancelled.set(e.state_key, typeof c.ts === 'number' ? c.ts : 0);
    } else if (e.type === OUTCOME_TYPE) {
      const id = c.scheduled_event_id;
      const state = c.state;
      if (typeof id !== 'string' || !id) continue;
      if (!['held', 'sent', 'refused', 'ambiguous', 'cancelled'].includes(state)) continue;
      const at = typeof c.outcome_ts === 'number' ? c.outcome_ts : 0;
      const prev = outcomes.get(id);
      if (!prev || at >= prev.at) outcomes.set(id, { at, state, reason: typeof c.reason === 'string' ? c.reason : '', sentEventId: typeof c.sent_event_id === 'string' ? c.sent_event_id : '' });
    }
  }
  const out = [];
  for (const it of items.values()) {
    const o = outcomes.get(it.eventId);
    if (o) out.push(Object.assign({}, it, { state: o.state, reason: o.reason, outcomeTs: o.at }));
    else if (cancelled.has(it.eventId)) out.push(Object.assign({}, it, { state: 'cancelled', reason: '', outcomeTs: cancelled.get(it.eventId) }));
    else out.push(Object.assign({}, it, { state: 'scheduled', reason: '', outcomeTs: 0 }));
  }
  return out.sort((a, b) => a.sendAt - b.sendAt);
}

// PURE write-time gate. Returns null when the schedule may be written, else a
// human-readable refusal. `record` is the target's feedModel entry (or null),
// `joined` a Set-like with .has, `mgmt` the set of bridge management room ids.
// `serverNow` may be null — the skew check is UX, NOT a security boundary: the
// daemon re-checks every one of these conditions at fire time, and its clock is
// the only one that decides anything.
function scheduleRefusal({ roomId, body, sendAt, now, serverNow, record, joined, mgmt, roomIdRe }) {
  const re = roomIdRe || ROOMID_RE;
  if (typeof roomId !== 'string' || !re.test(roomId)) return 'This conversation is not available.';
  if (!record || !record.sourceId) return 'Only a messaging conversation can be scheduled.';
  if (joined && typeof joined.has === 'function' && !joined.has(roomId)) return 'This conversation is not available.';
  if (mgmt && mgmt.has && mgmt.has(roomId)) return 'Cannot schedule a message here.';
  const text = sanitize(typeof body === 'string' ? body : '');
  if (!text.trim()) return 'Type a message before scheduling it.';
  // Same rule as the daemon's sanitize_send_body: a bridge command sigil must
  // never leave this app unattended.
  if (text.trimStart().startsWith('!')) return 'A scheduled message cannot start with "!".';
  if (typeof sendAt !== 'number' || !isFinite(sendAt) || Math.floor(sendAt) !== sendAt) return 'Pick a valid time.';
  if (sendAt < now + MIN_LEAD_MS) return 'Pick a time at least a minute from now.';
  if (sendAt > now + MAX_LEAD_MS) return 'Pick a time within the next 30 days.';
  if (typeof serverNow === 'number' && isFinite(serverNow) && Math.abs(now - serverNow) > SKEW_MAX_MS) {
    return 'This device’s clock is more than 5 minutes off the server’s. Fix the clock, then schedule.';
  }
  return null;
}

// ---- live state --------------------------------------------------------------
let scheduleRooms = [];        // discovered com.jkali.proposals rooms (proposals.js owns discovery)
let allSchedules = [];
let serverNowMs = null;        // last observed server clock, or null (unknown)
let afterChange = () => {};

function setScheduleRooms(ids) {
  scheduleRooms = (Array.isArray(ids) ? ids : []).filter(r => typeof r === 'string' && ROOMID_RE.test(r));
}
function setScheduleChangeHook(fn) { afterChange = typeof fn === 'function' ? fn : () => {}; }
function ingestScheduleEvents(events) { allSchedules = classifySchedules(events); }
function schedulesForRoom(roomId) {
  return allSchedules.filter(s => s.targetRoom === roomId);
}
// Rows with a schedule still waiting, written onto the feed records so the
// chat list can show the clock indicator (same shape as `drafts`).
function attachScheduled(feed) {
  const byRoom = new Map();
  for (const s of allSchedules) {
    if (s.state !== 'scheduled' && s.state !== 'held') continue;
    byRoom.set(s.targetRoom, (byRoom.get(s.targetRoom) || 0) + 1);
  }
  for (const rec of feed.values()) rec.scheduled = byRoom.get(rec.id) || 0;
  return byRoom.size;
}

function mgmtRoomIds() {
  const out = new Set();
  for (const k of Object.keys(runtime || {})) {
    const id = runtime[k] && runtime[k].mgmtRoomId;
    if (typeof id === 'string' && id) out.add(id);
  }
  return out;
}

// The server's clock, read from an ordinary response's Date header. api() does
// not expose headers, so this is a bare GET of the public versions endpoint
// (no auth, no body, same origin policy as every other call). When the header
// is missing or unparseable the skew check is simply skipped — it is UX, not a
// boundary, and the daemon's own clock decides when a schedule fires.
async function refreshServerClock() {
  try {
    const r = await fetch(HS + '/_matrix/client/versions', { cache: 'no-store' });
    const d = r.headers.get('Date');
    const t = d ? Date.parse(d) : NaN;
    serverNowMs = isFinite(t) ? t : null;
  } catch (e) { serverNowMs = null; }
  return serverNowMs;
}

// ---- writes ------------------------------------------------------------------
function targetRoomId() { return scheduleRooms[0] || null; }

async function writeSchedule(roomId, body, sendAt, fromProposal) {
  const dest = targetRoomId();
  if (!dest) return 'The scheduling channel is not ready yet.';
  const refusal = scheduleRefusal({
    roomId, body, sendAt, now: Date.now(), serverNow: serverNowMs,
    record: feedModel.get(roomId), joined: S.joinedSet, mgmt: mgmtRoomIds(),
  });
  if (refusal) return refusal;
  const content = {
    target_room: roomId,
    body: sanitize(body),              // the exact string the daemon will re-read
    send_at: sendAt,
    created_by: S.userId,              // cosmetic; the daemon trusts the server stamp
  };
  if (typeof fromProposal === 'string' && EVENT_ID_RE.test(fromProposal)) content.from_proposal = fromProposal;
  try {
    await api('PUT', '/_matrix/client/v3/rooms/' + encodeURIComponent(dest)
      + '/send/' + SEND_TYPE + '/' + encodeURIComponent('sched-' + Date.now() + '-' + Math.random().toString(36).slice(2)), content);
  } catch (e) { return 'Could not schedule: ' + String(e.message || e); }
  await refresh();
  return null;
}

async function cancelSchedule(eventId) {
  const dest = targetRoomId();
  if (!dest || typeof eventId !== 'string' || !EVENT_ID_RE.test(eventId)) return false;
  try {
    await api('PUT', '/_matrix/client/v3/rooms/' + encodeURIComponent(dest)
      + '/state/' + CANCEL_TYPE + '/' + encodeURIComponent(eventId), { ts: Date.now() });
  } catch (e) { return false; }
  await refresh();
  return true;
}

// "Send now" on a HELD schedule: the ordinary guarded send path with the body
// RE-READ from the classified event, then the schedule is cancelled so the
// daemon can never also fire it.
async function sendHeldNow(item) {
  if (!item) return false;
  const ok = await sendConvoMessage(item.targetRoom, sanitize(item.body));
  if (!ok) return false;
  await cancelSchedule(item.eventId);
  return true;
}

// ---- composer UI --------------------------------------------------------------
function schedulePane() {
  let pane = $('convo-schedule-pane');
  if (pane) return pane;
  const compose = $('convo-compose');
  if (!compose || !compose.parentNode) return null;
  pane = el('div', 'hidden');
  pane.id = 'convo-schedule-pane';
  const when = document.createElement('input');
  when.type = 'datetime-local';           // native picker, no library
  when.id = 'convo-schedule-when';
  const go = el('button', 'primary', 'Schedule'); go.type = 'button'; go.id = 'convo-schedule-go';
  const off = el('button', '', 'Cancel'); off.type = 'button'; off.id = 'convo-schedule-off';
  const err = el('div', ''); err.id = 'convo-schedule-error';
  pane.appendChild(el('span', '', 'Send at'));
  pane.appendChild(when); pane.appendChild(go); pane.appendChild(off); pane.appendChild(err);
  compose.parentNode.insertBefore(pane, compose);
  go.addEventListener('click', () => { confirmSchedule().catch(() => {}); });
  off.addEventListener('click', () => closeSchedulePicker());
  return pane;
}

function localInputValue(ms) {
  const d = new Date(ms);
  const pad = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
    + 'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
}

function openSchedulePicker(prefill) {
  const pane = schedulePane();
  if (!pane || !S.openRoomId) return;
  const when = $('convo-schedule-when');
  const now = Date.now();
  when.min = localInputValue(now + MIN_LEAD_MS);
  when.max = localInputValue(now + MAX_LEAD_MS);
  if (!when.value) when.value = localInputValue(now + 60 * 60 * 1000);
  pane.dataset.prefill = typeof prefill === 'string' ? prefill : '';
  pane.dataset.proposal = '';
  $('convo-schedule-error').textContent = '';
  pane.classList.remove('hidden');
  refreshServerClock().catch(() => {});
  when.focus();
}
function closeSchedulePicker() {
  const pane = $('convo-schedule-pane');
  if (pane) { pane.classList.add('hidden'); pane.dataset.prefill = ''; pane.dataset.proposal = ''; }
}

async function confirmSchedule() {
  const pane = $('convo-schedule-pane');
  const when = $('convo-schedule-when');
  const err = $('convo-schedule-error');
  if (!pane || !when) return;
  const input = $('convo-input');
  const body = pane.dataset.prefill || (input ? input.value : '');
  const t = when.value ? Date.parse(when.value) : NaN;   // datetime-local parses as LOCAL time
  const reason = await (async () => {
    await refreshServerClock();
    return writeSchedule(S.openRoomId, body, isFinite(t) ? t : NaN, pane.dataset.proposal || undefined);
  })();
  if (reason) { err.textContent = reason; return; }
  if (input && !pane.dataset.prefill) input.value = '';
  closeSchedulePicker();
  convoSetStatus('');
}

// Offer the manager's timed suggestion as the teammate's OWN schedule: from
// here on it is theirs, and the daemon treats it exactly like one they typed.
async function acceptProposalSchedule(draft) {
  if (!draft || !S.openRoomId) return;
  const pane = schedulePane();
  if (!pane) return;
  openSchedulePicker(sanitize(draft.body));
  pane.dataset.proposal = typeof draft.eventId === 'string' ? draft.eventId : '';
  const when = $('convo-schedule-when');
  if (when && typeof draft.sendAt === 'number' && draft.sendAt > Date.now() + MIN_LEAD_MS) {
    when.value = localInputValue(draft.sendAt);
  }
}

async function refresh() {
  const out = [];
  for (const rid of scheduleRooms) {
    try {
      const data = await api('GET', '/_matrix/client/v3/rooms/' + encodeURIComponent(rid) + '/messages?dir=b&limit=100');
      for (const e of (Array.isArray(data.chunk) ? data.chunk : [])) out.push(e);
    } catch (e) { /* keep what we had for this room */ }
  }
  ingestScheduleEvents(out);
  afterChange();
}

function initScheduleUI() {
  const btn = $('convo-schedule');
  if (btn && !btn.dataset.wired) {
    btn.dataset.wired = '1';
    btn.addEventListener('click', () => openSchedulePicker());
  }
  refreshServerClock().catch(() => {});
}

export {
  classifySchedules, scheduleRefusal, MIN_LEAD_MS, MAX_LEAD_MS, SKEW_MAX_MS,
  setScheduleRooms, setScheduleChangeHook, ingestScheduleEvents, schedulesForRoom,
  attachScheduled, writeSchedule, cancelSchedule, sendHeldNow, openSchedulePicker,
  acceptProposalSchedule, initScheduleUI,
};
