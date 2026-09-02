// PLAN-MASTER-SYNC §5.1 (share controls) / §4.2 (trust guards). apps/user-only:
// the per-row share toggle (with effective state + reason), the consent summary
// panel, the contact-share controls, and — only while the daemon has not yet
// reported the explicit consent model — the legacy global Share All switch and
// per-source Share-all-<source> switch. Under the explicit model those two are
// dead and replaced by a banner; see the consent-model guard below.
// Reuses shared/model/consent.js for all resolution + storage;
// wires into shared/ui/{rows,search,nav}.js via their app-injection hooks so no
// shared module needs to know apps/user exists. textContent-only, no innerHTML,
// no CSP change. Local state + UI ONLY — the uplink (Phase 2) is not built here.

import {
  resolve, resolveAll, effectiveLevel,
  readSharePolicy, writeSharePolicy,
  writeShareOverride, overridesFromSync, SHARE_OVERRIDE_TYPE,
  readConsentModel, CONSENT_MODEL_EXPLICIT,
  normalizeContactPolicy, resolveContactShare, contactSharePolicyPath,
  CONTACT_OVERRIDES_CAP, CONTACT_OVERRIDE_STATES,
  contactOverrideKey, splitContactOverrideKey,
  readContactOverrides, writeContactOverrides,
} from '../../shared/model/consent.js';
import { readProfiles, writeProfiles, roomProfileMap } from '../../shared/model/contacts.js';
import { api } from '../../shared/matrix/client.js';
import { $, el, sanitizeLine } from '../../shared/ui/el.js';
import { setConvoRowDecorator } from '../../shared/ui/rows.js';
import { setSourceViewHook } from '../../shared/ui/search.js';
import { setSharingViewHook } from '../../shared/ui/nav.js';
import { SOURCES, validHandle } from '../../shared/ui/sources.js';
import { S, convosBySource, feedModel } from '../../shared/state.js';
import { feedHideRoom, feedUnhideRoom, feedIsHidden } from '../../shared/ui/account-data.js';
import { confirmModal } from '../../shared/ui/connections.js';
// The SAME origin-gated loopback helper enrich.js already talks to (custom
// X-Beepa-Connect header + application/json force a CORS preflight the helper
// only echoes for this app's origin). Imported, never re-implemented.
import { sessionConnectBase, SESSION_CONNECT_HEADERS } from './enrich.js';

// Local cache of the two consent-storage reads (§5.2). Writes below update it
// in place so rows/panels reflect the change immediately, with no re-fetch.
let policy = { global: 'private', sources: {} };
// The CONTACT-share policy (com.jkali.contact_share_policy) — a SEPARATE
// consent dimension from conversation sharing above: it decides whether this
// teammate's ADDRESS BOOK (contacts, per source) leaves the machine for the
// manager, not which conversations mirror. Its own account-data key, its own
// default: PRIVATE (empty/absent => not shared). Never conflated with `policy`.
let contactPolicy = { global: 'private', sources: {} };
// roomId -> the room's explicit level, 'share' | 'direct' | 'private'. A room
// absent from this map (or carrying a value the model does not recognize) is
// PRIVATE — it is never inherited from a profile or a standing policy.
const overrides = new Map();
// roomId -> { id, displayName, share } for the room's contact profile, if any
// (§12 phase 5). Populated from shared/model/contacts.js account-data. Since
// D1 a profile's share-state does NOT affect whether its conversations mirror
// — it only groups a person's threads — so this map is passed to the resolver
// for signature compatibility and is ignored by it.
let profileMap = {};

// roomIds whose current 'share' override was written by the D0 migration
// (content carries migrated:true) — populated from the same sync snapshot
// that builds `overrides`, so it needs no extra round trip. Drives the
// one-time "review migrated shares" list (D0/F11).
let migratedRoomIds = [];
// The master-identity re-confirm affordance (D2.11/F12) — null unless the
// local account-data event com.jkali.direct_send_suspended is present, in
// which case this is its normalized identity tuple. Owned entirely by S2:
// S3's uplink is what writes/clears the suspension and reads the ack this
// module writes below; nothing here talks to the uplink directly.
let directSendSuspension = null;

// ---- consent-model guard (direct-share-level plan, D0/F7) -------------------
// Conversation sharing is now EXPLICIT-ONLY in shared/model/consent.js: a
// conversation mirrors only on its own 'share'/'direct' override, never through
// a contact profile, a per-source policy or the global Share-All. Those three
// standing-policy controls below are therefore DEAD — but only once the uplink
// on this machine has actually migrated (it writes com.jkali.consent_model = 2
// when its one-time migration completes). Until then an older daemon may still
// be inheriting, so:
//   - marker present (v2): the dead conversation controls are removed and an
//     "updating to explicit levels…" banner takes their place, so this UI can
//     never show a room as shared on the strength of a policy the daemon has
//     stopped honoring;
//   - marker absent (v1): the controls stay, plus a banner warning that the
//     daemon has not confirmed the new model yet — the UI must not quietly
//     claim Private while a standing policy may still be mirroring.
// A failed/absent read is treated as v1 (the conservative direction).
// The full three-level surface (Share / Direct / Private) ships in the next
// slice; this slice only removes what can no longer be honored.
let consentModel = 1;
function explicitModel() { return consentModel >= CONSENT_MODEL_EXPLICIT; }

// h3 + p.muted, not the share-global-label title/desc pair: settings.css hides
// `.share-global-label .desc` (and `.share-newly-box > p.muted`) inside
// #detail-admin, and a banner whose explanation is invisible is worse than no
// banner. `share-model-banner` is only a styling hook for the next slice.
function modelBanner(title, text) {
  const box = el('div', 'share-model-banner');
  box.appendChild(el('h3', '', title));
  box.appendChild(el('p', 'muted', text));
  return box;
}
function pendingModelBanner() {
  return modelBanner('Sharing model is updating…',
    'Conversations are moving to explicit per-conversation sharing. This app '
    + 'already resolves sharing that way, but the background sync on this '
    + 'machine has not confirmed the change yet, so a conversation shown as '
    + 'private may still be mirrored under a standing policy until it does. '
    + 'Set anything you want kept private to Private on the conversation itself.');
}

// §4.2 guard 2 (auto-share visibility): which shared rooms have already been
// surfaced to the teammate, so only genuinely NEW auto-shares get flagged.
// Per-viewer convenience state only (never the authorization decision itself,
// which always comes from resolve()/resolveAll() below) — plain localStorage.
const SEEN_KEY = 'com.jkali.consent_seen_shared';
function loadSeen() {
  try { return new Set(JSON.parse(localStorage.getItem(SEEN_KEY) || '[]')); }
  catch (e) { return new Set(); }
}
function saveSeen(set) {
  try { localStorage.setItem(SEEN_KEY, JSON.stringify([...set])); } catch (e) { /* ignore */ }
}

// ---- loading the two consent-storage sources ----

// Per-room overrides via one filtered /sync snapshot (mirrors the pattern
// account-data.js already uses for m.tag), rather than a GET per room.
async function fetchOverridesSnapshot() {
  const filter = encodeURIComponent(JSON.stringify({
    room: { timeline: { limit: 0 }, state: { lazy_load_members: true },
      account_data: { types: ['com.jkali.share_override'] } },
    presence: { types: [] }, account_data: { types: [] },
  }));
  return await api('GET', '/_matrix/client/v3/sync?timeout=0&filter=' + filter);
}
// Read/write the contact-share policy account-data, mirroring the conversation
// policy's readSharePolicy/writeSharePolicy exactly: GET/PUT the account-data
// via the model's contactSharePolicyPath(S.userId) (never a hand-rolled path)
// and normalize through the model's normalizeContactPolicy so a malformed
// event can only ever collapse to the safe default (private), never smuggle a
// share through. Absent/404 => default { global:'private', sources:{} }.
async function readContactPolicy() {
  try {
    return normalizeContactPolicy(await api('GET', contactSharePolicyPath(S.userId)));
  } catch (e) {
    return { global: 'private', sources: {} };
  }
}
async function writeContactPolicy(p) {
  const body = normalizeContactPolicy(p);
  await api('PUT', contactSharePolicyPath(S.userId), body);
  return body;
}

