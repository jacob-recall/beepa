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
  { id: 'gmessages', label: 'Google Messages', kind: 'source', botMxid: '@gmessagesbot:localhost',
    spaceName: 'Google Messages', canStartChat: true, icon: '📱',
    blurb: 'Link your Google account with a quick sign-in; your chats appear as rooms in Element.' },
  { id: 'instagram', label: 'Instagram', kind: 'source', botMxid: '@instagrambot:localhost',
    spaceName: 'Instagram', canStartChat: true, icon: '📷',
    blurb: 'Bridge your Instagram DMs: log in on instagram.com, then paste your session; chats appear as rooms in Element.' },
  { id: 'linkedin', label: 'LinkedIn', kind: 'source', botMxid: '@linkedinbot:localhost',
    spaceName: 'LinkedIn', canStartChat: false, icon: '💼',
    blurb: 'Bridge your LinkedIn messages: log in on linkedin.com, then paste your session; chats appear as rooms in Element.' },
  { id: 'twitter', label: 'X', kind: 'source', botMxid: '@twitterbot:localhost',
    spaceName: 'Twitter', canStartChat: true, icon: '✖️',
    blurb: 'Bridge your X (Twitter) DMs: log in on x.com, then paste your session; chats appear as rooms in Element.' },
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
    { cmd: 'login instagram', label: 'Connect Instagram', desc: 'Start linking your Instagram account; then paste your instagram.com session as the next message.' },
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
    { cmd: 'login cookies', label: 'Connect LinkedIn', desc: 'Start linking your LinkedIn account; then paste your linkedin.com session (Copy as cURL) as the next message.' },
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
    { cmd: 'login cookies', label: 'Connect X', desc: 'Start linking your X account; then paste your x.com session (Copy as cURL) as the next message.' },
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
let qr = { eventId: null, blobUrl: null, boxId: null };
let loginFlowActive = false;
let busy = false;
let activeSettingsSource = 'whatsapp';
let joinedSet = new Set();
const convosBySource = {};
let sourceViewId = null;                    // which source #view-source is showing (for #source-search)                 // sourceId -> [convo]
const runtime = { whatsapp: { mgmtRoomId: null }, imessage: { mgmtRoomId: null }, gmessages: { mgmtRoomId: null }, instagram: { mgmtRoomId: null }, linkedin: { mgmtRoomId: null }, twitter: { mgmtRoomId: null } };

// ---- Home feed state (HF-2): fully independent of the command sync loop.
// These are NEVER the command loop's syncSince/syncRunning; the two /sync loops
// share no sync state (HF-1). The feed model holds exactly one record per room
// (overwrite, no history) for the validated portal rooms only.
let feedRunning = false, feedSince = null;
const feedModel = new Map();               // roomId -> {id, name, lastBody, lastTs, sourceId}
let feedRenderScheduled = false;           // HF-5: coalesce a burst into one render
let feedRevalTimer = null;                 // HF-3: debounced re-validation for new portals

// ---- Home feed hide-signals (HF-9): all read/derived in the feed data path
// only; none of these touch the command/console loop. A room is excluded from
// the default Home list if it is low-priority-tagged, muted by a push rule, or
// in the client-managed manual-hidden set. Manual hide sets the SAME
// m.lowpriority room tag, so a manual hide and a network archive share one
// mechanism (the exclusion below covers both).
let feedLowPriority = new Set();           // roomIds tagged m.lowpriority (from m.tag account_data)
let feedMuted = new Set();                 // roomIds muted by a global room push rule (no notify)
const feedManualHidden = new Set();        // client-managed: rooms hidden this session (also tag-set)
let feedShowHidden = false;                // Home "Show hidden" toggle state

// ---- Native conversation view state (CV.2) — a THIRD, isolated read/send path.
// Every convo function below references ONLY: the shared transport (api),
// sanitize/sanitizeLine, el/$, ROOMID_RE/joinedSet/feedModel, txn, userId,
// buildPlatBadge, openConversation (escape hatch) and showSection/setActiveNav.
// It references NONE of routeMgmtEvent/handleMgmtEvent/logConsole/
// reactToBotReply/sendCmd/updateImsgCard/startSync/startFeedSync (CV-I1/CV-3).
let openRoomId = null;                      // the ONE room the convo view targets
let convoRunning = false, convoSince = null; // the room-scoped live loop (own state)
const convoSeen = new Set();               // dedup: event_id and 'txn:'+transaction_id
const convoNames = new Map();              // sender mxid -> sanitized display name (cache)
const convoNamePending = new Set();        // mxids with an in-flight displayname fetch

