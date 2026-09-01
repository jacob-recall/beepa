// Relocated verbatim from hub/site/app.js (PLAN-MASTER-SYNC-IMPL P1.2).
// Shared ES module. Logic unchanged; only import/export + shared-state (S) access added.

import { fetchSnapshot, seedFeed, startFeedSync, startFeedRefresh } from '../../shared/ui/account-data.js';
import { countSharedNow, initConsentUI } from './consent.js';
import { bridgeInvitesToJoin } from './invites.js';
import { initContactsUI, openAddToContact } from './contacts.js';
import { autoMergeContacts } from './enrich.js';
import { initProposalsUI } from './proposals.js';
import { initOrgLinkUI } from './orglink.js';
import { sendConvoMessage, stopConvoWatch } from '../../shared/ui/chat.js';
import { ROOMID_RE, api, setOnUnauthorized } from '../../shared/matrix/client.js';
import { confirmModal, ensureConnections, ensureSettings, logConsole, setPlatformRailHook } from '../../shared/ui/connections.js';
import { $ } from '../../shared/ui/el.js';
import { buildNav, navTo, refreshPlatformRail, showAuth } from '../../shared/ui/nav.js';
import { setActiveConvoRow } from '../../shared/ui/rows.js';
import { renderHome, renderSourceList, setFeedRenderHook } from '../../shared/ui/search.js';
import { GMSG, IG, LI, SOURCES, TW, WA, clearQR, resolveImsgMgmt, resolveMgmt, sendCmd, sendStatusRefresh, startSync } from '../../shared/ui/sources.js';
import { S, convoNamePending, convoNames, convoSeen, feedManualHidden, feedModel, runtime } from '../../shared/state.js';

// Register the transport's 401 handler (see shared/matrix/client.js).
setOnUnauthorized(forgetSession);

// ---- session ----
function forgetSession() {
  S.token = null; S.userId = null;
  runtime.whatsapp.mgmtRoomId = null; runtime.imessage.mgmtRoomId = null; runtime.gmessages.mgmtRoomId = null; runtime.instagram.mgmtRoomId = null; runtime.linkedin.mgmtRoomId = null; runtime.twitter.mgmtRoomId = null;
  for (const k of Object.keys(runtime)) runtime[k].connected = false;
  S.syncRunning = false;
  S.feedRunning = false;                              // HF-2: stop the feed loop with the session
  S.feedSince = null;
  feedModel.clear();
  S.feedLowPriority = new Set();                        // HF-9: drop hide-signals with the session
  S.feedMuted = new Set();
  feedManualHidden.clear();
  S.feedShowHidden = false;
  if (S.feedRevalTimer) { clearTimeout(S.feedRevalTimer); S.feedRevalTimer = null; }
  S.convoRunning = false;                              // CV.2: stop the room watch with the session
  S.openRoomId = null;
  S.convoSince = null;
  convoSeen.clear();
  convoNames.clear();
  convoNamePending.clear();
  S.selfMxids = new Set();                              // self-align: drop self identities with the session
  setActiveConvoRow(null);                            // layout: clear active-row highlight on sign-out
  const convoPane = $('msgr-convo');
  if (convoPane) convoPane.classList.add('no-selection'); // reset the right pane to the placeholder
  S.joinedSet = new Set();
  userJoinFailed.clear();                             // auto-join memo is session-scoped
  autojoinSessionJoined = 0;
  if (inviteTimer) { clearInterval(inviteTimer); inviteTimer = null; }  // stop the invite poll with the session
  autojoinPending = { refused: 0, overCap: 0, declined: 0 };
  renderAutojoinNote();
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
  S.token = data.access_token; S.userId = data.user_id;
  try {
    sessionStorage.setItem('hub_token', S.token);
    sessionStorage.setItem('hub_user', S.userId);
    localStorage.setItem('hub_device', data.device_id);
  } catch (e) {}
}
async function signOut() {
  try { await api('POST', '/_matrix/client/v3/logout', {}); } catch (e) {}
  forgetSession();
  const wrap = $('signout-note-wrap');
  if (wrap) wrap.classList.remove('hidden');
}

