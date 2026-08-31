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

// ---------------------------------------------------------------------------
// INPUT CANONICALISATION — identical gates in agents/uplink/consent.py (see
// docs/superpowers/plans/2026-08-30-consent-conformance.md's table; the
// conformance harness tests/conformance/consent_conformance.py proves parity
// on ~84k vectors every run). Anything failing a gate is treated as ABSENT
// (the fall-through value) — note "absent" is safe only relative to the
// more-specific levels: the per-source level carries a deny (private-all),
// so a dropped malformed rule can fall through to a global share-all; the
// curated tests pin that. All regexes use end-of-STRING anchoring (`^…$`
// here; re.fullmatch in Python — Python's `$` also matches before a trailing
// newline, which is why Python must never use .match with `$`).
// ---------------------------------------------------------------------------

// A per-source policy key / contact source id. Shape-based (a new bridge id
// needs no change here); excludes __proto__-style names. Never tighten it to
// something a real source id could fail — dropping a key drops a private-all.
const SOURCE_KEY_RE = /^[a-z][a-z0-9]{0,31}$/;
// Room-id shape for overridesFromSync output keys. Static and server-name
// agnostic ON PURPOSE — never swap in shared/matrix/client.js's ROOMID_RE,
// which configureMatrixBase() rebinds to a server name at runtime.
const CONSENT_ROOMID_RE = /^![^:]+:[A-Za-z0-9.\-:]+$/;

// The shared "plain object" gate (a dict in Python): object, not null, not an
// array. A Map is deliberately NOT plain here — container Map handling lives
// only in resolveAll's accessors.
function plainObject(x) {
  return (x !== null && typeof x === 'object' && !Array.isArray(x) && !(x instanceof Map)) ? x : null;
}

function nonEmptyString(x) {
  return (typeof x === 'string' && x) ? x : null;
}

// 'share-all' | 'private-all' | null for a per-source rule lookup — THE
// consent gate for the per-source level: plain-object container, valid source
// id, OWN property (hardens against prototype pollution and keys like
// 'constructor'), exactly-valid value. Anything else is inherit.
function sourceRule(sources, sourceId) {
  if (!plainObject(sources)) return null;
  if (typeof sourceId !== 'string' || !SOURCE_KEY_RE.test(sourceId)) return null;
  if (!Object.prototype.hasOwnProperty.call(sources, sourceId)) return null;
  const v = sources[sourceId];
  return (v === 'share-all' || v === 'private-all') ? v : null;
}