// ---- migrated-shares review (D0/F11) -----------------------------------------
// PURE: which of the room ids that DO carry a currently-recognized override
// (validIds — the same set overridesFromSync() just produced, so a stale/
// unknown room id can never appear here) also carry migrated:true on their
// com.jkali.share_override account-data, straight from the sync snapshot
// already fetched for `overrides` — no extra round trip. A malformed/missing
// shape yields no rooms rather than throwing.
function migratedRoomIdsFromSync(syncData, validIds) {
  const out = [];
  try {
    const join = (syncData && syncData.rooms && syncData.rooms.join) || {};
    for (const rid of Object.keys(join)) {
      if (validIds && !validIds.has(rid)) continue;
      const events = (join[rid] && join[rid].account_data && join[rid].account_data.events) || [];
      for (const e of events) {
        if (e && e.type === SHARE_OVERRIDE_TYPE && e.content && e.content.migrated === true) { out.push(rid); break; }
      }
    }
  } catch (e) { /* malformed sync -> no migrated rooms surfaced */ }
  return out;
}

// Per-viewer dismissal for the migrated-shares list — convenience only, same
// pattern as SEEN_KEY above. Never the authorization decision.
const MIGRATED_DISMISSED_KEY = 'com.jkali.migrated_review_dismissed';
function loadMigratedDismissed() {
  try { return new Set(JSON.parse(localStorage.getItem(MIGRATED_DISMISSED_KEY) || '[]')); }
  catch (e) { return new Set(); }
}
function saveMigratedDismissed(set) {
  try { localStorage.setItem(MIGRATED_DISMISSED_KEY, JSON.stringify([...set])); } catch (e) { /* ignore */ }
}

// ---- master-identity re-confirm surface (D2.11/F12) --------------------------
const DIRECT_SEND_SUSPENDED_TYPE = 'com.jkali.direct_send_suspended';
const DIRECT_SEND_ACK_TYPE = 'com.jkali.direct_send_ack';

// PURE: normalize a com.jkali.direct_send_suspended account-data content into
// the affordance's identity tuple, or null if any of the four fields is
// missing/wrong-shaped. A partial/junk event must never render a broken
// confirm — same "safe default on malformed input" discipline as the rest of
// this model.
function suspensionAffordance(content) {
  if (!content || typeof content !== 'object' || Array.isArray(content)) return null;
  const { master_hs, master_user, manager_mxid, ts } = content;
  if (typeof master_hs !== 'string' || !master_hs) return null;
  if (typeof master_user !== 'string' || !master_user) return null;
  if (typeof manager_mxid !== 'string' || !manager_mxid) return null;
  if (typeof ts !== 'number' || !Number.isFinite(ts)) return null;
  return { master_hs, master_user, manager_mxid, ts };
}

// PURE: the exact content the teammate's confirm writes to
// com.jkali.direct_send_ack — the SAME identity tuple, verbatim, so S3's
// uplink can compare it byte-for-byte against the suspension it stored
// before resuming auto-send. Takes either a raw suspended-event content or an
// already-normalized affordance (both go through suspensionAffordance first).
function directSendAckContent(affordance) {
  const a = suspensionAffordance(affordance);
  return a ? { master_hs: a.master_hs, master_user: a.master_user, manager_mxid: a.manager_mxid, ts: a.ts } : null;
}

function userAccountDataPath(type) {
  return '/_matrix/client/v3/user/' + encodeURIComponent(S.userId) + '/account_data/' + type;
}
async function readDirectSendSuspension() {
  try { return suspensionAffordance(await api('GET', userAccountDataPath(DIRECT_SEND_SUSPENDED_TYPE))); }
  catch (e) { return null; }
}
// Writes the ack; returns the tuple written, or null if `affordance` didn't
// normalize (never PUTs a malformed body).
async function ackDirectSendSuspension(affordance) {
  const body = directSendAckContent(affordance);
  if (!body) return null;
  await api('PUT', userAccountDataPath(DIRECT_SEND_ACK_TYPE), body);
  return body;
}

async function loadConsentState() {
  // The model marker first: every render below branches on it.
  try { consentModel = await readConsentModel(); } catch (e) { consentModel = 1; }
  try { policy = await readSharePolicy(); } catch (e) { /* keep previous cache */ }
  try { contactPolicy = await readContactPolicy(); } catch (e) { /* keep previous cache */ }
  try {
    const syncData = await fetchOverridesSnapshot();
    const map = overridesFromSync(syncData);
    overrides.clear();
    for (const k of Object.keys(map)) overrides.set(k, map[k]);
    migratedRoomIds = migratedRoomIdsFromSync(syncData, new Set(overrides.keys()));
  } catch (e) { /* keep previous cache */ }
  try { profileMap = roomProfileMap(await readProfiles()); } catch (e) { /* keep previous cache */ }
  // F3: the three states are distinct on purpose — an unreadable overrides map
  // DISABLES the per-contact controls rather than rendering an empty one.
  try {
    const st = await readContactOverrides();
    contactOverridesStatus = st.status;
    contactOverrides = st.overrides || Object.create(null);
  } catch (e) {
    contactOverridesStatus = 'error';
    contactOverrides = Object.create(null);
  }
  await loadImportedContacts();
  try { directSendSuspension = await readDirectSendSuspension(); } catch (e) { directSendSuspension = null; }
}

// All known conversations across sources, deduped by room id (same
// first-SOURCES-order-wins rule seedFeed() uses for the feed model).
function allConvos() {
  const seen = new Set();
  const out = [];
  for (const s of SOURCES) {
    if (s.kind === 'all') continue;
    for (const c of (convosBySource[s.id] || [])) {
      if (seen.has(c.id)) continue;
      seen.add(c.id);
      out.push(c);
    }
  }
  return out;
}

function effectiveFor(convo) {
  return resolve(convo, policy, overrides.get(convo.id), profileMap[convo.id]);
}

// Three levels, no inherit (F13/consent-summary copy fix): the reason a
// resolve()/resolveAll() result carries is always one of exactly these four
// under the explicit model, and the wording never implies a standing policy.
function reasonText(r) {
  if (r.shared) return r.reason === 'direct' ? 'Direct — sent automatically' : 'Shared';
  return r.reason === 'excluded' ? 'Private' : 'Private (default)';
}

// ---- sliding tri-state control (kebab rows + per-source headers) ----

// The LEGACY per-conversation cycle, used only while the model marker is
// absent (an un-migrated daemon may still honor "Auto (inherit)").
const SHARE_CYCLE = [
  { val: 'inherit', label: 'Auto', override: null },
  { val: 'share', label: 'Share', override: 'share' },
  { val: 'private', label: 'Private', override: 'private' },
];

// Under the explicit model there is no inherit position: a conversation is
// either explicitly shared or private. ('Direct' is deliberately NOT a cycle
// position — it gets its own confirmed control in the next slice, so it can
// never be reached by a pass-through tap.)
const SHARE_CYCLE_V2 = [
  { val: 'share', label: 'Share', override: 'share' },
  { val: 'private', label: 'Private', override: 'private' },
];

const SOURCE_POLICY_CYCLE = [
  { val: 'inherit', label: 'Auto' },
  { val: 'share-all', label: 'Share' },
  { val: 'private-all', label: 'Private' },
];

function buildTriStateSlider(cycle, opts) {
  const { ariaLabel, getIndex, onAdvance, getHint, hintShared } = opts;
  const btn = el('button', 'share-slider');
  btn.type = 'button';
  btn.setAttribute('role', 'slider');
  btn.setAttribute('aria-label', ariaLabel);

  const track = el('span', 'share-slider-track');
  const thumb = el('span', 'share-slider-thumb');
  track.appendChild(thumb);
  const segs = el('span', 'share-slider-segs');
  for (const o of cycle) segs.appendChild(el('span', 'share-slider-seg', o.label));
  track.appendChild(segs);
  btn.appendChild(track);
  // beepa.css sizes the track for THREE positions; the explicit conversation
  // cycle has two. Size it from the cycle here (CSSOM, same mechanism as the
  // --idx write below) so the visible segments and the click-mapped thirds/
  // halves below can never disagree about which position was tapped.
  if (cycle.length !== 3) {
    segs.style.gridTemplateColumns = 'repeat(' + cycle.length + ', 1fr)';
    thumb.style.width = 'calc((100% - 4px) / ' + cycle.length + ')';
  }

  const hint = el('span', 'share-slider-hint');
  btn.appendChild(hint);
  // F8 CONSENT-WRITE INVARIANT: no consent control may swallow a write error.
  // A failed write is SURFACED here and the control keeps rendering
  // last-known-good state (refresh() is skipped), never the requested one — a
  // toggle that looks moved but never landed is a consent lie.
  const err = el('span', 'share-slider-error hidden');
  btn.appendChild(err);
  function showWriteError(message) {
    err.textContent = 'Not saved — ' + sanitizeLine(message || 'try again')
      + '. Still showing your last saved setting.';
    err.classList.remove('hidden');
  }
  function clearWriteError() {
    err.textContent = '';
    err.classList.add('hidden');
  }

  function refresh() {
    const idx = getIndex();
    track.style.setProperty('--idx', String(idx));
    for (let i = 0; i < segs.children.length; i++) {
      segs.children[i].classList.toggle('active', i === idx);
    }
    const hintText = getHint();
    hint.textContent = hintText;
    hint.classList.toggle('shared', hintShared ? hintShared() : false);
    btn.setAttribute('aria-valuenow', String(idx));
    btn.setAttribute('aria-valuetext', cycle[idx].label + (hintText ? ' — ' + hintText : ''));
  }

  btn.addEventListener('click', async (e) => {
    e.stopPropagation();
    // Pick the segment under the pointer directly — the old advance-by-one
    // cycle made rapid clicks overshoot the intended state (pm_mng-7r6).
    // The segs are pointer-events:none, so map the click X onto thirds of
    // the track. Keyboard activation (detail === 0) keeps advance-by-one.
    let next;
    const rect = track.getBoundingClientRect();
    if (e.detail > 0 && rect.width > 0) {
      const frac = (e.clientX - rect.left) / rect.width;
      next = Math.max(0, Math.min(cycle.length - 1, Math.floor(frac * cycle.length)));
      if (next === getIndex()) { clearWriteError(); return; }  // clicked the current state
    } else {
      next = (getIndex() + 1) % cycle.length;
    }
    clearWriteError();
    try {
      const ok = await onAdvance(cycle[next]);
      // A handler that returns false REFUSED deliberately (e.g. a declined
      // confirm) and has already said so in its own surface; anything that
      // THROWS is a failed write and must be visible here (F8).
      if (ok === false) return;
    } catch (e2) {
      showWriteError(e2 && e2.message);
      return;
    }
    refresh();
  });

  refresh();
  return btn;
}

