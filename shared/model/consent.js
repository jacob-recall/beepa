// PLAN-MASTER-SYNC §4 — the consent model (the authorization boundary).
// Shared ES module. Pure resolver + account-data storage helpers. NO DOM.
//
// FOUR layered levels, most-specific-wins (spec §4 + §12 phase 5 profile level):
//   1. per-conversation override  : 'share' | 'private'         (absent = inherit)
//   2. profile (contact profile)  : 'share' | 'private'         ('inherit' = fall through)
//   3. per-source policy          : 'share-all' | 'private-all' (absent = inherit)
//   4. global standing policy     : 'share-all' | 'private'     (default 'private')
// Anything not covered by a more-specific level falls through to the safe
// default: PRIVATE. Nothing is shared unless a level explicitly says so.
//
// The profile level lets a whole contact profile (one person's conversations
// across sources) share or hide together, while a per-conversation override
// still wins over it. The profile share-state lives in
// com.jkali.contact_profiles (see shared/model/contacts.js); the resolver here
// takes only the resolved { displayName, share } for the conversation's profile
// so it stays pure and byte-parity-portable to python.

import { ROOMID_RE, api } from '../matrix/client.js';
import { S } from '../state.js';

// Account-data event types (spec §5.2). Reuses the same account-data mechanism
// already used for m.tag, push rules, and com.jkali.self_identities.
const SHARE_POLICY_TYPE = 'com.jkali.share_policy';       // global user account-data
const SHARE_OVERRIDE_TYPE = 'com.jkali.share_override';   // per-room account-data

// ---- valid state tokens ------------------------------------------------------
const GLOBAL_STATES = new Set(['share-all', 'private']);
const SOURCE_STATES = new Set(['share-all', 'private-all', 'inherit']);
const OVERRIDE_STATES = new Set(['share', 'private']);
// Profile share-state (from a contact profile). 'inherit' means "no opinion —
// fall through to the per-source/global levels".
const PROFILE_STATES = new Set(['share', 'private', 'inherit']);

// A conversation's source label, for a human-readable "all <source>" reason.
// Falls back to the source id, then a generic token; never throws.
function sourceLabelOf(convo) {
  return (convo && (convo.sourceLabel || convo.sourceId)) || 'source';
}

// ===========================================================================
// PURE RESOLVER — the authorization decision. No I/O, no DOM.
// ===========================================================================

// Resolve one conversation's effective shared-state AND the reason for it.
//   convo    : { sourceId, sourceLabel, ... }
//   policy   : { global: 'share-all'|'private', sources: { <id>: state } }
//   override : 'share' | 'private' | undefined  (this room's per-conv override)
//   profile  : { displayName, share } | null/undefined  (the room's contact
//              profile, if any; share 'share'|'private'|'inherit')
// Returns { shared: boolean, reason: 'all <source>'|'explicit'|'excluded'
//           |'profile: <name>'|'private' }.
function resolve(convo, policy, override, profile) {
  const pol = policy || {};
  const sources = (pol.sources && typeof pol.sources === 'object') ? pol.sources : {};
  const sourceId = convo && convo.sourceId;

  // 1. Per-conversation override wins over everything (most specific).
  if (override === 'share') return { shared: true, reason: 'explicit' };
  if (override === 'private') return { shared: false, reason: 'excluded' };

  // 2. Profile level: a shared/private contact profile shares or hides all its
  //    members together, but only 'share'/'private' take effect — 'inherit'
  //    (or an absent profile) falls through to the source/global levels.
  if (profile) {
    const pname = 'profile: ' + (profile.displayName || 'profile');
    if (profile.share === 'share') return { shared: true, reason: pname };
    if (profile.share === 'private') return { shared: false, reason: pname };
  }

  // 3. Per-source standing policy.
  const src = sourceId ? sources[sourceId] : undefined;
  if (src === 'share-all') return { shared: true, reason: 'all ' + sourceLabelOf(convo) };
  if (src === 'private-all') return { shared: false, reason: 'private' };
  // (src === 'inherit' or absent -> fall through to global)

  // 4. Global standing policy: Share-All also covers conversations arriving
  //    later while it is on (spec §4.1).
  if (pol.global === 'share-all') return { shared: true, reason: 'all ' + sourceLabelOf(convo) };

  // 5. Safe default: private.
  return { shared: false, reason: 'private' };
}

// The boolean the uplink asks for when deciding whether to mirror a room.
function effectiveShared(convo, policy, override, profile) {
  return resolve(convo, policy, override, profile).shared;
}

