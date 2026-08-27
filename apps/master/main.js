// apps/master/main.js — read-only manager console (PLAN-MASTER-SYNC.md §6.4).
//
// Reuses the genuinely app-agnostic shared/ leaves: shared/matrix/client.js
// (transport — api()/configureMatrixBase(), no send-side effects of its own)
// and shared/ui/el.js (DOM + sanitize helpers, zero transitive imports) and
// shared/state.js (the plain S.token/S.userId session slot the transport
// reads). Deliberately does NOT import shared/ui/render.js, rows.js, nav.js,
// chat.js, search.js, sources.js, or connections.js: every one of those files
// is import-chained into shared/ui/chat.js's sendConvoMessage and/or
// shared/ui/sources.js's bridge-command sender (sendCmd) at module-evaluation
// time (ES module imports execute eagerly, whichever named export you take),
// so there is no way to import from that graph without the send path
// becoming *present code* in this app's bundle. PLAN-MASTER-SYNC.md is
// explicit that here read-only must be "absent code, not a hidden button",
// so this file re-implements the small, already-duplicated-per-read-path
// patterns those modules use (content whitelist, recency sort, a tailing
// long-poll) locally instead. See apps/master/CLAUDE.md for the full account.
//
// NO composer, NO send call, anywhere in this file.

import { api, configureMatrixBase, setOnUnauthorized, ROOMID_RE } from '../../shared/matrix/client.js';
import { $, el, sanitize, sanitizeLine } from '../../shared/ui/el.js';
import { S } from '../../shared/state.js';

// ---- point the shared transport at the MASTER homeserver, not the user hub's.
// Own compose project (matrix-master), own port (127.0.0.1:8018), own
// server_name ("master") — see master/docker-compose.master.yml + provision.sh.
// This call only affects THIS page's module instance of shared/matrix/client.js
// (each document gets its own ES module graph), so apps/user is unaffected.
configureMatrixBase({ csBase: 'http://127.0.0.1:8018', serverName: 'master' });

// ---- master-local state (deliberately separate from shared/state.js's S,
// whose other fields — qr, activeSettingsSource, feedLowPriority, ... — are
// user-hub/bridge concepts that do not apply here) ----
const MS = {
  rooms: {},           // roomId -> {id, name, isSpace, children, sourceId, lastBody, lastTs, userLabel}
  byUser: new Map(),   // teammate label -> [{id, title, preview, lastTs, sourceId, userLabel}]
  feed: [],            // flattened rows across all teammates, recency-sorted
  activeView: 'recent',
  openRoomId: null,
  openRoomUser: null,
  tailRunning: false,
  tailSince: null,
  pollTimer: null,
};

setOnUnauthorized(forgetSession);

// ===========================================================================
// Snapshot: one filtered /sync per refresh, across every room the manager has
// joined (all teammates' spaces + their mirror rooms). Mirrors the shape of
// shared/ui/account-data.js's fetchSnapshot/parseSnapshot (same filter idiom:
// small timeline window, full state, lazy-loaded members) but reads the
// mirror-room-specific state/content fields §8.2 defines instead of bridge
// concepts (SOURCES/mgmt rooms) that do not exist on the master.
// ===========================================================================
async function fetchSnapshot() {
  const filter = encodeURIComponent(JSON.stringify({
    room: { timeline: { limit: 5 }, state: { lazy_load_members: true } },
    presence: { types: [] }, account_data: { types: [] },
  }));
  return await api('GET', '/_matrix/client/v3/sync?timeout=0&filter=' + filter);
}

// CV-R4-equivalent content whitelist (mirrors shared/ui/render.js's
// convoResolveContent exactly in shape): reads content.body ONLY, never
// formatted_body; media msgtypes get a static label, never the bridged
// filename. Kept local (see file header) rather than importing render.js.
function resolveMirrorContent(ev) {
  if (!ev || ev.type !== 'm.room.message' || !ev.content) return null;
  let content = ev.content;
  const rel = content['m.relates_to'];
  if (rel && rel.rel_type === 'm.replace') {        // edit: read m.new_content only
    content = content['m.new_content'];
    if (!content) return null;
  }
  const mt = content.msgtype;
  if ((mt === 'm.text' || mt === 'm.notice') && typeof content.body === 'string') {
    return { text: sanitize(content.body), kind: mt === 'm.notice' ? 'notice' : 'text' };
  }
  if (mt === 'm.image') return { text: '📷 Photo', kind: 'media' };
  if (mt === 'm.video') return { text: '🎥 Video', kind: 'media' };
  if (mt === 'm.audio') return { text: '🎵 Audio', kind: 'media' };
  if (mt === 'm.file')  return { text: '📎 File',  kind: 'media' };
  return null;
}

