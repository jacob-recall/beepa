// Relocated verbatim from hub/site/app.js (PLAN-MASTER-SYNC-IMPL P1.2).
// Shared ES module. Logic unchanged; only import/export + shared-state (S) access added.

import { openConvo } from './chat.js';
import { $, el, sanitizeLine } from './el.js';
import { feedRelTime } from '../model/message_preview.js';
import { SOURCES } from '../model/source_catalog.js';
import { feedModel, runtime } from '../state.js';

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

// A feed row reuses the existing .convo structure; click → openConvo
// (the only validated nav path: the native in-app conversation view).
function buildFeedRow(r) {
  const name = sanitizeLine(r.name || r.id);
  const preview = sanitizeLine(r.lastBody || '');   // HF-4: single-line, clamped, textContent
  const row = el('div', 'convo');
  row.dataset.roomId = r.id;                         // active-row match key (layout only; not a nav/security input)
  row.setAttribute('role', 'button');
  row.tabIndex = 0;
  row.appendChild(el('div', 'avatar', (name || '?').slice(0, 1).toUpperCase()));
  const meta = el('div', 'meta');
  meta.appendChild(el('div', 'title', name));
  meta.appendChild(el('div', 'preview', preview));
  row.appendChild(meta);
  if (r.lastTs) row.appendChild(el('span', 'when', feedRelTime(r.lastTs)));
  row.appendChild(buildPlatBadge(r.sourceId));
  // #4: per-conversation reconnect flag. A bridge login is per-account, so a
  // dead login (needsReconnect, set by updateCardStatus) surfaces on every
  // conversation from that source — telling you which chats have stopped
  // syncing and need a re-pair in Settings.
  if (r.sourceId && runtime[r.sourceId] && runtime[r.sourceId].needsReconnect) {
    const flag = el('span', 'reconnect-flag', 'Reconnect');
    flag.title = 'This account’s bridge login needs reconnecting — reopen its card in Settings to re-pair.';
    row.appendChild(flag);
  }
  const open = () => openConvo(r.id);                 // CV.2: native hub conversation view
  row.addEventListener('click', open);
  row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  if (convoRowDecorator) convoRowDecorator(row, { id: r.id, sourceId: r.sourceId });
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

export { buildPlatBadge, buildFeedRow, setActiveConvoRow, buildConvoRow, elEmpty, setConvoRowDecorator };