function shareCycleIndex(convo) {
  if (explicitModel()) {
    // Absent / unrecognized resolves PRIVATE — never fall back to index 0
    // ("Share"), which would show a private conversation as shared. A stored
    // 'direct' is shared, so it sits on the Share position until the next
    // slice gives it its own control; tapping either position only ever
    // de-escalates it.
    return effectiveLevel(overrides.get(convo.id)) === 'private' ? 1 : 0;
  }
  const cur = overrides.get(convo.id) || 'inherit';
  const idx = SHARE_CYCLE.findIndex((o) => o.val === cur);
  return idx >= 0 ? idx : 0;
}

// `afterAdvance` (optional) fires once the write + local cache update above
// have completed, so a caller composing this slider with the separate Direct
// control (below) can re-derive whether Direct should still be offered —
// e.g. sliding Share->Private must hide it, Private->Share may reveal it.
function buildShareSlider(convo, afterAdvance) {
  return buildTriStateSlider(explicitModel() ? SHARE_CYCLE_V2 : SHARE_CYCLE, {
    ariaLabel: 'Sharing for ' + sanitizeLine(convo.title || convo.id),
    getIndex: () => shareCycleIndex(convo),
    getHint: () => reasonText(effectiveFor(convo)),
    hintShared: () => effectiveFor(convo).shared,
    onAdvance: async (opt) => {
      await writeShareOverride(convo.id, opt.override);
      if (opt.override) overrides.set(convo.id, opt.override); else overrides.delete(convo.id);
      if (afterAdvance) afterAdvance();
    },
  });
}

// ---- Direct: a separate confirmed control, never a cycle position (F6) ------
// The ONLY places allowed to write override 'direct' are this confirm, the
// identical confirm reused by the migrated-shares review below, and (as of
// the 2026-09-02 product-owner-approved reversal of F11's "bulk never offers
// direct" — see D3 in the plan doc) the bulk action's own confirmed path —
// all three go through escalateToDirect()/writeShareOverride('direct', …),
// so there is exactly one write primitive for the escalation and every call
// site is gated by a confirm carrying the same risk copy. The share
// cycle (SHARE_CYCLE_V2) still never includes 'direct' as a position — it
// cannot be reached by a pass-through tap.
// The risk paragraph is shared verbatim between the single-conversation
// confirm below and the bulk confirm (2026-09-02 product-owner-approved
// reversal of F11's "bulk never offers direct" — see D3 and the plan's F11
// row): only the trailing "into …" reference and the closing question
// differ between the two call sites, so factor those out rather than
// duplicating the risk copy.
function directRiskCopy(intoText) {
  return 'Your manager’s messages will be sent as you, without your review.\n\n'
    + 'A compromised manager session or master server could send messages as you '
    + 'into ' + intoText + '. Recipients will not be able to tell the '
    + 'difference between a message you typed and one your manager sent automatically.';
}
function directConfirmText(convoTitle) {
  return directRiskCopy('“' + convoTitle + '”') + '\n\nTurn on Direct for this conversation?';
}
function confirmDirect(convo) {
  return confirmModal('Turn on Direct?', directConfirmText(sanitizeLine(convo.title || convo.id)), false);
}
async function escalateToDirect(convo) {
  const ok = await confirmDirect(convo);
  if (!ok) return false;
  await writeShareOverride(convo.id, 'direct');
  overrides.set(convo.id, 'direct');
  return true;
}

// Revealed only once the conversation is already Share/Direct (never while
// Private — Direct implies mirrored) and only under the explicit model.
// Turning Direct OFF goes straight back to 'share' with NO confirm — F6/plan
// text: de-escalation via the normal cycle/control is unconfirmed, only the
// escalation into 'direct' requires one.
function buildDirectRow(convo, onChange) {
  const row = el('div', 'share-direct-row');
  const isDirect = effectiveLevel(overrides.get(convo.id)) === 'direct';
  const btn = el('button', 'share-direct-btn' + (isDirect ? ' active' : ''), isDirect ? 'Direct ✓' : 'Turn on Direct…');
  btn.type = 'button';
  btn.title = 'Auto-send: your manager’s messages go out without your review';
  btn.addEventListener('click', async (e) => {
    e.stopPropagation();
    btn.disabled = true;
    try {
      if (isDirect) {
        await writeShareOverride(convo.id, 'share');
        overrides.set(convo.id, 'share');
      } else {
        await escalateToDirect(convo);
      }
    } finally { btn.disabled = false; }
    if (onChange) onChange();
  });
  row.appendChild(btn);
  return row;
}

// Combines the Share/Private slider with the separate Direct control, keeping
// both in sync with each other without re-deriving the slider's own internal
// state (buildTriStateSlider owns that): a change on either side rebuilds
// just the Direct row via `refreshDirect`.
function buildShareMenuBody(convo) {
  const wrap = el('div', 'share-menu-body');
  const directHost = el('div', 'share-direct-host');
  function refreshDirect() {
    directHost.replaceChildren();
    if (!explicitModel()) return;
    if (effectiveLevel(overrides.get(convo.id)) === 'private') return;
    directHost.appendChild(buildDirectRow(convo, refreshDirect));
  }
  wrap.appendChild(buildShareSlider(convo, refreshDirect));
  wrap.appendChild(directHost);
  refreshDirect();
  return wrap;
}

function sourcePolicyIndex(sourceId) {
  const cur = (policy.sources && policy.sources[sourceId]) || 'inherit';
  const idx = SOURCE_POLICY_CYCLE.findIndex((o) => o.val === cur);
  return idx >= 0 ? idx : 0;
}

function sourcePolicyHint(source) {
  const cur = (policy.sources && policy.sources[source.id]) || 'inherit';
  if (cur === 'share-all') return 'All ' + source.label + ' → manager';
  if (cur === 'private-all') return 'All ' + source.label + ' private';
  if (policy.global === 'share-all') return 'Following global (share all)';
  return 'Following global (private)';
}

function sourcePolicyShared(source) {
  const cur = (policy.sources && policy.sources[source.id]) || 'inherit';
  if (cur === 'share-all') return true;
  if (cur === 'private-all') return false;
  return policy.global === 'share-all';
}

function buildSourcePolicySlider(source) {
  return buildTriStateSlider(SOURCE_POLICY_CYCLE, {
    ariaLabel: 'Sharing for all ' + source.label,
    getIndex: () => sourcePolicyIndex(source.id),
    getHint: () => sourcePolicyHint(source),
    hintShared: () => sourcePolicyShared(source),
    onAdvance: async (opt) => {
      const sources = { ...(policy.sources || {}) };
      if (opt.val === 'inherit') delete sources[source.id]; else sources[source.id] = opt.val;
      policy = await writeSharePolicy({ ...policy, sources });
      renderConsentSummary();
    },
  });
}

