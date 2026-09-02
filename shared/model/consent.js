// PLAN-MASTER-SYNC §4 — the consent model (the authorization boundary).
// Shared ES module. Pure resolver + account-data storage helpers. NO DOM.
//
// CONVERSATION SHARING IS EXPLICIT-ONLY (direct-share-level plan, D1). A
// conversation carries exactly ONE per-conversation level, and nothing else
// can share it:
//   'share'   -> mirrored to the master; manager suggestions land in the
//                teammate's proposal inbox for review
//   'direct'  -> mirrored to the master; the teammate's uplink auto-sends a
//                manager proposal into the conversation (see the plan's D2 —
//                the auto-send code itself lands in a later slice)
//   'private' -> not mirrored (the default)
// ABSENT OR ANY UNRECOGNIZED VALUE RESOLVES 'private'. That is a stated
// invariant with its own conformance vector class — a stored override this
// code does not recognize must never be able to share a conversation.
//
// There is NO inheritance on the conversation path any more: contact-profile
// share-state, the per-source policy and the global standing policy do NOT
// affect whether a conversation mirrors. resolve() still ACCEPTS those
// arguments (call-site and conformance-vector compatibility) and deliberately
// ignores them. The layered, most-specific-wins model survives only in the
// SEPARATE contact-sharing dimension at the bottom of this file
// (resolveContactShare / normalizeContactPolicy / normalizeContactOverrides),
// which keeps its standing policies on purpose — and, as of the per-contact-
// share plan, gains a per-CONTACT override that is more specific than both.
//
// The one-time migration that materializes previously-inherited shares into
// explicit 'share' overrides lives in agents/uplink/uplink.py (D0); it keeps
// its own copy of the old inherit-semantics resolver for that single purpose,
// deliberately NOT here.

import { ROOMID_RE, api } from '../matrix/client.js';
import { S } from '../state.js';

// Account-data event types (spec §5.2). Reuses the same account-data mechanism
// already used for m.tag, push rules, and com.jkali.self_identities.
const SHARE_POLICY_TYPE = 'com.jkali.share_policy';       // global user account-data
const SHARE_OVERRIDE_TYPE = 'com.jkali.share_override';   // per-room account-data
// The model-version marker (D0/F7): written to LOCAL user account-data by the
// uplink once the explicit-levels migration has completed. Its presence is what
// tells the teammate UI that the daemon no longer honors standing policies, so
// the UI can stop offering (and stop claiming to honor) the dead controls.
const CONSENT_MODEL_TYPE = 'com.jkali.consent_model';
const CONSENT_MODEL_EXPLICIT = 2;

// ---- valid state tokens ------------------------------------------------------
const GLOBAL_STATES = new Set(['share-all', 'private']);
const SOURCE_STATES = new Set(['share-all', 'private-all', 'inherit']);
// The THREE explicit conversation levels. Anything else (including the old
// 'inherit', an absent event, or junk) is 'private' — see effectiveLevel().
const OVERRIDE_STATES = new Set(['share', 'direct', 'private']);
// Profile share-state (from a contact profile). Retained for shared/model/
// contacts.js's storage shape ONLY: since D1 a profile's share-state has NO
// effect on conversation mirroring.
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

// ===========================================================================
// PURE RESOLVER — the authorization decision. No I/O, no DOM.
// ===========================================================================

// The conversation's explicit level: 'private' | 'share' | 'direct'.
// Accepts the raw account-data content (object form { state } or a bare
// string) as well as an already-normalized token, so the uplink's fresh
// point-read and the UI can both call it without a second gate. ABSENT OR
// UNRECOGNIZED => 'private' — the invariant this whole model rests on.
function effectiveLevel(override) {
  return normalizeOverride(override) || 'private';
}