// ===========================================================================
// Bridge invite auto-join (identity-gated).
//
// The six bridges create a room per conversation and INVITE the user; only
// Google Messages' double-puppeting joins on the user's behalf, so without this
// every other bridge's new conversations stay invisible. The decision of WHICH
// invites may be accepted lives entirely in ./invites.js (a pure zero-import
// leaf, unit-tested in tests/unit/user_invites.test.js) — this file only
// performs the joins it returns. Never re-implement the predicate here.
//
// Joining a room is membership, not a send: it grants no new capability to the
// remote side. It does, however, make a conversation eligible for the uplink's
// consent resolution, so the FIRST time this app would accept invites it asks,
// stating how many would become visible to the manager under the CURRENT
// policy (that count comes from the shared resolver via consent.js).
// ===========================================================================
const AUTOJOIN_ACK_KEY = 'beepa_autojoin_ack';
const AUTOJOIN_MAX_EXAMINE = 100;   // per call
const AUTOJOIN_MAX_JOINS = 30;      // per call
const AUTOJOIN_MAX_SESSION = 200;   // per session, across calls (anti-join-storm)
// Hard (non-429 4xx) join failures: withdrawn/forbidden/gone invites are never
// retried in-session. Session-scoped, like apps/master's MS.joinFailed.
const userJoinFailed = new Set();
let autojoinSessionJoined = 0;
let inviteTimer = null;   // periodic bridge-invite poll (started in enterApp)
// What was NOT accepted, for the visible count: refused on identity grounds,
// deferred by the per-call cap, or left pending because the user declined.
let autojoinPending = { refused: 0, overCap: 0, declined: 0 };

function renderAutojoinNote() {
  const host = $('autojoin-note');
  if (!host) return;
  const n = autojoinPending.refused + autojoinPending.overCap + autojoinPending.declined;
  host.textContent = n ? n + ' pending invitation(s) not accepted' : '';
  host.title = n
    ? 'These room invitations were not from a recognized bridge bot, or were left for a later pass. To review them, bring up the opt-in Element escape hatch (docker compose --profile escape up -d element) and accept or decline them there.'
    : '';
  host.classList.toggle('hidden', n === 0);
}

// The six bridge bots + their space names, straight from the code-owned SOURCES
// table. Nothing here is read off the wire.
function bridgeIdentities() {
  const sources = [];
  for (const s of SOURCES) {
    if (s.kind !== 'source' || typeof s.botMxid !== 'string' || !s.botMxid) continue;
    if (typeof s.spaceName !== 'string' || !s.spaceName) continue;
    sources.push(s);
  }
  return sources;
}

// Which source each admitted invite belongs to, for the confirm's honest count.
// Derived by re-running the SAME predicate restricted to one bot at a time —
// deliberately not by parsing the invite here, so main.js holds no copy of the
// identity logic. Labeling only; the authoritative admit list is `joinIds`.
function labelJoins(fresh, joinIds, sources, selfMxid) {
  const admitted = new Set(joinIds);
  const convos = [];
  const seen = new Set();
  for (const s of sources) {
    const r = bridgeInvitesToJoin(fresh, [s.botMxid], selfMxid,
      [{ spaceName: s.spaceName, botMxid: s.botMxid }],
      { maxExamine: AUTOJOIN_MAX_EXAMINE, maxJoins: AUTOJOIN_MAX_JOINS });
    for (const id of r.join) {
      if (!admitted.has(id) || seen.has(id)) continue;
      seen.add(id);
      convos.push({ id, sourceId: s.id, sourceLabel: s.label });
    }
  }
  // An admitted room with no source label still counts as a conversation whose
  // sharing must be resolved (it just has no per-source policy to match).
  for (const id of joinIds) if (!seen.has(id)) convos.push({ id, sourceId: null, sourceLabel: null });
  return convos;
}

