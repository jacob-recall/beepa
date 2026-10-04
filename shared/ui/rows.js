// Relocated verbatim from hub/site/app.js (PLAN-MASTER-SYNC-IMPL P1.2).
// Shared ES module. Logic unchanged; only import/export + shared-state (S) access added.

import { openConvo } from './chat.js';
import { $, el, sanitizeLine } from './el.js';
import { feedRelTime } from '../model/message_preview.js';
import { SOURCES } from '../model/source_catalog.js';
import { S, feedModel, runtime } from '../state.js';
import { initials, indicatorsFor, pendingDrafts } from '../model/attention.js';
import { scheduleFeedRender } from './search.js';

// Optional app-injected per-row decorator (e.g. apps/user's share controls,
// PLAN-MASTER-SYNC §5.1). Shared code never imports from apps/; the app
// registers a callback here instead, same pattern as setOnUnauthorized
// (shared/matrix/client.js). Left unset, rows render exactly as before.
let convoRowDecorator = null;
function setConvoRowDecorator(fn) { convoRowDecorator = typeof fn === 'function' ? fn : null; }

// HF-6/HF-7: badge derived ONLY from the record's sourceId (which space the
// room is in) — never a bridged field. A CSS-classed pill carrying the source
// icon via textContent. No <img>, no data:/remote URL (CSP byte-identical).
function buildPlatBadge(sourceId) {
  // beepa.css renders a per-source logo via .plat-badge.<sourceId> background-image
  // (shared/assets/logo-<sourceId>.png). sourceId is an internal SOURCES id
  // (whatsapp/imessage/gmessages/instagram/linkedin/twitter), safe as a class.
  // The source emoji stays as a fallback for any source without a logo.
  const cls = 'plat-badge' + (sourceId ? ' ' + sourceId : '');
  const source = SOURCES.find(s => s.id === sourceId);
  return el('span', cls, (source && source.icon) || '');
}

// Indicator flags for one feed record. Pure over the record + runtime.
function rowFlags(rec) {
  return indicatorsFor({
    unread: rec.unread,
    draft: pendingDrafts(rec.drafts, rec.lastTs).length,
    reconnect: !!(rec.sourceId && runtime[rec.sourceId] && runtime[rec.sourceId].needsReconnect),
  });
}
// 4px left edge split into one segment per active indicator (CSS colors by class).
function buildStripe(flags) {
  const s = el('div', 'stripe');
  for (const f of flags) s.appendChild(el('i', 'seg ' + f));
  return s;
}
// Two-letter initials with the platform logo(s) badged at the lower right.
// sourceIds come ONLY from feed records (the room's space), never from content.
function buildAvatar(name, sourceIds) {
  const a = el('div', 'avatar', initials(name));
  for (const sid of (sourceIds || []).filter(Boolean)) {
    const b = buildPlatBadge(sid);
    b.classList.add('avatar-plat');
    a.appendChild(b);
  }
  return a;
}
function buildSide(rec, flags, unreadCount, draftCount) {
  const side = el('div', 'side');
  if (rec.lastTs) side.appendChild(el('span', 'when', feedRelTime(rec.lastTs)));
  if (flags.includes('unread')) side.appendChild(el('span', 'pill unread', String(unreadCount)));
  if (flags.includes('draft')) side.appendChild(el('span', 'pill draft', String(draftCount)));
  if (flags.includes('reconnect')) {
    // #4: a bridge login is per-account, so a dead login (needsReconnect, set by
    // updateCardStatus) surfaces on every conversation from that source.
    const flag = el('span', 'pill reconnect', '!');
    flag.title = 'This account’s bridge login needs reconnecting — reopen its card in Settings to re-pair.';
    side.appendChild(flag);
  }
  return side;
}
function wireOpen(row, roomId) {
  row.dataset.roomId = roomId;                        // active-row match key (layout only; not a nav/security input)
  row.setAttribute('role', 'button');
  row.tabIndex = 0;
  const open = () => openConvo(roomId);               // CV.2: the only validated nav path
  row.addEventListener('click', open);
  row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
}
function previewText(rec, pend) {
  // HF-4: single-line, clamped, textContent only. A pending manager draft
  // previews as "Draft: …" so the row says what is waiting.
  return pend.length ? 'Draft: ' + sanitizeLine(pend[0].body || '') : sanitizeLine(rec.lastBody || '');
}

// A feed row (Triage Rail anatomy): stripe · avatar+badge · title/preview · side.
function buildFeedRow(r) {
  const name = sanitizeLine(r.name || r.id);
  const flags = rowFlags(r);
  const pend = pendingDrafts(r.drafts, r.lastTs);
  const row = el('div', 'convo');
  row.appendChild(buildStripe(flags));
  row.appendChild(buildAvatar(name, [r.sourceId]));
  const meta = el('div', 'meta');
  meta.appendChild(el('div', 'title', name));
  meta.appendChild(el('div', 'preview' + (pend.length ? ' draft' : ''), previewText(r, pend)));
  row.appendChild(meta);
  row.appendChild(buildSide(r, flags, r.unread || 0, pend.length));
  wireOpen(row, r.id);
  if (convoRowDecorator) convoRowDecorator(row, { id: r.id, sourceId: r.sourceId });
  return row;
}

