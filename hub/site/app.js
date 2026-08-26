'use strict';
/* Bridge Hub — static client for mautrix / iMessage bridges via the Matrix
 * client API. Phase 3 shell: left sidebar (per-source conversation lists +
 * cross-source Directory) + Phase 2 iMessage Connections card.
 *
 * Security invariants (PLAN-HUB / PLAN-IMSG2 / PLAN-IMSG3, GOVERNING):
 *  - No HTML-string sinks anywhere (CSP Trusted Types enforces): DOM is built
 *    only with createElement + textContent / createTextNode. No innerHTML.
 *  - All remote/bridged strings pass sanitize()/sanitizeLine() then textContent;
 *    they never build a URL and are never linkified (U-2 / D-7).
 *  - The embedded Element iframe.src is ONLY the constant base or the constant
 *    base + '/#/room/' + a room id that passed ROOMID_RE AND was in
 *    /joined_rooms — never typed or bridged text (U-1 / D-4 / D-5).
 *  - Every bot-command send goes through sendCmd(), which verifies the target
 *    management room UNCONDITIONALLY before every send (C-1).
 *  - The command/console read path is isolated to the resolved mgmt rooms and
 *    their own bot mxids; the sidebar conversation-list read path is separate
 *    (D-3). */

const HS = 'http://127.0.0.1:8008';
const SERVER_NAME = 'localhost';
const CHATS_URL = 'http://127.0.0.1:8009';

// ---- SOURCES (U-5): static, code-owned. A new bridge = one array entry.
// No remote/bridged data selects what the sidebar renders or which space it
// reads. `all` is the embedded Element pane; the others name a Matrix space
// and a management bot. `canStartChat` is the Directory capability gate (D-1).
const SOURCES = [
  { id: 'all', label: 'All chats', kind: 'all', icon: '💬' },
  { id: 'whatsapp', label: 'WhatsApp', kind: 'source', botMxid: '@whatsappbot:localhost',
    spaceName: 'WhatsApp', canStartChat: true, icon: '🟢',
    blurb: 'Link your personal WhatsApp; chats appear as rooms in Element.' },
  { id: 'imessage', label: 'iMessage', kind: 'source', botMxid: '@imessagebot:localhost',
    spaceName: 'iMessage', mgmtMarker: 'com.jkali.bridge.mgmt', canStartChat: true,
    icon: '🔵',
    blurb: 'Bridge iMessage from this Mac; grant the permissions once and chats appear in Element.' },
];
const WA = SOURCES.find(s => s.id === 'whatsapp');
const IMSG = SOURCES.find(s => s.id === 'imessage');

// Future sources: inert placeholders in the Connections view until deployed.
const PLANNED_SOURCES = ['Telegram', 'Signal', 'Discord', 'Slack', 'Google Messages'];

// ---- WhatsApp command surface (portal-scoped commands excluded) ----
const COMMAND_GROUPS = [
  { title: 'General', cmds: [
    { cmd: 'help', label: 'Help', desc: "Show the bridge's own list of every command." },
    { cmd: 'version', label: 'Version', desc: 'Show which bridge version is running.' },
  ]},
  { title: 'Account', cmds: [
    { cmd: 'list-logins', label: 'List logins', desc: 'List the WhatsApp accounts linked to the bridge and their connection state.' },
    { cmd: 'login qr', label: 'Link by QR', desc: 'Start linking a WhatsApp account by scanning a QR code from your phone.' },
    { cmd: 'login phone', label: 'Link by code', desc: 'Link by typing an 8-character code into WhatsApp instead of scanning (the bridge will ask for your phone number below).' },
    { cmd: 'relogin', label: 'Re-authenticate', desc: 'Repair an existing linked account that lost its session.', arg: 'login ID (e.g. 14146149941)' },
    { cmd: 'logout', label: 'Unlink account', desc: 'Disconnect a linked WhatsApp account from the bridge.', arg: 'login ID (e.g. 14146149941)', confirm: 'click' },
    { cmd: 'set-preferred-login', label: 'Preferred account', desc: 'Choose which account sends your messages when more than one is linked.', arg: 'login ID' },
  ]},
  { title: 'Chats & contacts', cmds: [
    { cmd: 'sync contacts', label: 'Sync contacts', desc: 'Refresh your WhatsApp contact names and avatars.' },
    { cmd: 'sync groups', label: 'Sync groups', desc: 'Refresh the list of your WhatsApp groups.' },
    { cmd: 'start-chat', label: 'Start a chat', desc: 'Open a new direct chat with a phone number.', arg: '+14155551234' },
    { cmd: 'search', label: 'Search contacts', desc: 'Search your WhatsApp contacts by name or number.', arg: 'name or number' },
    { cmd: 'resolve-identifier', label: 'Check a number', desc: 'Check whether a phone number is on WhatsApp without starting a chat.', arg: '+14155551234' },
    { cmd: 'join', label: 'Join group', desc: 'Join a WhatsApp group using an invite link.', arg: 'invite link' },
    { cmd: 'resolve-link', label: 'Preview link', desc: 'Preview what a WhatsApp group, contact, or message link points to.', arg: 'link' },
  ]},
  { title: 'Relay', cmds: [
    { cmd: 'set-relay', label: 'Enable relay', desc: "Let other Matrix users in a room send messages through your WhatsApp account.", confirm: 'click' },
    { cmd: 'unset-relay', label: 'Disable relay', desc: "Stop relaying other users' messages through your account." },
  ]},
  { title: 'Advanced', cmds: [
    { cmd: 'debug-reset-network', label: 'Reset connection', desc: 'Force the bridge to drop and rebuild its connection to WhatsApp.', confirm: 'click' },
  ]},
  { title: 'Danger zone', cmds: [
    { cmd: 'delete-all-portals', label: 'Delete all bridged rooms', desc: 'Permanently delete every bridged chat room on the Matrix side (nothing is deleted on WhatsApp itself).', confirm: 'type' },
  ]},
];

// ---- iMessage command surface (management room only; PLAN-IMSG2 B1) ----
const IMSG_COMMAND_GROUPS = [
  { title: 'iMessage', cmds: [
    { cmd: 'status', label: 'Check status', desc: 'Show which macOS permissions the iMessage bridge has.' },
    { cmd: 'setup', label: 'Set up', desc: 'Open the next missing permission pane in System Settings.' },
    { cmd: 'help', label: 'Help', desc: 'List the iMessage bridge commands.' },
  ]},
];
function groupsFor(sourceId) { return sourceId === 'imessage' ? IMSG_COMMAND_GROUPS : COMMAND_GROUPS; }