// One pass: read pending invites, admit the bridge-identified ones, join them.
async function joinBridgeInvites() {
  const sources = bridgeIdentities();
  if (!sources.length || !S.userId) return;
  let data;
  try { data = await fetchSnapshot(); } catch (e) { return; }   // same filter as the feed seed
  const section = (data.rooms && data.rooms.invite) || {};
  // Pre-filter BEFORE the predicate so already-joined / hard-failed rooms never
  // consume the examine budget (anti-starvation).
  const fresh = {};
  for (const id of Object.keys(section)) {
    if (S.joinedSet.has(id) || userJoinFailed.has(id)) continue;
    fresh[id] = section[id];
  }
  const res = bridgeInvitesToJoin(fresh, sources.map(s => s.botMxid), S.userId,
    sources.map(s => ({ spaceName: s.spaceName, botMxid: s.botMxid })),
    { maxExamine: AUTOJOIN_MAX_EXAMINE, maxJoins: AUTOJOIN_MAX_JOINS });
  autojoinPending = { refused: res.refusedNonBridge, overCap: res.overCap, declined: 0 };
  renderAutojoinNote();
  if (!res.join.length) return;

  // First run (per browser profile): confirm before accepting anything.
  let acked = false;
  try { acked = localStorage.getItem(AUTOJOIN_ACK_KEY) === '1'; } catch (e) { acked = false; }
  if (!acked) {
    let visible = 0;
    try { visible = countSharedNow(labelJoins(fresh, res.join, sources, S.userId)); }
    catch (e) { visible = 0; }
    const ok = await confirmModal('Accept pending conversations?',
      'Accept ' + res.join.length + ' pending conversation(s)? Under your current sharing policy, '
      + visible + ' would become visible to your manager.', false);
    if (!ok) {
      autojoinPending.declined = res.join.length;   // still pending, and now visible as such
      renderAutojoinNote();
      return;
    }
    try { localStorage.setItem(AUTOJOIN_ACK_KEY, '1'); } catch (e) { /* re-ask next session */ }
  }

  let joined = 0;
  for (const id of res.join) {
    if (autojoinSessionJoined >= AUTOJOIN_MAX_SESSION) break;
    if (!ROOMID_RE.test(id)) continue;              // server-pinned shape, re-asserted at the call
    try {
      await api('POST', '/_matrix/client/v3/rooms/' + encodeURIComponent(id) + '/join', {});
      joined++; autojoinSessionJoined++;
    } catch (e) {
      // 4xx other than 429 will not succeed by retrying (withdrawn invite,
      // forbidden, gone) -> stop asking this session. 429/5xx/network stay retryable.
      const code = e && typeof e.status === 'number' ? e.status : 0;
      if (code >= 400 && code < 500 && code !== 429) userJoinFailed.add(id);
    }
  }
  if (joined) {
    try { await seedFeed(); renderHome(); refreshPlatformRail(); } catch (e) { /* next refresh picks it up */ }
  }
}