// One row per person (contact profile) with every platform badged on the
// avatar and a caret; opening the row opens the NEWEST member. Expanded
// clusters (S.expandedClusters) get a sub-row per member beneath (search.js).
function buildClusterRow(item) {
  const newest = item.members[0];
  const flags = indicatorsFor({ unread: item.unread, draft: item.draft,
    reconnect: item.members.some(m => m.sourceId && runtime[m.sourceId] && runtime[m.sourceId].needsReconnect) });
  const row = el('div', 'convo cluster');
  row.appendChild(buildStripe(flags));
  const sources = [...new Set(item.members.map(m => m.sourceId).filter(Boolean))];
  row.appendChild(buildAvatar(item.displayName || newest.name, sources));
  const meta = el('div', 'meta');
  const title = el('div', 'title', sanitizeLine(item.displayName || newest.name || ''));
  const open = S.expandedClusters.has(item.profileId);
  const caret = el('button', 'cluster-caret', (open ? '▾ ' : '▸ ') + item.members.length);
  caret.type = 'button';
  caret.title = open ? 'Collapse platforms' : 'Show platforms';
  caret.addEventListener('click', (e) => {
    e.stopPropagation();
    if (open) S.expandedClusters.delete(item.profileId); else S.expandedClusters.add(item.profileId);
    scheduleFeedRender();
  });
  title.appendChild(caret);
  meta.appendChild(title);
  const pend = pendingDrafts(newest.drafts, newest.lastTs);
  meta.appendChild(el('div', 'preview' + (pend.length ? ' draft' : ''), previewText(newest, pend)));
  row.appendChild(meta);
  row.appendChild(buildSide(newest, flags, item.unread, item.draft));
  wireOpen(row, newest.id);
  if (convoRowDecorator) convoRowDecorator(row, { id: newest.id, sourceId: newest.sourceId });
  return row;
}
// Member sub-row under an expanded cluster: platform + its own state.
function buildSubRow(rec) {
  const flags = rowFlags(rec);
  const pend = pendingDrafts(rec.drafts, rec.lastTs);
  const row = el('div', 'convo sub');
  row.appendChild(buildStripe(flags));
  const b = buildPlatBadge(rec.sourceId); b.classList.add('sub-plat');
  row.appendChild(b);
  const meta = el('div', 'meta');
  const source = SOURCES.find(s => s.id === rec.sourceId);
  meta.appendChild(el('div', 'title', sanitizeLine((source && source.label) || rec.sourceId || '')));
  meta.appendChild(el('div', 'preview' + (pend.length ? ' draft' : ''), previewText(rec, pend)));
  row.appendChild(meta);
  row.appendChild(buildSide(rec, flags, rec.unread || 0, pend.length));
  wireOpen(row, rec.id);
  return row;
}

// Layout-only helper: mark the messenger-list row for `roomId` active and clear
// it on every other row. Matches by the row's dataset.roomId (set in
// buildFeedRow); textContent/dataset only, no innerHTML. Passing null clears all.
// Not a navigation or security input — it only sets a CSS highlight class.
function setActiveConvoRow(roomId) {
  const list = $('list-body');
  if (!list) return;
  for (const row of list.children) {
    const rid = row.dataset ? row.dataset.roomId : undefined;
    row.classList.toggle('active', roomId != null && rid === roomId);
  }
}

// ---- conversation-row + list rendering ----
function buildConvoRow(c, withBadge) {
  const row = el('div', 'convo');
  row.setAttribute('role', 'button');
  row.tabIndex = 0;
  row.appendChild(el('div', 'avatar', (c.title || '?').slice(0, 1).toUpperCase()));
  const meta = el('div', 'meta');
  meta.appendChild(el('div', 'title', c.title));
  meta.appendChild(el('div', 'sub', c.sub || ''));
  row.appendChild(meta);
  if (withBadge) row.appendChild(el('span', 'badge', sanitizeLine(c.sourceLabel)));
  const open = () => openConvo(c.id);                 // CV.2: native hub conversation view
  row.addEventListener('click', open);
  row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  if (convoRowDecorator) convoRowDecorator(row, c);
  return row;
}
function elEmpty(text) { return el('div', 'list-empty', text); }

export { buildPlatBadge, buildFeedRow, buildClusterRow, buildSubRow, buildAvatar, buildStripe, rowFlags, setActiveConvoRow, buildConvoRow, elEmpty, setConvoRowDecorator };
