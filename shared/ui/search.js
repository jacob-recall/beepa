import { feedRelTime } from '../model/message_preview.js';
// Relocated verbatim from hub/site/app.js (PLAN-MASTER-SYNC-IMPL P1.2).
// Shared ES module. Logic unchanged; only import/export + shared-state (S) access added.

import { feedIsHidden, refreshConvos } from './account-data.js';
import { $, el, sanitizeLine } from './el.js';
import { buildConvoRow, buildFeedRow, buildClusterRow, buildSubRow, rowFlags, elEmpty } from './rows.js';
import { clusterFeed, applyFilter, indicatorsFor } from '../model/attention.js';
import { SOURCES } from '../model/source_catalog.js';
import { S, convosBySource, feedModel } from '../state.js';

// Optional app-injected hook: called with the sourceId whenever the per-source
// view is (re)loaded, so apps/user can mount its "Share all <source>" switch
// (PLAN-MASTER-SYNC §5.1) into that view's header without shared/ importing
// from apps/ (same hook pattern as setConvoRowDecorator in rows.js).
let sourceViewHook = null;
function setSourceViewHook(fn) { sourceViewHook = typeof fn === 'function' ? fn : null; }

const feedRenderHooks = [];
function setFeedRenderHook(fn) { if (typeof fn === 'function') feedRenderHooks.push(fn); }
function runFeedRenderHooks() { for (const fn of feedRenderHooks) { try { fn(); } catch (e) { /* one app hook must not break the list */ } } }

// HF-5: coalesce renders — one timer per batch so a burst = one re-render.
function scheduleFeedRender() {
  if (S.feedRenderScheduled) return;
  S.feedRenderScheduled = true;
  setTimeout(() => {
    S.feedRenderScheduled = false;
    if (S.sourceViewId && S.activeNavKey === 'source:' + S.sourceViewId) {
      renderSourceList();
      runFeedRenderHooks();
    } else {
      renderHome();
    }
  }, 0);
}

function itemFlags(it) {
  return it.kind === 'single' ? rowFlags(it.rec) : indicatorsFor({ unread: it.unread, draft: it.draft });
}
// HF-8: render the merged feed sorted by recency, capped at ~200 items, with
// Triage Rail clustering (one row per contact profile, via S.roomProfile) and
// the Needs you / All / Drafts filter (S.feedFilter). #home-search is a pure
// client-side filter over the in-memory model; it never builds a URL, sends a
// command, or navigates.
function renderHome() {
  const list = $('list-body');
  if (!list) return;
  // #list-body is shared by Home, the per-source lists, and People. Guard
  // against clobbering a non-Home view the user is currently looking at.
  const k = S.activeNavKey;
  if (k && (k.indexOf('source:') === 0 || k === 'people' || k === 'settings')) return;
  ensureHomeFilters();
  const q = (($('home-search') && $('home-search').value) || '').trim().toLowerCase();
  const all = [...feedModel.values()].sort((a, b) => b.lastTs - a.lastTs);
  // HF-9: hidden rooms (low-priority/muted/manual) stay in feedModel but are
  // excluded from the default list; the chip reveals them.
  const visible = S.feedShowHidden ? all : all.filter(r => !feedIsHidden(r.id));
  const matched = q
    ? visible.filter(r => sanitizeLine(r.name).toLowerCase().includes(q) ||
                          sanitizeLine(r.lastBody || '').toLowerCase().includes(q))
    : visible;
  const items = applyFilter(clusterFeed(matched, S.roomProfile), S.feedFilter, itemFlags).slice(0, 200);
  list.replaceChildren();
  if (!items.length) {
    list.appendChild(elEmpty(q ? 'No conversations match your search.'
      : (S.feedFilter === 'drafts' ? 'No suggestions waiting.' : 'No conversations yet.')));
    runFeedRenderHooks();
    return;
  }
  for (const it of items) {
    if (it.kind === 'single') { list.appendChild(buildFeedRow(it.rec)); continue; }
    list.appendChild(buildClusterRow(it));
    if (S.expandedClusters.has(it.profileId)) for (const m of it.members) list.appendChild(buildSubRow(m));
  }
  runFeedRenderHooks();
}

