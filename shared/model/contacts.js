// PLAN-MASTER-SYNC §12 phase 5 — Unified contacts (the person-level unit).
// Shared ES module. Pure profile helpers + account-data storage. NO DOM.
//
// A ContactProfile links several conversations (across sources/rooms) to ONE
// person:
//     { id, displayName, roomIds: [...], share: 'share'|'private'|'inherit' }
// stored under user account-data `com.jkali.contact_profiles` as
//     { profiles: [ ContactProfile, ... ] }
//
// Invariants enforced in the helpers (never trusted from stored data):
//   * A conversation belongs to AT MOST ONE profile (first profile wins).
//   * `share` is one of share|private|inherit (default inherit).
//   * ids are unique non-empty strings; roomIds are valid + deduped.
// Linking is MANUAL only (link/unlink helpers). `suggestions()` only proposes
// candidates — it NEVER mutates and NEVER auto-merges.
//
// APPROVED EXCEPTION — `autoMergeByNumber()` (below) is the ONE sanctioned
// auto-merge path: it groups conversations that a trusted local helper has
// resolved to the SAME real phone number (E.164), so the same person on two
// bridges lands in one profile without manual curation. It is safe because it
// (1) only ever CREATES an auto-profile (share:'inherit', never 'share' — so
// auto-merge never shares anything new with the manager) or ADDS rooms to a
// single already-existing profile without touching that profile's share; and
// (2) NEVER merges two existing profiles — if a number's rooms already span two
// or more distinct profiles it skips the number entirely, so a deliberate
// user separation is never undone. It is pure and idempotent.

import { ROOMID_RE, api } from '../matrix/client.js';
import { S } from '../state.js';

const CONTACT_PROFILES_TYPE = 'com.jkali.contact_profiles'; // user account-data
const PROFILE_SHARE_STATES = new Set(['share', 'private', 'inherit']);

function normShare(s) {
  return PROFILE_SHARE_STATES.has(s) ? s : 'inherit';
}

// ===========================================================================
// NORMALIZATION — coerce stored/incoming data into a known-safe shape. A
// malformed account-data event can never smuggle a room into two profiles, an
// unknown share state, or a junk room id.
// ===========================================================================
function normalizeProfiles(data) {
  const raw = (data && Array.isArray(data.profiles)) ? data.profiles : [];
  const seenIds = new Set();
  const claimedRooms = new Set(); // enforce: at most one profile per room
  const claimedHandles = new Set(); // enforce: at most one profile per handle
  const profiles = [];
  for (const p of raw) {
    if (!p || typeof p !== 'object') continue;
    const id = typeof p.id === 'string' ? p.id : '';
    if (!id || seenIds.has(id)) continue;
    seenIds.add(id);
    const displayName = typeof p.displayName === 'string' ? p.displayName : '';
    const roomIds = [];
    const rawRooms = Array.isArray(p.roomIds) ? p.roomIds : [];
    for (const rid of rawRooms) {
      if (typeof rid !== 'string' || !ROOMID_RE.test(rid)) continue;
      if (claimedRooms.has(rid)) continue;
      claimedRooms.add(rid);
      roomIds.push(rid);
    }
    const handleIds = [];
    const rawHandles = Array.isArray(p.handleIds) ? p.handleIds : [];
    for (const h of rawHandles) {
      if (!h || typeof h !== 'object') continue;
      const source = typeof h.source === 'string' ? h.source : '';
      const network_id = typeof h.network_id === 'string' ? h.network_id : '';
      if (!source || !network_id) continue;
      const key = source + '|' + network_id;
      if (claimedHandles.has(key)) continue;
      claimedHandles.add(key);
      handleIds.push({ source, network_id });
    }
    profiles.push({ id, displayName, roomIds, handleIds, share: normShare(p.share) });
  }
  return { profiles };
}

function emptyProfiles() {
  return { profiles: [] };
}

// ===========================================================================
// PURE LOOKUP HELPERS
// ===========================================================================

function findProfile(store, id) {
  return normalizeProfiles(store).profiles.find((p) => p.id === id) || null;
}

// The profile object that contains roomId, or null.
function profileForRoom(store, roomId) {
  for (const p of normalizeProfiles(store).profiles) {
    if (p.roomIds.indexOf(roomId) !== -1) return p;
  }
  return null;
}

// { displayName, share } for a room's profile, or null. This is exactly what
// the consent resolver consumes as its `profile` argument.
function profileShareForRoom(store, roomId) {
  const p = profileForRoom(store, roomId);
  return p ? { displayName: p.displayName, share: p.share } : null;
}

// roomId -> { id, displayName, share } for every claimed room (drives the
// uplink's per-room profile decisions + the master stamp).
function roomProfileMap(store) {
  const out = {};
  for (const p of normalizeProfiles(store).profiles) {
    for (const rid of p.roomIds) {
      out[rid] = { id: p.id, displayName: p.displayName, share: p.share };
    }
  }
  return out;
}

