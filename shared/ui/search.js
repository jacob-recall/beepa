// Relocated verbatim from hub/site/app.js (PLAN-MASTER-SYNC-IMPL P1.2).
// Shared ES module. Logic unchanged; only import/export + shared-state (S) access added.

import { feedIsHidden, refreshConvos } from './account-data.js';
import { confirmModal, logConsole, setButtonsDisabled } from './connections.js';
import { $, el, sanitizeLine } from './el.js';
import { buildConvoRow, buildFeedRow, elEmpty } from './rows.js';
import { SOURCES, sendCmd, validHandle } from './sources.js';
import { S, convosBySource, feedModel } from '../state.js';

// Optional app-injected hook: called with the sourceId whenever the per-source
// view is (re)loaded, so apps/user can mount its "Share all <source>" switch
// (PLAN-MASTER-SYNC §5.1) into that view's header without shared/ importing
// from apps/ (same hook pattern as setConvoRowDecorator in rows.js).
let sourceViewHook = null;
function setSourceViewHook(fn) { sourceViewHook = typeof fn === 'function' ? fn : null; }

// HF-5: coalesce renders — one timer per batch so a burst = one re-render.
function scheduleFeedRender() {
  if (S.feedRenderScheduled) return;
  S.feedRenderScheduled = true;
  setTimeout(() => { S.feedRenderScheduled = false; renderHome(); }, 0);
}

function feedRelTime(ts) {
  const d = Date.now() - ts;
  if (d < 0) return '';
  if (d < 60000) return 'now';
  if (d < 3600000) return Math.floor(d / 60000) + 'm';
  if (d < 86400000) return Math.floor(d / 3600000) + 'h';
  if (d < 604800000) return Math.floor(d / 86400000) + 'd';
  const dt = new Date(ts);
  return (dt.getMonth() + 1) + '/' + dt.getDate();
}

// HF-8: render the merged feed sorted by recency, capped at ~200 rows. The
// #home-search box is a pure client-side filter over the full in-memory model
// (sanitized name + preview); it never builds a URL, sends a command, or
// navigates. Clearing restores the full recency-sorted list.
function renderHome() {
  const list = $('list-body');
  if (!list) return;
  ensureHomeHiddenToggle();                           // HF-9: "Show hidden" chip above the list
  const q = (($('home-search') && $('home-search').value) || '').trim().toLowerCase();
  const all = [...feedModel.values()].sort((a, b) => b.lastTs - a.lastTs);
  // HF-9: hidden rooms (low-priority/muted/manual) stay in feedModel but are
  // excluded from the default list; the toggle reveals them (with Unhide).
  const visible = S.feedShowHidden ? all : all.filter(r => !feedIsHidden(r.id));
  const rows = (q
    ? visible.filter(r => sanitizeLine(r.name).toLowerCase().includes(q) ||
                          sanitizeLine(r.lastBody || '').toLowerCase().includes(q))
    : visible).slice(0, 200);
  list.replaceChildren();
  if (!rows.length) {
    list.appendChild(elEmpty(q ? 'No conversations match your search.' : 'No conversations yet.'));
    return;
  }
  for (const r of rows) list.appendChild(buildFeedRow(r));
}

// HF-9: a small "Show hidden" toggle chip inserted once, just above the Home
// list (built with el()/textContent; no HTML strings). Toggles S.feedShowHidden
// and re-renders so hidden (low-priority/muted/manual) rooms appear with an
// Unhide action. Pure client-side state; sends no command and builds no URL.
function ensureHomeHiddenToggle() {
  const list = $('list-body');
  if (!list || !list.parentNode) return;
  let chip = $('home-hidden-toggle');
  if (!chip) {
    chip = el('button', 'feed-showhidden');
    chip.id = 'home-hidden-toggle';
    chip.type = 'button';
    chip.addEventListener('click', () => { S.feedShowHidden = !S.feedShowHidden; renderHome(); });
    list.parentNode.insertBefore(chip, list);
  }
  chip.setAttribute('aria-pressed', S.feedShowHidden ? 'true' : 'false');
  chip.classList.toggle('active', S.feedShowHidden);
  chip.textContent = S.feedShowHidden ? 'Hide hidden' : 'Show hidden';
}

async function loadSourceList(sourceId) {
  const source = SOURCES.find(s => s.id === sourceId);
  const list = $('list-body');
  if (!list) return;
  S.sourceViewId = sourceId;
  if (sourceViewHook) sourceViewHook(sourceId);
  const search = $('source-search');
  if (search) {
    search.value = '';
    search.placeholder = 'Search ' + (source ? source.label : 'conversations');
  }
  list.replaceChildren();
  list.appendChild(elEmpty('Loading…'));
  try {
    await refreshConvos();
  } catch (e) {
    list.replaceChildren(elEmpty('Could not load conversations: ' + String(e.message || e)));
    return;
  }
  renderSourceList();
}

// #source-search is a pure client-side filter over the loaded per-source list,
// mirroring #home-search: matches on the conversation title + preview only.
function renderSourceList() {
  const list = $('list-body');
  if (!list || !S.sourceViewId) return;
  const source = SOURCES.find(s => s.id === S.sourceViewId);
  const convos = convosBySource[S.sourceViewId] || [];
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

// ---- Directory (P3.3): cross-source local filter + gated start-chat ----
function buildDirectory() {
  /* people-search wired in contacts.js initContactsUI */
}
// D-1/D-2: only a USER-TYPED handle, strictly validated, is ever sent; search
// results are display-only and never become a destination handle.
async function startChat(sourceId, input, msgInput) {
  const source = SOURCES.find(s => s.id === sourceId);
  if (!source || !source.canStartChat) return;     // capability gate
  const h = (input.value || '').trim();
  if (!validHandle(h)) {                            // UX pre-check; daemon re-validates
    logConsole('error', source.label + ': enter a valid phone (+14155551234) or email address.');
    return;
  }
  if (msgInput) {
    // iMessage two-field form: a non-empty first message is required.
    const message = (msgInput.value || '').trim();
    if (!message) {
      logConsole('error', source.label + ': enter a first message to start the chat.');
      return;
    }
    // SC-P7: the confirm modal (el()/textContent) shows the EXACT handle and
    // message that will be sent; validHandle already rejected control/bidi.
    if (!(await confirmModal('Start ' + source.label + ' chat',
      'Start a new ' + source.label + ' chat with ' + h + ' and send this first message:\n\n' + message,
      false))) return;
    await sendCmd(sourceId, 'start-chat ' + h + ' | ' + message);  // C-1 verifies mgmt room
    input.value = '';
    msgInput.value = '';
    return;
  }
  // Single-field sources (e.g. WhatsApp): handle-only start-chat, unchanged.
  if (!(await confirmModal('Start ' + source.label + ' chat',
    'Start a new ' + source.label + ' chat with ' + h + '?', false))) return;
  await sendCmd(sourceId, 'start-chat ' + h);      // C-1 verifies mgmt room before send
  input.value = '';
}
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

export { scheduleFeedRender, feedRelTime, renderHome, ensureHomeHiddenToggle, loadSourceList, renderSourceList, buildDirectory, startChat, renderDirectory, renderPeople, appendDirectoryRows, setSourceViewHook };