// ---- validation regexes ----
// Element route target: a room id that is URL-fragment-safe (no #,?,%,\,space,
// controls). Validate-then-concatenate the RAW id (D-4: do NOT encode — the
// charset is fragment-safe and encoding breaks Element's route parser).
const ROOMID_RE = /^![A-Za-z0-9._=/+-]+:localhost$/;
const MXC_RE = /^mxc:\/\/([A-Za-z0-9.\-:]+)\/([A-Za-z0-9_-]+)$/;
// start-chat handles (D-2): strict; only a user-typed handle is ever sent.
const PHONE_RE = /^\+[1-9]\d{6,14}$/;
const EMAIL_RE = /^[^\s@]{1,64}@[^\s@]{1,255}\.[^\s@]{2,24}$/;
// SC-P7: also reject control/bidi/zero-width chars and a leading '-' so the
// confirm modal shows EXACTLY what will be sent (confirm-equals-send); the
// daemon re-validates authoritatively (SC-P2). sanitize() strips those chars,
// so any change means the handle carried one -> reject.
function validHandle(h) {
  if (typeof h !== 'string' || h.startsWith('-') || sanitize(h) !== h) return false;
  return PHONE_RE.test(h) || EMAIL_RE.test(h);
}

// ---- state ----
let token = null, userId = null;
let syncRunning = false, syncSince = null;
let qr = { eventId: null, blobUrl: null };
let loginFlowActive = false;
let busy = false;
let activeSettingsSource = 'whatsapp';
let joinedSet = new Set();
const convosBySource = {};                 // sourceId -> [convo]
const runtime = { whatsapp: { mgmtRoomId: null }, imessage: { mgmtRoomId: null } };

// ---- Home feed state (HF-2): fully independent of the command sync loop.
// These are NEVER the command loop's syncSince/syncRunning; the two /sync loops
// share no sync state (HF-1). The feed model holds exactly one record per room
// (overwrite, no history) for the validated portal rooms only.
let feedRunning = false, feedSince = null;
const feedModel = new Map();               // roomId -> {id, name, lastBody, lastTs, sourceId}
let feedRenderScheduled = false;           // HF-5: coalesce a burst into one render
let feedRevalTimer = null;                 // HF-3: debounced re-validation for new portals

// ---- tiny DOM helpers (no HTML-string sinks anywhere) ----
const $ = (id) => document.getElementById(id);
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}
// Strip C0 controls (keep \n), bidi overrides, zero-width chars; clamp length.
function sanitize(s) {
  if (typeof s !== 'string') return '';
  return s.replace(/[ -	-‪-‮⁦-⁩​-‏﻿]/g, '')
          .slice(0, 4000);
}
// D-7: single-line variant for rows / titles / subs / badges / previews.
function sanitizeLine(s) {
  if (typeof s !== 'string') return '';
  return sanitize(s).replace(/[\r\n\t]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 64);
}
const txn = () => 'hub-' + crypto.randomUUID();