// PLAN §8.2/§11: sort/display by com.jkali.origin_ts (the ORIGINAL message
// time the uplink stamped), not origin_server_ts — a normal client cannot
// backdate server timestamps, so historical backfill posted in one burst
// would otherwise cluster at sync-time instead of showing true history order.
function mirrorTs(ev) {
  const c = ev && ev.content;
  const ots = c && typeof c['com.jkali.origin_ts'] === 'number' ? c['com.jkali.origin_ts'] : null;
  return ots != null ? ots : (typeof ev.origin_server_ts === 'number' ? ev.origin_server_ts : 0);
}

function localpart(mxid) {
  const s = typeof mxid === 'string' ? mxid : '';
  const colon = s.indexOf(':');
  const lp = (colon > 0 ? s.slice(0, colon) : s).replace(/^@/, '');
  return sanitizeLine(lp) || 'unknown';
}

function parseSnapshot(data) {
  const rooms = {};
  const join = (data.rooms && data.rooms.join) || {};
  for (const rid of Object.keys(join)) {
    const r = join[rid];
    const info = { id: rid, name: null, isSpace: false, children: [], sourceId: null, lastBody: '', lastTs: 0 };
    // State from BOTH the `state` block and `timeline` (a newer space's
    // create/name/child events can still be in the timeline window).
    const stateEvents = ((r.state && r.state.events) || []).concat((r.timeline && r.timeline.events) || []);
    const seenChild = new Set();
    for (const e of stateEvents) {
      if (e.type === 'm.room.name' && e.state_key === '') info.name = e.content && e.content.name;
      if (e.type === 'm.room.create' && e.content && e.content.type === 'm.space') info.isSpace = true;
      // §8.2: the uplink tags each mirror room's platform at creation as a
      // room STATE event (not per-account_data, so it is visible to @manager
      // — a different account than the room's creator) so the master app can
      // show the platform badge.
      if (e.type === 'com.jkali.source' && e.state_key === '' && e.content && typeof e.content.source === 'string') {
        info.sourceId = e.content.source;
      }
      if (e.type === 'm.space.child' && e.state_key && e.content && Object.keys(e.content).length) {
        if (!seenChild.has(e.state_key)) { seenChild.add(e.state_key); info.children.push(e.state_key); }
      }
    }
    const tl = (r.timeline && r.timeline.events) || [];
    for (const ev of tl) {
      const resolved = resolveMirrorContent(ev);
      if (!resolved) continue;
      const ts = mirrorTs(ev);
      if (ts >= info.lastTs) { info.lastBody = resolved.text; info.lastTs = ts; }
    }
    rooms[rid] = info;
  }
  return rooms;
}

// Teammate spaces are named "space:<User>" (master/provision.sh). Discovered
// dynamically from whatever the manager is actually joined to — never a
// hardcoded roster — so a newly-provisioned teammate appears with no code
// change. Each mirror room is listed only if it is itself a joined room (same
// "space child must also be joined" gate apps/user's buildConvos uses).
function buildByUser(rooms) {
  const byUser = new Map();
  const spaces = Object.values(rooms).filter(r =>
    r.isSpace && typeof r.name === 'string' && r.name.indexOf('space:') === 0);
  for (const sp of spaces) {
    const label = sanitizeLine(sp.name.slice('space:'.length)) || sp.id;
    const convos = [];
    for (const childId of sp.children) {
      const r = rooms[childId];
      if (!r) continue;                              // not in the joined set -> excluded
      if (!ROOMID_RE.test(childId)) continue;         // malformed id -> excluded
      r.userLabel = label;                            // back-reference for the room viewer
      convos.push({
        id: childId,
        title: sanitizeLine(r.name || childId),
        preview: sanitizeLine(r.lastBody || ''),
        lastTs: r.lastTs || 0,
        sourceId: r.sourceId,
        userLabel: label,
      });
    }
    byUser.set(label, convos);
  }
  return byUser;
}

async function refreshAll() {
  const data = await fetchSnapshot();
  const rooms = parseSnapshot(data);
  MS.rooms = rooms;
  MS.byUser = buildByUser(rooms);
  MS.feed = [].concat(...[...MS.byUser.values()]).sort((a, b) => b.lastTs - a.lastTs);
  renderTeammateNav();
  if (MS.activeView === 'recent') renderRecent();
  else if (MS.activeView === 'search') renderSearch();
  else if (typeof MS.activeView === 'string' && MS.activeView.indexOf('teammate:') === 0) {
    renderTeammate(MS.activeView.slice('teammate:'.length));
  }
}