// Resolve a whole list at once (drives the consent summary panel + row badges).
//   convos    : array of conversation objects (each with an `id` room id + sourceId)
//   policy    : the global/per-source policy object
//   overrides : per-room overrides keyed by room id — a Map or a plain object,
//               value 'share'|'private' (absent = inherit).
//   profiles  : per-room profile share info keyed by room id — a Map, a plain
//               object, or a function (id) => { displayName, share }; value
//               { displayName, share } (absent = no profile). Optional.
// Returns [{ convo, shared, reason }, ...] in input order.
function resolveAll(convos, policy, overrides, profiles) {
  const list = Array.isArray(convos) ? convos : [];
  const get = (id) => {
    if (!overrides) return undefined;
    if (typeof overrides.get === 'function') return overrides.get(id);
    return overrides[id];
  };
  const getProfile = (id) => {
    if (!profiles || id == null) return undefined;
    if (typeof profiles === 'function') return profiles(id);
    if (typeof profiles.get === 'function') return profiles.get(id);
    return profiles[id];
  };
  return list.map((convo) => {
    const id = convo && convo.id;
    const r = resolve(convo, policy, get(id), getProfile(id));
    return { convo, shared: r.shared, reason: r.reason };
  });
}

// ===========================================================================
// NORMALIZATION — coerce stored/incoming data into a known-safe shape so a
// malformed account-data event can never smuggle an unexpected state through.
// ===========================================================================

// A policy is always { global: 'share-all'|'private', sources: { <id>: state } }.
// Unknown global -> 'private' (safe default). Only recognized source states are
// kept; 'inherit' is dropped (absent == inherit), and anything unrecognized is
// discarded rather than trusted.
function normalizePolicy(p) {
  const src = (p && p.sources && typeof p.sources === 'object') ? p.sources : {};
  const global = (p && GLOBAL_STATES.has(p.global) && p.global === 'share-all') ? 'share-all' : 'private';
  const sources = {};
  for (const k of Object.keys(src)) {
    const v = src[k];
    if (v === 'share-all' || v === 'private-all') sources[k] = v; // drop 'inherit'/junk
  }
  return { global, sources };
}

// A per-room override is 'share' | 'private' | null (null == inherit). Accepts
// either the object form { state: 'share' } or a bare string, tolerating both.
function normalizeOverride(data) {
  if (!data) return null;
  const v = typeof data === 'string' ? data : data.state;
  return OVERRIDE_STATES.has(v) ? v : null;
}

// ===========================================================================
// STORAGE HELPERS — read/write the account-data events on the LOCAL homeserver
// as the user's own account (S.token). Not pure (they do I/O); everything above
// is. A 404 / absent event resolves to the safe default, never an error.
// ===========================================================================

function policyPath() {
  return '/_matrix/client/v3/user/' + encodeURIComponent(S.userId) +
    '/account_data/' + SHARE_POLICY_TYPE;
}
// The per-room override lives in ROOM account-data. Validate the room id with
// the shared ROOMID_RE before building the URL (same guard the rest of the
// codebase applies to room ids before path concatenation).
function overridePath(roomId) {
  if (!ROOMID_RE.test(roomId)) throw new Error('invalid room id');
  return '/_matrix/client/v3/user/' + encodeURIComponent(S.userId) +
    '/rooms/' + encodeURIComponent(roomId) + '/account_data/' + SHARE_OVERRIDE_TYPE;
}

// Read the global + per-source policy. Absent -> default { global:'private', sources:{} }.
async function readSharePolicy() {
  try {
    return normalizePolicy(await api('GET', policyPath()));
  } catch (e) {
    return { global: 'private', sources: {} };
  }
}

// Write the global + per-source policy (normalized before send). Returns what
// was written so callers can update local state without a re-read.
async function writeSharePolicy(policy) {
  const body = normalizePolicy(policy);
  await api('PUT', policyPath(), body);
  return body;
}

// Read one room's override. Absent -> null (inherit).
async function readShareOverride(roomId) {
  try {
    return normalizeOverride(await api('GET', overridePath(roomId)));
  } catch (e) {
    return null;
  }
}

// Write one room's override. state 'share'|'private' sets it; anything else
// (including null/'inherit') clears it back to inherit (an empty content event,
// since account-data cannot be deleted). Returns the normalized state or null.
async function writeShareOverride(roomId, state) {
  const s = OVERRIDE_STATES.has(state) ? state : null;
  await api('PUT', overridePath(roomId), s === null ? {} : { state: s });
  return s;
}

// Extract per-room overrides from a /sync response's room account-data blocks,
// so a caller can build the `overrides` map for resolveAll() without a GET per
// room. Reads only com.jkali.share_override; ignores everything else. Returns a
// plain object { <roomId>: 'share'|'private' } (rooms set to inherit omitted).
function overridesFromSync(syncData) {
  const out = {};
  const join = (syncData && syncData.rooms && syncData.rooms.join) || {};
  for (const rid of Object.keys(join)) {
    const ad = join[rid] && join[rid].account_data;
    for (const e of ((ad && ad.events) || [])) {
      if (e && e.type === SHARE_OVERRIDE_TYPE) {
        const v = normalizeOverride(e.content);
        if (v) out[rid] = v; else delete out[rid];
      }
    }
  }
  return out;
}

export {
  SHARE_POLICY_TYPE, SHARE_OVERRIDE_TYPE, PROFILE_STATES,
  resolve, effectiveShared, resolveAll,
  normalizePolicy, normalizeOverride,
  readSharePolicy, writeSharePolicy,
  readShareOverride, writeShareOverride,
  overridesFromSync,
};