// ===========================================================================
// PURE MUTATION HELPERS — each returns a NEW normalized store; never mutates
// its input. Manual curation only.
// ===========================================================================

// Create or update a profile's metadata (displayName/share). Requires an id
// (the caller mints it, e.g. newProfileId()). Existing roomIds are preserved.
function upsertProfile(store, { id, displayName, share } = {}) {
  const norm = normalizeProfiles(store);
  if (typeof id !== 'string' || !id) return norm;
  const existing = norm.profiles.find((p) => p.id === id);
  if (existing) {
    if (typeof displayName === 'string') existing.displayName = displayName;
    if (share !== undefined) existing.share = normShare(share);
  } else {
    norm.profiles.push({
      id,
      displayName: typeof displayName === 'string' ? displayName : '',
      roomIds: [],
      share: normShare(share),
    });
  }
  return norm;
}

function removeProfile(store, id) {
  const norm = normalizeProfiles(store);
  return { profiles: norm.profiles.filter((p) => p.id !== id) };
}

// Attach roomId to profile `id`, first detaching it from any other profile so
// the at-most-one invariant holds. No-op if the target profile is absent.
function linkRoom(store, id, roomId) {
  const norm = normalizeProfiles(store);
  const target = norm.profiles.find((p) => p.id === id);
  if (!target || typeof roomId !== 'string' || !ROOMID_RE.test(roomId)) return norm;
  for (const p of norm.profiles) {
    p.roomIds = p.roomIds.filter((r) => r !== roomId);
  }
  target.roomIds.push(roomId);
  return norm;
}

// Detach roomId from whatever profile currently holds it.
function unlinkRoom(store, roomId) {
  const norm = normalizeProfiles(store);
  for (const p of norm.profiles) {
    p.roomIds = p.roomIds.filter((r) => r !== roomId);
  }
  return norm;
}

function setProfileShare(store, id, share) {
  return upsertProfile(store, { id, share });
}

// ===========================================================================
// HANDLE OWNERSHIP — the cross-platform identity anchor. person_id =
// ContactProfile.id. A handle (source + network_id) belongs to AT MOST ONE
// profile; the helpers below uphold that just like the room helpers do.
// They accept the profiles ARRAY (as normalizeProfiles returns) and re-
// normalize, so a malformed stored profile can never smuggle a handle into
// two profiles.
// ===========================================================================

// The id of the profile currently owning (source, network_id), or null.
function handleOwner(profiles, source, network_id) {
  for (const p of normalizeProfiles({ profiles }).profiles) {
    for (const h of p.handleIds) {
      if (h.source === source && h.network_id === network_id) return p.id;
    }
  }
  return null;
}

// Attach a handle to profile `profileId`, first detaching it from any other
// profile so the at-most-one invariant holds. No-op if the target is absent
// or source/network_id is not a non-empty string.
function linkHandle(profiles, profileId, source, network_id) {
  const norm = normalizeProfiles({ profiles });
  const target = norm.profiles.find((p) => p.id === profileId);
  if (!target || typeof source !== 'string' || !source ||
      typeof network_id !== 'string' || !network_id) return norm;
  for (const p of norm.profiles) {
    p.handleIds = p.handleIds.filter(
      (h) => !(h.source === source && h.network_id === network_id));
  }
  target.handleIds.push({ source, network_id });
  return norm;
}

// Detach a handle from whatever profile currently holds it.
function unlinkHandle(profiles, source, network_id) {
  const norm = normalizeProfiles({ profiles });
  for (const p of norm.profiles) {
    p.handleIds = p.handleIds.filter(
      (h) => !(h.source === source && h.network_id === network_id));
  }
  return norm;
}

// A random profile id (opaque). Not pure; used only by the create-UI.
function newProfileId() {
  return 'cp_' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
}

// ===========================================================================
// SUGGESTIONS — proposes candidate merges by shared displayName/handle. PURE,
// read-only, and advisory: it returns groups the user MAY choose to link; it
// never links anything itself.
// ===========================================================================
function suggestions(convos, store) {
  const norm = normalizeProfiles(store);
  const roomToProfile = {};
  for (const p of norm.profiles) {
    for (const r of p.roomIds) roomToProfile[r] = p.id;
  }
  const keyOf = (c) => {
    const k = (c && (c.handle || c.displayName || c.name)) || '';
    return String(k).trim().toLowerCase();
  };
  const groups = new Map();
  for (const c of (Array.isArray(convos) ? convos : [])) {
    const k = keyOf(c);
    if (!k) continue;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(c);
  }
  const out = [];
  for (const [k, cs] of groups) {
    if (cs.length < 2) continue;
    // Skip if every candidate already sits in the same single profile.
    const profs = new Set(cs.map((c) => roomToProfile[c && c.id]));
    if (profs.size === 1 && !profs.has(undefined)) continue;
    out.push({ key: k, convos: cs });
  }
  return out;
}