// ===========================================================================
// Rendering — rows, badges, empty states. Same DOM shape (.convo/.avatar/
// .meta/.plat-badge classes, style.css) as apps/user's rows for a consistent
// look, small enough here not to be worth importing rows.js for (which would
// also pull in chat.js's sendConvoMessage transitively — see file header).
// ===========================================================================
function elEmpty(text) { return el('div', 'list-empty', text); }

function relTime(ts) {
  const d = Date.now() - ts;
  if (d < 0) return '';
  if (d < 60000) return 'now';
  if (d < 3600000) return Math.floor(d / 60000) + 'm';
  if (d < 86400000) return Math.floor(d / 3600000) + 'h';
  if (d < 604800000) return Math.floor(d / 86400000) + 'd';
  const dt = new Date(ts);
  return (dt.getMonth() + 1) + '/' + dt.getDate();
}

// Badge derived ONLY from the room's own com.jkali.source state (read in
// parseSnapshot above) — never a bridged message field.
const PLATFORM_ICON = {
  whatsapp: '🟢', imessage: '🔵', gmessages: '📱',
  instagram: '📷', linkedin: '💼', twitter: '✖️',
};
function buildPlatBadge(sourceId) {
  const safe = typeof sourceId === 'string' ? sourceId.replace(/[^a-z]/g, '') : '';
  const cls = 'plat-badge' + (safe ? ' ' + safe : '');
  return el('span', cls, (sourceId && PLATFORM_ICON[sourceId]) || '•');
}

// One row = one mirror room, whichever list it appears in (Recent / a
// teammate's section / Search results). Shows the conversation name, the
// preview, whose account it is (userLabel), and the platform badge — the
// "each row shows whose account + which platform" requirement (§6.4).
function buildFeedRow(c) {
  const row = el('div', 'convo');
  row.setAttribute('role', 'button');
  row.tabIndex = 0;
  row.appendChild(el('div', 'avatar', (c.title || '?').slice(0, 1).toUpperCase()));
  const meta = el('div', 'meta');
  meta.appendChild(el('div', 'title', c.title));
  meta.appendChild(el('div', 'preview', c.preview));
  row.appendChild(meta);
  if (c.lastTs) row.appendChild(el('span', 'when', relTime(c.lastTs)));
  row.appendChild(el('span', 'badge', c.userLabel || ''));
  row.appendChild(buildPlatBadge(c.sourceId));
  const open = () => { openRoom(c.id).catch(() => {}); };
  row.addEventListener('click', open);
  row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  return row;
}

function renderRecent() {
  const list = $('recent-list');
  if (!list) return;
  list.replaceChildren();
  if (!MS.feed.length) { list.appendChild(elEmpty('No shared conversations yet.')); return; }
  for (const c of MS.feed.slice(0, 200)) list.appendChild(buildFeedRow(c));
}

function renderTeammateNav() {
  const nav = $('nav-teammates');
  if (!nav) return;
  nav.replaceChildren();
  for (const label of MS.byUser.keys()) {
    const key = 'teammate:' + label;
    const btn = el('button', 'navitem');
    btn.type = 'button';
    btn.dataset.navkey = key;
    btn.appendChild(el('span', 'ic', '👤'));
    btn.appendChild(document.createTextNode(' ' + label));
    btn.addEventListener('click', () => navTo(key));
    nav.appendChild(btn);
  }
  setActiveNav(MS.activeView);
}

function renderTeammate(label) {
  $('teammate-title').textContent = label;
  const convos = (MS.byUser.get(label) || []).slice().sort((a, b) => b.lastTs - a.lastTs);
  $('teammate-sub').textContent = convos.length + ' shared conversation' + (convos.length === 1 ? '' : 's');
  const list = $('teammate-list');
  list.replaceChildren();
  if (!convos.length) { list.appendChild(elEmpty('Nothing shared yet.')); return; }
  for (const c of convos) list.appendChild(buildFeedRow(c));
}

// #search-input is a pure client-side filter over the in-memory flattened
// feed; it never builds a URL, sends a command, or navigates.
function renderSearch() {
  const q = (($('search-input') && $('search-input').value) || '').trim().toLowerCase();
  const out = $('search-results');
  if (!out) return;
  out.replaceChildren();
  if (!q) { out.appendChild(elEmpty('Type to search across every teammate.')); return; }
  const rows = MS.feed.filter(c =>
    c.title.toLowerCase().includes(q) || (c.preview || '').toLowerCase().includes(q));
  if (!rows.length) { out.appendChild(elEmpty('No conversations match "' + q + '".')); return; }
  for (const c of rows) out.appendChild(buildFeedRow(c));
}