// Registered via setConvoRowDecorator (rows.js): kebab with sliding share control.
function decorateRow(row, convo) {
  if (!convo || !convo.id) return;
  // Design 1a: sharing controls are NOT shown on every row; a kebab (⋯) on the
  // row reveals them on demand in a small popover.
  const holder = el('span', 'share-controls');
  const kebab = el('button', 'share-kebab', '⋯');   // ⋯
  kebab.type = 'button';
  kebab.title = 'Sharing';
  kebab.setAttribute('aria-label', 'Sharing controls');
  const menu = el('div', 'share-menu hidden');
  menu.appendChild(buildShareMenuBody(convo));
  // Hide/Unhide — only for Home-feed rows (feedModel.has is true for every Home row).
  if (feedModel.has(convo.id)) {
    const hideBtn = el('button', 'share-menu-link', feedIsHidden(convo.id) ? 'Unhide conversation' : 'Hide conversation');
    hideBtn.type = 'button';
    hideBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (feedIsHidden(convo.id)) await feedUnhideRoom(convo.id);
      else await feedHideRoom(convo.id);
      hideBtn.textContent = feedIsHidden(convo.id) ? 'Unhide conversation' : 'Hide conversation';
      menu.classList.add('hidden');
    });
    menu.appendChild(hideBtn);
  }
  const stop = (e) => e.stopPropagation();
  kebab.addEventListener('click', (e) => {
    e.stopPropagation();                               // don't open the conversation
    document.querySelectorAll('.share-menu:not(.hidden)')
      .forEach((m) => { if (m !== menu) m.classList.add('hidden'); });
    menu.classList.toggle('hidden');
  });
  menu.addEventListener('click', stop);
  menu.addEventListener('keydown', stop);
  holder.appendChild(kebab);
  holder.appendChild(menu);
  holder.addEventListener('click', stop);
  row.appendChild(holder);
  // one-time: click anywhere else closes any open sharing popover
  if (!window.__shareKebabCloser) {
    window.__shareKebabCloser = true;
    document.addEventListener('click', () => {
      document.querySelectorAll('.share-menu:not(.hidden)').forEach((m) => m.classList.add('hidden'));
    });
  }
}

// ---- global "Share All" switch ----

function buildGlobalSwitch() {
  const wrap = el('div', 'share-global-row');
  const label = el('div', 'share-global-label');
  label.appendChild(el('div', 'title', 'Share All'));
  label.appendChild(el('div', 'desc',
    'Mirror every conversation to the manager by default. Per-conversation and per-source settings still override this.'));
  wrap.appendChild(label);
  const btn = el('button', 'switch' + (policy.global === 'share-all' ? ' on' : ''));
  btn.type = 'button';
  btn.setAttribute('role', 'switch');
  btn.setAttribute('aria-checked', policy.global === 'share-all' ? 'true' : 'false');
  btn.appendChild(el('span', 'switch-knob'));
  const err = el('p', 'error hidden');
  btn.addEventListener('click', async () => {
    const next = policy.global === 'share-all' ? 'private' : 'share-all';
    err.classList.add('hidden');
    try { policy = await writeSharePolicy({ ...policy, global: next }); }
    catch (e) {                                   // F8: never a silent swallow
      err.textContent = 'Not saved — ' + sanitizeLine((e && e.message) || 'try again')
        + '. Still showing your last saved setting.';
      err.classList.remove('hidden');
      return;
    }
    btn.classList.toggle('on', policy.global === 'share-all');
    btn.setAttribute('aria-checked', policy.global === 'share-all' ? 'true' : 'false');
    renderConsentSummary();
  });
  wrap.appendChild(btn);
  wrap.appendChild(err);
  return wrap;
}

// ---- per-source "Share all <source>" tri-state switch ----

function buildSourceSwitchRow(source) {
  const row = el('div', 'share-source-row');
  const label = el('div', 'share-source-label');
  label.appendChild(el('span', 'plat-badge ' + source.id, ''));
  label.appendChild(document.createTextNode(' ' + source.label));
  row.appendChild(label);
  row.appendChild(buildSourcePolicySlider(source));
  return row;
}
function buildSourceSwitches() {
  const wrap = el('div', 'share-sources-list');
  for (const s of SOURCES) {
    if (s.kind === 'all') continue;
    wrap.appendChild(buildSourceSwitchRow(s));
  }
  return wrap;
}

// ---- bulk action per source (F11, later amended): "set all conversations
// in this source" ------------------------------------------------------------
// PURE: what a bulk Share/Private/Direct write on `convos` would do against
// the current `overridesMap`. Refuses anything not in BULK_SHARE_LEVELS.
// 'direct' was added to that set by a 2026-09-02 product-owner-approved
// reversal of F11's original "bulk never offers direct" disposition — see
// D3 in docs/superpowers/plans/2026-09-02-direct-share-level.md — gated on
// the caller side by a mandatory full-enumeration risk confirm
// (`requiresRiskConfirm` below; see bulkSetSourceLevel()). `overwritesPrivate`
// lists every convo id that is CURRENTLY an explicit 'private' override and
// would be changed by this bulk write ('share' and 'direct' both widen
// visibility, so both are checked) — the caller must list these in the
// confirm before writing; this function only computes the plan, it never
// writes.
const BULK_SHARE_LEVELS = new Set(['share', 'private', 'direct']);
function planBulkShareChange(convos, overridesMap, level) {
  if (!BULK_SHARE_LEVELS.has(level)) return null;
  const get = (id) => (overridesMap instanceof Map
    ? overridesMap.get(id)
    : (overridesMap && typeof overridesMap === 'object' ? overridesMap[id] : undefined));
  const ids = [];
  const overwritesPrivate = [];
  for (const c of (Array.isArray(convos) ? convos : [])) {
    if (!c || typeof c.id !== 'string' || !c.id) continue;
    ids.push(c.id);
    if ((level === 'share' || level === 'direct') && get(c.id) === 'private') overwritesPrivate.push(c.id);
  }
  return { level, ids, overwritesPrivate, requiresRiskConfirm: level === 'direct' };
}

async function bulkSetSourceLevel(source, level) {
  const convos = convosBySource[source.id] || [];
  const plan = planBulkShareChange(convos, overrides, level);
  if (!plan) return;                                    // refuses anything but share/private/direct
  const levelLabel = level === 'share' ? 'Share' : (level === 'direct' ? 'Direct' : 'Private');

  if (plan.requiresRiskConfirm) {
    // Full informed-consent confirm for bulk Direct (product-owner-approved
    // 2026-09-02 reversal of F11 — see D3): the SAME risk copy as the
    // single-conversation Direct confirm, PLUS every one of the affected
    // conversations enumerated by name (not just the explicit-private
    // overwrites — Direct demands total enumeration, since it is a much
    // bigger step than Share), PLUS the explicit-model note that this only
    // touches conversations that exist right now.
    const names = plan.ids.map((rid) => {
      const c = convos.find((x) => x.id === rid);
      return sanitizeLine((c && c.title) || rid);
    });
    let text = directRiskCopy('any of these ' + plan.ids.length + ' ' + source.label + ' conversations')
      + '\n\nThis will turn on Direct for all ' + plan.ids.length + ' ' + source.label
      + ' conversation(s), listed below in full:\n' + names.join('\n');
    if (plan.overwritesPrivate.length) {
      const privateNames = plan.overwritesPrivate.map((rid) => {
        const c = convos.find((x) => x.id === rid);
        return sanitizeLine((c && c.title) || rid);
      });
      text += '\n\nThese are currently set to Private and will change to Direct:\n' + privateNames.join('\n');
    }
    text += '\n\nThis affects only these EXISTING conversations. Any new '
      + source.label + ' conversation that arrives later stays Private until '
      + 'you set it explicitly.'
      + '\n\nTurn on Direct for all of these conversations?';
    const ok = await confirmModal('Turn on Direct for all ' + source.label + '?', text, false);
    if (!ok) return;
    for (const rid of plan.ids) {
      const convo = convos.find((x) => x.id === rid) || { id: rid };
      await writeShareOverride(convo.id, 'direct');
      overrides.set(convo.id, 'direct');
    }
    renderSharingView();
    return;
  }

  let text = 'Set all ' + plan.ids.length + ' ' + source.label + ' conversation(s) to ' + levelLabel + '?';
  // Never silently overwrite an explicit 'private' — list exactly what changes.
  if (plan.overwritesPrivate.length) {
    const names = plan.overwritesPrivate.map((rid) => {
      const c = convos.find((x) => x.id === rid);
      return sanitizeLine((c && c.title) || rid);
    });
    text += '\n\nThese are currently set to Private and will change to ' + levelLabel + ':\n' + names.join('\n');
  }
  const ok = await confirmModal('Set all to ' + levelLabel + '?', text, false);
  if (!ok) return;
  for (const rid of plan.ids) {
    await writeShareOverride(rid, level);
    overrides.set(rid, level);
  }
  renderSharingView();
}

