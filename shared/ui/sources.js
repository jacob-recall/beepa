// Relocated verbatim from hub/site/app.js (PLAN-MASTER-SYNC-IMPL P1.2).
// Shared ES module. Logic unchanged; only import/export + shared-state (S) access added.

import { HS, MXC_RE, SERVER_NAME, api } from '../matrix/client.js';
import { logConsole, setButtonsDisabled, setLoginFlow, updateCardStatus, updateImsgCard } from './connections.js';
import { $, el, sanitize, txn } from './el.js';
import { S, runtime } from '../state.js';

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
  { id: 'gmessages', label: 'Google Messages', kind: 'source', botMxid: '@gmessagesbot:localhost',
    spaceName: 'Google Messages', canStartChat: true, icon: '📱',
    blurb: 'Link your Google account with a quick sign-in; your chats appear as rooms in Element.' },
  { id: 'instagram', label: 'Instagram', kind: 'source', botMxid: '@instagrambot:localhost',
    spaceName: 'Instagram', canStartChat: true, icon: '📷',
    blurb: 'Bridge your Instagram DMs: sign into instagram.com, then run the connect helper (it copies your session for one paste); chats appear as rooms in Element.' },
  { id: 'linkedin', label: 'LinkedIn', kind: 'source', botMxid: '@linkedinbot:localhost',
    spaceName: 'LinkedIn', canStartChat: false, icon: '💼',
    blurb: 'Bridge your LinkedIn messages: sign into linkedin.com, then run the connect helper — it links automatically; chats appear as rooms in Element.' },
  { id: 'twitter', label: 'X', kind: 'source', botMxid: '@twitterbot:localhost',
    spaceName: 'Twitter', canStartChat: true, icon: '✖️',
    blurb: 'Bridge your X (Twitter) DMs: sign into x.com, then run the connect helper — it links automatically; chats appear as rooms in Element.' },
];
const WA = SOURCES.find(s => s.id === 'whatsapp');
const IMSG = SOURCES.find(s => s.id === 'imessage');
const GMSG = SOURCES.find(s => s.id === 'gmessages');
const IG = SOURCES.find(s => s.id === 'instagram');
const LI = SOURCES.find(s => s.id === 'linkedin');
const TW = SOURCES.find(s => s.id === 'twitter');
// The ONLY sender whose com.jkali.from_me marker is trusted (anti-spoof): our
// own iMessage appservice bot. Never a ghost (@imessage_*) or a remote contact.
const IMSG_BOT_MXID = IMSG.botMxid;                // '@imessagebot:localhost'

// Future sources: inert placeholders in the Connections view until deployed.
const PLANNED_SOURCES = ['Telegram', 'Signal', 'Discord', 'Slack'];

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