// ===========================================================================
// Read-only conversation viewer. No input element exists anywhere in this
// view (index.html has none); nothing here ever calls
// PUT .../send/m.room.message or any other write endpoint.
// ===========================================================================
function roomStatus(text) {
  const s = $('room-status');
  if (!s) return;
  s.textContent = text || '';
  s.classList.toggle('hidden', !text);
}

function shortTime(ts) {
  if (typeof ts !== 'number' || !isFinite(ts) || !ts) return '';
  try { return new Date(ts).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }); }
  catch (e) { return ''; }
}

// Alignment/attribution comes from the trusted com.jkali.from_me flag the
// uplink stamps. Unlike apps/user's iMessage-bot gate, no sender/bot check is
// needed here: master-side power levels (§8.3) mean only @<teammate>:master
// can ever post into that teammate's own mirror room, so the flag cannot be
// spoofed by anyone else within that room.
function renderBubble(ev) {
  const box = $('room-messages');
  if (!box) return;
  const resolved = resolveMirrorContent(ev);
  if (!resolved) return;                              // reaction/redaction/state/etc. — skip
  const sent = !!(ev.content && ev.content['com.jkali.from_me'] === true);
  const senderName = (ev.content && typeof ev.content['com.jkali.sender_name'] === 'string')
    ? sanitizeLine(ev.content['com.jkali.sender_name'])
    : localpart(ev.sender);
  let cls = 'msg ' + (sent ? 'sent' : 'recv');
  if (resolved.kind === 'media') cls += ' media';
  else if (resolved.kind === 'notice') cls += ' notice';
  const bubble = el('div', cls);
  bubble.appendChild(el('div', 'who', sent ? (MS.openRoomUser || 'Teammate') : senderName));
  bubble.appendChild(el('div', 'body', resolved.text));
  bubble.appendChild(el('div', 'when', shortTime(mirrorTs(ev))));
  box.appendChild(bubble);
  while (box.childElementCount > 300) {               // bounded, drop oldest
    const first = box.firstElementChild;
    if (!first) break;
    box.removeChild(first);
  }
}

async function openRoom(roomId) {
  // Re-validate against the current snapshot every time — never trust a
  // stale/typed id (same discipline as apps/user's openConversation).
  if (!ROOMID_RE.test(roomId) || !MS.rooms[roomId]) return;
  stopTail();
  MS.openRoomId = roomId;
  const rec = MS.rooms[roomId];
  MS.openRoomUser = rec.userLabel || null;
  $('room-title').textContent = sanitizeLine(rec.name || roomId);
  $('room-owner').textContent = rec.userLabel ? ('· ' + rec.userLabel) : '';
  const badge = $('room-badge');
  const b = buildPlatBadge(rec.sourceId);
  badge.className = b.className;
  badge.textContent = b.textContent;
  const box = $('room-messages');
  if (box) box.replaceChildren();
  roomStatus('');
  showSection('view-room');

  try {
    const q = '/_matrix/client/v3/rooms/' + encodeURIComponent(roomId) + '/messages?dir=b&limit=100';
    const data = await api('GET', q);
    if (MS.openRoomId !== roomId) return;             // navigated away mid-fetch
    const chunk = Array.isArray(data.chunk) ? data.chunk : [];
    // §8.2/§11: sort by com.jkali.origin_ts — backfill can arrive out of
    // chronological order, so timeline/arrival order is not display order.
    const sorted = chunk.slice().sort((a, c2) => mirrorTs(a) - mirrorTs(c2));
    for (const ev of sorted) renderBubble(ev);
    if (box) box.scrollTop = box.scrollHeight;
  } catch (e) {
    roomStatus('Could not load messages: ' + String(e.message || e));
  }
  startTail(roomId);
}