// Chips above the Home list: Needs you / All / Drafts + Show hidden. Built once
// with el()/textContent; pure client-side state (S.feedFilter, S.feedShowHidden).
const FILTERS = [['needs', 'Needs you'], ['all', 'All'], ['drafts', 'Drafts']];
function ensureHomeFilters() {
  const list = $('list-body');
  if (!list || !list.parentNode) return;
  let bar = $('home-filters');
  if (!bar) {
    bar = el('div', 'home-filters');
    bar.id = 'home-filters';
    for (const [key, label] of FILTERS) {
      const chip = el('button', 'chip', label);
      chip.type = 'button'; chip.dataset.filter = key;
      chip.addEventListener('click', () => { S.feedFilter = key; renderHome(); });
      bar.appendChild(chip);
    }
    const hidden = el('button', 'chip chip-hidden');
    hidden.type = 'button'; hidden.id = 'home-hidden-toggle';
    hidden.addEventListener('click', () => { S.feedShowHidden = !S.feedShowHidden; renderHome(); });
    bar.appendChild(hidden);
    list.parentNode.insertBefore(bar, list);
  }
  let needs = 0, drafts = 0;
  for (const r of feedModel.values()) { const f = rowFlags(r); if (f.length) needs++; if (f.includes('draft')) drafts++; }
  for (const chip of bar.querySelectorAll('.chip[data-filter]')) {
    const key = chip.dataset.filter;
    chip.classList.toggle('on', S.feedFilter === key);
    chip.setAttribute('aria-pressed', S.feedFilter === key ? 'true' : 'false');
    chip.textContent = key === 'needs' ? 'Needs you · ' + needs : key === 'drafts' ? 'Drafts · ' + drafts : 'All';
  }
  const hidden = $('home-hidden-toggle');
  if (hidden) { hidden.textContent = S.feedShowHidden ? 'Hide hidden' : 'Show hidden'; hidden.classList.toggle('on', S.feedShowHidden); }
}

async function loadSourceList(sourceId) {
  const source = SOURCES.find(s => s.id === sourceId);
  const list = $('list-body');
  if (!list) return;
  S.sourceViewId = sourceId;
  const search = $('source-search');
  if (search) {
    search.value = '';
    search.placeholder = 'Search ' + (source ? source.label : 'conversations');
  }
  // Render from the in-memory cache (convosBySource, built once by the initial
  // seed and kept fresh by the periodic re-seed + live feed sync) — NO per-click
  // snapshot fetch, so switching sources is instant. Only fetch the one time the
  // cache has never been built (a click before the first seed finished).
  if (!convosBySource[sourceId]) {
    list.replaceChildren();
    list.appendChild(elEmpty('Loading…'));
    try {
      await refreshConvos();
    } catch (e) {
      list.replaceChildren(elEmpty('Could not load conversations: ' + String(e.message || e)));
      return;
    }
  }
  renderSourceList();
  runFeedRenderHooks();
}

// #source-search is a pure client-side filter over the loaded per-source list,
// mirroring #home-search: matches on the conversation title + preview only.
function renderSourceList() {
  const list = $('list-body');
  if (!list || !S.sourceViewId) return;
  if (sourceViewHook) sourceViewHook(S.sourceViewId);
  const source = SOURCES.find(s => s.id === S.sourceViewId);
  // Sort most-recent-first (by last-activity ts); ties/no-message rooms fall to
  // the bottom. A copy — never reorder the shared convosBySource array in place.
  const convos = (convosBySource[S.sourceViewId] || [])
    .slice().sort((a, b) => (b.lastTs || 0) - (a.lastTs || 0));
  list.replaceChildren();
  if (!convos.length) {
    list.appendChild(elEmpty('No conversations yet on ' + (source ? source.label : 'this service') + '.'));
    return;
  }
  const q = (($('source-search') && $('source-search').value) || '').trim().toLowerCase();
  const rows = q
    ? convos.filter(c => sanitizeLine(c.title || '').toLowerCase().includes(q) ||
                         sanitizeLine(c.sub || '').toLowerCase().includes(q))
    : convos;
  if (!rows.length) { list.appendChild(elEmpty('No conversations match "' + q + '".')); return; }
  for (const c of rows) list.appendChild(buildConvoRow(c, false));
}

// ---- Directory (P3.3): cross-source local filter ----
function appendDirectoryRows(out, q) {
  let total = 0;
  for (const s of SOURCES) {
    if (s.kind === 'all') continue;
    const convos = (convosBySource[s.id] || []).filter(c =>
      !q || c.title.toLowerCase().includes(q) || (c.sub || '').toLowerCase().includes(q));
    if (!convos.length) continue;
    out.appendChild(el('div', 'list-section', s.label));
    const wrap = el('div', 'convo-list');
    for (const c of convos) { wrap.appendChild(buildConvoRow(c, true)); total++; }
    out.appendChild(wrap);
  }
  return total;
}

function renderDirectory() {
  const q = (($('people-search') && $('people-search').value) || '').trim().toLowerCase();
  const out = $('list-body');
  if (!out) return;
  out.replaceChildren();
  const total = appendDirectoryRows(out, q);
  if (!total) out.appendChild(elEmpty(q ? 'No conversations match your search.' : 'No conversations yet.'));
}

function renderPeople() {
  renderDirectory();
}

export { scheduleFeedRender, feedRelTime, renderHome, ensureHomeFilters, loadSourceList, renderSourceList, renderDirectory, renderPeople, appendDirectoryRows, setSourceViewHook, setFeedRenderHook };