// ---- Google Messages command surface (mirrors WhatsApp's shape) ----
const GMSG_COMMAND_GROUPS = [
  { title: 'General', cmds: [
    { cmd: 'help', label: 'Help', desc: "Show the bridge's own list of every command." },
    { cmd: 'version', label: 'Version', desc: 'Show which bridge version is running.' },
  ]},
  { title: 'Account', cmds: [
    { cmd: 'list-logins', label: 'List logins', desc: 'List the Google Messages accounts linked to the bridge and their connection state.' },
    { cmd: 'logout', label: 'Unlink account', desc: 'Disconnect a linked Google Messages account from the bridge.', arg: 'login ID', confirm: 'click' },
    { cmd: 'set-preferred-login', label: 'Preferred account', desc: 'Choose which account sends your messages when more than one is linked.', arg: 'login ID' },
  ]},
  { title: 'Chats & contacts', cmds: [
    { cmd: 'sync', label: 'Sync now', desc: 'Refresh chats and contacts from Google Messages.' },
    { cmd: 'start-chat', label: 'Start a chat', desc: 'Open a new direct chat with a phone number.', arg: '+14155551234' },
    { cmd: 'search', label: 'Search contacts', desc: 'Search your Google Messages contacts by name or number.', arg: 'name or number' },
    { cmd: 'resolve-identifier', label: 'Check a number', desc: 'Check whether a phone number is on Google Messages without starting a chat.', arg: '+14155551234' },
  ]},
  { title: 'Danger zone', cmds: [
    { cmd: 'delete-all-portals', label: 'Delete all bridged rooms', desc: 'Permanently delete every bridged chat room on the Matrix side (nothing is deleted on Google Messages itself).', confirm: 'type' },
  ]},
];
// ---- Instagram command surface (mirrors Google Messages' shape) ----
// No bulk follower/following enumeration command (anti-ban posture, SPEC §3/§7).
const IG_COMMAND_GROUPS = [
  { title: 'General', cmds: [
    { cmd: 'help', label: 'Help', desc: "Show the bridge's own list of every command." },
    { cmd: 'version', label: 'Version', desc: 'Show which bridge version is running.' },
  ]},
  { title: 'Account', cmds: [
    { cmd: 'list-logins', label: 'List logins', desc: 'List the Instagram accounts linked to the bridge and their connection state.' },
    { cmd: 'login instagram', label: 'Connect Instagram', desc: 'Start linking Instagram. Easiest: run  session-connect/connect.py instagram  and paste what it copies. Fallback: paste your instagram.com session as the next message.' },
    { cmd: 'logout', label: 'Unlink account', desc: 'Disconnect a linked Instagram account from the bridge.', arg: 'login ID', confirm: 'click' },
  ]},
  { title: 'Chats & contacts', cmds: [
    { cmd: 'sync', label: 'Sync now', desc: 'Refresh chats and contacts from Instagram.' },
    { cmd: 'search', label: 'Search by username', desc: 'Find an Instagram user by username to start a new chat.', arg: 'username' },
  ]},
];
// ---- LinkedIn command surface (mirrors Instagram's session-paste shape) ----
const LI_COMMAND_GROUPS = [
  { title: 'General', cmds: [
    { cmd: 'help', label: 'Help', desc: "Show the bridge's own list of every command." },
    { cmd: 'version', label: 'Version', desc: 'Show which bridge version is running.' },
  ]},
  { title: 'Account', cmds: [
    { cmd: 'list-logins', label: 'List logins', desc: 'List the LinkedIn accounts linked to the bridge and their connection state.' },
    { cmd: 'login cookies', label: 'Connect LinkedIn', desc: 'Start linking LinkedIn. Easiest: run  session-connect/connect.py linkedin  (links automatically). Fallback: paste a Copy-as-cURL as the next message.' },
    { cmd: 'logout', label: 'Unlink account', desc: 'Disconnect a linked LinkedIn account from the bridge.', arg: 'login ID', confirm: 'click' },
    { cmd: 'set-preferred-login', label: 'Preferred account', desc: 'Choose which account sends your messages when more than one is linked.', arg: 'login ID' },
  ]},
  { title: 'Chats & contacts', cmds: [
    { cmd: 'search', label: 'Search contacts', desc: 'Search your LinkedIn contacts by name.', arg: 'name' },
    { cmd: 'start-chat', label: 'Start a chat', desc: 'Open a new direct chat with a LinkedIn contact.', arg: 'identifier' },
    { cmd: 'resolve-identifier', label: 'Check an identifier', desc: 'Check whether an identifier is on LinkedIn without starting a chat.', arg: 'identifier' },
    { cmd: 'sync', label: 'Sync now', desc: 'Refresh chats and contacts from LinkedIn.' },
  ]},
  { title: 'Relay', cmds: [
    { cmd: 'set-relay', label: 'Enable relay', desc: 'Let other Matrix users in a room send messages through your LinkedIn account.', confirm: 'click' },
    { cmd: 'unset-relay', label: 'Disable relay', desc: "Stop relaying other users' messages through your account." },
  ]},
  { title: 'Danger zone', cmds: [
    { cmd: 'delete-all-portals', label: 'Delete all bridged rooms', desc: 'Permanently delete every bridged chat room on the Matrix side (nothing is deleted on LinkedIn itself).', confirm: 'type' },
  ]},
];

