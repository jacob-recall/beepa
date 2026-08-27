// Relocated verbatim from hub/site/app.js (PLAN-MASTER-SYNC-IMPL P1.2).
// Shared ES module. Logic unchanged; only import/export + shared-state (S) access added.

import { seedFeed, startFeedSync } from '../../shared/ui/account-data.js';
import { initConsentUI } from './consent.js';
import { initProposalsUI } from './proposals.js';
import { sendConvoMessage, stopConvoWatch } from '../../shared/ui/chat.js';
import { api, setOnUnauthorized } from '../../shared/matrix/client.js';
import { buildConnections, buildSettings, logConsole } from '../../shared/ui/connections.js';
import { $ } from '../../shared/ui/el.js';
import { buildNav, mountChats, navTo, openConversation, showAuth, unmountChats } from '../../shared/ui/nav.js';
import { setActiveConvoRow } from '../../shared/ui/rows.js';
import { buildDirectory, renderHome, renderSourceList } from '../../shared/ui/search.js';
import { GMSG, IG, LI, TW, WA, clearQR, resolveImsgMgmt, resolveMgmt, sendCmd, sendStatusRefresh, startSync } from '../../shared/ui/sources.js';
import { S, convoNamePending, convoNames, convoSeen, feedManualHidden, feedModel, runtime } from '../../shared/state.js';

// Register the transport's 401 handler (see shared/matrix/client.js).
setOnUnauthorized(forgetSession);

// ---- session ----
function forgetSession() {
  S.token = null; S.userId = null;
  runtime.whatsapp.mgmtRoomId = null; runtime.imessage.mgmtRoomId = null; runtime.gmessages.mgmtRoomId = null; runtime.instagram.mgmtRoomId = null; runtime.linkedin.mgmtRoomId = null; runtime.twitter.mgmtRoomId = null;
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

// ---- app entry ----
async function enterApp() {
  $('whoami').textContent = S.userId;
  const wrap = $('signout-note-wrap');
  if (wrap) wrap.classList.add('hidden');
  buildNav();
  buildConnections();
  buildSettings();
  buildDirectory();
  try { await initConsentUI(); } catch (e) { /* share controls stay at safe defaults on error */ }
  try { initProposalsUI(); } catch (e) { /* proposal inbox hook stays unregistered on error */ }
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
  const convoSend = $('convo-send');
  // Wrap so the click Event is never passed as an explicit target/body — the
  // composer path must run with no arguments (reads S.openRoomId + #convo-input).
  if (convoSend) convoSend.addEventListener('click', () => sendConvoMessage());
  const convoInput = $('convo-input');
  if (convoInput) convoInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); sendConvoMessage(); }
  });

  // restore session
  try {
    const t = sessionStorage.getItem('hub_token'), u = sessionStorage.getItem('hub_user');
    if (t && u) { S.token = t; S.userId = u; enterApp(); return; }
  } catch (e) {}
  showAuth(false);
});