// ---- API ----
async function api(method, path, body) {
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = 'Bearer ' + token;
  const res = await fetch(HS + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (res.status === 401) { forgetSession(); throw new Error('Signed out: session expired.'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(sanitize(data.error || ('HTTP ' + res.status)));
  return data;
}

// ---- session ----
function forgetSession() {
  token = null; userId = null;
  runtime.whatsapp.mgmtRoomId = null; runtime.imessage.mgmtRoomId = null;
  syncRunning = false;
  feedRunning = false;                              // HF-2: stop the feed loop with the session
  feedSince = null;
  feedModel.clear();
  if (feedRevalTimer) { clearTimeout(feedRevalTimer); feedRevalTimer = null; }
  joinedSet = new Set();
  unmountChats();
  clearQR();
  try { sessionStorage.removeItem('hub_token'); sessionStorage.removeItem('hub_user'); } catch (e) {}
  showAuth(false);
}
async function signIn(user, pass) {
  const body = {
    type: 'm.login.password',
    identifier: { type: 'm.id.user', user },
    password: pass,
    initial_device_display_name: 'Bridge Hub',
  };
  try { const dev = localStorage.getItem('hub_device'); if (dev) body.device_id = dev; } catch (e) {}
  const data = await api('POST', '/_matrix/client/v3/login', body);
  token = data.access_token; userId = data.user_id;
  try {
    sessionStorage.setItem('hub_token', token);
    sessionStorage.setItem('hub_user', userId);
    localStorage.setItem('hub_device', data.device_id);
  } catch (e) {}
}
async function signOut() {
  try { await api('POST', '/_matrix/client/v3/logout', {}); } catch (e) {}
  forgetSession();
  const wrap = $('signout-note-wrap');
  if (wrap) wrap.classList.remove('hidden');
}

// ---- management-room resolution + verification (C-1) ----
async function resolveMgmt(source) {
  if (source.id === 'whatsapp') return await findWaMgmt();
  if (source.id === 'imessage') return await resolveImsgMgmt();
  return null;
}
async function verifyMgmt(source, roomId) {
  if (source.id === 'whatsapp') return await isWaMgmt(roomId);
  if (source.id === 'imessage') return await verifyImsgMgmt(roomId);
  return false;
}

// WhatsApp: find the bot DM (bot + me, exactly 2 members) or create it.
async function findWaMgmt() {
  const joined = await api('GET', '/_matrix/client/v3/joined_rooms');
  for (const roomId of joined.joined_rooms) {
    if (await isWaMgmt(roomId)) return roomId;
  }
  const created = await api('POST', '/_matrix/client/v3/createRoom',
    { invite: [WA.botMxid], is_direct: true, preset: 'trusted_private_chat' });
  return created.room_id;
}
async function isWaMgmt(roomId) {
  try {
    const m = await api('GET', '/_matrix/client/v3/rooms/' + encodeURIComponent(roomId) + '/joined_members');
    const members = Object.keys(m.joined);
    return members.length === 2 && members.includes(WA.botMxid) && members.includes(userId);
  } catch (e) { return false; }
}

// iMessage (B-2 hub-side): select the mgmt room ONLY by the marker state event
// com.jkali.bridge.mgmt/imessage AND the ABSENCE of a portal marker
// (uk.half-shot.bridge). NEVER by member count; NEVER auto-created here.
async function stateEvent(roomId, type, key) {
  try {
    return await api('GET', '/_matrix/client/v3/rooms/' + encodeURIComponent(roomId) +
      '/state/' + encodeURIComponent(type) + '/' + encodeURIComponent(key));
  } catch (e) { return null; }
}
async function roomFullState(roomId) {
  try { return await api('GET', '/_matrix/client/v3/rooms/' + encodeURIComponent(roomId) + '/state'); }
  catch (e) { return null; }
}
async function verifyImsgMgmt(roomId) {
  const marker = await stateEvent(roomId, 'com.jkali.bridge.mgmt', 'imessage');
  if (!marker) return false;                       // fast reject on the common case
  const st = await roomFullState(roomId);
  if (!Array.isArray(st)) return false;
  const hasMarker = st.some(e => e.type === 'com.jkali.bridge.mgmt' && e.state_key === 'imessage');
  const isPortal = st.some(e => e.type === 'uk.half-shot.bridge'); // any state_key
  return hasMarker && !isPortal;                   // marker present AND not a portal
}
async function resolveImsgMgmt() {
  const joined = await api('GET', '/_matrix/client/v3/joined_rooms');
  for (const roomId of joined.joined_rooms) {
    if (await verifyImsgMgmt(roomId)) return roomId;
  }
  return null;                                      // never auto-create for iMessage
}

// ---- sending commands (C-1: mgmt-room verified before EVERY send) ----
async function sendCmd(sourceId, text) {
  if (busy) return;
  const source = SOURCES.find(s => s.id === sourceId);
  if (!source || !source.botMxid) return;
  busy = true; setButtonsDisabled(true);
  try {
    const rt = runtime[sourceId];
    if (!rt.mgmtRoomId) rt.mgmtRoomId = await resolveMgmt(source);
    if (!rt.mgmtRoomId) throw new Error(source.label + ': management room not found.');
    if (!(await verifyMgmt(source, rt.mgmtRoomId))) {
      throw new Error('Refusing to send: ' + source.label + ' management room failed verification.');
    }
    await api('PUT', '/_matrix/client/v3/rooms/' + encodeURIComponent(rt.mgmtRoomId) +
      '/send/m.room.message/' + encodeURIComponent(txn()), { msgtype: 'm.text', body: text });
    logConsole('you', text, source.id);
    if (sourceId === 'whatsapp') {
      if (text === 'login qr' || text === 'login phone') setLoginFlow(true);
      if (text === 'cancel') setLoginFlow(false);
    }
  } catch (e) {
    logConsole('error', String(e.message || e));
  } finally {
    busy = false; setButtonsDisabled(false);
  }
}
function sendStatusRefresh() { return sendCmd('whatsapp', 'list-logins'); }

// ---- command/console sync loop (D-3: isolated to the resolved mgmt rooms) --
async function startSync() {
  if (syncRunning) return;
  syncRunning = true;
  syncSince = null;
  while (syncRunning && token) {
    try {
      const ids = [runtime.whatsapp.mgmtRoomId, runtime.imessage.mgmtRoomId].filter(Boolean);
      const filter = encodeURIComponent(JSON.stringify(
        { room: { rooms: ids, timeline: { limit: 30 } }, presence: { types: [] } }));
      const q = '/_matrix/client/v3/sync?timeout=25000&filter=' + filter +
        (syncSince ? '&since=' + encodeURIComponent(syncSince) : '');
      const data = await api('GET', q);
      syncSince = data.next_batch;
      const join = (data.rooms && data.rooms.join) || {};
      for (const rid of Object.keys(join)) {
        const room = join[rid];
        if (!room.timeline || !room.timeline.events) continue;
        for (const ev of room.timeline.events) routeMgmtEvent(rid, ev);
      }
    } catch (e) {
      if (!token) return;
      await new Promise(r => setTimeout(r, 3000));
    }
  }
}

// D-3: dispatch ONLY events whose room is a resolved mgmt room; anything else
// (a bridged/portal room that ever appears in the sync set) is dropped here
// and never reaches the console or the connection-status parser.
function routeMgmtEvent(roomId, ev) {
  const wa = runtime.whatsapp.mgmtRoomId;
  const im = runtime.imessage.mgmtRoomId;
  if (wa && roomId === wa) { handleMgmtEvent(WA, ev); return; }
  if (im && roomId === im) { handleMgmtEvent(IMSG, ev); return; }
  return; // not a management room -> ignore entirely
}

function handleMgmtEvent(source, ev) {
  if (ev.type === 'm.room.redaction') {
    if (source.id === 'whatsapp' && qr.eventId && ev.redacts === qr.eventId) clearQR();
    return;
  }
  if (ev.type !== 'm.room.message' || !ev.content) return;
  const fromBot = ev.sender === source.botMxid;   // D-3: only this source's bot
  const fromMe = ev.sender === userId;
  if (!fromBot && !fromMe) return;

  const rel = ev.content['m.relates_to'];
  const isEdit = rel && rel.rel_type === 'm.replace';
  const content = isEdit && ev.content['m.new_content'] ? ev.content['m.new_content'] : ev.content;

  if (fromBot && source.id === 'whatsapp' && content.msgtype === 'm.image') {
    if (isEdit && qr.eventId && rel.event_id !== qr.eventId) return;
    showQR(isEdit ? rel.event_id : ev.event_id, content.url);
    return;
  }
  if (fromBot && typeof content.body === 'string') {
    const body = sanitize(content.body.replace(/^\* /, ''));
    logConsole('bot', body, source.id);
    if (source.id === 'whatsapp') reactToBotReply(body);
    else if (source.id === 'imessage') updateImsgCard(content.body);
  } else if (fromMe && typeof content.body === 'string' &&
             !String(ev.unsigned && ev.unsigned.transaction_id || '').startsWith('hub-')) {
    logConsole('you', sanitize(content.body), source.id); // sent from Element etc.
  }
}

function reactToBotReply(body) {
  // WhatsApp status parsing for the Connections card.
  if (/You're not logged in/i.test(body)) updateCardStatus([]);
  const logins = [...body.matchAll(/^\* `([^`\n]+)` \(([^)\n]*)\) - `([A-Z_]+)`/gm)]
    .map(m => ({ id: m[1], name: m[2], state: m[3] }));
  if (logins.length) updateCardStatus(logins);
  if (/Successfully logged in/i.test(body)) {
    setLoginFlow(false); clearQR();
    sendStatusRefresh();
  }
  if (/Login cancelled|cancell?ed/i.test(body) && loginFlowActive) { setLoginFlow(false); clearQR(); }
}

// ---- WhatsApp QR handling ----
async function showQR(eventId, mxcUrl) {
  const m = MXC_RE.exec(String(mxcUrl || ''));
  if (!m || m[1] !== SERVER_NAME) return; // only local, well-formed media
  try {
    const res = await fetch(HS + '/_matrix/client/v1/media/download/' +
      encodeURIComponent(m[1]) + '/' + encodeURIComponent(m[2]),
      { headers: { 'Authorization': 'Bearer ' + token } });
    if (!res.ok) return;
    const blob = new Blob([await res.arrayBuffer()], { type: 'image/png' });
    clearQR();
    qr.eventId = eventId;
    qr.blobUrl = URL.createObjectURL(blob);
    const box = $('qr-box');
    if (!box) { URL.revokeObjectURL(qr.blobUrl); qr = { eventId: null, blobUrl: null }; return; }
    const img = el('img');
    img.alt = 'WhatsApp login QR code';
    img.src = qr.blobUrl;
    box.appendChild(el('div', 'muted', 'Scan with WhatsApp: Settings → Linked devices → Link a device'));
    box.appendChild(img);
    box.classList.remove('hidden');
    setLoginFlow(true);
  } catch (e) { /* leave card unchanged */ }
}
function clearQR() {
  if (qr.blobUrl) URL.revokeObjectURL(qr.blobUrl);
  qr = { eventId: null, blobUrl: null };
  const box = $('qr-box');
  if (box) { box.replaceChildren(); box.classList.add('hidden'); }
}

// ---- sidebar conversation lists (SEPARATE read path from the command loop) --
// One-shot filtered sync snapshot: room names, space memberships/children, and
// last message. This never feeds the console or the status parser (D-3).
async function fetchSnapshot() {
  const filter = encodeURIComponent(JSON.stringify({
    room: { timeline: { limit: 5 }, state: { lazy_load_members: true } },
    presence: { types: [] }, account_data: { types: [] },
  }));
  return await api('GET', '/_matrix/client/v3/sync?timeout=0&filter=' + filter);
}
function parseSnapshot(data) {
  const rooms = {};
  const join = (data.rooms && data.rooms.join) || {};
  for (const rid of Object.keys(join)) {
    const r = join[rid];
    const info = { id: rid, name: null, isSpace: false, children: [], lastBody: null };
    const states = (r.state && r.state.events) || [];
    for (const e of states) {
      if (e.type === 'm.room.name' && e.state_key === '') info.name = e.content && e.content.name;
      if (e.type === 'm.room.create' && e.content && e.content.type === 'm.space') info.isSpace = true;
      if (e.type === 'm.space.child' && e.state_key && e.content && Object.keys(e.content).length) {
        info.children.push(e.state_key);           // bridge-written; intersected below (D-5)
      }
    }
    const tl = (r.timeline && r.timeline.events) || [];
    for (let i = tl.length - 1; i >= 0; i--) {
      const e = tl[i];
      if (e.type === 'm.room.message' && e.content && typeof e.content.body === 'string') {
        info.lastBody = e.content.body; break;
      }
    }
    rooms[rid] = info;
  }
  return rooms;
}
// D-5: a space child is listed/navigable ONLY if it is itself a joined room.
function buildConvos(source, rooms) {
  // Match by name prefix: mautrix names its space "WhatsApp (+1...)", iMessage is exact.
  // (Purely which space feeds the tab; D-5's joined-rooms intersection below still
  // governs what is listed/navigable, so this is functional, not a security control.)
  const space = Object.values(rooms).find(r => r.isSpace && typeof r.name === 'string' && r.name.startsWith(source.spaceName));
  const convos = [];
  if (!space) return convos;
  for (const childId of space.children) {
    if (!rooms[childId]) continue;                 // not in joined set -> excluded
    if (!ROOMID_RE.test(childId)) continue;        // malformed id -> excluded
    const r = rooms[childId];
    convos.push({
      id: childId,
      title: sanitizeLine(r.name || childId),
      sub: sanitizeLine(r.lastBody || ''),
      sourceId: source.id,
      sourceLabel: source.label,
    });
  }
  return convos;
}
async function refreshConvos() {
  const rooms = parseSnapshot(await fetchSnapshot());
  joinedSet = new Set(Object.keys(rooms));         // authoritative joined set (D-5)
  for (const s of SOURCES) {
    if (s.kind === 'all') continue;
    convosBySource[s.id] = buildConvos(s, rooms);
  }
}

// ===========================================================================
// Home feed (HM.2) — a SECOND, isolated read path (HF-1). Every function below
// touches only the feed model + render/nav; NONE references handleMgmtEvent,
// logConsole, reactToBotReply, sendCmd, updateImsgCard, or the status parser.
// Bridged portal content (names, last-message bodies) is read only here and in
// the existing snapshot path (parseSnapshot/buildConvos, already D-3-separate);
// it never reaches the command/console path. Nav is only via openConversation.
// ===========================================================================

// HF-4: compute a room's "last message" preview under a strict whitelist.
// Returns {body, ts} for a qualifying event, else null. Reads `body` ONLY,
// never `formatted_body`. Media msgtypes yield a STATIC label, never the
// bridged filename/body. Reactions/redactions/receipts/typing/state are not
// messages and return null (they never update lastBody/lastTs).
function feedPreviewFromEvent(ev) {
  if (!ev || ev.type !== 'm.room.message' || !ev.content) return null;
  let content = ev.content;
  const rel = content['m.relates_to'];
  if (rel && rel.rel_type === 'm.replace') {        // edit: read m.new_content.body only
    content = content['m.new_content'];
    if (!content) return null;
  }
  const mt = content.msgtype;
  let body;
  if ((mt === 'm.text' || mt === 'm.notice') && typeof content.body === 'string') {
    body = content.body;                            // text/notice: the real (sanitized-on-render) body
  } else if (mt === 'm.image') { body = 'Photo'; }  // media: static label ONLY (never the filename)
  else if (mt === 'm.video') { body = 'Video'; }
  else if (mt === 'm.audio') { body = 'Audio'; }
  else if (mt === 'm.file')  { body = 'File'; }
  else { return null; }                             // anything else is not a previewable message
  return { body, ts: typeof ev.origin_server_ts === 'number' ? ev.origin_server_ts : 0 };
}

// HF-5: keep only the LAST qualifying message in a room's timeline slice.
function feedLastPreview(room) {
  const tl = (room && room.timeline && room.timeline.events) || [];
  for (let i = tl.length - 1; i >= 0; i--) {
    const p = feedPreviewFromEvent(tl[i]);
    if (p) return p;
  }
  return null;
}

// Seed / re-validate the feed model from the SAME validated snapshot path the
// sidebar uses (ROOMID_RE ∩ joinedSet ∩ known-source-space child, via
// buildConvos). One record per room; dedup a doubly-listed room by first
// SOURCES order (HF-6). Merge preserves live-fresher previews on re-validation.
async function seedFeed() {
  const data = await fetchSnapshot();
  const rooms = parseSnapshot(data);
  joinedSet = new Set(Object.keys(rooms));          // authoritative joined set (D-5)
  for (const s of SOURCES) {
    if (s.kind === 'all') continue;
    convosBySource[s.id] = buildConvos(s, rooms);   // reuse the validated builder
  }
  const join = (data.rooms && data.rooms.join) || {};
  const seen = new Set();
  for (const s of SOURCES) {
    if (s.kind === 'all') continue;
    for (const c of (convosBySource[s.id] || [])) {
      if (seen.has(c.id)) continue;                 // HF-6: first SOURCES order wins
      seen.add(c.id);
      const p = feedLastPreview(join[c.id]);        // HF-4 whitelist
      const existing = feedModel.get(c.id);
      if (existing) {
        existing.name = c.title;                    // refresh name; keep original attribution
        if (p && p.ts > existing.lastTs) { existing.lastBody = p.body; existing.lastTs = p.ts; }
      } else {
        feedModel.set(c.id, {
          id: c.id, name: c.title,                  // c.title already sanitizeLine'd by buildConvos
          lastBody: p ? p.body : '', lastTs: p ? p.ts : 0, sourceId: s.id,
        });
      }
    }
  }
  for (const rid of [...feedModel.keys()]) {         // drop rooms no longer validated
    if (!seen.has(rid)) feedModel.delete(rid);
  }
}

// HF-3: pick up genuinely-new portals only via a DEBOUNCED re-validation,
// never by trusting the live stream to add a room.
function scheduleFeedRevalidate() {
  if (feedRevalTimer) return;
  feedRevalTimer = setTimeout(async () => {
    feedRevalTimer = null;
    try { await seedFeed(); scheduleFeedRender(); } catch (e) { /* keep current model */ }
  }, 4000);
}

// The live feed handler. Updates/bubbles ONLY roomIds already in the validated
// feed model; any other roomId is ignored (never added from the live stream).
function feedIngest(data) {
  const join = (data.rooms && data.rooms.join) || {};
  let changed = false, sawUnknown = false;
  for (const rid of Object.keys(join)) {
    if (!feedModel.has(rid)) { sawUnknown = true; continue; }  // HF-3: ignore unknown room ids
    const p = feedLastPreview(join[rid]);            // HF-4/HF-5: last qualifying message only
    if (!p) continue;
    const rec = feedModel.get(rid);
    if (p.ts >= rec.lastTs) { rec.lastBody = p.body; rec.lastTs = p.ts; changed = true; }
  }
  if (sawUnknown) scheduleFeedRevalidate();
  if (changed) scheduleFeedRender();
}

// HF-1/HF-5: the isolated feed /sync long-poll. Its handler (feedIngest) has no
// lexical path to any command/console function. Small timeline limit, ~25000
// long-poll timeout + 3s error backoff, mirroring startSync (but with its own
// feedSince/feedRunning). No message bodies are logged anywhere.
async function startFeedSync() {
  if (feedRunning) return;
  feedRunning = true;
  feedSince = null;
  while (feedRunning && token) {
    try {
      const filter = encodeURIComponent(JSON.stringify({
        room: { timeline: { limit: 5 }, state: { types: [] } },
        presence: { types: [] }, account_data: { types: [] },
      }));
      const q = '/_matrix/client/v3/sync?timeout=25000&filter=' + filter +
        (feedSince ? '&since=' + encodeURIComponent(feedSince) : '');
      const data = await api('GET', q);
      feedSince = data.next_batch;
      feedIngest(data);
    } catch (e) {
      if (!token) return;
      await new Promise(r => setTimeout(r, 3000));
    }
  }
}

// HF-5: coalesce renders — one timer per batch so a burst = one re-render.
function scheduleFeedRender() {
  if (feedRenderScheduled) return;
  feedRenderScheduled = true;
  setTimeout(() => { feedRenderScheduled = false; renderHome(); }, 0);
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

// HF-6/HF-7: badge derived ONLY from the record's sourceId (which space the
// room is in) — never a bridged field. A CSS-classed pill carrying the source
// icon via textContent. No <img>, no data:/remote URL (CSP byte-identical).
function buildPlatBadge(sourceId) {
  const cls = sourceId === 'imessage' ? 'plat-badge imessage'
            : sourceId === 'whatsapp' ? 'plat-badge whatsapp'
            : 'plat-badge';
  const source = SOURCES.find(s => s.id === sourceId);
  return el('span', cls, (source && source.icon) || '');
}

// A feed row reuses the existing .convo structure; click → openConversation
// (the only validated nav path; it also shows the Element pane via navTo('all')).
function buildFeedRow(r) {
  const name = sanitizeLine(r.name || r.id);
  const preview = sanitizeLine(r.lastBody || '');   // HF-4: single-line, clamped, textContent
  const row = el('div', 'convo');
  row.setAttribute('role', 'button');
  row.tabIndex = 0;
  row.appendChild(el('div', 'avatar', (name || '?').slice(0, 1).toUpperCase()));
  const meta = el('div', 'meta');
  meta.appendChild(el('div', 'title', name));
  meta.appendChild(el('div', 'preview', preview));
  row.appendChild(meta);
  if (r.lastTs) row.appendChild(el('span', 'when', feedRelTime(r.lastTs)));
  row.appendChild(buildPlatBadge(r.sourceId));
  const open = () => openConversation(r.id);         // U-1: validated; shows Element pane
  row.addEventListener('click', open);
  row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  return row;
}

// HF-8: render the merged feed sorted by recency, capped at ~200 rows. The
// #home-search box is a pure client-side filter over the full in-memory model
// (sanitized name + preview); it never builds a URL, sends a command, or
// navigates. Clearing restores the full recency-sorted list.
function renderHome() {
  const list = $('home-list');
  if (!list) return;
  const q = (($('home-search') && $('home-search').value) || '').trim().toLowerCase();
  const all = [...feedModel.values()].sort((a, b) => b.lastTs - a.lastTs);
  const rows = (q
    ? all.filter(r => sanitizeLine(r.name).toLowerCase().includes(q) ||
                      sanitizeLine(r.lastBody || '').toLowerCase().includes(q))
    : all).slice(0, 200);
  list.replaceChildren();
  if (!rows.length) {
    list.appendChild(elEmpty(q ? 'No conversations match your search.' : 'No conversations yet.'));
    return;
  }
  for (const r of rows) list.appendChild(buildFeedRow(r));
}

// ---- open a conversation (U-1 / D-4) ----
function openConversation(roomId) {
  if (!ROOMID_RE.test(roomId)) return;             // reject ids failing the regex
  if (!joinedSet.has(roomId)) return;              // D-5: must be a joined room
  const f = $('chats-container') && $('chats-container').querySelector('iframe');
  if (f) f.src = CHATS_URL + '/#/room/' + roomId;  // constant prefix + validated RAW id
  navTo('all');
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
  const open = () => openConversation(c.id);
  row.addEventListener('click', open);
  row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  return row;
}
function elEmpty(text) { return el('div', 'list-empty', text); }

async function loadSourceList(sourceId) {
  const source = SOURCES.find(s => s.id === sourceId);
  const list = $('source-list');
  $('source-title').textContent = source.label;
  $('source-sub').textContent = 'Your conversations on ' + source.label + '. Click one to open it.';
  list.replaceChildren();
  list.appendChild(elEmpty('Loading…'));
  try {
    await refreshConvos();
  } catch (e) {
    list.replaceChildren(elEmpty('Could not load conversations: ' + String(e.message || e)));
    return;
  }
  list.replaceChildren();
  const convos = convosBySource[sourceId] || [];
  if (!convos.length) { list.appendChild(elEmpty('No conversations yet on ' + source.label + '.')); return; }
  for (const c of convos) list.appendChild(buildConvoRow(c, false));
}

// ---- Directory (P3.3): cross-source local filter + gated start-chat ----
function buildDirectory() {
  const results = $('directory-results');
  const card = el('div', 'card');
  card.id = 'directory-startchat';
  card.appendChild(el('h3', '', 'Start a new chat'));
  for (const s of SOURCES) {
    if (s.kind === 'all' || !s.botMxid) continue;
    // iMessage needs a first message (the engine cannot open an empty thread),
    // so its Directory control is a two-field form (handle + first message).
    const twoField = s.id === 'imessage';
    const row = el('div', 'cmd start-chat');
    const info = el('div', 'info');
    info.appendChild(el('div', 'name', s.label));
    info.appendChild(el('div', 'desc', s.canStartChat
      ? (twoField
          ? 'Enter a phone number (+1…) or email and a first message to open a new chat.'
          : 'Enter a phone number (+1…) or email to open a new chat.')
      : 'Starting a new chat here is not available yet.'));
    row.appendChild(info);
    const input = el('input');
    input.spellcheck = false;
    input.autocomplete = 'off';
    input.placeholder = s.canStartChat ? '+14155551234 or name@example.com' : 'not available yet';
    let msgInput = null;
    if (twoField) {
      msgInput = el('input');
      msgInput.spellcheck = false;
      msgInput.autocomplete = 'off';
      msgInput.maxLength = 900;                     // UX clamp; daemon re-clamps to MAX_TEXT
      msgInput.placeholder = s.canStartChat ? 'first message' : 'not available yet';
    }
    const btn = el('button', s.canStartChat ? 'primary' : '', 'Start chat');
    btn.style.width = 'auto';
    if (s.canStartChat) {
      btn.classList.add('startable');              // toggled by setButtonsDisabled
      const go = () => startChat(s.id, input, msgInput);
      btn.addEventListener('click', go);
      input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
      if (msgInput) msgInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
    } else {
      // D-1: capability-gated OFF -> inert control that issues NO bot command.
      input.disabled = true;
      if (msgInput) msgInput.disabled = true;
      btn.disabled = true;
      btn.textContent = 'Not available yet';
    }
    row.appendChild(input);
    if (msgInput) row.appendChild(msgInput);
    row.appendChild(btn);
    card.appendChild(row);
  }
  results.parentNode.insertBefore(card, results);
  $('directory-search').addEventListener('input', renderDirectory);
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
function renderDirectory() {
  const q = ($('directory-search').value || '').trim().toLowerCase();
  const out = $('directory-results');
  out.replaceChildren();
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
  if (!total) out.appendChild(elEmpty(q ? 'No conversations match your search.' : 'No conversations yet.'));
}

// ---- navigation / views ----
function showAuth(signedIn) {
  $('shell').classList.toggle('hidden', !signedIn);
  $('view-signin').classList.toggle('hidden', signedIn);
}
function setActiveNav(key) {
  for (const b of document.querySelectorAll('.navitem')) b.classList.toggle('active', b.dataset.navkey === key);
}
function showSection(id) {
  for (const s of document.querySelectorAll('#content .view')) s.classList.toggle('hidden', s.id !== id);
}
async function navTo(key) {
  setActiveNav(key);
  if (key === 'home') {
    showSection('view-home');                        // unified recent-conversations feed
    renderHome();
  } else if (key === 'all') {
    showSection('view-chats');                      // Element pane stays mounted
  } else if (key.indexOf('source:') === 0) {
    showSection('view-source');
    await loadSourceList(key.slice(7));
  } else if (key === 'directory') {
    showSection('view-directory');
    try { await refreshConvos(); } catch (e) {}
    renderDirectory();
  } else if (key === 'connections') {
    showSection('view-connections');
    if (runtime.imessage.mgmtRoomId) sendCmd('imessage', 'status'); // refresh checklist
  } else if (key === 'settings') {
    showSection('view-settings');
  }
}
function buildNav() {
  const nav = $('nav-sources');
  nav.replaceChildren();
  for (const s of SOURCES) {
    // The former "All chats" Element tab becomes Home — the default landing
    // feed. The Element pane (view-chats) has no nav item; it is shown when a
    // feed/list row is opened via openConversation.
    if (s.kind === 'all') {
      const home = el('button', 'navitem');
      home.type = 'button';
      home.dataset.navkey = 'home';
      home.appendChild(el('span', 'ic', '🏠'));
      home.appendChild(document.createTextNode(' Home'));
      home.addEventListener('click', () => navTo('home'));
      nav.appendChild(home);
      continue;
    }
    const key = 'source:' + s.id;
    const btn = el('button', 'navitem');
    btn.type = 'button';
    btn.dataset.navkey = key;
    btn.appendChild(el('span', 'ic', s.icon || '•'));
    btn.appendChild(document.createTextNode(' ' + s.label));
    btn.addEventListener('click', () => navTo(key));
    nav.appendChild(btn);
  }
  wireTool('nav-directory', 'directory');
  wireTool('nav-connections', 'connections');
  wireTool('nav-settings', 'settings');
}
function wireTool(id, key) {
  const b = $(id);
  if (!b) return;
  b.dataset.navkey = key;
  b.addEventListener('click', () => navTo(key));
}

// A2-4: the Element iframe exists only while the hub is signed in, and stays
// mounted across view switches (its src is a constant, never derived from data
// except via the validated openConversation() path).
function mountChats() {
  const holder = $('chats-container');
  if (holder.querySelector('iframe')) return;
  const f = el('iframe');
  f.src = CHATS_URL;
  f.allow = 'clipboard-write; fullscreen';
  f.title = 'Chats (Element)';
  holder.appendChild(f);
}
function unmountChats() {
  const f = $('chats-container') && $('chats-container').querySelector('iframe');
  if (f) f.remove();
}

// ---- console ----
function logConsole(who, text, srcId) {
  const c = $('console');
  if (!c) return;
  const entry = el('div', 'entry');
  const label = who === 'you' ? 'you' : who === 'error' ? 'error' : (srcId || 'bridge');
  entry.appendChild(el('span', 'who' + (who === 'you' ? ' you' : ''), label + '  '));
  entry.appendChild(el('span', who === 'error' ? 'error' : '', text));
  c.appendChild(entry);
  while (c.childElementCount > 200) c.removeChild(c.firstElementChild);
  c.scrollTop = c.scrollHeight;
}

function setButtonsDisabled(v) {
  for (const b of document.querySelectorAll('#command-groups button, .bridge-actions button, .startable')) {
    b.disabled = v;                                 // capability-disabled controls are excluded
  }
}
function setLoginFlow(active) {
  loginFlowActive = active;
  const btn = $('btn-cancel-login');
  if (btn) btn.classList.toggle('hidden', !active);
  if (!active) clearQR();
}

// ---- WhatsApp Connections card status ----
function updateCardStatus(logins) {
  const pill = $('wa-status');
  const disc = $('btn-disconnect');
  if (!pill) return;
  if (logins.length) {
    const l = logins[0];
    pill.textContent = 'Connected: ' + l.name + ' (' + l.state + ')';
    pill.classList.add('ok');
    if (disc) { disc.classList.remove('hidden'); disc.dataset.loginId = l.id; }
  } else {
    pill.textContent = 'Not connected';
    pill.classList.remove('ok');
    if (disc) disc.classList.add('hidden');
  }
}

// ---- iMessage Connections card (Phase 2 B2 / P2.5) ----
// Renders the bot's plain-text checklist reply via sanitize + textContent.
function updateImsgCard(rawBody) {
  const ul = $('imsg-checklist');
  if (!ul) return;
  ul.replaceChildren();
  const clean = sanitize(rawBody);                 // keeps \n; strips controls/bidi
  const lines = clean.split('\n').map(l => l.trim()).filter(Boolean);
  for (const line of lines) ul.appendChild(el('li', '', sanitize(line)));
  const pill = $('imsg-status');
  if (pill) {
    const ok = /✓/.test(clean) && !/✗/.test(clean); // all ✓, no ✗
    pill.textContent = lines.length ? (ok ? 'Ready' : 'Setup needed') : 'No status yet';
    pill.classList.toggle('ok', ok);
  }
}

// Confirmation modal; type-to-confirm for the most destructive action.
function confirmModal(title, text, typed) {
  return new Promise((resolve) => {
    $('modal-title').textContent = title;
    $('modal-text').textContent = text;
    const input = $('modal-input');
    input.value = '';
    input.classList.toggle('hidden', !typed);
    $('modal-backdrop').classList.remove('hidden');
    const done = (ok) => {
      $('modal-backdrop').classList.add('hidden');
      $('modal-ok').onclick = null; $('modal-cancel').onclick = null;
      resolve(ok);
    };
    $('modal-ok').onclick = () => {
      if (typed && input.value !== 'delete') return;
      done(true);
    };
    $('modal-cancel').onclick = () => done(false);
  });
}

// ---- Connections view ----
function buildConnections() {
  const holder = $('bridge-cards');
  holder.replaceChildren();

  // WhatsApp card
  const wa = el('div', 'card bridge-card');
  const waHead = el('div', 'bridge-head');
  waHead.appendChild(el('span', 'bridge-name', WA.label));
  const waPill = el('span', 'status-pill', 'Checking…');
  waPill.id = 'wa-status';
  waHead.appendChild(waPill);
  wa.appendChild(waHead);
  wa.appendChild(el('p', 'muted', WA.blurb));

  const waActions = el('div', 'bridge-actions');
  const connect = el('button', 'primary', 'Connect (scan QR)');
  connect.style.width = 'auto';
  connect.addEventListener('click', () => sendCmd('whatsapp', 'login qr'));
  waActions.appendChild(connect);

  const cancel = el('button', '', 'Cancel login');
  cancel.id = 'btn-cancel-login';
  cancel.classList.add('hidden');
  cancel.addEventListener('click', () => sendCmd('whatsapp', 'cancel'));
  waActions.appendChild(cancel);

  const disc = el('button', 'danger', 'Disconnect');
  disc.id = 'btn-disconnect';
  disc.classList.add('hidden');
  disc.addEventListener('click', async () => {
    const id = disc.dataset.loginId;
    if (!id) return;
    if (await confirmModal('Disconnect WhatsApp?',
      'This unlinks the bridge from your WhatsApp account. Bridged rooms stay until deleted.', false)) {
      await sendCmd('whatsapp', 'logout ' + id);
      sendStatusRefresh();
    }
  });
  waActions.appendChild(disc);

  const refresh = el('button', '', 'Refresh status');
  refresh.addEventListener('click', sendStatusRefresh);
  waActions.appendChild(refresh);
  wa.appendChild(waActions);

  const qrBox = el('div', 'qr-box hidden');
  qrBox.id = 'qr-box';
  wa.appendChild(qrBox);
  holder.appendChild(wa);

  // iMessage card (B2 hub-side)
  const im = el('div', 'card bridge-card');
  const imHead = el('div', 'bridge-head');
  imHead.appendChild(el('span', 'bridge-name', IMSG.label));
  const imPill = el('span', 'status-pill', 'Checking…');
  imPill.id = 'imsg-status';
  imHead.appendChild(imPill);
  im.appendChild(imHead);
  im.appendChild(el('p', 'muted', IMSG.blurb));

  const imActions = el('div', 'bridge-actions');
  const setup = el('button', 'primary', 'Set up iMessage');
  setup.style.width = 'auto';
  setup.addEventListener('click', () => sendCmd('imessage', 'setup'));
  imActions.appendChild(setup);

  const imStatus = el('button', '', 'Check status');
  imStatus.addEventListener('click', () => sendCmd('imessage', 'status'));
  imActions.appendChild(imStatus);
  im.appendChild(imActions);

  const checklist = el('ul', 'checklist');
  checklist.id = 'imsg-checklist';
  im.appendChild(checklist);
  holder.appendChild(im);

  // Planned sources (inert placeholders).
  const more = el('div', 'card src-placeholder');
  more.appendChild(el('h3', '', 'More sources'));
  more.appendChild(el('p', 'muted',
    'This hub is built to bridge every messaging account into one place. Each source below becomes a card like WhatsApp once its bridge is deployed on this stack.'));
  for (const name of PLANNED_SOURCES) {
    const row = el('div', 'cmd');
    const info = el('div', 'info');
    info.appendChild(el('div', 'name', name));
    info.appendChild(el('div', 'desc', 'Not connected — bridge not deployed yet.'));
    row.appendChild(info);
    more.appendChild(row);
  }
  holder.appendChild(more);
}

// ---- Settings view (per-source command surface) ----
function buildSettings() {
  const tabs = $('settings-source-tabs');
  tabs.replaceChildren();
  for (const s of SOURCES) {
    if (s.kind === 'all' || !s.botMxid) continue;
    const b = el('button', '', s.label);
    b.type = 'button';
    b.dataset.src = s.id;
    b.addEventListener('click', () => {
      activeSettingsSource = s.id;
      renderSettingsTabs();
      renderCommandGroups(s.id);
    });
    tabs.appendChild(b);
  }
  renderSettingsTabs();
  renderCommandGroups(activeSettingsSource);
}
function renderSettingsTabs() {
  for (const b of $('settings-source-tabs').children) {
    b.classList.toggle('active', b.dataset.src === activeSettingsSource);
  }
}
function renderCommandGroups(sourceId) {
  const holder = $('command-groups');
  holder.replaceChildren();
  for (const g of groupsFor(sourceId)) {
    const groupEl = el('div', 'card cmd-group');
    groupEl.appendChild(el('h3', '', g.title));
    for (const c of g.cmds) {
      const row = el('div', 'cmd');
      const info = el('div', 'info');
      info.appendChild(el('div', 'name', c.label));
      info.appendChild(el('div', 'desc', c.desc));
      row.appendChild(info);
      let argInput = null;
      if (c.arg) {
        argInput = el('input');
        argInput.placeholder = c.arg;
        row.appendChild(argInput);
      }
      const btn = el('button', c.confirm === 'type' ? 'danger' : '', 'Run');
      btn.addEventListener('click', async () => {
        let text = c.cmd;
        if (argInput) {
          const v = argInput.value.trim();
          if (!v) { logConsole('error', c.label + ': an argument is required (' + c.arg + ').'); return; }
          text += ' ' + v;
        }
        if (c.confirm === 'type') {
          if (!(await confirmModal(c.label, 'This is irreversible on the Matrix side. Type "delete" to confirm.', true))) return;
        } else if (c.confirm === 'click') {
          if (!(await confirmModal(c.label, c.desc + ' Continue?', false))) return;
        }
        await sendCmd(sourceId, text);
        if (argInput) argInput.value = '';
      });
      row.appendChild(btn);
      groupEl.appendChild(row);
    }
    holder.appendChild(groupEl);
  }
}

// ---- app entry ----
async function enterApp() {
  $('whoami').textContent = userId;
  const wrap = $('signout-note-wrap');
  if (wrap) wrap.classList.add('hidden');
  buildNav();
  buildConnections();
  buildSettings();
  buildDirectory();
  mountChats();                                     // Element pane exists so openConversation can target it
  showAuth(true);
  navTo('home');                                    // HF-8: default view = Home feed
  // Seed the isolated feed model from the validated snapshot, render, then start
  // the SEPARATE feed /sync loop (HF-1). This is independent of startSync below.
  try { await seedFeed(); renderHome(); } catch (e) { /* feed stays empty on error */ }
  startFeedSync();
  try { runtime.whatsapp.mgmtRoomId = await resolveMgmt(WA); }
  catch (e) { logConsole('error', 'WhatsApp management room: ' + String(e.message || e)); }
  try { runtime.imessage.mgmtRoomId = await resolveImsgMgmt(); }
  catch (e) { /* iMessage mgmt room may not exist yet; card stays "not set up" */ }
  startSync();
  await sendStatusRefresh();                        // WhatsApp list-logins
  if (runtime.imessage.mgmtRoomId) await sendCmd('imessage', 'status');
  else { const pill = $('imsg-status'); if (pill) pill.textContent = 'Not set up'; }
}

// ---- wiring ----
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
  $('console-send').addEventListener('click', async () => {
    const v = $('console-input').value.trim();
    if (!v) return;
    $('console-input').value = '';
    await sendCmd(activeSettingsSource, v);
  });
  $('console-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('console-send').click(); });

  // HF-8: Home search is a pure client-side filter over the in-memory feed
  // model; it never builds a URL, sends a command, or navigates.
  const homeSearch = $('home-search');
  if (homeSearch) homeSearch.addEventListener('input', renderHome);

  // restore session
  try {
    const t = sessionStorage.getItem('hub_token'), u = sessionStorage.getItem('hub_user');
    if (t && u) { token = t; userId = u; enterApp(); return; }
  } catch (e) {}
  showAuth(false);
});