// ===========================================================================
// AUTO-MERGE BY PHONE NUMBER — the one approved auto-merge (see the module
// header). Pure; no I/O, no DOM. `roomNumbers` is { roomId: '<E.164>' } — PHONES
// ONLY (the caller filters to kind==='phone'; email is single-bridge and must
// never drive a cross-bridge merge). `opts.nameOf(roomId) -> string` optionally
// seeds a freshly-created auto-profile's displayName.
//
// Rules (SAFETY-CRITICAL):
//   * Group roomIds by their shared E.164; only numbers with >=2 rooms matter.
//   * For each such group, look at which existing profiles already hold any of
//     its rooms (per normalizeProfiles):
//       - NONE  -> create ONE new auto-profile (newProfileId(), share:'inherit'
//                  NEVER 'share', displayName from nameOf(firstRoom) or '') and
//                  link every room in the group into it.
//       - EXACTLY ONE -> link the group's other (ungrouped) rooms into THAT
//                  profile; never change that profile's share.
//       - TWO OR MORE distinct profiles -> SKIP the group entirely (never merge
//                  existing profiles; a user may have split them on purpose).
//   * IDEMPOTENT: a group whose rooms already all sit in one profile is a no-op,
//     so a second run yields changed:false.
// Returns the new normalized { profiles } plus a boolean `changed`.
// ===========================================================================
function autoMergeByNumber(store, roomNumbers, opts = {}) {
  const nameOf = typeof opts.nameOf === 'function' ? opts.nameOf : null;
  let work = normalizeProfiles(store);

  // room -> owning profile id, from the ORIGINAL state. Groups are disjoint
  // (each room maps to exactly one number), so this snapshot stays valid across
  // groups; we update it in place as we link so idempotency holds within a run.
  const roomToProfile = {};
  for (const p of work.profiles) {
    for (const rid of p.roomIds) roomToProfile[rid] = p.id;
  }

  // Group rooms by number, first-seen order, validating both sides. A malformed
  // room id or number is dropped here — never trusted into a link.
  const rn = (roomNumbers && typeof roomNumbers === 'object') ? roomNumbers : {};
  const groups = new Map(); // e164 -> [roomId, ...]
  for (const rid of Object.keys(rn)) {
    if (typeof rid !== 'string' || !ROOMID_RE.test(rid)) continue;
    const num = rn[rid];
    if (typeof num !== 'string' || !num) continue;
    if (!groups.has(num)) groups.set(num, []);
    const arr = groups.get(num);
    if (arr.indexOf(rid) === -1) arr.push(rid);
  }

  let changed = false;
  for (const [, rooms] of groups) {
    if (rooms.length < 2) continue;                 // a lone room can't merge
    const profs = new Set();
    for (const rid of rooms) {
      const pid = roomToProfile[rid];
      if (pid) profs.add(pid);
    }
    if (profs.size >= 2) continue;                  // spans distinct profiles -> never merge
    if (profs.size === 1) {
      const targetId = profs.values().next().value;
      for (const rid of rooms) {                    // pull the ungrouped rooms into it
        if (!roomToProfile[rid]) {
          work = linkRoom(work, targetId, rid);
          roomToProfile[rid] = targetId;
          changed = true;
        }
      }
    } else {                                        // none in a profile -> new auto-profile
      const id = newProfileId();
      const displayName = (nameOf && String(nameOf(rooms[0]) || '')) || '';
      work = upsertProfile(work, { id, displayName, share: 'inherit' });
      for (const rid of rooms) {
        work = linkRoom(work, id, rid);
        roomToProfile[rid] = id;
      }
      changed = true;
    }
  }
  return { profiles: work.profiles, changed };
}

// ===========================================================================
// STORAGE HELPERS — read/write com.jkali.contact_profiles on the LOCAL
// homeserver as the user's own account. Absent -> empty (never an error).
// ===========================================================================
function profilesPath() {
  return '/_matrix/client/v3/user/' + encodeURIComponent(S.userId) +
    '/account_data/' + CONTACT_PROFILES_TYPE;
}

async function readProfiles() {
  try {
    return normalizeProfiles(await api('GET', profilesPath()));
  } catch (e) {
    return emptyProfiles();
  }
}

async function writeProfiles(store) {
  const body = normalizeProfiles(store);
  await api('PUT', profilesPath(), body);
  return body;
}

export {
  CONTACT_PROFILES_TYPE, PROFILE_SHARE_STATES,
  normalizeProfiles, emptyProfiles,
  findProfile, profileForRoom, profileShareForRoom, roomProfileMap,
  upsertProfile, removeProfile, linkRoom, unlinkRoom, setProfileShare, newProfileId,
  handleOwner, linkHandle, unlinkHandle,
  suggestions, autoMergeByNumber,
  readProfiles, writeProfiles,
};