// A conversation's source label, for a human-readable "all <source>" reason.
// Only a non-empty STRING label counts; falls back to a non-empty-string
// source id, then a generic token; never throws, never coerces junk into the
// reason string.
function sourceLabelOf(convo) {
  const c = plainObject(convo) || {};
  return nonEmptyString(c.sourceLabel) || nonEmptyString(c.sourceId) || 'source';
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
  const pol = plainObject(policy) || {};
  const c = plainObject(convo) || {};
  const sourceId = nonEmptyString(c.sourceId);

  // 1. Per-conversation override wins over everything (most specific).
  //    Only the exact strings count; any other shape is inherit.
  if (override === 'share') return { shared: true, reason: 'explicit' };
  if (override === 'private') return { shared: false, reason: 'excluded' };

  // 2. Profile level: a shared/private contact profile shares or hides all its
  //    members together, but only 'share'/'private' take effect — 'inherit'
  //    (a non-object profile, or an absent one) falls through.
  const prof = plainObject(profile);
  if (prof) {
    const pname = 'profile: ' + (nonEmptyString(prof.displayName) || 'profile');
    if (prof.share === 'share') return { shared: true, reason: pname };
    if (prof.share === 'private') return { shared: false, reason: pname };
  }

  // 3. Per-source standing policy (gated: valid key, own entry, exact value).
  const src = sourceRule(pol.sources, sourceId);
  if (src === 'share-all') return { shared: true, reason: 'all ' + sourceLabelOf(convo) };
  if (src === 'private-all') return { shared: false, reason: 'private' };
  // (inherit / absent / malformed -> fall through to global)

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
  // Containers: a real Map (instanceof, never duck-typed — a JSON object with
  // an own "get" key is a plain object), a function (profiles only), or a
  // plain object with OWN-property lookup. Keys must be strings (Python gates
  // identically on its dict form; Map/function forms are JS-only and outside
  // the conformance harness's JSON domain — curated tests cover them).
  const get = (id) => {
    if (typeof id !== 'string') return undefined;
    if (overrides instanceof Map) return overrides.get(id);
    if (!plainObject(overrides)) return undefined;
    return Object.prototype.hasOwnProperty.call(overrides, id) ? overrides[id] : undefined;
  };
  const getProfile = (id) => {
    if (typeof id !== 'string') return undefined;
    if (typeof profiles === 'function') return profiles(id);
    if (profiles instanceof Map) return profiles.get(id);
    if (!plainObject(profiles)) return undefined;
    return Object.prototype.hasOwnProperty.call(profiles, id) ? profiles[id] : undefined;
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
  const src = (p && p.sources && typeof p.sources === 'object' && !Array.isArray(p.sources)) ? p.sources : {};
  const global = (p && GLOBAL_STATES.has(p.global) && p.global === 'share-all') ? 'share-all' : 'private';
  const sources = {};
  for (const k of Object.keys(src)) {
    // key must be a valid source id (drops __proto__-style and junk keys —
    // same gate as sourceRule, so normalize+resolve agree with Python; it
    // also makes the plain assignment below safe: no key that survives the
    // regex can be a prototype-mutating name)
    if (!SOURCE_KEY_RE.test(k)) continue;
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
  const rooms = plainObject(syncData) ? syncData.rooms : null;
  const join = plainObject(rooms) ? plainObject(rooms.join) : null;
  if (!join) return out;
  for (const rid of Object.keys(join)) {
    // output keys are gated by the STATIC room-id shape (parity with
    // Python's _CONSENT_ROOMID_RE; a junk/"__proto__" key never enters the
    // map, which also makes the plain assignment below safe)
    if (!CONSENT_ROOMID_RE.test(rid)) continue;
    const room = plainObject(join[rid]);
    const ad = room ? plainObject(room.account_data) : null;
    const events = ad && Array.isArray(ad.events) ? ad.events : null;
    if (!events) continue;
    for (const e of events) {
      if (plainObject(e) && e.type === SHARE_OVERRIDE_TYPE) {
        const v = normalizeOverride(e.content);
        if (v) out[rid] = v; else delete out[rid];
      }
    }
  }
  return out;
}

// ===========================================================================
// CONTACT-SHARE — a SEPARATE consent dimension from conversation sharing above.
// Decides whether a teammate's address-book contacts (per source) leave their
// machine for the manager. Same shape/precedence philosophy as the conversation
// resolver, but its own policy, its own account-data key, and its own default:
// PRIVATE (absent policy => not shared). MUST stay byte-parity with
// agents/uplink/consent.py's resolve_contact_share/normalize_contact_policy.
// ===========================================================================

const CONTACT_SHARE_POLICY_TYPE = 'com.jkali.contact_share_policy'; // global user account-data
const CONTACT_GLOBAL_STATES = new Set(['share-all', 'private']);
const CONTACT_SOURCE_STATES = new Set(['share-all', 'private-all', 'inherit']);

// Coerce a stored/incoming contact-share policy into the known-safe shape
// { global: 'share-all'|'private', sources: { <source>: 'share-all'|'private-all' } }.
// Unknown global -> 'private' (safe default). Only recognized source states are
// kept; 'inherit' (source omitted == inherit) and anything unrecognized are dropped.
function normalizeContactPolicy(raw) {
  const src = (raw && raw.sources && typeof raw.sources === 'object' && !Array.isArray(raw.sources)) ? raw.sources : {};
  const global = (raw && CONTACT_GLOBAL_STATES.has(raw.global) && raw.global === 'share-all') ? 'share-all' : 'private';
  const sources = {};
  for (const k of Object.keys(src)) {
    if (!SOURCE_KEY_RE.test(k)) continue; // same key gate as the conversation dimension
    const v = src[k];
    if (v === 'share-all' || v === 'private-all') sources[k] = v; // drop 'inherit'/junk
  }
  return { global, sources };
}

// Resolve whether a given source's contacts are shared AND the reason.
//   source : the source id (e.g. 'imessage')
//   policy : a normalized contact policy (from normalizeContactPolicy)
// Precedence (most-specific-wins), mirroring resolve() above:
//   1. per-source 'share-all'   -> shared,     reason 'all <source> contacts'
//   2. per-source 'private-all' -> not shared, reason 'private'
//   3. global 'share-all'       -> shared,     reason 'all contacts'
//   4. safe default             -> not shared, reason 'private'
function resolveContactShare(source, policy) {
  const pol = plainObject(policy) || {};

  // same gated lookup as the conversation dimension: valid source id, own
  // entry, exact value — anything else is inherit
  const src = sourceRule(pol.sources, source);
  if (src === 'share-all') return { shared: true, reason: 'all ' + source + ' contacts' };
  if (src === 'private-all') return { shared: false, reason: 'private' };
  // (inherit / absent / malformed -> fall through to global)

  if (pol.global === 'share-all') return { shared: true, reason: 'all contacts' };

  return { shared: false, reason: 'private' };
}

// Account-data path for the global contact-share policy on the LOCAL homeserver.
// userId is already validated by the caller (same as policyPath()'s S.userId).
function contactSharePolicyPath(userId) {
  return '/_matrix/client/v3/user/' + encodeURIComponent(userId) +
    '/account_data/' + CONTACT_SHARE_POLICY_TYPE;
}

export {
  SHARE_POLICY_TYPE, SHARE_OVERRIDE_TYPE, PROFILE_STATES,
  resolve, effectiveShared, resolveAll,
  normalizePolicy, normalizeOverride,
  readSharePolicy, writeSharePolicy,
  writeShareOverride,
  overridesFromSync,
  CONTACT_SHARE_POLICY_TYPE,
  normalizeContactPolicy, resolveContactShare, contactSharePolicyPath,
};