function buildBulkShareRow(source) {
  const wrap = el('div', 'share-bulk-row');
  wrap.appendChild(el('span', 'muted', 'Set all ' + source.label + ' conversations:'));
  const shareBtn = el('button', 'share-bulk-btn', 'Share');
  shareBtn.type = 'button';
  shareBtn.addEventListener('click', () => bulkSetSourceLevel(source, 'share'));
  const directBtn = el('button', 'share-bulk-btn', 'Direct');
  directBtn.type = 'button';
  directBtn.title = 'Auto-send: your manager’s messages go out without your review';
  directBtn.addEventListener('click', () => bulkSetSourceLevel(source, 'direct'));
  const privateBtn = el('button', 'share-bulk-btn', 'Private');
  privateBtn.type = 'button';
  privateBtn.addEventListener('click', () => bulkSetSourceLevel(source, 'private'));
  wrap.appendChild(shareBtn);
  wrap.appendChild(directBtn);
  wrap.appendChild(privateBtn);
  return wrap;
}

// Mounted into the per-source view's header via setSourceViewHook (search.js).
function mountSourceSwitch(sourceId) {
  const host = $('source-share-switch');
  if (!host) return;
  const source = SOURCES.find((s) => s.id === sourceId);
  host.replaceChildren();
  // The per-source STANDING POLICY is dead under the explicit model — a
  // conversation no longer inherits from it. The bulk action below is a
  // deliberate, one-shot, per-conversation WRITE (never an inherited state),
  // offered here in its place.
  if (explicitModel()) {
    if (source) host.appendChild(buildBulkShareRow(source));
    else host.appendChild(el('span', 'muted', 'Updating to explicit levels…'));
    return;
  }
  if (source) host.appendChild(buildSourcePolicySlider(source));
}

// ---- consent summary panel (§4.2): truthfully what the manager can see now ----

function renderConsentSummary() {
  const host = $('share-summary');
  if (!host) return;
  host.replaceChildren();

  const convos = allConvos();
  const results = resolveAll(convos, policy, overrides, profileMap);
  const shared = results.filter((r) => r.shared);
  const seen = loadSeen();
  // "Newly" = currently shared WITHOUT a deliberate per-conversation action,
  // and not already surfaced in a previous render of this panel. Under the
  // explicit model that set is always empty — nothing can be auto-shared — and
  // 'direct' is excluded because it is a deliberate (separately confirmed)
  // per-conversation choice, not a standing policy sweeping a room in.
  const newlyShared = shared.filter((r) => r.reason !== 'explicit'
    && r.reason !== 'direct' && !seen.has(r.convo.id));

  if (!shared.length) {
    host.appendChild(el('p', 'muted', 'The manager can currently see: nothing.'));
  } else {
    const groups = new Map();
    for (const r of shared) {
      if (!groups.has(r.reason)) groups.set(r.reason, []);
      groups.get(r.reason).push(r.convo);
    }
    // Three levels, no inherit (F13): under the explicit model `reason` is
    // always exactly 'explicit' (Share) or 'direct' — nothing else can make a
    // conversation shared any more, so the copy states exactly that rather
    // than a generic reason list left over from the standing-policy model.
    const parts = [];
    if (groups.has('explicit')) parts.push(groups.get('explicit').length + ' shared');
    if (groups.has('direct')) parts.push(groups.get('direct').length + ' set to Direct');
    host.appendChild(el('p', '', 'The manager can currently see: ' + parts.join(', ') + '.'));
    if (groups.has('direct')) {
      host.appendChild(el('p', 'muted',
        'Direct conversations are sent by your manager as you, without your review. A '
        + 'compromised manager session or master server could send messages as you into '
        + 'these conversations, and recipients cannot tell the difference.'));
    }

    const list = el('div', 'share-summary-list');
    for (const r of shared) {
      const row = el('div', 'share-summary-row');
      row.appendChild(el('span', 'title', sanitizeLine(r.convo.title || r.convo.id)));
      row.appendChild(el('span', 'reason', reasonText(r)));
      list.appendChild(row);
    }
    host.appendChild(list);
  }

  // Explicit shares the uplink cannot honor (pm_mng-j92): the override points
  // at a room that is not in any connected source's conversation list — e.g.
  // a portal from a previous bridge login, or a community-space child. The
  // uplink silently skips those, so without this box the share LOOKS
  // successful while the manager never sees the conversation.
  const knownIds = new Set(convos.map((c) => c.id));
  const orphanShares = [...overrides.entries()]
    .filter(([rid, st]) => st === 'share' && !knownIds.has(rid));
  if (orphanShares.length) {
    const box = el('div', 'share-newly-box');
    box.appendChild(el('h3', '', 'Shared but not mirrorable'));
    box.appendChild(el('p', 'muted',
      'These are marked Share, but they no longer belong to any connected source '
      + '(for example a chat from a previous bridge login). The manager does NOT '
      + 'see them. Clear the stale share, or re-share the conversation from its '
      + 'current entry in the chat list.'));
    for (const [rid] of orphanShares) {
      const row = el('div', 'share-summary-row');
      row.appendChild(el('span', 'title', sanitizeLine(rid)));
      row.appendChild(el('span', 'reason', 'no source'));
      const btn = el('button', 'danger', 'Clear');
      btn.type = 'button';
      // F8: surfaced, never swallowed — row keeps last-known-good state.
      const rowErr = el('span', 'share-slider-error hidden');
      btn.addEventListener('click', async () => {
        try {
          await writeShareOverride(rid, null);
          overrides.delete(rid);
        } catch (e) {
          rowErr.textContent = 'Not saved — ' + sanitizeLine((e && e.message) || 'try again')
            + '. Still showing your last saved setting.';
          rowErr.classList.remove('hidden');
          return;
        }
        renderConsentSummary();
      });
      row.appendChild(rowErr);
      row.appendChild(btn);
      box.appendChild(row);
    }
    host.appendChild(box);
  }

  if (newlyShared.length) {
    const box = el('div', 'share-newly-box');
    box.appendChild(el('h3', '', 'Newly auto-shared'));
    box.appendChild(el('p', 'muted',
      'These became visible to the manager automatically under a standing "share all" policy. Exclude any you did not mean to share.'));
    for (const r of newlyShared) {
      const row = el('div', 'share-summary-row new');
      row.appendChild(el('span', 'title', sanitizeLine(r.convo.title || r.convo.id)));
      row.appendChild(el('span', 'reason', reasonText(r)));
      const btn = el('button', 'danger', 'Exclude');
      btn.type = 'button';
      // F8: surfaced, never swallowed — the load-bearing direction is exactly
      // this one (a revocation the user believes landed but did not).
      const rowErr = el('span', 'share-slider-error hidden');
      btn.addEventListener('click', async () => {
        try {
          await writeShareOverride(r.convo.id, 'private');
          overrides.set(r.convo.id, 'private');
        } catch (e) {
          rowErr.textContent = 'Not saved — ' + sanitizeLine((e && e.message) || 'try again')
            + '. Still showing your last saved setting.';
          rowErr.classList.remove('hidden');
          return;
        }
        renderConsentSummary();
      });
      row.appendChild(rowErr);
      row.appendChild(btn);
      box.appendChild(row);
    }
    host.appendChild(box);
  }
  // Whatever was shown (flagged-new or not) is now "seen" — the panel only
  // flags a share the FIRST time it appears in it.
  saveSeen(new Set([...seen, ...shared.map((r) => r.convo.id)]));
}

// How many of `convos` are SHARED right now, under the cached policy/overrides/
// profiles. Used by main.js's first-run auto-join confirm to state truthfully
// how many rooms it is about to accept would become visible to the manager.
// It is a COUNT for a prompt — never an authorization decision — and it still
// goes through the shared resolver rather than re-deriving precedence here.
// Each convo needs at least { id, sourceId } (sourceLabel only affects wording).
function countSharedNow(convos) {
  return resolveAll(convos, policy, overrides, profileMap).filter((r) => r.shared).length;
}

// ===========================================================================
// CONTACT-SHARE consent UI — a SEPARATE dimension from conversation sharing.
// Writes the contact-share policy (com.jkali.contact_share_policy) only; it
// NEVER touches `policy` / com.jkali.share_policy above. Mirrors the
// conversation global-switch + per-source tri-state pattern, but for the
// address book (people), and reads/writes through readContactPolicy /
// writeContactPolicy → the model's contactSharePolicyPath + normalizer.
// ===========================================================================