// ---- app entry ----
async function enterApp() {
  $('whoami').textContent = S.userId;
  const wrap = $('signout-note-wrap');
  if (wrap) wrap.classList.add('hidden');
  buildNav();
  setPlatformRailHook(refreshPlatformRail);
  setFeedRenderHook(refreshPlatformRail);
  ensureConnections();
  ensureSettings();
  try { await initConsentUI(); } catch (e) { /* share controls stay at safe defaults on error */ }
  try { initProposalsUI(); } catch (e) { /* proposal inbox hook stays unregistered on error */ }
  try { initContactsUI(); } catch (e) { /* contacts hook stays unregistered on error */ }
  try { await initOrgLinkUI(); } catch (e) { /* org-link panel stays absent on error */ }
  showAuth(true);
  navTo('home');                                    // HF-8: default view = Home feed
  // Seed the isolated feed model from the validated snapshot, render, then start
  // the SEPARATE feed /sync loop (HF-1). This is independent of startSync below.
  try { await seedFeed(); renderHome(); } catch (e) { /* feed stays empty on error */ }
  startFeedSync();
  startFeedRefresh();                               // periodic re-seed keeps newest convos time-ordered
  // Conversation-number enrichment: once per session, in the background, group
  // same-phone-number conversations into one contact. Fire-and-forget so it
  // never blocks the UI; fails soft if the loopback helper is not reachable.
  autoMergeContacts().catch(() => {});
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
  // AFTER mgmt-room resolution on purpose: resolveMgmt scans joined rooms with a
  // GET per room, so accepting invites first would enlarge that scan, and the
  // mgmt rooms are pinned before any newly joined room can be considered.
  try { await joinBridgeInvites(); }
  catch (e) { /* invites stay pending; the note keeps the count visible */ }
  // New conversations from bridges that INVITE (every bridge except Google
  // Messages, which double-puppets and self-joins) only appear once their
  // invite is accepted. joinBridgeInvites ran once above; keep polling so a
  // brand-new chat syncs within ~one interval instead of waiting for the next
  // sign-in/reload (the old behaviour — an unbounded "why is this so slow?").
  // Cleared in forgetSession.
  if (inviteTimer) clearInterval(inviteTimer);
  inviteTimer = setInterval(() => { joinBridgeInvites().catch(() => {}); }, 20000);
  startSync();
  await sendStatusRefresh();                        // WhatsApp list-logins
  if (runtime.imessage.mgmtRoomId) await sendCmd('imessage', 'status');
  else { const pill = $('imsg-status'); if (pill) pill.textContent = 'Not set up'; }
  await sendCmd('gmessages', 'list-logins');
  await sendCmd('instagram', 'list-logins');
  await sendCmd('linkedin', 'list-logins');
  await sendCmd('twitter', 'list-logins');
  refreshPlatformRail();
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
    await sendCmd(S.activeSettingsSource, v);
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
  const convoAddContact = $('convo-add-contact');
  if (convoAddContact) convoAddContact.addEventListener('click', () => {
    if (!S.openRoomId) return;                       // no-op with no conversation open
    openAddToContact(S.openRoomId);
  });
  const convoSend = $('convo-send');
  // Wrap so the click Event is never passed as an explicit target/body — the
  // composer path must run with no arguments (reads S.openRoomId + #convo-input).
  if (convoSend) convoSend.addEventListener('click', () => sendConvoMessage());
  const convoInput = $('convo-input');
  if (convoInput) convoInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); sendConvoMessage(); }
  });

  // restore session, else auto-login from a provisioned local session file
  (async () => {
    try {
      const t = sessionStorage.getItem('hub_token'), u = sessionStorage.getItem('hub_user');
      if (t && u) { S.token = t; S.userId = u; await enterApp(); return; }
    } catch (e) {}
    // Passwordless local login: setup.sh writes apps/user/session.local.json
    // (gitignored, served only on loopback) with the already-provisioned token,
    // so this single-user local hub has no login screen. Same-origin fetch, so
    // it stays within the app's CSP connect-src 'self'. Falls back to the login
    // form if the file is absent or its token is invalid.
    try {
      const r = await fetch('session.local.json', { cache: 'no-store' });
      if (r.ok) {
        const s = await r.json();
        if (s && s.access_token && s.user_id) {
          S.token = s.access_token; S.userId = s.user_id;
          try { sessionStorage.setItem('hub_token', S.token); sessionStorage.setItem('hub_user', S.userId); } catch (e) {}
          await enterApp(); return;
        }
      }
    } catch (e) {}
    showAuth(false);
  })();
});
