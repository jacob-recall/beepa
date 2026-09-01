// Conversation-number enrichment → auto-merge contacts by phone number.
//
// A trusted local loopback helper (same origin gate as the connect helpers in
// shared/ui/connections.js: custom X-Beepa-Connect header + application/json
// force a CORS preflight the helper only echoes for this app's origin) resolves
// each 1:1 conversation on THIS machine to a real number. We take only the
// phone results (email is single-bridge and must never drive a cross-bridge
// merge), validate every value, and group same-number conversations into one
// contact profile via the pure, idempotent shared model helper.
//
// SAFETY: this only GROUPS contacts — it never sends anything, and every
// auto-created profile is share:'inherit', so nothing new becomes visible to
// the manager. The endpoint's output is never trusted blindly: each value is
// checked against the E.164 shape AND each room id must be one the user is
// actually in (intersection with the live convo model), so a stale or foreign
// room id from the helper can never be injected into a profile.

import { readProfiles, writeProfiles, autoMergeByNumber } from '../../shared/model/contacts.js';
import { convosBySource, feedModel } from '../../shared/state.js';
import { SOURCES, PHONE_RE } from '../../shared/ui/sources.js';
import { sanitizeLine } from '../../shared/ui/el.js';

// Same loopback helper + guarded headers the connect flows use. The helper may
// have fallen back from 8021 to 8022-8025 (if the default was taken); it
// publishes its chosen base in apps/user/connect.local.json (same-origin).
// Discover + cache it; fall back to the default so a missing file is harmless.
const SESSION_CONNECT_HEADERS = { 'Content-Type': 'application/json', 'X-Beepa-Connect': '1' };
let _sessionConnectBase = null;
async function sessionConnectBase() {
  if (_sessionConnectBase) return _sessionConnectBase;
  try {
    const r = await fetch('connect.local.json', { cache: 'no-store' });
    if (r.ok) {
      const j = await r.json();
      if (j && typeof j.base === 'string' && /^http:\/\/127\.0\.0\.1:\d+$/.test(j.base)) {
        _sessionConnectBase = j.base; return _sessionConnectBase;
      }
    }
  } catch (e) { /* fall back to the default below */ }
  _sessionConnectBase = 'http://127.0.0.1:8021';
  return _sessionConnectBase;
}

let mergeDone = false;        // becomes true after the ONE real pass (rooms present)
let mergeAttempts = 0;        // retry budget while the feed is still seeding

// The rooms the user is actually in, from the live model (union of the
// per-source convo lists and the Home feed model), plus their display names.
function liveRooms() {
  const known = new Set();
  const nameByRoom = new Map();
  for (const s of SOURCES) {
    if (s.kind === 'all') continue;
    for (const c of (convosBySource[s.id] || [])) {
      if (!c || typeof c.id !== 'string') continue;
      known.add(c.id);
      if (c.title) nameByRoom.set(c.id, c.title);
    }
  }
  for (const rid of feedModel.keys()) known.add(rid);
  return { known, nameByRoom };
}

// One-shot: fetch resolved numbers, build a validated roomId->E.164 map limited
// to rooms the user is in, auto-merge, and persist only if something changed.
// Fail-soft throughout: an unreachable/erroring helper does nothing, no error UI.
async function autoMergeContacts() {
  if (mergeDone) return;
  try {
    // The feed may not be seeded yet at app init. Do NOT consume the one pass on
    // an empty model — retry every 5s (up to ~1 min) until rooms are present, so
    // the merge can't silently miss the whole session.
    const { known, nameByRoom } = liveRooms();
    if (!known.size) {
      if (mergeAttempts++ < 12) setTimeout(() => { autoMergeContacts().catch(() => {}); }, 5000);
      return;
    }
    mergeDone = true;                                 // rooms present — this is our one real pass

    let numbers;
    try {
      const r = await fetch((await sessionConnectBase()) + '/enrich/numbers',
        { method: 'POST', headers: SESSION_CONNECT_HEADERS, body: '{}' });
      if (!r.ok) return;                              // non-2xx -> do nothing
      const body = await r.json().catch(() => null);
      numbers = body && body.numbers;
    } catch (e) {
      return;                                         // helper unreachable -> do nothing
    }
    if (!numbers || typeof numbers !== 'object') return;

    // Build roomNumbers: phones only, E.164-validated, membership-intersected.
    const roomNumbers = {};
    for (const rid of Object.keys(numbers)) {
      if (!known.has(rid)) continue;                  // room-membership intersection
      const rec = numbers[rid];
      if (!rec || typeof rec !== 'object') continue;
      if (rec.kind !== 'phone') continue;             // phones only (email excluded)
      const val = typeof rec.value === 'string' ? rec.value : '';
      if (!PHONE_RE.test(val)) continue;               // validate before trusting
      roomNumbers[rid] = val;
    }
    if (!Object.keys(roomNumbers).length) return;

    const store = await readProfiles();
    const nameOf = (rid) => sanitizeLine(nameByRoom.get(rid) || '');
    const { profiles, changed } = autoMergeByNumber(store, roomNumbers, { nameOf });
    if (changed) await writeProfiles({ profiles });
  } catch (e) {
    /* fail soft — auto-merge never blocks or surfaces an error to the UI */
  }
}

export { autoMergeContacts };