const TW_COMMAND_GROUPS = [
  { title: 'General', cmds: [
    { cmd: 'help', label: 'Help', desc: "Show the bridge's own list of every command." },
    { cmd: 'version', label: 'Version', desc: 'Show which bridge version is running.' },
  ]},
  { title: 'Account', cmds: [
    { cmd: 'list-logins', label: 'List logins', desc: 'List the X accounts linked to the bridge and their connection state.' },
    { cmd: 'login cookies', label: 'Connect X', desc: 'Start linking X. Easiest: run  session-connect/connect.py twitter  (links automatically). Fallback: paste a Copy-as-cURL as the next message.' },
    { cmd: 'logout', label: 'Unlink account', desc: 'Disconnect a linked X account from the bridge.', arg: 'login ID', confirm: 'click' },
    { cmd: 'set-preferred-login', label: 'Preferred account', desc: 'Choose which account sends your messages when more than one is linked.', arg: 'login ID' },
  ]},
  { title: 'Chats & contacts', cmds: [
    { cmd: 'search', label: 'Search contacts', desc: 'Search your X contacts by name.', arg: 'name' },
    { cmd: 'start-chat', label: 'Start a chat', desc: 'Open a new direct chat with an X user.', arg: 'identifier' },
    { cmd: 'resolve-identifier', label: 'Check an identifier', desc: 'Check whether an identifier is on X without starting a chat.', arg: 'identifier' },
    { cmd: 'sync', label: 'Sync now', desc: 'Refresh chats and contacts from X.' },
  ]},
  { title: 'Relay', cmds: [
    { cmd: 'set-relay', label: 'Enable relay', desc: 'Let other Matrix users in a room send messages through your X account.', confirm: 'click' },
    { cmd: 'unset-relay', label: 'Disable relay', desc: "Stop relaying other users' messages through your account." },
  ]},
  { title: 'Danger zone', cmds: [
    { cmd: 'delete-all-portals', label: 'Delete all bridged rooms', desc: 'Permanently delete every bridged chat room on the Matrix side (nothing is deleted on X itself).', confirm: 'type' },
  ]},
];
function groupsFor(sourceId) {
  if (sourceId === 'imessage') return IMSG_COMMAND_GROUPS;
  if (sourceId === 'gmessages') return GMSG_COMMAND_GROUPS;
  if (sourceId === 'instagram') return IG_COMMAND_GROUPS;
  if (sourceId === 'linkedin') return LI_COMMAND_GROUPS;
  if (sourceId === 'twitter') return TW_COMMAND_GROUPS;
  return COMMAND_GROUPS;
}

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

// ---- management-room resolution + verification (C-1) ----
async function resolveMgmt(source) {
  if (source.id === 'whatsapp' || source.id === 'gmessages' || source.id === 'instagram' || source.id === 'linkedin') return await findBotDmMgmt(source);
  if (source.id === 'imessage') return await resolveImsgMgmt();
  return null;
}
async function verifyMgmt(source, roomId) {
  if (source.id === 'whatsapp' || source.id === 'gmessages' || source.id === 'instagram' || source.id === 'linkedin') return await isBotDmMgmt(source, roomId);
  if (source.id === 'imessage') return await verifyImsgMgmt(roomId);
  return false;
}