// A room-scoped long-poll tail, mirroring the shape of apps/user's three
// independent sync loops (each read path owns its own loop in this codebase;
// see shared/ui/account-data.js/chat.js) — kept local rather than imported
// from shared/ui/chat.js's startConvoWatch, which lives in the same file as
// sendConvoMessage (see header). Read-only: appends via renderBubble only.
async function startTail(roomId) {
  if (MS.tailRunning) return;
  MS.tailRunning = true;
  MS.tailSince = null;
  while (MS.tailRunning && S.token && MS.openRoomId === roomId) {
    try {
      const filter = encodeURIComponent(JSON.stringify({
        room: { rooms: [roomId], timeline: { limit: 20 }, state: { types: [] } },
        presence: { types: [] }, account_data: { types: [] },
      }));
      const q = '/_matrix/client/v3/sync?timeout=25000&filter=' + filter +
        (MS.tailSince ? '&since=' + encodeURIComponent(MS.tailSince) : '');
      const data = await api('GET', q);
      MS.tailSince = data.next_batch;
      const join = (data.rooms && data.rooms.join) || {};
      const room = join[roomId];
      if (room && room.timeline && Array.isArray(room.timeline.events) && MS.openRoomId === roomId) {
        for (const ev of room.timeline.events) {
          if (MS.openRoomId !== roomId) break;
          renderBubble(ev);
        }
        const box = $('room-messages');
        if (box) box.scrollTop = box.scrollHeight;
      }
    } catch (e) {
      if (!S.token) { MS.tailRunning = false; return; }
      await new Promise(r => setTimeout(r, 3000));
    }
  }
}
function stopTail() { MS.tailRunning = false; MS.openRoomId = null; MS.openRoomUser = null; }

// ---- navigation ----
function showSection(id) {
  for (const s of document.querySelectorAll('#content .view')) s.classList.toggle('hidden', s.id !== id);
}
function setActiveNav(key) {
  for (const b of document.querySelectorAll('.navitem')) b.classList.toggle('active', b.dataset.navkey === key);
}
function navTo(key) {
  MS.activeView = key;
  setActiveNav(key);
  if (key === 'recent') { showSection('view-recent'); renderRecent(); }
  else if (key === 'search') { showSection('view-search'); renderSearch(); }
  else if (key.indexOf('teammate:') === 0) { showSection('view-teammate'); renderTeammate(key.slice('teammate:'.length)); }
}

// ---- session ----
function forgetSession() {
  S.token = null; S.userId = null;
  MS.rooms = {}; MS.byUser = new Map(); MS.feed = [];
  stopTail();
  if (MS.pollTimer) { clearInterval(MS.pollTimer); MS.pollTimer = null; }
  try { sessionStorage.removeItem('master_token'); sessionStorage.removeItem('master_user'); } catch (e) {}
  const shell = $('shell'); if (shell) shell.classList.add('hidden');
  const signin = $('view-signin'); if (signin) signin.classList.remove('hidden');
}
async function signIn(user, pass) {
  const body = {
    type: 'm.login.password',
    identifier: { type: 'm.id.user', user },
    password: pass,
    initial_device_display_name: 'Master Console',
  };
  const data = await api('POST', '/_matrix/client/v3/login', body);
  S.token = data.access_token; S.userId = data.user_id;
  try { sessionStorage.setItem('master_token', S.token); sessionStorage.setItem('master_user', S.userId); } catch (e) {}
}
async function signOut() {
  try { await api('POST', '/_matrix/client/v3/logout', {}); } catch (e) {}
  forgetSession();
}

async function enterApp() {
  $('whoami').textContent = S.userId;
  $('view-signin').classList.add('hidden');
  $('shell').classList.remove('hidden');
  try { await refreshAll(); } catch (e) { /* stays empty on error */ }
  navTo('recent');
  if (MS.pollTimer) clearInterval(MS.pollTimer);
  // Periodic re-snapshot for freshness (no in-place live merge needed for a
  // manager-facing recent/grouped list — a simple poll is enough here).
  MS.pollTimer = setInterval(() => { refreshAll().catch(() => {}); }, 20000);
}

document.addEventListener('DOMContentLoaded', () => {
  $('btn-signin').addEventListener('click', async () => {
    const err = $('signin-error');
    err.classList.add('hidden');
    try {
      await signIn($('in-user').value.trim(), $('in-pass').value);
      $('in-pass').value = '';
      await enterApp();
    } catch (e) {
      err.textContent = String(e.message || e);
      err.classList.remove('hidden');
    }
  });
  $('in-pass').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('btn-signin').click(); });
  $('signout').addEventListener('click', signOut);

  $('nav-recent').dataset.navkey = 'recent';
  $('nav-recent').addEventListener('click', () => navTo('recent'));
  $('nav-search').dataset.navkey = 'search';
  $('nav-search').addEventListener('click', () => navTo('search'));
  $('search-input').addEventListener('input', renderSearch);
  $('room-back').addEventListener('click', () => { stopTail(); navTo(MS.activeView === 'room' ? 'recent' : MS.activeView); });

  try {
    const t = sessionStorage.getItem('master_token'), u = sessionStorage.getItem('master_user');
    if (t && u) { S.token = t; S.userId = u; enterApp(); return; }
  } catch (e) {}
  $('shell').classList.add('hidden');
  $('view-signin').classList.remove('hidden');
});