// Resolve one conversation's effective shared-state AND the reason for it.
//   convo    : { sourceId, sourceLabel, ... }   IGNORED (see D1)
//   policy   : the standing-policy object       IGNORED (see D1)
//   override : 'share' | 'direct' | 'private' | undefined — the ONLY input
//   profile  : the room's contact profile       IGNORED (see D1)
// Returns { shared: boolean, reason: 'explicit'|'direct'|'excluded'|'private' }.
// convo/policy/profile stay in the signature so existing call sites and the
// conformance vectors keep working; they can no longer influence the decision.
// `reason` is UI-only — never parse it for authorization (that is what
// `shared` / effectiveLevel() are for).
function resolve(convo, policy, override, profile) {
  const level = effectiveLevel(override);
  if (level === 'share') return { shared: true, reason: 'explicit' };
  if (level === 'direct') return { shared: true, reason: 'direct' };
  // Private either way; the reason distinguishes a deliberate exclusion from
  // "never set" purely for the UI's wording.
  return { shared: false, reason: normalizeOverride(override) ? 'excluded' : 'private' };
}

// The boolean the uplink asks for when deciding whether to mirror a room.
function effectiveShared(convo, policy, override, profile) {
  return resolve(convo, policy, override, profile).shared;
}

// Resolve a whole list at once (drives the consent summary panel + row badges).
//   convos    : array of conversation objects (each with an `id` room id)
//   policy    : IGNORED (see D1) — kept so call sites need no change
//   overrides : per-room overrides keyed by room id — a Map or a plain object,
//               value 'share'|'direct'|'private' (absent/unknown = private).
//   profiles  : IGNORED (see D1) — a profile no longer shares a conversation.
// Returns [{ convo, shared, reason }, ...] in input order.
function resolveAll(convos, policy, overrides, profiles) {
  const list = Array.isArray(convos) ? convos : [];
  // Container: a real Map (instanceof, never duck-typed — a JSON object with
  // an own "get" key is a plain object) or a plain object with OWN-property
  // lookup. Keys must be strings (Python gates identically on its dict form;
  // the Map form is JS-only and outside the conformance harness's JSON domain
  // — curated tests cover it).
  const get = (id) => {
    if (typeof id !== 'string') return undefined;
    if (overrides instanceof Map) return overrides.get(id);
    if (!plainObject(overrides)) return undefined;
    return Object.prototype.hasOwnProperty.call(overrides, id) ? overrides[id] : undefined;
  };
  return list.map((convo) => {
    // PARITY (found by the conformance harness): the key must be read only
    // from a plain-object convo, exactly as Python's
    // `convo.get("id") if isinstance(convo, dict)` does. `convo && convo.id`
    // returned "" for the empty-string convo, which then matched an override
    // stored under "" and shared a conversation Python resolved private.
    const id = plainObject(convo) ? convo.id : undefined;
    const r = resolve(convo, policy, get(id), undefined);
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

// A per-room override is 'share' | 'direct' | 'private' | null. Accepts either
// the object form { state: 'share' } or a bare string, tolerating both.
// null means "no recognized level stored" — and under the explicit model that
// resolves to PRIVATE, never to an inherited share (effectiveLevel()).
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

// Write one room's override. state 'share'|'direct'|'private' sets it;
// anything else (including null) clears the event to empty content, which the
// explicit model reads back as PRIVATE (account-data cannot be deleted).
// Returns the normalized state or null.
async function writeShareOverride(roomId, state) {
  const s = OVERRIDE_STATES.has(state) ? state : null;
  await api('PUT', overridePath(roomId), s === null ? {} : { state: s });
  return s;
}

// The consent-model marker (D0/F7). The uplink writes it once the explicit-
// levels migration has completed; the teammate UI reads it to decide whether
// the daemon still honors the old standing policies. Returns an integer
// version (1 = the old inherit model). ANY read failure, absent event, or junk
// content returns 1 — the UI must never assume the new model on a bad read,
// because a new-model UI over an old inherit daemon is exactly the skew F7
// forbids (UI says Private while the daemon still shares).
async function readConsentModel() {
  try {
    const data = await api('GET', '/_matrix/client/v3/user/' +
      encodeURIComponent(S.userId) + '/account_data/' + CONSENT_MODEL_TYPE);
    const v = plainObject(data) ? data.version : null;
    return (typeof v === 'number' && Number.isFinite(v) && v >= CONSENT_MODEL_EXPLICIT)
      ? CONSENT_MODEL_EXPLICIT : 1;
  } catch (e) {
    return 1;
  }
}

// Extract per-room overrides from a /sync response's room account-data blocks,
// so a caller can build the `overrides` map for resolveAll() without a GET per
// room. Reads only com.jkali.share_override; ignores everything else. Returns a
// plain object { <roomId>: 'share'|'direct'|'private' }; a room whose stored
// value is absent/cleared/unrecognized is OMITTED (a later junk event even
// deletes an earlier valid one — pinned by the unit tests), and an omitted room
// resolves private.
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

// ---- per-contact overrides (per-contact-share plan, C1) ---------------------
// A SECOND, more specific level in the contact dimension: one HANDLE
// ('<source>|<network_id>') may be pinned 'share' or 'private', winning over the
// per-source and global standing policies. Unlike the conversation dimension,
// the contact dimension deliberately KEEPS its standing policies — absent means
// inherit, not private.
const CONTACT_OVERRIDES_TYPE = 'com.jkali.contact_overrides'; // global user account-data
// F5: every WRITE path must refuse before crossing this, and a STORED map above
// it reads as a read-failure (normalizeContactOverrides -> null) so a bloated
// event can never be silently half-honored. Only the destructive recovery
// writes (single-key removal, clear-all) are permitted in that state.
const CONTACT_OVERRIDES_CAP = 1024;
const CONTACT_OVERRIDE_STATES = new Set(['share', 'private']);

// F5/F6 KEY SPEC — the override key is '<source>|<network_id>'. The segment
// before the FIRST '|' must be a valid source id; the remainder is taken
// VERBATIM and may itself contain '|' (the importer's email charset admits it),
// which is why nothing here ever uses split('|'). The SOURCE_KEY_RE prefix is
// what makes the composite injective.
function contactOverrideKey(source, network_id) {
  if (typeof source !== 'string' || !SOURCE_KEY_RE.test(source)) return null;
  if (typeof network_id !== 'string' || !network_id) return null;
  return source + '|' + network_id;
}

// The inverse: { source, network_id } or null. Splits ONCE, on the first '|'.
function splitContactOverrideKey(key) {
  if (typeof key !== 'string') return null;
  const i = key.indexOf('|');
  if (i <= 0) return null;
  const source = key.slice(0, i);
  const network_id = key.slice(i + 1);
  if (!SOURCE_KEY_RE.test(source) || !network_id) return null;
  return { source, network_id };
}

// The stored-map reader, split out from normalizeContactOverrides so the UI's
// over-cap recovery path can still see the (validated) entries it must delete.
// Returns { count, map } — count is the number of STORED entries (cap check) —
// or null for a READ FAILURE. Own-property discipline throughout, output built
// on a null-prototype object so no surviving key can reach Object.prototype.
function contactOverrideEntries(raw) {
  const content = plainObject(raw);
  const out = Object.create(null);
  if (!content) return { count: 0, map: out };
  // An absent `overrides` field is an empty map; a PRESENT but non-plain-object
  // one is a READ FAILURE, never {} (F5) — a partially corrupt event must not
  // silently drop a 'private' deny and re-widen the contact to its source.
  if (!Object.prototype.hasOwnProperty.call(content, 'overrides')) return { count: 0, map: out };
  const src = plainObject(content.overrides);
  if (!src) return null;
  const keys = Object.keys(src);
  for (const k of keys) {
    if (!splitContactOverrideKey(k)) continue;      // malformed KEY -> dropped (inherit)
    const v = src[k];
    if (v === 'share' || v === 'private') out[k] = v; // unknown VALUE -> dropped (inherit)
  }
  return { count: keys.length, map: out };
}

// { '<source>|<network_id>': 'share'|'private' } — or null on a READ FAILURE
// (non-plain-object `overrides` field, or a stored map over CONTACT_OVERRIDES_CAP).
// MUST stay byte-parity with agents/uplink/consent.py's normalize_contact_overrides.
function normalizeContactOverrides(raw) {
  const e = contactOverrideEntries(raw);
  if (e === null) return null;
  if (e.count > CONTACT_OVERRIDES_CAP) return null;
  return e.map;
}

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
//   source   : the source id (e.g. 'imessage')
//   policy   : a normalized contact policy (from normalizeContactPolicy)
//   override : this ONE contact's stored override, 'share'|'private'|absent
//              (from normalizeContactOverrides, keyed by contactOverrideKey)
// Precedence (most-specific-wins), mirroring resolve() above:
//   0. per-contact 'share'      -> shared,     reason 'this contact'
//   0. per-contact 'private'    -> not shared, reason 'this contact private'
//   1. per-source 'share-all'   -> shared,     reason 'all <source> contacts'
//   2. per-source 'private-all' -> not shared, reason 'private'
//   3. global 'share-all'       -> shared,     reason 'all contacts'
//   4. safe default             -> not shared, reason 'private'
// An unrecognized override VALUE falls through to the source/global levels —
// the contact dimension keeps its standing policies (F5's fall-through is safe
// here only because normalizeContactOverrides drops unknown values on the way
// IN, and a non-dict stored map is a read failure rather than an empty one).
function resolveContactShare(source, policy, override) {
  if (override === 'share') return { shared: true, reason: 'this contact' };
  if (override === 'private') return { shared: false, reason: 'this contact private' };

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

// Account-data path for the per-contact overrides map.
function contactOverridesPath(userId) {
  return '/_matrix/client/v3/user/' + encodeURIComponent(userId) +
    '/account_data/' + CONTACT_OVERRIDES_TYPE;
}

// F3 WRITE DISCIPLINE — read the overrides map, distinguishing the three states
// a caller must treat differently. NEVER collapses an error into {}: a
// fail-to-empty read followed by a blind PUT is exactly how a stored 'private'
// deny gets erased and a withheld contact re-widens to its source policy.
//   { status: 'ok',       overrides }  — 404/absent (empty) or a valid map
//   { status: 'over-cap', overrides }  — stored map above the cap; `overrides`
//                                        holds the validated entries so the UI
//                                        can offer the destructive-only
//                                        recovery (remove one / clear all)
//   { status: 'error',    overrides: null } — anything else; controls DISABLE
async function readContactOverrides() {
  let data;
  try {
    data = await api('GET', contactOverridesPath(S.userId));
  } catch (e) {
    if (e && e.status === 404) return { status: 'ok', overrides: Object.create(null) };
    return { status: 'error', overrides: null };
  }
  const entries = contactOverrideEntries(data);
  if (entries === null) return { status: 'error', overrides: null };
  if (entries.count > CONTACT_OVERRIDES_CAP) {
    return { status: 'over-cap', overrides: entries.map };
  }
  return { status: 'ok', overrides: entries.map };
}

// Write the whole overrides map. The caller is responsible for having built it
// as a MERGE over a fresh read (see apps/user/consent.js's applyContactOverrides)
// — this helper deliberately does no merging of its own, so there is exactly one
// place where the merge discipline lives.
async function writeContactOverrides(overrides) {
  const body = { overrides: {} };
  const src = plainObject(overrides) || {};
  for (const k of Object.keys(src)) {
    if (!splitContactOverrideKey(k)) continue;
    if (CONTACT_OVERRIDE_STATES.has(src[k])) body.overrides[k] = src[k];
  }
  await api('PUT', contactOverridesPath(S.userId), body);
  return normalizeContactOverrides(body);
}

export {
  SHARE_POLICY_TYPE, SHARE_OVERRIDE_TYPE, PROFILE_STATES,
  CONSENT_MODEL_TYPE, CONSENT_MODEL_EXPLICIT,
  resolve, effectiveShared, effectiveLevel, resolveAll,
  normalizePolicy, normalizeOverride,
  readSharePolicy, writeSharePolicy,
  writeShareOverride, readConsentModel,
  overridesFromSync,
  CONTACT_SHARE_POLICY_TYPE,
  normalizeContactPolicy, resolveContactShare, contactSharePolicyPath,
  CONTACT_OVERRIDES_TYPE, CONTACT_OVERRIDES_CAP, CONTACT_OVERRIDE_STATES,
  contactOverrideKey, splitContactOverrideKey,
  contactOverrideEntries, normalizeContactOverrides,
  contactOverridesPath, readContactOverrides, writeContactOverrides,
};