// Global "share all contacts" switch (writes contactPolicy.global).
function buildContactGlobalSwitch() {
  const wrap = el('div', 'share-global-row');
  const label = el('div', 'share-global-label');
  label.appendChild(el('div', 'title', 'Share all contacts'));
  wrap.appendChild(label);
  const btn = el('button', 'switch' + (contactPolicy.global === 'share-all' ? ' on' : ''));
  btn.type = 'button';
  btn.setAttribute('role', 'switch');
  btn.setAttribute('aria-label', 'Share my contacts with my manager');
  btn.setAttribute('aria-checked', contactPolicy.global === 'share-all' ? 'true' : 'false');
  btn.appendChild(el('span', 'switch-knob'));
  const err = el('p', 'error hidden');
  btn.addEventListener('click', async () => {
    const next = contactPolicy.global === 'share-all' ? 'private' : 'share-all';
    err.classList.add('hidden');
    try { contactPolicy = await writeContactPolicy({ ...contactPolicy, global: next }); }
    catch (e) {                                   // F8: never a silent swallow
      err.textContent = 'Not saved — ' + sanitizeLine((e && e.message) || 'try again')
        + '. Still showing your last saved setting.';
      err.classList.remove('hidden');
      return;
    }
    // Full re-render so the per-source "following global" hints update too.
    renderContactShareView();
  });
  wrap.appendChild(btn);
  wrap.appendChild(err);
  return wrap;
}

function contactSourcePolicyIndex(sourceId) {
  const cur = (contactPolicy.sources && contactPolicy.sources[sourceId]) || 'inherit';
  const idx = SOURCE_POLICY_CYCLE.findIndex((o) => o.val === cur);
  return idx >= 0 ? idx : 0;
}

function contactSourcePolicyHint(source) {
  const cur = (contactPolicy.sources && contactPolicy.sources[source.id]) || 'inherit';
  if (cur === 'share-all') return 'All ' + source.label + ' contacts → manager';
  if (cur === 'private-all') return 'All ' + source.label + ' contacts private';
  if (contactPolicy.global === 'share-all') return 'Following global (share all)';
  return 'Following global (private)';
}

function contactSourcePolicyShared(source) {
  return resolveContactShare(source.id, contactPolicy).shared;
}

function buildContactSourcePolicySlider(source) {
  return buildTriStateSlider(SOURCE_POLICY_CYCLE, {
    ariaLabel: 'Share ' + source.label + ' contacts',
    getIndex: () => contactSourcePolicyIndex(source.id),
    getHint: () => contactSourcePolicyHint(source),
    hintShared: () => contactSourcePolicyShared(source),
    onAdvance: async (opt) => {
      const sources = { ...(contactPolicy.sources || {}) };
      if (opt.val === 'inherit') delete sources[source.id]; else sources[source.id] = opt.val;
      contactPolicy = await writeContactPolicy({ ...contactPolicy, sources });
    },
  });
}

function buildContactSourceSwitchRow(source) {
  const row = el('div', 'share-source-row');
  const label = el('div', 'share-source-label');
  label.appendChild(el('span', 'plat-badge ' + source.id, ''));
  label.appendChild(document.createTextNode(' ' + source.label));
  row.appendChild(label);
  row.appendChild(buildContactSourcePolicySlider(source));
  return row;
}

// ===========================================================================
// PER-CONTACT OVERRIDES (per-contact-share plan, C3) — the most specific level
// of the contact dimension. The control unit is a HANDLE, `<source>|<network_id>`.
// Everything below is either PURE (unit-tested in plain node) or takes its I/O
// as an injected `io` object, so the write discipline can be proven without a
// browser: F3 (merge over a fresh read, never a blind PUT of a cached map),
// F5 (entry cap refused BEFORE any PUT), F7 (a fan-out key that would never
// apply is refused VISIBLY, never minted).
// ===========================================================================

// The source ids this app knows. A handle outside it can never become a valid
// override key (the uplink would not mirror that source anyway).
function knownSourceIds() {
  const out = new Set();
  for (const s of SOURCES) if (s.kind !== 'all') out.add(s.id);
  return out;
}

// PURE. Validate a /contacts/list response into rows the UI may render and key
// on — shape, known source, handle shape — mirroring enrich.js's discipline of
// never trusting the loopback helper's output blindly. Returns [] for junk.
function validImportedContacts(body, knownSources) {
  const known = knownSources instanceof Set ? knownSources : new Set();
  const rows = (body && Array.isArray(body.contacts)) ? body.contacts : [];
  const out = [];
  const seen = new Set();
  for (const r of rows) {
    if (!r || typeof r !== 'object' || Array.isArray(r)) continue;
    const source = typeof r.source === 'string' ? r.source : '';
    const network_id = typeof r.network_id === 'string' ? r.network_id : '';
    if (!known.has(source)) continue;
    if (!validHandle(network_id)) continue;
    const key = contactOverrideKey(source, network_id);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push({
      source,
      network_id,
      key,
      display_name: typeof r.display_name === 'string' ? r.display_name : '',
    });
  }
  return out;
}

// PURE (F7). One override key per LINKED HANDLE of a profile — but only for
// handles that pass the key spec + known-source gate AND reconcile against the
// imported address book. Everything else is reported as `unmatched` so the
// caller can refuse it VISIBLY: a 'private' that silently never applies is a
// leak the teammate believes closed, and a 'share' on a not-yet-imported handle
// is a dormant grant that would fire on the next import.
function planHandleFanOut(handles, importedKeys, knownSources) {
  const known = knownSources instanceof Set ? knownSources : new Set();
  const imported = importedKeys instanceof Set ? importedKeys : new Set();
  const keys = [];
  const unmatched = [];
  const seen = new Set();
  for (const h of (Array.isArray(handles) ? handles : [])) {
    const source = (h && typeof h.source === 'string') ? h.source : '';
    const network_id = (h && typeof h.network_id === 'string') ? h.network_id : '';
    const key = contactOverrideKey(source, network_id);
    if (!key || !known.has(source)) {
      unmatched.push({ source, network_id, reason: 'unknown source' });
      continue;
    }
    if (!imported.has(key)) {
      unmatched.push({ source, network_id, reason: 'not in your imported contacts' });
      continue;
    }
    if (seen.has(key)) continue;
    seen.add(key);
    keys.push(key);
  }
  return { keys, unmatched };
}

// PURE (F3/F5). What a set of changes would make the stored map, or a refusal.
//   current : the map from a FRESH read (never a cached one)
//   changes : [[key, 'share'|'private'|null], ...]   null clears the key
// Refuses BEFORE any write if the result would cross `cap` — a write that
// strictly REDUCES the entry count is always allowed, which is what keeps the
// over-cap recovery (remove one / clear all) in-app.
function planContactOverrideWrite(current, changes, cap) {
  const limit = typeof cap === 'number' ? cap : CONTACT_OVERRIDES_CAP;
  const src = (current && typeof current === 'object' && !Array.isArray(current)) ? current : {};
  const next = Object.create(null);
  for (const k of Object.keys(src)) {
    if (splitContactOverrideKey(k) && CONTACT_OVERRIDE_STATES.has(src[k])) next[k] = src[k];
  }
  const baseCount = Object.keys(next).length;
  const refused = [];
  for (const c of (Array.isArray(changes) ? changes : [])) {
    const key = Array.isArray(c) ? c[0] : null;
    const value = Array.isArray(c) ? c[1] : undefined;
    if (!splitContactOverrideKey(key)) { refused.push(key); continue; }
    if (value === null || value === undefined) { delete next[key]; continue; }
    if (!CONTACT_OVERRIDE_STATES.has(value)) { refused.push(key); continue; }
    next[key] = value;
  }
  const count = Object.keys(next).length;
  if (count > limit && count >= baseCount) {
    return { ok: false, reason: 'cap', cap: limit, count, baseCount, refused, next: null };
  }
  return { ok: true, reason: null, cap: limit, count, baseCount, refused, next };
}

// Are these changes destructive-only (removals)? The one thing still permitted
// while the STORED map is over the cap, so recovery never needs a raw client.
function isDestructiveOnly(changes) {
  const list = Array.isArray(changes) ? changes : [];
  if (!list.length) return false;
  return list.every((c) => Array.isArray(c) && (c[1] === null || c[1] === undefined));
}

