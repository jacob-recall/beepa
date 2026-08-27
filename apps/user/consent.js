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
import { convosBySource } from '../../shared/state.js';

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

// ---- per-row: effective-state badge + tri-state override toggle ----

function buildShareBadge(convo) {
  const r = effectiveFor(convo);
  return el('span', 'share-badge ' + (r.shared ? 'shared' : 'private'), reasonText(r));
}

function buildShareToggle(convo, badgeEl) {
  const wrap = el('span', 'share-toggle');
  const opts = [['share', 'Share'], ['inherit', 'Auto'], ['private', 'Private']];
  const buttons = [];
  function refresh() {
    const cur = overrides.get(convo.id) || 'inherit';
    for (const [val, b] of buttons) b.classList.toggle('active', val === cur);
    const r = effectiveFor(convo);
    badgeEl.textContent = reasonText(r);
    badgeEl.className = 'share-badge ' + (r.shared ? 'shared' : 'private');
  }
  for (const [val, label] of opts) {
    const b = el('button', 'share-opt', label);
    b.type = 'button';
    b.setAttribute('aria-label', label + ' ' + sanitizeLine(convo.title || convo.id));
    b.addEventListener('click', async (e) => {
      e.stopPropagation();
      const state = val === 'inherit' ? null : val;
      try {
        await writeShareOverride(convo.id, state);
        if (state) overrides.set(convo.id, state); else overrides.delete(convo.id);
      } catch (err) { /* leave the row as-is on failure */ return; }
      refresh();
    });
    buttons.push([val, b]);
    wrap.appendChild(b);
  }
  refresh();
  return wrap;
}

// Registered via setConvoRowDecorator (rows.js) / feed rows: appends the
// badge + toggle to an already-built row, and swallows clicks so they never
// open the conversation underneath.
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
  const badge = buildShareBadge(convo);
  menu.appendChild(badge);
  menu.appendChild(buildShareToggle(convo, badge));
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
  label.appendChild(el('span', 'ic', source.icon || ''));
  label.appendChild(document.createTextNode(' ' + source.label));
  row.appendChild(label);
  const wrap = el('span', 'share-toggle');
  const opts = [['share-all', 'Share all'], ['inherit', 'Auto'], ['private-all', 'Private all']];
  const buttons = [];
  function refresh() {
    const cur = (policy.sources && policy.sources[source.id]) || 'inherit';
    for (const [val, b] of buttons) b.classList.toggle('active', val === cur);
  }
  for (const [val, lbl] of opts) {
    const b = el('button', 'share-opt', lbl);
    b.type = 'button';
    b.addEventListener('click', async () => {
      const sources = { ...(policy.sources || {}) };
      if (val === 'inherit') delete sources[source.id]; else sources[source.id] = val;
      try { policy = await writeSharePolicy({ ...policy, sources }); }
      catch (e) { return; }
      refresh();
      renderConsentSummary();
    });
    buttons.push([val, b]);
    wrap.appendChild(b);
  }
  refresh();
  row.appendChild(wrap);
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
  if (source) host.appendChild(buildSourceSwitchRow(source));
}

// ---- consent summary panel (§4.2): truthfully what the manager can see now ----

function renderConsentSummary() {
  const host = $('share-summary');
  if (!host) return;
  host.replaceChildren();

  const results = resolveAll(allConvos(), policy, overrides, profileMap);
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

export { initConsentUI, renderSharingView, loadConsentState };