// ---- Self-identity detection (self-align) — COSMETIC ONLY.
// selfMxids is the set of the user's OWN bridge identities (e.g. their own
// WhatsApp ghost @whatsapp_lid-...:localhost, whose phone-sent messages carry no
// per-message flag). It affects ONLY bubble ALIGNMENT + the "You" label in
// renderMessageEvent — it grants NO capability and NEVER influences sending, room
// validation, or the iMessage com.jkali.from_me trust gate. Built (union) two
// ways during seedFeed and the debounced revalidate; cleared on sign-out. The
// build path is read-only and references no command/console symbol.
let selfMxids = new Set();
const MXID_RE = /^@[^:]+:localhost$/;      // shape gate for account_data-listed mxids
const SELF_MIN_ROOMS = 5;                  // heuristic threshold: min distinct rooms to claim a self-ghost

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
  return s.replace(/[\x00-	-‪-‮⁦-⁩​-‏﻿]/g, '')
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
  runtime.whatsapp.mgmtRoomId = null; runtime.imessage.mgmtRoomId = null; runtime.gmessages.mgmtRoomId = null; runtime.instagram.mgmtRoomId = null; runtime.linkedin.mgmtRoomId = null; runtime.twitter.mgmtRoomId = null;
  syncRunning = false;
  feedRunning = false;                              // HF-2: stop the feed loop with the session
  feedSince = null;
  feedModel.clear();
  feedLowPriority = new Set();                        // HF-9: drop hide-signals with the session
  feedMuted = new Set();
  feedManualHidden.clear();
  feedShowHidden = false;
  if (feedRevalTimer) { clearTimeout(feedRevalTimer); feedRevalTimer = null; }
  convoRunning = false;                              // CV.2: stop the room watch with the session
  openRoomId = null;
  convoSince = null;
  convoSeen.clear();
  convoNames.clear();
  convoNamePending.clear();
  selfMxids = new Set();                              // self-align: drop self identities with the session
  setActiveConvoRow(null);                            // layout: clear active-row highlight on sign-out
  const convoPane = $('msgr-convo');
  if (convoPane) convoPane.classList.add('no-selection'); // reset the right pane to the placeholder
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
async function isBotDmMgmt(source, roomId) {
  try {
    const m = await api('GET', '/_matrix/client/v3/rooms/' + encodeURIComponent(roomId) + '/joined_members');
    const members = Object.keys(m.joined);
    return members.length === 2 && members.includes(source.botMxid) && members.includes(userId);
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
    if (sourceId === 'whatsapp' || sourceId === 'gmessages') {
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
// Redact (delete) the credential-carrying event with the user's own token.
async function redactMgmtEvent(roomId, eventId) {
  await api('PUT', '/_matrix/client/v3/rooms/' + encodeURIComponent(roomId) +
    '/redact/' + encodeURIComponent(eventId) + '/' + encodeURIComponent(txn()), {});
}

// ---- command/console sync loop (D-3: isolated to the resolved mgmt rooms) --
async function startSync() {
  if (syncRunning) return;
  syncRunning = true;
  syncSince = null;
  while (syncRunning && token) {
    try {
      const ids = [runtime.whatsapp.mgmtRoomId, runtime.imessage.mgmtRoomId, runtime.gmessages.mgmtRoomId, runtime.instagram.mgmtRoomId, runtime.linkedin.mgmtRoomId, runtime.twitter.mgmtRoomId].filter(Boolean);
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
  const logins = [...body.matchAll(/^\* `([^`\n]+)` \(([^)\n]*)\) - `([A-Z_]+)`/gm)]
    .map(m => ({ id: m[1], name: m[2], state: m[3] }));
  if (logins.length) updateCardStatus(logins, pillId, discId);
  if (/Successfully logged in/i.test(body)) {
    setLoginFlow(false); clearQR();
    sendCmd(source.id, 'list-logins');
  }
  if (/Login cancelled|cancell?ed/i.test(body) && loginFlowActive) { setLoginFlow(false); clearQR(); }
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
      { headers: { 'Authorization': 'Bearer ' + token } });
    if (!res.ok) return;
    const blob = new Blob([await res.arrayBuffer()], { type: 'image/png' });
    clearQR();
    qr.eventId = eventId;
    qr.blobUrl = URL.createObjectURL(blob);
    qr.boxId = boxId;
    const box = $(boxId);
    if (!box) { URL.revokeObjectURL(qr.blobUrl); qr = { eventId: null, blobUrl: null, boxId: null }; return; }
    const img = el('img');
    img.alt = altText;
    img.src = qr.blobUrl;
    box.appendChild(el('div', 'muted', scanHint));
    box.appendChild(img);
    box.classList.remove('hidden');
    setLoginFlow(true);
  } catch (e) { /* leave card unchanged */ }
}
function clearQR() {
  if (qr.blobUrl) URL.revokeObjectURL(qr.blobUrl);
  const boxId = qr.boxId || 'qr-box';
  qr = { eventId: null, blobUrl: null, boxId: null };
  const box = $(boxId);
  if (box) { box.replaceChildren(); box.classList.add('hidden'); }
}

// ---- sidebar conversation lists (SEPARATE read path from the command loop) --
// One-shot filtered sync snapshot: room names, space memberships/children, and
// last message. This never feeds the console or the status parser (D-3).
async function fetchSnapshot() {
  const filter = encodeURIComponent(JSON.stringify({
    // Per-room account_data limited to m.tag (HF-9: read low-priority/archive
    // tags in the feed's own data path). Global account_data + presence stay
    // excluded; the sidebar parser ignores account_data, so this is additive.
    room: { timeline: { limit: 5 }, state: { lazy_load_members: true }, account_data: { types: ['m.tag'] } },
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
    // Read state from BOTH the sync `state` block AND `timeline` — a newer
    // space (e.g. iMessage) keeps its m.space.child / name in the timeline
    // window, not the `state` block, so state-only misses its children.
    // Functional only: children are still ROOMID_RE ∩ joinedSet-gated below (D-5).
    const stateEvents = ((r.state && r.state.events) || []).concat((r.timeline && r.timeline.events) || []);
    const seenChild = new Set();
    for (const e of stateEvents) {
      if (e.type === 'm.room.name' && e.state_key === '') info.name = e.content && e.content.name;
      if (e.type === 'm.room.create' && e.content && e.content.type === 'm.space') info.isSpace = true;
      if (e.type === 'm.space.child' && e.state_key && e.content && Object.keys(e.content).length) {
        if (!seenChild.has(e.state_key)) { seenChild.add(e.state_key); info.children.push(e.state_key); }
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

// ---- Self-identity detection (self-align) — build path. COSMETIC ONLY:
// nothing below sends, validates a room, or touches the from_me trust gate; it
// only populates selfMxids, which renderMessageEvent reads for alignment. No
// command/console symbol is referenced.

// (1) Authoritative source: the user-written account_data event
// com.jkali.self_identities ({ mxids: ["@whatsapp_lid-...:localhost", ...] }).
// Only the user's OWN token can write account_data, so this is trusted and not
// spoofable by a remote sender. Absent/404 -> empty set. Each listed string must
// look like a valid local mxid to be admitted.
async function fetchSelfIdentityAccountData() {
  const out = new Set();
  try {
    const data = await api('GET', '/_matrix/client/v3/user/' + encodeURIComponent(userId) +
      '/account_data/com.jkali.self_identities');
    const arr = data && Array.isArray(data.mxids) ? data.mxids : [];
    for (const m of arr) if (typeof m === 'string' && MXID_RE.test(m)) out.add(m);
  } catch (e) { /* 404 / absent / error -> treat as empty (additive union below) */ }
  return out;
}

// (2) Heuristic auto-derivation (generalizable, zero-setup): for EACH source,
// over that source's already-validated portal rooms (convosBySource — the same
// ROOMID_RE ∩ joinedSet ∩ source-space-child set buildConvos produced), tally
// per candidate sender the number of DISTINCT rooms it appears in as an
// m.room.message sender. The user themself is a participant in ALL their own
// conversations, so their own ghost tops the per-source count. Pick the top
// sender per source AS the user's own identity — but ONLY if it clears a
// threshold (>= SELF_MIN_ROOMS distinct rooms AND a strict plurality, >= 2x the
// runner-up) so a chatty single contact in a tiny account is not mislabeled;
// otherwise pick none for that source. Never counts userId (@jkali:localhost) or
// the source's own bot mxid. A misfire is at worst a wrong-side bubble — never a
// data/capability issue.
function deriveSelfMxidsHeuristic(join) {
  const winners = new Set();
  for (const s of SOURCES) {
    if (s.kind === 'all') continue;
    const convos = convosBySource[s.id] || [];        // pre-validated portals only
    const counts = new Map();                          // sender mxid -> # distinct rooms of this source
    for (const c of convos) {
      const room = join[c.id];
      const tl = (room && room.timeline && room.timeline.events) || [];
      const inThisRoom = new Set();
      for (const ev of tl) {
        if (ev && ev.type === 'm.room.message' && typeof ev.sender === 'string') inThisRoom.add(ev.sender);
      }
      for (const sender of inThisRoom) {
        if (sender === userId || sender === s.botMxid) continue;   // never the user's mxid or the bot
        counts.set(sender, (counts.get(sender) || 0) + 1);
      }
    }
    let top = null, topN = 0, secondN = 0;
    for (const [sender, n] of counts) {
      if (n > topN) { secondN = topN; top = sender; topN = n; }
      else if (n > secondN) { secondN = n; }
    }
    if (top && topN >= SELF_MIN_ROOMS && topN >= 2 * secondN) winners.add(top); // strict-plurality gate
  }
  return winners;
}

// Rebuild selfMxids as the UNION of the two sources. Read-only; alignment only.
async function refreshSelfMxids(join) {
  const next = new Set();
  for (const m of await fetchSelfIdentityAccountData()) next.add(m);  // trusted (own account_data)
  for (const m of deriveSelfMxidsHeuristic(join)) next.add(m);         // cosmetic heuristic (thresholded)
  selfMxids = next;
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
  // HF-9: rebuild the low-priority set from per-room m.tag account_data. A room
  // is low-priority/archived if its tags include m.lowpriority. Read only here,
  // in the feed data path; never routed to the console/status parser.
  const low = new Set();
  for (const rid of Object.keys(join)) {
    const ad = join[rid] && join[rid].account_data;
    for (const e of ((ad && ad.events) || [])) {
      if (e.type === 'm.tag' && e.content && e.content.tags &&
          Object.prototype.hasOwnProperty.call(e.content.tags, 'm.lowpriority')) {
        low.add(rid);
      }
    }
  }
  feedLowPriority = low;
  await feedRefreshMuted();                          // HF-9: refresh muted push-rule set
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
  await refreshSelfMxids(join);                        // self-align: rebuild self identities (alignment only)
}

// HF-9: derive the muted-room set from the user's own push rules. A global
// `room` rule mutes the room whose id equals its rule_id when its actions carry
// no notify action (actions is [], or ['dont_notify'], or otherwise lacks the
// 'notify' string). Reading push rules adds no capability (own account data)
// and stays in the feed data path. On error the previous set is kept.
async function feedRefreshMuted() {
  try {
    const pr = await api('GET', '/_matrix/client/v3/pushrules/');
    const roomRules = (pr && pr.global && Array.isArray(pr.global.room)) ? pr.global.room : [];
    const set = new Set();
    for (const rule of roomRules) {
      if (!rule || typeof rule.rule_id !== 'string') continue;
      if (rule.enabled === false) continue;          // a disabled rule does not mute
      const acts = Array.isArray(rule.actions) ? rule.actions : [];
      const notifies = acts.some(a => a === 'notify');
      if (!notifies) set.add(rule.rule_id);          // "no notify action" -> muted
    }
    feedMuted = set;
  } catch (e) { /* keep the previous muted set */ }
}

// HF-9: a room is hidden from the default Home list if it is low-priority/
// archived, muted, or in the client-managed manual-hidden set.
function feedIsHidden(roomId) {
  return feedManualHidden.has(roomId) || feedLowPriority.has(roomId) || feedMuted.has(roomId);
}

// HF-9: manual hide sets the m.lowpriority room tag. The roomId is used to build
// the tag URL ONLY after it is validated ∈ feedModel ∩ joinedSet AND matches
// ROOMID_RE — never a typed/bridged value. Goes through api() (same-origin,
// bearer). Optimistically updates local sets + re-renders so the row leaves the
// list at once; the next seed reads the tag back authoritatively.
async function feedHideRoom(roomId) {
  if (!feedModel.has(roomId) || !joinedSet.has(roomId) || !ROOMID_RE.test(roomId)) return;
  try {
    await api('PUT', '/_matrix/client/v3/user/' + encodeURIComponent(userId) +
      '/rooms/' + encodeURIComponent(roomId) + '/tags/m.lowpriority', { order: 0.5 });
    feedManualHidden.add(roomId);
    feedLowPriority.add(roomId);
    scheduleFeedRender();
  } catch (e) { /* leave the row in place on failure */ }
}
// HF-9: unhide removes the m.lowpriority tag (DELETE) and clears the manual set.
// A still-muted room stays hidden (mute is a separate mechanism). Same roomId
// validation as hide.
async function feedUnhideRoom(roomId) {
  if (!feedModel.has(roomId) || !ROOMID_RE.test(roomId)) return;
  try {
    await api('DELETE', '/_matrix/client/v3/user/' + encodeURIComponent(userId) +
      '/rooms/' + encodeURIComponent(roomId) + '/tags/m.lowpriority');
    feedManualHidden.delete(roomId);
    feedLowPriority.delete(roomId);
    scheduleFeedRender();
  } catch (e) { /* leave state unchanged on failure */ }
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
  const open = () => openConvo(r.id);                 // CV.2: native hub conversation view
  // HF-9: per-row hide/unhide. A currently-hidden row (only reachable with
  // "Show hidden" on) offers Unhide; otherwise Hide. textContent only, built
  // with el(); the click acts on r.id (validated ∈ feedModel inside the handler)
  // and never opens the conversation (stopPropagation).
  const hidden = feedIsHidden(r.id);
  const hideBtn = el('button', 'feed-hide', hidden ? 'Unhide' : 'Hide');
  hideBtn.type = 'button';
  hideBtn.setAttribute('aria-label', (hidden ? 'Unhide ' : 'Hide ') + name);
  hideBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (hidden) feedUnhideRoom(r.id); else feedHideRoom(r.id);
  });
  hideBtn.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') e.stopPropagation(); });
  row.appendChild(hideBtn);
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
  ensureHomeHiddenToggle();                           // HF-9: "Show hidden" chip above the list
  const q = (($('home-search') && $('home-search').value) || '').trim().toLowerCase();
  const all = [...feedModel.values()].sort((a, b) => b.lastTs - a.lastTs);
  // HF-9: hidden rooms (low-priority/muted/manual) stay in feedModel but are
  // excluded from the default list; the toggle reveals them (with Unhide).
  const visible = feedShowHidden ? all : all.filter(r => !feedIsHidden(r.id));
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
// list (built with el()/textContent; no HTML strings). Toggles feedShowHidden
// and re-renders so hidden (low-priority/muted/manual) rooms appear with an
// Unhide action. Pure client-side state; sends no command and builds no URL.
function ensureHomeHiddenToggle() {
  const list = $('home-list');
  if (!list || !list.parentNode) return;
  let chip = $('home-hidden-toggle');
  if (!chip) {
    chip = el('button', 'feed-showhidden');
    chip.id = 'home-hidden-toggle';
    chip.type = 'button';
    chip.addEventListener('click', () => { feedShowHidden = !feedShowHidden; renderHome(); });
    list.parentNode.insertBefore(chip, list);
  }
  chip.setAttribute('aria-pressed', feedShowHidden ? 'true' : 'false');
  chip.classList.toggle('active', feedShowHidden);
  chip.textContent = feedShowHidden ? 'Hide hidden' : 'Show hidden';
}

// Layout-only helper: mark the messenger-list row for `roomId` active and clear
// it on every other row. Matches by the row's dataset.roomId (set in
// buildFeedRow); textContent/dataset only, no innerHTML. Passing null clears all.
// Not a navigation or security input — it only sets a CSS highlight class.
function setActiveConvoRow(roomId) {
  const list = $('home-list');
  if (!list) return;
  for (const row of list.children) {
    const rid = row.dataset ? row.dataset.roomId : undefined;
    row.classList.toggle('active', roomId != null && rid === roomId);
  }
}

// ---- open a conversation (U-1 / D-4) ----
function openConversation(roomId) {
  if (!ROOMID_RE.test(roomId)) return;             // reject ids failing the regex
  if (!joinedSet.has(roomId)) return;              // D-5: must be a joined room
  const f = $('chats-container') && $('chats-container').querySelector('iframe');
  if (f) f.src = CHATS_URL + '/#/room/' + roomId;  // constant prefix + validated RAW id
  navTo('all');
}

// ===========================================================================
// Native conversation view (CV.2) — read + reply inside the hub's own DOM.
// A THIRD isolated path (CV-I1/CV-3): it reads/sends ONLY the open PORTAL room;
// it never touches a management room or the command/console handler.
// ===========================================================================

// CV-R3: hub errors/status go to a SEPARATE, distinctly-styled region — NEVER a
// message bubble in #convo-messages (anti-phishing). Created lazily since the
// layout scaffold has no dedicated status node.
function convoSetStatus(text) {
  const pane = $('msgr-convo');                       // was #view-convo (removed in the two-pane merge)
  if (!pane) return;
  let s = $('convo-status');
  if (!s) {
    s = el('div', 'convo-status');
    s.id = 'convo-status';
    const compose = $('convo-compose');
    if (compose && compose.parentNode) compose.parentNode.insertBefore(s, compose);
    else pane.appendChild(s);
  }
  s.textContent = text || '';
  s.classList.toggle('hidden', !text);
}

// A short local wall-clock time for a bubble's decorative .when node.
function convoTime(ts) {
  if (typeof ts !== 'number' || !isFinite(ts)) return '';
  try { return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }
  catch (e) { return ''; }
}

// Decorative sender label. mxid localpart, sanitized; NEVER used for ownership
// (CV-R1 derives sent/recv from ev.sender === userId only).
function convoLocalpart(mxid) {
  const s = typeof mxid === 'string' ? mxid : '';
  const colon = s.indexOf(':');
  const lp = (colon > 0 ? s.slice(0, colon) : s).replace(/^@/, '');
  return sanitizeLine(lp) || sanitizeLine(s) || 'unknown';
}
// Cached display-name lookup (own account scope). Returns a synchronous best
// guess (cache or localpart) and, on a miss, fetches the profile once and
// patches any existing recv bubbles for that sender in place. Decorative only.
function convoDisplayName(mxid) {
  if (convoNames.has(mxid)) return convoNames.get(mxid);
  convoFetchName(mxid);
  return convoLocalpart(mxid);
}
async function convoFetchName(mxid) {
  if (typeof mxid !== 'string' || !mxid) return;
  if (convoNames.has(mxid) || convoNamePending.has(mxid)) return;
  convoNamePending.add(mxid);
  try {
    const data = await api('GET', '/_matrix/client/v3/profile/' + encodeURIComponent(mxid) + '/displayname');
    const name = sanitizeLine(data && data.displayname) || convoLocalpart(mxid);
    convoNames.set(mxid, name);
    const box = $('convo-messages');
    if (box) {
      for (const b of box.children) {
        if (b.dataset && b.dataset.sender === mxid) {
          const who = b.querySelector('.who');
          if (who) who.textContent = name;         // textContent only — no HTML sink
        }
      }
    }
  } catch (e) {
    convoNames.set(mxid, convoLocalpart(mxid));    // cache the fallback to stop refetching
  } finally {
    convoNamePending.delete(mxid);
  }
}

// CV-R4: the SINGLE shared resolver for BOTH history (/messages) and live
// (/sync) events — mirrors feedPreviewFromEvent EXACTLY. Returns {text, kind}
// for a renderable message, else null (reactions/redactions/state/edits-of-
// nonmessage are skipped). Reads content.body ONLY; NEVER formatted_body.
function convoResolveContent(ev) {
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
  if (mt === 'm.image') return { text: '📷 Photo', kind: 'media' };  // static label, never the filename
  if (mt === 'm.video') return { text: '🎥 Video', kind: 'media' };
  if (mt === 'm.audio') return { text: '🎵 Audio', kind: 'media' };
  if (mt === 'm.file')  return { text: '📎 File',  kind: 'media' };
  return null;                                       // not a previewable message
}

// CV-R4: the SINGLE shared renderer. Appends at most one bubble to
// #convo-messages, deduped by event_id (and by 'txn:'+transaction_id so the
// server echo of an optimistic bubble does not double). CV-R1: ownership from
// ev.sender === userId (mxid) ONLY. CV-R2: who / body / when are three separate
// el() nodes; the body is sanitize()'d in its own node, never concatenated into
// chrome. CV-D1: caps the list at 200 bubbles (drops oldest).
function renderMessageEvent(ev) {
  const box = $('convo-messages');
  if (!box) return;
  const resolved = convoResolveContent(ev);
  if (!resolved) return;                            // skip: reaction/redaction/state/etc.

  const eid = typeof ev.event_id === 'string' ? ev.event_id : null;
  const txnId = ev.unsigned && typeof ev.unsigned.transaction_id === 'string'
    ? ev.unsigned.transaction_id : null;
  if (eid && convoSeen.has(eid)) return;            // already rendered this event
  if (txnId && convoSeen.has('txn:' + txnId)) {     // echo of our optimistic bubble
    if (eid) convoSeen.add(eid);
    return;
  }
  if (eid) convoSeen.add(eid);
  if (txnId) convoSeen.add('txn:' + txnId);

  // CV-R1: own message => right-aligned "You". TRUE when EITHER (a) ev.sender is
  // us (sent via this bridge), OR (b) a TRUSTED from_me marker: the daemon stamps
  // com.jkali.from_me on messages the user sent from the iMessage app, posted
  // ONLY as @imessagebot. ANTI-SPOOF: the marker is honored ONLY when ev.sender
  // is exactly our own appservice bot (IMSG_BOT_MXID). A ghost (@imessage_*),
  // another bridge's sender, or a remote contact carrying the flag is IGNORED
  // (treated as received/left-aligned) — a remote party can never render as "You".
  const trustedFromMe = !!(ev.content && ev.content['com.jkali.from_me'] === true
                           && ev.sender === IMSG_BOT_MXID);
  // Self-align (cosmetic): a sender that is one of the user's OWN bridge
  // identities (their own ghost, e.g. their WhatsApp @whatsapp_lid-...:localhost)
  // renders as "You"/right-aligned even with no per-message flag. selfMxids is
  // built ONLY from trusted own account_data + a thresholded cosmetic heuristic
  // (never from this event) and grants no capability; it does NOT relax the
  // iMessage from_me trust gate above, which stays keyed to IMSG_BOT_MXID.
  const sent = ev.sender === userId || trustedFromMe || selfMxids.has(ev.sender);
  let cls = 'msg ' + (sent ? 'sent' : 'recv');
  if (resolved.kind === 'media') cls += ' media';
  else if (resolved.kind === 'notice') cls += ' notice';
  const bubble = el('div', cls);
  if (eid) bubble.dataset.eventId = eid;
  if (txnId) bubble.dataset.txnId = txnId;

  // CV-R2: three separate nodes. who is decorative (sanitizeLine); "You" for own.
  const who = el('div', 'who');
  if (sent) { who.textContent = 'You'; }
  else { bubble.dataset.sender = ev.sender; who.textContent = convoDisplayName(ev.sender); }
  bubble.appendChild(who);
  bubble.appendChild(el('div', 'body', resolved.text));   // sanitized text/label, own node
  bubble.appendChild(el('div', 'when', convoTime(ev.origin_server_ts)));
  box.appendChild(bubble);

  while (box.childElementCount > 200) {             // CV-D1: bounded, drop oldest
    const first = box.firstElementChild;
    if (!first) break;
    if (first.dataset) {
      if (first.dataset.eventId) convoSeen.delete(first.dataset.eventId);
      if (first.dataset.txnId) convoSeen.delete('txn:' + first.dataset.txnId);
    }
    box.removeChild(first);
  }
}

// Open the native conversation view for a validated room (CV-6): same
// ROOMID_RE ∩ joinedSet gate openConversation uses. Loads recent history via
// /messages, renders through the shared renderer, then starts the room-scoped
// live watch.
async function openConvo(roomId) {
  if (!ROOMID_RE.test(roomId) || !joinedSet.has(roomId)) return;  // reject unvalidated ids
  stopConvoWatch();                                 // stop any prior room's watch first
  openRoomId = roomId;
  convoSeen.clear();
  convoSetStatus('');

  const rec = feedModel.get(roomId);
  $('convo-title').textContent = sanitizeLine((rec && rec.name) || roomId);
  const badge = $('convo-badge');
  if (badge) {
    const b = buildPlatBadge(rec && rec.sourceId); // derived from record sourceId only
    badge.className = b.className;
    badge.textContent = b.textContent;
  }
  // "Open in Element" reuses the exact validated openConversation path (CV-E1).
  const link = $('convo-element-link');
  if (link) link.onclick = (e) => { e.preventDefault(); openConversation(roomId); };

  const box = $('convo-messages');
  if (box) box.replaceChildren();                   // #convo-messages holds ONLY bubbles (CV-R3)
  // Layout/nav only: reveal the Home messenger and open the right-hand chat pane.
  // Show the same Home section the Home nav shows, WITHOUT re-rendering the list
  // (which would drop the active highlight); then reveal #msgr-convo by removing
  // the no-selection class and mark the clicked row active.
  showSection('view-home');
  setActiveNav('home');
  const convoPane = $('msgr-convo');
  if (convoPane) convoPane.classList.remove('no-selection');
  setActiveConvoRow(roomId);

  try {
    const q = '/_matrix/client/v3/rooms/' + encodeURIComponent(roomId) + '/messages?dir=b&limit=50';
    const data = await api('GET', q);
    if (openRoomId === roomId) {                    // guard: user may have switched rooms mid-fetch
      const chunk = Array.isArray(data.chunk) ? data.chunk.slice().reverse() : [];  // b -> chronological
      for (const ev of chunk) renderMessageEvent(ev);
      if (box) box.scrollTop = box.scrollHeight;
    }
  } catch (e) {
    convoSetStatus('Could not load messages: ' + String(e.message || e));  // CV-R3: status, not a bubble
  }
  startConvoWatch(roomId);
}

// CV-I1 / CV-4: a THIRD independent long-poll, server-filtered to the open room
// only, plus a client guard that drops anything whose room != openRoomId. It
// appends ONLY to #convo-messages (via renderMessageEvent) and references none
// of the command/console symbols. 25s long-poll timeout + 3s error backoff.
async function startConvoWatch(roomId) {
  if (convoRunning) return;
  convoRunning = true;
  convoSince = null;
  const watchRoom = roomId;
  while (convoRunning && token && openRoomId === watchRoom) {
    try {
      const filter = encodeURIComponent(JSON.stringify({
        room: { rooms: [watchRoom], timeline: { limit: 20 }, state: { types: [] } },
        presence: { types: [] }, account_data: { types: [] },
      }));
      const q = '/_matrix/client/v3/sync?timeout=25000&filter=' + filter +
        (convoSince ? '&since=' + encodeURIComponent(convoSince) : '');
      const data = await api('GET', q);
      convoSince = data.next_batch;
      const join = (data.rooms && data.rooms.join) || {};
      const room = join[watchRoom];                 // read ONLY the open room's timeline
      if (room && room.timeline && Array.isArray(room.timeline.events) && openRoomId === watchRoom) {
        for (const ev of room.timeline.events) {
          if (openRoomId !== watchRoom) break;      // client guard: drop if the room changed
          renderMessageEvent(ev);
        }
        const box = $('convo-messages');
        if (box) box.scrollTop = box.scrollHeight;
      }
    } catch (e) {
      if (!token) { convoRunning = false; return; }
      await new Promise(r => setTimeout(r, 3000));
    }
  }
}
function stopConvoWatch() {
  convoRunning = false;
  openRoomId = null;
}

// CV-S1 / CV-2: send the user's own typed text to the CURRENTLY-open room only,
// re-validating that room at send time (ROOMID_RE ∩ feedModel ∩ joinedSet, and
// never any management room). Fresh txn(); body length clamped. Optimistic echo
// deduped against the server echo by transaction_id.
async function sendConvoMessage() {
  const input = $('convo-input');
  if (!input) return;
  const text = (typeof input.value === 'string' ? input.value : '').trim();
  if (!text) return;
  const roomId = openRoomId;
  // CV-S1: no stale-room trust — re-validate the open room at the moment of send.
  if (!roomId || !ROOMID_RE.test(roomId) || !feedModel.has(roomId) || !joinedSet.has(roomId)) {
    convoSetStatus('Cannot send: this conversation is not available.');
    return;
  }
  // Defense-in-depth: never send into a bridge management room from this surface.
  if (roomId === runtime.whatsapp.mgmtRoomId ||
      roomId === runtime.imessage.mgmtRoomId ||
      roomId === runtime.gmessages.mgmtRoomId ||
      roomId === runtime.instagram.mgmtRoomId ||
      roomId === runtime.linkedin.mgmtRoomId ||
      roomId === runtime.twitter.mgmtRoomId) {
    convoSetStatus('Cannot send a message here.');
    return;
  }
  const body = text.slice(0, 8000);                 // clamp length
  const t = txn();                                  // fresh random transaction id
  input.value = '';
  convoSetStatus('');
  // Optimistic echo through the SAME shared renderer; deduped vs the server echo
  // by 'txn:'+t (renderMessageEvent). No event_id yet -> keyed by txn only.
  renderMessageEvent({
    type: 'm.room.message', sender: userId,
    content: { msgtype: 'm.text', body },
    origin_server_ts: Date.now(), unsigned: { transaction_id: t },
  });
  const box = $('convo-messages');
  if (box) box.scrollTop = box.scrollHeight;
  try {
    await api('PUT', '/_matrix/client/v3/rooms/' + encodeURIComponent(roomId) +
      '/send/m.room.message/' + encodeURIComponent(t), { msgtype: 'm.text', body });
  } catch (e) {
    convoSetStatus('Message failed to send: ' + String(e.message || e));  // CV-R3: status, not a bubble
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
  return row;
}
function elEmpty(text) { return el('div', 'list-empty', text); }

async function loadSourceList(sourceId) {
  const source = SOURCES.find(s => s.id === sourceId);
  const list = $('source-list');
  $('source-title').textContent = source.label;
  $('source-sub').textContent = 'Your conversations on ' + source.label + '. Click one to open it.';
  sourceViewId = sourceId;
  const search = $('source-search');
  if (search) { search.value = ''; search.placeholder = 'Search ' + source.label + ' conversations'; }
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
  const list = $('source-list');
  if (!list || !sourceViewId) return;
  const source = SOURCES.find(s => s.id === sourceViewId);
  const convos = convosBySource[sourceViewId] || [];
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
  if (openRoomId && key !== 'home') stopConvoWatch(); // CV.2: leaving the messenger stops its watch; Home keeps an open chat
  setActiveNav(key);
  if (key === 'home') {
    showSection('view-home');                        // unified two-pane messenger
    renderHome();
    // Right pane: if a chat is open (openRoomId), keep it shown and re-mark its
    // row active (renderHome rebuilt the rows); otherwise show the placeholder.
    const convoPane = $('msgr-convo');
    if (openRoomId) {
      if (convoPane) convoPane.classList.remove('no-selection');
      setActiveConvoRow(openRoomId);
    } else {
      if (convoPane) convoPane.classList.add('no-selection');
      setActiveConvoRow(null);
    }
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
  const gmBtn = $('btn-gmsg-cancel-login');
  if (gmBtn) gmBtn.classList.toggle('hidden', !active);
  if (!active) clearQR();
}

// ---- WhatsApp / Google Messages Connections card status ----
function updateCardStatus(logins, pillId, discId) {
  const pill = $(pillId || 'wa-status');
  const disc = discId ? $(discId) : null;
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

  // Google Messages card (mirrors the WhatsApp card)
  const gm = el('div', 'card bridge-card');
  const gmHead = el('div', 'bridge-head');
  gmHead.appendChild(el('span', 'bridge-name', GMSG.label));
  const gmPill = el('span', 'status-pill', 'Checking…');
  gmPill.id = 'gmsg-status';
  gmHead.appendChild(gmPill);
  gm.appendChild(gmHead);
  gm.appendChild(el('p', 'muted', GMSG.blurb));

  const gmActions = el('div', 'bridge-actions');
  const gmSignin = el('button', 'primary', 'Open Google sign-in ↗');
  gmSignin.style.width = 'auto';
  gmSignin.addEventListener('click', () => window.open(
    'https://accounts.google.com/AccountChooser?continue=https://messages.google.com/web/config',
    '_blank', 'noopener'));
  gmActions.appendChild(gmSignin);

  const gmRefresh = el('button', '', 'Refresh status');
  gmRefresh.addEventListener('click', () => sendCmd('gmessages', 'list-logins'));
  gmActions.appendChild(gmRefresh);
  gm.appendChild(gmActions);

  const gmSteps = el('ol', 'connect-steps muted');
  gmSteps.style.cssText = 'margin:10px 0 0;padding-left:22px;line-height:1.7;';
  gmSteps.appendChild(el('li', '', 'Click "Open Google sign-in" and sign into your Google account.'));
  gmSteps.appendChild(el('li', '', 'Run the connect helper: python3 gmessages-connect/connect.py  (or ask your assistant to connect Google Messages).'));
  gmSteps.appendChild(el('li', '', 'Tap the emoji it shows, in the Google Messages app on your phone. Done.'));
  gm.appendChild(gmSteps);
  holder.appendChild(gm);

  // Instagram card (mirrors the gmessages card, but with a session PASTE flow
  // instead of a QR — SPEC §5/§6). The pasted value is a bearer credential:
  // it is sent through the C-1 mgmt guard, redacted immediately, and never
  // logged, sanitize-rendered, persisted, or turned into a URL.
  const ig = el('div', 'card bridge-card');
  const igHead = el('div', 'bridge-head');
  igHead.appendChild(el('span', 'bridge-name', IG.label));
  const igPill = el('span', 'status-pill', 'Checking…');
  igPill.id = 'ig-status';
  igHead.appendChild(igPill);
  ig.appendChild(igHead);
  ig.appendChild(el('p', 'muted', IG.blurb));

  // Paste UI (built up front, revealed by Connect). textContent-only; no
  // innerHTML. The textarea value is treated like a password: never echoed.
  const igPaste = el('div', 'ig-paste hidden');
  igPaste.style.cssText = 'margin-top:10px;';
  igPaste.appendChild(el('p', 'muted',
    'On instagram.com: DevTools → Network → filter graphql → right-click a request → Copy as cURL, then paste here.'));
  const igArea = el('textarea');
  igArea.placeholder = 'Paste your Instagram session (Copy as cURL, or the cookies JSON) here';
  igArea.rows = 4;
  igArea.autocomplete = 'off';
  igArea.spellcheck = false;
  igArea.style.cssText = 'width:100%;box-sizing:border-box;font-family:monospace;resize:vertical;';
  igPaste.appendChild(igArea);
  const igWarn = el('p', 'error hidden');           // visible warnings (never the secret)
  igWarn.style.cssText = 'margin:6px 0 0;';
  const igSubmit = el('button', 'primary', 'Submit session');
  igSubmit.style.width = 'auto';
  const igSubmitRow = el('div', 'bridge-actions');
  igSubmitRow.appendChild(igSubmit);
  igPaste.appendChild(igSubmitRow);
  igPaste.appendChild(igWarn);

  const igActions = el('div', 'bridge-actions');
  const igConnect = el('button', 'primary', 'Connect Instagram');
  igConnect.style.width = 'auto';
  igConnect.addEventListener('click', async () => {
    igWarn.classList.add('hidden');
    igWarn.textContent = '';
    await sendCmd('instagram', 'login instagram');  // C-1 guarded
    igPaste.classList.remove('hidden');
    igArea.focus();
    window.open('https://www.instagram.com/', '_blank', 'noopener');
  });
  igActions.appendChild(igConnect);

  const igDisc = el('button', 'danger', 'Disconnect');
  igDisc.id = 'btn-ig-disconnect';
  igDisc.classList.add('hidden');
  igDisc.addEventListener('click', async () => {
    const id = igDisc.dataset.loginId;
    if (!id) return;
    if (await confirmModal('Disconnect Instagram?',
      'This unlinks the bridge from your Instagram account. Bridged rooms stay until deleted.', false)) {
      await sendCmd('instagram', 'logout ' + id);
      sendCmd('instagram', 'list-logins');
    }
  });
  igActions.appendChild(igDisc);

  const igRefresh = el('button', '', 'Refresh status');
  igRefresh.addEventListener('click', () => sendCmd('instagram', 'list-logins'));
  igActions.appendChild(igRefresh);
  ig.appendChild(igActions);

  // Submit: send the pasted secret through the C-1 guard, capture the event_id,
  // and redact it immediately. The value lives ONLY in this handler's scope; the
  // textarea is cleared before the network call and the value is dropped after.
  igSubmit.addEventListener('click', async () => {
    if (busy) return;
    igWarn.classList.add('hidden');
    igWarn.textContent = '';
    let secret = igArea.value;                       // read once; never logged
    igArea.value = '';                               // clear the field immediately
    if (!secret || !secret.trim()) {
      secret = null;
      igWarn.textContent = 'Paste your Instagram session before submitting.';
      igWarn.classList.remove('hidden');
      return;
    }
    busy = true; setButtonsDisabled(true); igSubmit.disabled = true;
    try {
      const sent = await sendSecretToMgmt('instagram', secret);
      secret = null;                                 // drop the credential from memory
      if (!sent || !sent.eventId) {
        igWarn.textContent = 'Sent, but could not confirm the message id to delete it — please delete your pasted message in Element manually.';
        igWarn.classList.remove('hidden');
      } else {
        try {
          await redactMgmtEvent(sent.roomId, sent.eventId);
          igPaste.classList.add('hidden');           // hide the paste UI on success
        } catch (e) {
          igWarn.textContent = 'Session sent, but auto-deleting it failed — please delete your pasted message in Element manually.';
          igWarn.classList.remove('hidden');
        }
      }
    } catch (e) {
      secret = null;                                 // never surface the secret in errors
      igWarn.textContent = 'Could not send the session: ' + String(e.message || e);
      igWarn.classList.remove('hidden');
    } finally {
      busy = false; setButtonsDisabled(false); igSubmit.disabled = false;
      sendCmd('instagram', 'list-logins');           // refresh the pill
    }
  });

  ig.appendChild(igPaste);
  holder.appendChild(ig);

  // LinkedIn card (mirrors the Instagram card exactly: a session PASTE flow,
  // not a QR — the "Copy as cURL" carries the X-LI-Track / X-LI-Page-Instance
  // headers as well as the cookies). The pasted value is a bearer credential:
  // it is sent through the C-1 mgmt guard, redacted immediately, and never
  // logged, sanitize-rendered, persisted, or turned into a URL.
  const li = el('div', 'card bridge-card');
  const liHead = el('div', 'bridge-head');
  liHead.appendChild(el('span', 'bridge-name', LI.label));
  const liPill = el('span', 'status-pill', 'Checking…');
  liPill.id = 'li-status';
  liHead.appendChild(liPill);
  li.appendChild(liHead);
  li.appendChild(el('p', 'muted', LI.blurb));

  // Paste UI (built up front, revealed by Connect). textContent-only; no
  // innerHTML. The textarea value is treated like a password: never echoed.
  const liPaste = el('div', 'li-paste hidden');
  liPaste.style.cssText = 'margin-top:10px;';
  liPaste.appendChild(el('p', 'muted',
    'On linkedin.com: DevTools → Network → filter graphql (voyager) → right-click a request → Copy as cURL, then paste here.'));
  const liArea = el('textarea');
  liArea.placeholder = 'Paste your LinkedIn session (Copy as cURL) here';
  liArea.rows = 4;
  liArea.autocomplete = 'off';
  liArea.spellcheck = false;
  liArea.style.cssText = 'width:100%;box-sizing:border-box;font-family:monospace;resize:vertical;';
  liPaste.appendChild(liArea);
  const liWarn = el('p', 'error hidden');           // visible warnings (never the secret)
  liWarn.style.cssText = 'margin:6px 0 0;';
  const liSubmit = el('button', 'primary', 'Submit session');
  liSubmit.style.width = 'auto';
  const liSubmitRow = el('div', 'bridge-actions');
  liSubmitRow.appendChild(liSubmit);
  liPaste.appendChild(liSubmitRow);
  liPaste.appendChild(liWarn);

  const liActions = el('div', 'bridge-actions');
  const liConnect = el('button', 'primary', 'Connect LinkedIn');
  liConnect.style.width = 'auto';
  liConnect.addEventListener('click', async () => {
    liWarn.classList.add('hidden');
    liWarn.textContent = '';
    await sendCmd('linkedin', 'login cookies');      // C-1 guarded
    liPaste.classList.remove('hidden');
    liArea.focus();
    window.open('https://www.linkedin.com/', '_blank', 'noopener');
  });
  liActions.appendChild(liConnect);

  const liDisc = el('button', 'danger', 'Disconnect');
  liDisc.id = 'btn-li-disconnect';
  liDisc.classList.add('hidden');
  liDisc.addEventListener('click', async () => {
    const id = liDisc.dataset.loginId;
    if (!id) return;
    if (await confirmModal('Disconnect LinkedIn?',
      'This unlinks the bridge from your LinkedIn account. Bridged rooms stay until deleted.', false)) {
      await sendCmd('linkedin', 'logout ' + id);
      sendCmd('linkedin', 'list-logins');
    }
  });
  liActions.appendChild(liDisc);

  const liRefresh = el('button', '', 'Refresh status');
  liRefresh.addEventListener('click', () => sendCmd('linkedin', 'list-logins'));
  liActions.appendChild(liRefresh);
  li.appendChild(liActions);

  // Submit: send the pasted secret through the C-1 guard, capture the event_id,
  // and redact it immediately. The value lives ONLY in this handler's scope; the
  // textarea is cleared before the network call and the value is dropped after.
  liSubmit.addEventListener('click', async () => {
    if (busy) return;
    liWarn.classList.add('hidden');
    liWarn.textContent = '';
    let secret = liArea.value;                       // read once; never logged
    liArea.value = '';                               // clear the field immediately
    if (!secret || !secret.trim()) {
      secret = null;
      liWarn.textContent = 'Paste your LinkedIn session before submitting.';
      liWarn.classList.remove('hidden');
      return;
    }
    busy = true; setButtonsDisabled(true); liSubmit.disabled = true;
    try {
      const sent = await sendSecretToMgmt('linkedin', secret);
      secret = null;                                 // drop the credential from memory
      if (!sent || !sent.eventId) {
        liWarn.textContent = 'Sent, but could not confirm the message id to delete it — please delete your pasted message in Element manually.';
        liWarn.classList.remove('hidden');
      } else {
        try {
          await redactMgmtEvent(sent.roomId, sent.eventId);
          liPaste.classList.add('hidden');           // hide the paste UI on success
        } catch (e) {
          liWarn.textContent = 'Session sent, but auto-deleting it failed — please delete your pasted message in Element manually.';
          liWarn.classList.remove('hidden');
        }
      }
    } catch (e) {
      secret = null;                                 // never surface the secret in errors
      liWarn.textContent = 'Could not send the session: ' + String(e.message || e);
      liWarn.classList.remove('hidden');
    } finally {
      busy = false; setButtonsDisabled(false); liSubmit.disabled = false;
      sendCmd('linkedin', 'list-logins');            // refresh the pill
    }
  });

  li.appendChild(liPaste);
  holder.appendChild(li);

  // X (Twitter) card (mirrors the LinkedIn card exactly: a session PASTE flow,
  // not a QR. The pasted value is a bearer credential: sent through the C-1 mgmt
  // guard, redacted immediately, and never logged, sanitize-rendered, persisted,
  // or turned into a URL.)
  const tw = el('div', 'card bridge-card');
  const twHead = el('div', 'bridge-head');
  twHead.appendChild(el('span', 'bridge-name', TW.label));
  const twPill = el('span', 'status-pill', 'Checking\u2026');
  twPill.id = 'tw-status';
  twHead.appendChild(twPill);
  tw.appendChild(twHead);
  tw.appendChild(el('p', 'muted', TW.blurb));

  const twPaste = el('div', 'tw-paste hidden');
  twPaste.style.cssText = 'margin-top:10px;';
  twPaste.appendChild(el('p', 'muted',
    'On x.com: DevTools \u2192 Network \u2192 filter a request \u2192 right-click \u2192 Copy as cURL, then paste here.'));
  const twArea = el('textarea');
  twArea.placeholder = 'Paste your X session (Copy as cURL) here';
  twArea.rows = 4;
  twArea.autocomplete = 'off';
  twArea.spellcheck = false;
  twArea.style.cssText = 'width:100%;box-sizing:border-box;font-family:monospace;resize:vertical;';
  twPaste.appendChild(twArea);
  const twWarn = el('p', 'error hidden');
  twWarn.style.cssText = 'margin:6px 0 0;';
  const twSubmit = el('button', 'primary', 'Submit session');
  twSubmit.style.width = 'auto';
  const twSubmitRow = el('div', 'bridge-actions');
  twSubmitRow.appendChild(twSubmit);
  twPaste.appendChild(twSubmitRow);
  twPaste.appendChild(twWarn);

  const twActions = el('div', 'bridge-actions');
  const twConnect = el('button', 'primary', 'Connect X');
  twConnect.style.width = 'auto';
  twConnect.addEventListener('click', async () => {
    twWarn.classList.add('hidden');
    twWarn.textContent = '';
    await sendCmd('twitter', 'login cookies');      // C-1 guarded
    twPaste.classList.remove('hidden');
    twArea.focus();
    window.open('https://x.com/', '_blank', 'noopener');
  });
  twActions.appendChild(twConnect);

  const twDisc = el('button', 'danger', 'Disconnect');
  twDisc.id = 'btn-tw-disconnect';
  twDisc.classList.add('hidden');
  twDisc.addEventListener('click', async () => {
    const id = twDisc.dataset.loginId;
    if (!id) return;
    if (await confirmModal('Disconnect X?',
      'This unlinks the bridge from your X account. Bridged rooms stay until deleted.', false)) {
      await sendCmd('twitter', 'logout ' + id);
      sendCmd('twitter', 'list-logins');
    }
  });
  twActions.appendChild(twDisc);

  const twRefresh = el('button', '', 'Refresh status');
  twRefresh.addEventListener('click', () => sendCmd('twitter', 'list-logins'));
  twActions.appendChild(twRefresh);
  tw.appendChild(twActions);

  twSubmit.addEventListener('click', async () => {
    if (busy) return;
    twWarn.classList.add('hidden');
    twWarn.textContent = '';
    let secret = twArea.value;                       // read once; never logged
    twArea.value = '';                               // clear the field immediately
    if (!secret || !secret.trim()) {
      secret = null;
      twWarn.textContent = 'Paste your X session before submitting.';
      twWarn.classList.remove('hidden');
      return;
    }
    busy = true; setButtonsDisabled(true); twSubmit.disabled = true;
    try {
      const sent = await sendSecretToMgmt('twitter', secret);
      secret = null;                                 // drop the credential from memory
      if (!sent || !sent.eventId) {
        twWarn.textContent = 'Sent, but could not confirm the message id to delete it \u2014 please delete your pasted message in Element manually.';
        twWarn.classList.remove('hidden');
      } else {
        try {
          await redactMgmtEvent(sent.roomId, sent.eventId);
          twPaste.classList.add('hidden');           // hide the paste UI on success
        } catch (e) {
          twWarn.textContent = 'Session sent, but auto-deleting it failed \u2014 please delete your pasted message in Element manually.';
          twWarn.classList.remove('hidden');
        }
      }
    } catch (e) {
      secret = null;                                 // never surface the secret in errors
      twWarn.textContent = 'Could not send the session: ' + String(e.message || e);
      twWarn.classList.remove('hidden');
    } finally {
      busy = false; setButtonsDisabled(false); twSubmit.disabled = false;
      sendCmd('twitter', 'list-logins');             // refresh the pill
    }
  });

  tw.appendChild(twPaste);
  holder.appendChild(tw);

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
  try { runtime.gmessages.mgmtRoomId = await resolveMgmt(GMSG); }
  catch (e) { logConsole('error', 'Google Messages management room: ' + String(e.message || e)); }
  try { runtime.instagram.mgmtRoomId = await resolveMgmt(IG); }
  catch (e) { logConsole('error', 'Instagram management room: ' + String(e.message || e)); }
  try { runtime.twitter.mgmtRoomId = await resolveMgmt(TW); }
  catch (e) { logConsole('error', 'X management room: ' + String(e.message || e)); }
  try { runtime.linkedin.mgmtRoomId = await resolveMgmt(LI); }
  catch (e) { logConsole('error', 'LinkedIn management room: ' + String(e.message || e)); }
  startSync();
  await sendStatusRefresh();                        // WhatsApp list-logins
  if (runtime.imessage.mgmtRoomId) await sendCmd('imessage', 'status');
  else { const pill = $('imsg-status'); if (pill) pill.textContent = 'Not set up'; }
  await sendCmd('gmessages', 'list-logins');
  await sendCmd('instagram', 'list-logins');
  await sendCmd('linkedin', 'list-logins');
  await sendCmd('twitter', 'list-logins');
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
  const sourceSearch = $('source-search');
  if (sourceSearch) sourceSearch.addEventListener('input', renderSourceList);

  // CV.2: native conversation view controls. Back stops the room watch and
  // returns to the filtered Home; send/Enter deliver to the OPEN room only.
  const convoBack = $('convo-back');
  if (convoBack) convoBack.addEventListener('click', () => {
    // Narrow-screen "back": DESELECT the open chat without leaving Home. Stop the
    // room watch, show the placeholder again, and clear the active-row highlight.
    stopConvoWatch();
    const convoPane = $('msgr-convo');
    if (convoPane) convoPane.classList.add('no-selection');
    setActiveConvoRow(null);
  });
  const convoSend = $('convo-send');
  if (convoSend) convoSend.addEventListener('click', sendConvoMessage);
  const convoInput = $('convo-input');
  if (convoInput) convoInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); sendConvoMessage(); }
  });

  // restore session
  try {
    const t = sessionStorage.getItem('hub_token'), u = sessionStorage.getItem('hub_user');
    if (t && u) { token = t; userId = u; enterApp(); return; }
  } catch (e) {}
  showAuth(false);
});
