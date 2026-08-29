// PLAN-MASTER-SYNC §5.1 (share controls) / §4.2 (trust guards). apps/user-only:
// global Share All switch, per-source Share-all-<source> switch, per-row
// tri-state share toggle (with effective state + reason), and the consent
// summary panel. Reuses shared/model/consent.js for all resolution + storage;
// wires into shared/ui/{rows,search,nav}.js via their app-injection hooks so no
// shared module needs to know apps/user exists. textContent-only, no innerHTML,
// no CSP change. Local state + UI ONLY — the uplink (Phase 2) is not built here.

import {
  resolve, resolveAll,
  readSharePolicy, writeSharePolicy,
  writeShareOverride, overridesFromSync,
} from '../../shared/model/consent.js';
import { readProfiles, roomProfileMap } from '../../shared/model/contacts.js';
import { api } from '../../shared/matrix/client.js';
import { $, el, sanitizeLine } from '../../shared/ui/el.js';
import { setConvoRowDecorator } from '../../shared/ui/rows.js';
import { setSourceViewHook } from '../../shared/ui/search.js';
import { setSharingViewHook } from '../../shared/ui/nav.js';
import { SOURCES } from '../../shared/ui/sources.js';
import { convosBySource, feedModel } from '../../shared/state.js';
import { feedHideRoom, feedUnhideRoom, feedIsHidden } from '../../shared/ui/account-data.js';

// Local cache of the two consent-storage reads (§5.2). Writes below update it
// in place so rows/panels reflect the change immediately, with no re-fetch.
let policy = { global: 'private', sources: {} };
const overrides = new Map(); // roomId -> 'share' | 'private' (absent = inherit)
// roomId -> { id, displayName, share } for the room's contact profile, if any
// (§12 phase 5). Populated from shared/model/contacts.js account-data; a
// profile 'share'/'private' outranks per-source/global but a per-conversation
// override above still wins (see shared/model/consent.js §4 precedence).
let profileMap = {};

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
async function loadConsentState() {
  try { policy = await readSharePolicy(); } catch (e) { /* keep previous cache */ }
  try {
    const map = overridesFromSync(await fetchOverridesSnapshot());
    overrides.clear();
    for (const k of Object.keys(map)) overrides.set(k, map[k]);
  } catch (e) { /* keep previous cache */ }
  try { profileMap = roomProfileMap(await readProfiles()); } catch (e) { /* keep previous cache */ }
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

function reasonText(r) {
  if (r.shared) return r.reason === 'explicit' ? 'shared (explicit)' : 'shared (' + r.reason + ')';
  return r.reason === 'excluded' ? 'private (excluded)' : 'private';
}

// ---- sliding tri-state control (kebab rows + per-source headers) ----

const SHARE_CYCLE = [
  { val: 'inherit', label: 'Auto', override: null },
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
  track.appendChild(el('span', 'share-slider-thumb'));
  const segs = el('span', 'share-slider-segs');
  for (const o of cycle) segs.appendChild(el('span', 'share-slider-seg', o.label));
  track.appendChild(segs);
  btn.appendChild(track);

  const hint = el('span', 'share-slider-hint');
  btn.appendChild(hint);

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
      if (next === getIndex()) return;                 // clicked the current state
    } else {
      next = (getIndex() + 1) % cycle.length;
    }
    try {
      const ok = await onAdvance(cycle[next]);
      if (ok === false) return;
    } catch (err) { return; }
    refresh();
  });

  refresh();
  return btn;
}

function shareCycleIndex(convo) {
  const cur = overrides.get(convo.id) || 'inherit';
  const idx = SHARE_CYCLE.findIndex((o) => o.val === cur);
  return idx >= 0 ? idx : 0;
}

function buildShareSlider(convo) {
  return buildTriStateSlider(SHARE_CYCLE, {
    ariaLabel: 'Sharing for ' + sanitizeLine(convo.title || convo.id),
    getIndex: () => shareCycleIndex(convo),
    getHint: () => reasonText(effectiveFor(convo)),
    hintShared: () => effectiveFor(convo).shared,
    onAdvance: async (opt) => {
      await writeShareOverride(convo.id, opt.override);
      if (opt.override) overrides.set(convo.id, opt.override); else overrides.delete(convo.id);
    },
  });
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
  menu.appendChild(buildShareSlider(convo));
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
  btn.addEventListener('click', async () => {
    const next = policy.global === 'share-all' ? 'private' : 'share-all';
    try { policy = await writeSharePolicy({ ...policy, global: next }); }
    catch (e) { return; }
    btn.classList.toggle('on', policy.global === 'share-all');
    btn.setAttribute('aria-checked', policy.global === 'share-all' ? 'true' : 'false');
    renderConsentSummary();
  });
  wrap.appendChild(btn);
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

// Mounted into the per-source view's header via setSourceViewHook (search.js).
function mountSourceSwitch(sourceId) {
  const host = $('source-share-switch');
  if (!host) return;
  const source = SOURCES.find((s) => s.id === sourceId);
  host.replaceChildren();
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
  // "Newly" = currently shared, not via an explicit per-conversation share (that
  // was a deliberate one-at-a-time action, not an auto-share), and not already
  // surfaced in a previous render of this panel.
  const newlyShared = shared.filter((r) => r.reason !== 'explicit' && !seen.has(r.convo.id));

  if (!shared.length) {
    host.appendChild(el('p', 'muted', 'The manager can currently see: nothing.'));
  } else {
    const groups = new Map();
    for (const r of shared) {
      if (!groups.has(r.reason)) groups.set(r.reason, []);
      groups.get(r.reason).push(r.convo);
    }
    const parts = [];
    for (const [reason, list] of groups) {
      parts.push(reason === 'explicit'
        ? list.length + ' individually shared'
        : reason + ' (' + list.length + ')');
    }
    host.appendChild(el('p', '', 'The manager can currently see: ' + parts.join(', ') + '.'));

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
      btn.addEventListener('click', async () => {
        try {
          await writeShareOverride(rid, null);
          overrides.delete(rid);
        } catch (e) { return; }
        renderConsentSummary();
      });
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
      btn.addEventListener('click', async () => {
        try {
          await writeShareOverride(r.convo.id, 'private');
          overrides.set(r.convo.id, 'private');
        } catch (e) { return; }
        renderConsentSummary();
      });
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

// Rendered whenever the 'sharing' nav view opens, via setSharingViewHook (nav.js).
function renderSharingView() {
  const globalHost = $('share-global');
  if (globalHost) { globalHost.replaceChildren(); globalHost.appendChild(buildGlobalSwitch()); }
  const sourcesHost = $('share-sources');
  if (sourcesHost) { sourcesHost.replaceChildren(); sourcesHost.appendChild(buildSourceSwitches()); }
  renderConsentSummary();
}

// ---- entry point (call once from apps/user/main.js after sign-in) ----
async function initConsentUI() {
  setConvoRowDecorator(decorateRow);
  setSourceViewHook(mountSourceSwitch);
  setSharingViewHook(renderSharingView);
  await loadConsentState();
}

export { initConsentUI, renderSharingView, loadConsentState, countSharedNow };