// The guarded write path. `io` is { read, write } so tests can prove "a read
// that fails performs ZERO writes" without a homeserver. Never widens on a bad
// read, never blind-PUTs a cached map, and refuses over-cap before writing.
// Returns { ok, reason, wrote, overrides, plan } — `reason` is what the caller
// must SHOW (F8: a failure is never silent).
async function applyContactOverrides(io, changes) {
  let state;
  try {
    state = await io.read();
  } catch (e) {
    return { ok: false, reason: 'read', wrote: false, message: (e && e.message) || 'read failed' };
  }
  if (!state || state.status === 'error') {
    return { ok: false, reason: 'read', wrote: false, message: 'could not read your current settings' };
  }
  if (state.status === 'over-cap' && !isDestructiveOnly(changes)) {
    return { ok: false, reason: 'over-cap', wrote: false, cap: CONTACT_OVERRIDES_CAP,
      message: 'your saved per-contact list is over the ' + CONTACT_OVERRIDES_CAP
        + '-entry limit; remove entries (or clear them all) before adding more' };
  }
  const plan = planContactOverrideWrite(state.overrides, changes);
  if (!plan.ok) {
    return { ok: false, reason: 'cap', wrote: false, cap: plan.cap, count: plan.count, plan,
      message: 'that would store ' + plan.count + ' per-contact settings, over the '
        + plan.cap + '-entry limit; nothing was saved' };
  }
  try {
    await io.write(plan.next);
  } catch (e) {
    return { ok: false, reason: 'write', wrote: false, message: (e && e.message) || 'write failed' };
  }
  return { ok: true, reason: null, wrote: true, overrides: plan.next, plan };
}

// The same discipline for com.jkali.contact_profiles (F3): read fresh, mutate
// the FRESH store, write. A read failure performs ZERO writes — the old
// catch-all-to-empty + blind PUT could destroy the whole profile store on a blip.
async function saveProfilesGuarded(io, mutate) {
  let store;
  try {
    store = await io.read();
  } catch (e) {
    return { ok: false, reason: 'read', wrote: false, message: (e && e.message) || 'read failed' };
  }
  let next;
  try {
    next = mutate(store);
  } catch (e) {
    return { ok: false, reason: 'mutate', wrote: false, message: (e && e.message) || 'invalid change' };
  }
  try {
    return { ok: true, reason: null, wrote: true, store: await io.write(next) };
  } catch (e) {
    return { ok: false, reason: 'write', wrote: false, message: (e && e.message) || 'write failed' };
  }
}

// The real I/O bindings the app passes to the two helpers above.
const OVERRIDES_IO = { read: readContactOverrides, write: writeContactOverrides };
const PROFILES_IO = { read: readProfiles, write: writeProfiles };

// ---- module cache + the imported address book --------------------------------
// contactOverrides is a RENDER cache only. Every write re-reads first
// (applyContactOverrides), so this can never become the basis of a blind PUT.
let contactOverrides = Object.create(null);
let contactOverridesStatus = 'ok';     // 'ok' | 'over-cap' | 'error'
let importedContacts = [];
let importedContactsError = null;

// The teammate's own imported address book, via the loopback helper's
// origin-gated POST /contacts/list. Fail-soft with a VISIBLE reason: an
// unreachable helper must not look like "you have no contacts".
async function loadImportedContacts() {
  try {
    const r = await fetch((await sessionConnectBase()) + '/contacts/list',
      { method: 'POST', headers: SESSION_CONNECT_HEADERS, body: '{}' });
    if (!r.ok) {
      importedContacts = [];
      importedContactsError = 'the local contacts helper returned ' + r.status;
      return;
    }
    const body = await r.json().catch(() => null);
    importedContacts = validImportedContacts(body, knownSourceIds());
    importedContactsError = null;
  } catch (e) {
    importedContacts = [];
    importedContactsError = 'the local contacts helper is not running';
  }
}

function overrideKeyCount() {
  return Object.keys(contactOverrides).length;
}

// The imported address book as a key set — what a profile fan-out reconciles
// its linked handles against (F7).
function importedContactKeys() {
  return new Set(importedContacts.map((c) => c.key));
}

// Run one override change set, refresh the cache from what actually landed, and
// hand the caller a message to SHOW on refusal (F5/F7/F8 — never silent).
async function commitOverrides(changes) {
  const res = await applyContactOverrides(OVERRIDES_IO, changes);
  if (res.ok) {
    contactOverrides = res.overrides;
    contactOverridesStatus = 'ok';
  }
  return res;
}

// The active-override count + clear-all, shown wherever overrides are edited.
// The count deliberately includes keys that match no imported contact — those
// are exactly the ones a teammate cannot otherwise find to remove (F7).
function buildOverrideSummary(onChange) {
  const box = el('div', 'contact-override-summary');
  const total = overrideKeyCount();
  const importedKeys = new Set(importedContacts.map((c) => c.key));
  const orphan = Object.keys(contactOverrides).filter((k) => !importedKeys.has(k)).length;
  if (contactOverridesStatus === 'error') {
    box.appendChild(el('p', 'error',
      'Your per-contact settings could not be read, so these controls are '
      + 'disabled. Nothing was changed. Reload once your homeserver responds.'));
    return box;
  }
  if (contactOverridesStatus === 'over-cap') {
    box.appendChild(el('p', 'error',
      'Your saved per-contact list is over the ' + CONTACT_OVERRIDES_CAP
      + '-entry limit, so it is not being applied. Remove entries, or clear them '
      + 'all below, to start applying it again.'));
  }
  box.appendChild(el('p', 'muted', total + ' contact(s) set individually'
    + (orphan ? ' (' + orphan + ' not in your imported contacts)' : '') + '.'));
  const err = el('p', 'error hidden');
  if (total) {
    const clear = el('button', 'danger', 'Clear all per-contact settings');
    clear.type = 'button';
    clear.addEventListener('click', async () => {
      const ok = await confirmModal('Clear all per-contact settings?',
        'This removes all ' + total + ' individual contact settings. Each contact '
        + 'goes back to following its source setting — which may mean sharing it '
        + 'again. Contacts already mirrored are not un-sent.', false);
      if (!ok) return;
      clear.disabled = true;
      const res = await commitOverrides(Object.keys(contactOverrides).map((k) => [k, null]));
      clear.disabled = false;
      if (!res.ok) {
        err.textContent = 'Not saved — ' + sanitizeLine(res.message || 'try again') + '.';
        err.classList.remove('hidden');
        return;
      }
      if (onChange) onChange();
    });
    box.appendChild(clear);
  }
  box.appendChild(err);
  return box;
}

// One imported contact: display name AND network_id, always both (P3 — two
// contacts can carry visually identical names, and the handle is what the
// override actually keys on), plus the three-way control.
function buildImportedContactRow(row, onChange) {
  const wrap = el('div', 'contact-override-row');
  const meta = el('span', 'contact-row-meta');
  const src = SOURCES.find((s) => s.id === row.source);
  meta.appendChild(el('span', 'title', sanitizeLine(row.display_name || 'Unnamed')));
  meta.appendChild(el('span', 'muted',
    ' ' + (src ? src.label : sanitizeLine(row.source)) + ' · ' + sanitizeLine(row.network_id)));
  wrap.appendChild(meta);

  const current = contactOverrides[row.key];
  const inherited = resolveContactShare(row.source, contactPolicy);
  const err = el('p', 'error hidden');
  const toggle = el('span', 'share-toggle');
  const disabled = contactOverridesStatus === 'error';
  for (const [val, label] of [['share', 'Share'], [null, 'Auto'], ['private', 'Private']]) {
    const active = (val === null) ? !current : current === val;
    const b = el('button', 'share-opt' + (active ? ' active' : ''), label);
    b.type = 'button';
    b.disabled = disabled;
    b.addEventListener('click', async () => {
      err.classList.add('hidden');
      b.disabled = true;
      const res = await commitOverrides([[row.key, val]]);
      b.disabled = disabled;
      if (!res.ok) {                                  // F8: visible, never silent
        err.textContent = 'Not saved — ' + sanitizeLine(res.message || 'try again')
          + '. Still showing your last saved setting.';
        err.classList.remove('hidden');
        return;
      }
      if (onChange) onChange();
    });
    toggle.appendChild(b);
  }
  wrap.appendChild(toggle);
  wrap.appendChild(el('span', 'share-slider-hint' + (
    (current ? current === 'share' : inherited.shared) ? ' shared' : ''),
  current
    ? (current === 'share' ? 'Shared — overrides the ' + row.source + ' setting for this contact only'
      : 'Private — overrides the ' + row.source + ' setting for this contact only')
    : (inherited.shared ? 'Following ' + row.source + ' (shared)' : 'Following ' + row.source + ' (private)')));
  wrap.appendChild(err);
  return wrap;
}

// The imported-contacts panel: the surface that covers store-only contacts (a
// contact with no conversation, e.g. an alerting bot) which no conversation row
// could ever reach.
function renderImportedContactsPanel(host) {
  const box = el('div', 'contact-override-panel');
  box.appendChild(el('h3', '', 'Individual contacts'));
  box.appendChild(el('p', 'muted',
    'Each contact below can override the source setting above for that contact '
    + 'only. Turning a contact off stops sharing it and removes it from your '
    + 'manager’s list; it cannot un-send what was already mirrored.'));
  box.appendChild(buildOverrideSummary(() => renderContactShareView()));
  if (importedContactsError) {
    box.appendChild(el('p', 'error',
      'Your imported contacts could not be listed — ' + sanitizeLine(importedContactsError)
      + '. Per-contact settings you already saved are still in force.'));
  } else if (!importedContacts.length) {
    box.appendChild(el('p', 'muted', 'No imported contacts yet.'));
  } else {
    const list = el('div', 'contact-override-list');
    for (const row of importedContacts) {
      list.appendChild(buildImportedContactRow(row, () => renderContactShareView()));
    }
    box.appendChild(list);
  }
  host.appendChild(box);
}