// WhatsApp / Google Messages: find the bot DM (bot + me, exactly 2 members) or create it.
async function findBotDmMgmt(source) {
  const joined = await api('GET', '/_matrix/client/v3/joined_rooms');
  for (const roomId of joined.joined_rooms) {
    if (await isBotDmMgmt(source, roomId)) return roomId;
  }
  const created = await api('POST', '/_matrix/client/v3/createRoom',
    { invite: [source.botMxid], is_direct: true, preset: 'trusted_private_chat' });
  return created.room_id;
}
// A mgmt room is a 2-member {bot, me} DM that is NOT a bridge portal. The
// portal check is load-bearing: sendSecretToMgmt() posts a session credential
// into whatever this returns first, and a degenerate portal room (every ghost
// left, leaving bot + user) has exactly the same membership shape as the real
// mgmt DM. mautrix stamps `uk.half-shot.bridge` state on every portal it
// creates, so its ABSENCE — the same test verifyImsgMgmt() already makes — is
// what separates the two. Failing to read the state refuses the room (a mgmt
// room we cannot prove is not a portal is not a mgmt room).
async function isBotDmMgmt(source, roomId) {
  try {
    const m = await api('GET', '/_matrix/client/v3/rooms/' + encodeURIComponent(roomId) + '/joined_members');
    const members = Object.keys(m.joined);
    if (!(members.length === 2 && members.includes(source.botMxid) && members.includes(S.userId))) return false;
    const st = await roomFullState(roomId);
    if (!Array.isArray(st)) return false;            // cannot prove "not a portal" -> refuse
    const isPortal = st.some(e => e && e.type === 'uk.half-shot.bridge');     // any state_key
    // ...and not a SPACE. A bridge's source space (e.g. "WhatsApp (+1...)") is
    // ALSO a 2-member {bot, user} room with no portal marker, so without this it
    // is indistinguishable from the mgmt DM and can win findBotDmMgmt's
    // first-match. Verified live on this hub: both the WhatsApp and Google
    // Messages spaces matched the old membership-only predicate.
    const isSpace = st.some(e => e && e.type === 'm.room.create' && e.state_key === ''
      && e.content && e.content.type === 'm.space');
    return !isPortal && !isSpace;
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
  if (S.busy) return;
  const source = SOURCES.find(s => s.id === sourceId);
  if (!source || !source.botMxid) return;
  S.busy = true; setButtonsDisabled(true);
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
    if (sourceId === 'whatsapp' || sourceId === 'gmessages') {
      if (text === 'login qr' || text === 'login phone') setLoginFlow(true);
      if (text === 'cancel') setLoginFlow(false);
    }
  } catch (e) {
    logConsole('error', String(e.message || e));
  } finally {
    S.busy = false; setButtonsDisabled(false);
  }
}
function sendStatusRefresh() { return sendCmd('whatsapp', 'list-logins'); }

// ---- Instagram session paste (SPEC §5/§6, security review M1) ----
// The pasted value is a BEARER CREDENTIAL. It is delivered through the SAME C-1
// mgmt-room guard sendCmd uses (resolve + verifyMgmt before the send), but it is
// NEVER logged, sanitize-rendered, echoed to the console, kept in any long-lived
// variable, or used to build a URL. This dedicated path exists ONLY so the send
// returns the event_id (sendCmd logs its body via logConsole, which is forbidden
// for the secret). Returns { roomId, eventId } so the carrier event can be
// redacted immediately. C-1 verification is preserved verbatim below.
async function sendSecretToMgmt(sourceId, secret) {
  const source = SOURCES.find(s => s.id === sourceId);
  if (!source || !source.botMxid) throw new Error('Unknown source.');
  const rt = runtime[sourceId];
  if (!rt.mgmtRoomId) rt.mgmtRoomId = await resolveMgmt(source);
  if (!rt.mgmtRoomId) throw new Error(source.label + ': management room not found.');
  if (!(await verifyMgmt(source, rt.mgmtRoomId))) {          // C-1: verify before EVERY send
    throw new Error('Refusing to send: ' + source.label + ' management room failed verification.');
  }
  const res = await api('PUT', '/_matrix/client/v3/rooms/' + encodeURIComponent(rt.mgmtRoomId) +
    '/send/m.room.message/' + encodeURIComponent(txn()), { msgtype: 'm.text', body: secret });
  return { roomId: rt.mgmtRoomId, eventId: res && res.event_id };   // `secret` is never logged
}
// Redact (delete) the credential-carrying event with the user's own S.token.
async function redactMgmtEvent(roomId, eventId) {
  await api('PUT', '/_matrix/client/v3/rooms/' + encodeURIComponent(roomId) +
    '/redact/' + encodeURIComponent(eventId) + '/' + encodeURIComponent(txn()), {});
}

