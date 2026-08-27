// Relocated verbatim from hub/site/app.js (PLAN-MASTER-SYNC-IMPL P1.2).
// Shared ES module. Logic unchanged; only import/export + shared-state (S) access added.

import { refreshConvos } from './account-data.js';
import { stopConvoWatch } from './chat.js';
import { CHATS_URL, ROOMID_RE } from '../matrix/client.js';
import { $, el } from './el.js';
import { setActiveConvoRow } from './rows.js';
import { loadSourceList, renderDirectory, renderHome } from './search.js';
import { SOURCES, sendCmd } from './sources.js';
import { S, runtime } from '../state.js';

// Optional app-injected hook: called (with no args) whenever the 'sharing' nav
// key is opened, so apps/user can (re)render its consent/share-controls view
// (PLAN-MASTER-SYNC §5.1/§4.2) without shared/ importing from apps/ (same hook
// pattern as setConvoRowDecorator / setSourceViewHook).
let sharingViewHook = null;
function setSharingViewHook(fn) { sharingViewHook = typeof fn === 'function' ? fn : null; }

// Same app-injection pattern for apps/user's proposal inbox (PLAN §2 v2 / §7):
// nav.js only knows the 'proposals' key; the render logic (reading the local
// proposals room, the approve/edit/send-through-the-guarded-path UI) lives in
// apps/user/proposals.js. no-op in apps/master, which has no #nav-proposals.
let proposalsViewHook = null;
function setProposalsViewHook(fn) { proposalsViewHook = typeof fn === 'function' ? fn : null; }

// Same app-injection pattern for apps/user's contact-profile management (PLAN
// §12 phase 5): nav.js only knows the 'contacts' key; the render logic (list
// profiles, create/attach/detach, share toggle, merge suggestions) lives in
// apps/user/contacts.js. no-op in apps/master, which has no #nav-contacts.
let contactsViewHook = null;
function setContactsViewHook(fn) { contactsViewHook = typeof fn === 'function' ? fn : null; }

// ---- open a conversation (U-1 / D-4) ----
function openConversation(roomId) {
  if (!ROOMID_RE.test(roomId)) return;             // reject ids failing the regex
  if (!S.joinedSet.has(roomId)) return;              // D-5: must be a joined room
  const f = $('chats-container') && $('chats-container').querySelector('iframe');
  if (f) f.src = CHATS_URL + '/#/room/' + roomId;  // constant prefix + validated RAW id
  navTo('all');
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
  if (S.openRoomId && key !== 'home') stopConvoWatch(); // CV.2: leaving the messenger stops its watch; Home keeps an open chat
  setActiveNav(key);
  if (key === 'home') {
    showSection('view-home');                        // unified two-pane messenger
    renderHome();
    // Right pane: if a chat is open (S.openRoomId), keep it shown and re-mark its
    // row active (renderHome rebuilt the rows); otherwise show the placeholder.
    const convoPane = $('msgr-convo');
    if (S.openRoomId) {
      if (convoPane) convoPane.classList.remove('no-selection');
      setActiveConvoRow(S.openRoomId);
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
  } else if (key === 'sharing') {
    showSection('view-sharing');
    if (sharingViewHook) sharingViewHook();
  } else if (key === 'proposals') {
    showSection('view-proposals');
    if (proposalsViewHook) proposalsViewHook();
  } else if (key === 'contacts') {
    showSection('view-contacts');
    if (contactsViewHook) contactsViewHook();
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
  wireTool('nav-sharing', 'sharing');           // no-op if the app has no #nav-sharing
  wireTool('nav-proposals', 'proposals');       // no-op if the app has no #nav-proposals
  wireTool('nav-contacts', 'contacts');         // no-op if the app has no #nav-contacts
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

export { openConversation, showAuth, setActiveNav, showSection, navTo, buildNav, wireTool, mountChats, unmountChats, setSharingViewHook, setProposalsViewHook, setContactsViewHook };