// Rendered into #share-contacts (its own settings-block) whenever the sharing
// view opens. Default/empty policy renders as PRIVATE (every switch off) —
// resolveContactShare returns not-shared for an absent/default policy.
function renderContactShareView() {
  const host = $('share-contacts');
  if (!host) return;
  host.replaceChildren();

  const head = el('h3', 'share-contacts-title', 'Share my contacts with my manager');
  head.style.margin = '0 0 6px';
  host.appendChild(head);
  host.appendChild(el('p', 'muted',
    'Separate from conversation sharing above: this shares your ADDRESS BOOK — '
    + 'the people in your contacts, not just your conversations — with your '
    + 'manager. It is OFF by default; nothing about who you know leaves this '
    + 'machine until you turn it on.'));

  host.appendChild(buildContactGlobalSwitch());

  // F2 RETRACTION HONESTY. The old copy claimed turning this off "removes the
  // contacts already shared" — it does not. The uplink tombstones the contact
  // state events, but a tombstone is itself a state event: prior content stays
  // retrievable from room history to anyone already joined. Say so.
  host.appendChild(el('p', 'muted',
    'Turning a contact off stops sharing it and removes it from your manager’s '
    + 'list; it cannot un-send what was already mirrored. Anyone already in the '
    + 'shared contacts room can still read the earlier copy from that room’s '
    + 'history.'));

  // Optional per-source contact sharing (mirrors the per-source conversation
  // switches): each source can override the global with share-all / private-all
  // / inherit ("Auto").
  const list = el('div', 'share-sources-list');
  for (const s of SOURCES) {
    if (s.kind === 'all') continue;
    list.appendChild(buildContactSourceSwitchRow(s));
  }
  host.appendChild(list);

  // The most specific level, last: per-contact overrides over the per-source
  // switches above.
  renderImportedContactsPanel(host);
}

// ---- master-identity re-confirm affordance (D2.11/F12) -----------------------
// Rendered only when readDirectSendSuspension() (loadConsentState) found a
// com.jkali.direct_send_suspended event. The teammate's confirm writes
// com.jkali.direct_send_ack with the SAME identity tuple (directSendAckContent);
// S3's uplink resumes auto-send only when that ack matches its current binding.
function directSendSuspensionBanner() {
  const a = directSendSuspension;
  if (!a) return null;
  const box = el('div', 'share-model-banner share-reconfirm-banner');
  box.appendChild(el('h3', '', 'Manager identity changed — Direct auto-send is paused'));
  box.appendChild(el('p', 'muted',
    'Auto-send was paused because the manager account or master server this app '
    + 'talks to changed. New identity: manager ' + sanitizeLine(a.manager_mxid)
    + ' on master server ' + sanitizeLine(a.master_hs)
    + ' (master account ' + sanitizeLine(a.master_user) + '). Confirm you recognize '
    + 'this change to resume Direct auto-send for any conversation set to Direct.'));
  const btn = el('button', 'share-bulk-btn primary', 'Confirm and resume');
  btn.type = 'button';
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      await ackDirectSendSuspension(a);
      directSendSuspension = null;
      renderSharingView();
    } catch (e) { btn.disabled = false; }
  });
  box.appendChild(btn);
  return box;
}

// ---- migrated-shares review (D0/F11) ------------------------------------------
// One-time (per room, per browser profile — dismissal is convenience-only
// localStorage) list of conversations D0's migration converted from a
// standing policy into an explicit 'share' override, so that conversion is
// surfaced rather than silent.
function renderMigratedReview() {
  const host = $('share-migrated');
  if (!host) return;
  host.replaceChildren();
  if (!explicitModel()) return;
  const dismissed = loadMigratedDismissed();
  const pending = migratedRoomIds.filter((rid) => !dismissed.has(rid));
  if (!pending.length) return;

  const box = el('div', 'share-model-banner share-migrated-box');
  box.appendChild(el('h3', '', 'Review migrated shares'));
  box.appendChild(el('p', 'muted',
    'These conversations were converted from a standing "share all" policy into '
    + 'an explicit Share when this app moved to per-conversation sharing. Review '
    + 'each one: keep it Shared, set it Private, or (with confirmation) Direct.'));
  const dismissOne = (rid) => { dismissed.add(rid); saveMigratedDismissed(dismissed); renderSharingView(); };
  for (const rid of pending) {
    const convo = allConvos().find((c) => c.id === rid);
    const row = el('div', 'share-summary-row');
    row.appendChild(el('span', 'title', sanitizeLine((convo && convo.title) || rid)));
    const keepBtn = el('button', 'share-bulk-btn', 'Keep Share');
    keepBtn.type = 'button';
    keepBtn.addEventListener('click', () => dismissOne(rid));
    row.appendChild(keepBtn);
    const privateBtn = el('button', 'share-bulk-btn', 'Set Private');
    privateBtn.type = 'button';
    privateBtn.addEventListener('click', async () => {
      await writeShareOverride(rid, 'private');
      overrides.set(rid, 'private');
      dismissOne(rid);
    });
    row.appendChild(privateBtn);
    if (convo) {
      const directBtn = el('button', 'share-bulk-btn', 'Set Direct…');
      directBtn.type = 'button';
      directBtn.addEventListener('click', async () => {
        if (await escalateToDirect(convo)) dismissOne(rid);
      });
      row.appendChild(directBtn);
    }
    box.appendChild(row);
  }
  const dismissAll = el('button', 'share-bulk-btn', 'Dismiss all');
  dismissAll.type = 'button';
  dismissAll.addEventListener('click', () => {
    for (const rid of pending) dismissed.add(rid);
    saveMigratedDismissed(dismissed);
    renderSharingView();
  });
  box.appendChild(dismissAll);
  host.appendChild(box);
}

// Rendered whenever the 'sharing' nav view opens, via setSharingViewHook (nav.js).
// The two conversation standing-policy controls (global Share-All, per-source
// cycle) are replaced by a banner once the daemon reports the explicit model —
// they can no longer share anything, so continuing to show them would claim a
// sharing state that is not real. The CONTACT-share controls below are a
// different consent dimension and are unaffected.
function renderSharingView() {
  const reconfirmHost = $('share-reconfirm');
  if (reconfirmHost) {
    reconfirmHost.replaceChildren();
    const banner = directSendSuspensionBanner();
    if (banner) reconfirmHost.appendChild(banner);
  }
  const globalHost = $('share-global');
  if (globalHost) {
    globalHost.replaceChildren();
    if (explicitModel()) {
      globalHost.appendChild(modelBanner('Updating to explicit levels…',
        'Standing "share all" policies no longer share anything: each '
        + 'conversation is now shared, or private, entirely on its own. Share a '
        + 'conversation from the ⋯ menu on its row. The full set of levels '
        + 'arrives in the next update.'));
    } else {
      globalHost.appendChild(pendingModelBanner());
      globalHost.appendChild(buildGlobalSwitch());
    }
  }
  const sourcesHost = $('share-sources');
  if (sourcesHost) {
    sourcesHost.replaceChildren();
    sourcesHost.appendChild(explicitModel()
      ? modelBanner('Updating to explicit levels…',
        'Per-source conversation sharing has been removed. Existing shares were '
        + 'kept: any conversation that a standing policy was sharing has been '
        + 'written down as an explicit share you can review and change per '
        + 'conversation.')
      : buildSourceSwitches());
  }
  renderMigratedReview();
  renderConsentSummary();
  renderContactShareView();
}

// ---- entry point (call once from apps/user/main.js after sign-in) ----
async function initConsentUI() {
  setConvoRowDecorator(decorateRow);
  setSourceViewHook(mountSourceSwitch);
  setSharingViewHook(renderSharingView);
  await loadConsentState();
}

export {
  initConsentUI, renderSharingView, loadConsentState, countSharedNow,
  planBulkShareChange, migratedRoomIdsFromSync,
  suspensionAffordance, directSendAckContent,
  knownSourceIds, validImportedContacts, planHandleFanOut,
  planContactOverrideWrite, isDestructiveOnly,
  applyContactOverrides, saveProfilesGuarded,
  OVERRIDES_IO, PROFILES_IO,
  commitOverrides, buildOverrideSummary, importedContactKeys,
};