// ---- command/console sync loop (D-3: isolated to the resolved mgmt rooms) --
async function startSync() {
  if (S.syncRunning) return;
  S.syncRunning = true;
  S.syncSince = null;
  while (S.syncRunning && S.token) {
    try {
      const ids = [runtime.whatsapp.mgmtRoomId, runtime.imessage.mgmtRoomId, runtime.gmessages.mgmtRoomId, runtime.instagram.mgmtRoomId, runtime.linkedin.mgmtRoomId, runtime.twitter.mgmtRoomId].filter(Boolean);
      const filter = encodeURIComponent(JSON.stringify(
        { room: { rooms: ids, timeline: { limit: 30 } }, presence: { types: [] } }));
      const q = '/_matrix/client/v3/sync?timeout=25000&filter=' + filter +
        (S.syncSince ? '&since=' + encodeURIComponent(S.syncSince) : '');
      const data = await api('GET', q);
      S.syncSince = data.next_batch;
      const join = (data.rooms && data.rooms.join) || {};
      for (const rid of Object.keys(join)) {
        const room = join[rid];
        if (!room.timeline || !room.timeline.events) continue;
        for (const ev of room.timeline.events) routeMgmtEvent(rid, ev);
      }
    } catch (e) {
      if (!S.token) return;
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
  const gm = runtime.gmessages.mgmtRoomId;
  const ig = runtime.instagram.mgmtRoomId;
  const li = runtime.linkedin.mgmtRoomId;
  const tw = runtime.twitter.mgmtRoomId;
  if (wa && roomId === wa) { handleMgmtEvent(WA, ev); return; }
  if (im && roomId === im) { handleMgmtEvent(IMSG, ev); return; }
  if (gm && roomId === gm) { handleMgmtEvent(GMSG, ev); return; }
  if (ig && roomId === ig) { handleMgmtEvent(IG, ev); return; }
  if (li && roomId === li) { handleMgmtEvent(LI, ev); return; }
  if (tw && roomId === tw) { handleMgmtEvent(TW, ev); return; }
  return; // not a management room -> ignore entirely
}

function handleMgmtEvent(source, ev) {
  if (ev.type === 'm.room.redaction') {
    if (source.id === 'whatsapp' && S.qr.eventId && ev.redacts === S.qr.eventId) clearQR();
    return;
  }
  if (ev.type !== 'm.room.message' || !ev.content) return;
  const fromBot = ev.sender === source.botMxid;   // D-3: only this source's bot
  const fromMe = ev.sender === S.userId;
  if (!fromBot && !fromMe) return;

  const rel = ev.content['m.relates_to'];
  const isEdit = rel && rel.rel_type === 'm.replace';
  const content = isEdit && ev.content['m.new_content'] ? ev.content['m.new_content'] : ev.content;

  if (fromBot && source.id === 'whatsapp' && content.msgtype === 'm.image') {
    if (isEdit && S.qr.eventId && rel.event_id !== S.qr.eventId) return;
    showQR(isEdit ? rel.event_id : ev.event_id, content.url, 'qr-box',
      'WhatsApp login QR code',
      'Scan with WhatsApp: Settings → Linked devices → Link a device');
    return;
  }
  if (fromBot && typeof content.body === 'string') {
    const body = sanitize(content.body.replace(/^\* /, ''));
    logConsole('bot', body, source.id);
    if (source.id === 'whatsapp' || source.id === 'gmessages' || source.id === 'instagram' || source.id === 'linkedin' || source.id === 'twitter') reactToBotReply(body, source);
    else if (source.id === 'imessage') updateImsgCard(content.body);
  } else if (fromMe && typeof content.body === 'string' &&
             !String(ev.unsigned && ev.unsigned.transaction_id || '').startsWith('hub-')) {
    logConsole('you', sanitize(content.body), source.id); // sent from Element etc.
  }
}

// WhatsApp / Google Messages status parsing for their Connections cards.
function reactToBotReply(body, source) {
  const pillId = source.id === 'gmessages' ? 'gmsg-status'
               : source.id === 'instagram' ? 'ig-status'
               : source.id === 'linkedin' ? 'li-status'
               : source.id === 'twitter' ? 'tw-status' : 'wa-status';
  const discId = source.id === 'instagram' ? 'btn-ig-disconnect'
               : source.id === 'linkedin' ? 'btn-li-disconnect'
               : source.id === 'twitter' ? 'btn-tw-disconnect'
               : source.id === 'gmessages' ? null : 'btn-disconnect';
  if (/You're not logged in/i.test(body)) updateCardStatus([], pillId, discId);
  // The reply lists each login as a markdown bullet "* `id` (name) - `STATE`".
  // handleMgmtEvent strips the leading "* " off the first line for the console
  // log, so the bullet is optional here — otherwise a single-login reply
  // (the common case) parses to zero logins and the pill never updates.
  const logins = [...body.matchAll(/^(?:\* )?`([^`\n]+)` \(([^)\n]*)\) - `([A-Z_]+)`/gm)]
    .map(m => ({ id: m[1], name: m[2], state: m[3] }));
  if (logins.length) updateCardStatus(logins, pillId, discId);
  if (/Successfully logged in/i.test(body)) {
    setLoginFlow(false); clearQR();
    sendCmd(source.id, 'list-logins');
  }
  if (/Login cancelled|cancell?ed/i.test(body) && S.loginFlowActive) { setLoginFlow(false); clearQR(); }
}

// ---- QR handling (WhatsApp + Google Messages share this path) ----
async function showQR(eventId, mxcUrl, boxId, altText, scanHint) {
  boxId = boxId || 'qr-box';
  altText = altText || 'WhatsApp login QR code';
  scanHint = scanHint || 'Scan with WhatsApp: Settings → Linked devices → Link a device';
  const m = MXC_RE.exec(String(mxcUrl || ''));
  if (!m || m[1] !== SERVER_NAME) return; // only local, well-formed media
  try {
    const res = await fetch(HS + '/_matrix/client/v1/media/download/' +
      encodeURIComponent(m[1]) + '/' + encodeURIComponent(m[2]),
      { headers: { 'Authorization': 'Bearer ' + S.token } });
    if (!res.ok) return;
    const blob = new Blob([await res.arrayBuffer()], { type: 'image/png' });
    clearQR();
    S.qr.eventId = eventId;
    S.qr.blobUrl = URL.createObjectURL(blob);
    S.qr.boxId = boxId;
    const box = $(boxId);
    if (!box) { URL.revokeObjectURL(S.qr.blobUrl); S.qr = { eventId: null, blobUrl: null, boxId: null }; return; }
    const img = el('img');
    img.alt = altText;
    img.src = S.qr.blobUrl;
    box.appendChild(el('div', 'muted', scanHint));
    box.appendChild(img);
    box.classList.remove('hidden');
    setLoginFlow(true);
  } catch (e) { /* leave card unchanged */ }
}
function clearQR() {
  if (S.qr.blobUrl) URL.revokeObjectURL(S.qr.blobUrl);
  const boxId = S.qr.boxId || 'qr-box';
  S.qr = { eventId: null, blobUrl: null, boxId: null };
  const box = $(boxId);
  if (box) { box.replaceChildren(); box.classList.add('hidden'); }
}

export { SOURCES, WA, IMSG, GMSG, IG, LI, TW, IMSG_BOT_MXID, PLANNED_SOURCES, COMMAND_GROUPS, IMSG_COMMAND_GROUPS, GMSG_COMMAND_GROUPS, IG_COMMAND_GROUPS, LI_COMMAND_GROUPS, TW_COMMAND_GROUPS, groupsFor, PHONE_RE, EMAIL_RE, validHandle, resolveMgmt, verifyMgmt, findBotDmMgmt, isBotDmMgmt, stateEvent, roomFullState, verifyImsgMgmt, resolveImsgMgmt, sendCmd, sendStatusRefresh, sendSecretToMgmt, redactMgmtEvent, startSync, routeMgmtEvent, handleMgmtEvent, reactToBotReply, showQR, clearQR };
