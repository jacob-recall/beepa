// Shared navigation + unified two-pane workspace shell.

import { refreshConvos } from './account-data.js';
import { stopConvoWatch } from './chat.js';
import { $, el } from './el.js';
import { setActiveConvoRow } from './rows.js';
import { ensureConnections, ensureSettings } from './connections.js';
import { loadSourceList, renderHome, renderPeople } from './search.js';
import { SOURCES, sendCmd } from './sources.js';
import { S, runtime, convosBySource, feedModel } from '../state.js';

let sharingViewHook = null;
function setSharingViewHook(fn) { sharingViewHook = typeof fn === 'function' ? fn : null; }

let proposalsViewHook = null;
function setProposalsViewHook(fn) { proposalsViewHook = typeof fn === 'function' ? fn : null; }

let contactsViewHook = null;
function setContactsViewHook(fn) { contactsViewHook = typeof fn === 'function' ? fn : null; }

let listMode = 'chats'; // 'chats' | 'proposals' — conversation-layer toggle on Home

const LIST_SEARCH = {
  home: 'home-search',
  people: 'people-search',
};
for (const s of SOURCES) {
  if (s.kind !== 'all') LIST_SEARCH['source:' + s.id] = 'source-search';
}

const PILL_BY_SOURCE = {
  whatsapp: 'wa-status', imessage: 'imsg-status', gmessages: 'gmsg-status',
  instagram: 'ig-status', linkedin: 'li-status', twitter: 'tw-status',
};

function platformLogoBadge(sourceId) {
  return el('span', 'plat-badge' + (sourceId ? ' ' + sourceId : ''), '');
}

function sourceConnected(sourceId) {
  if (!sourceId) return false;
  if ((convosBySource[sourceId] || []).length > 0) return true;
  for (const row of feedModel.values()) {
    if (row.sourceId === sourceId) return true;
  }
  const rt = runtime[sourceId];
  if (rt && rt.connected) return true;
  const pillId = PILL_BY_SOURCE[sourceId];
  if (pillId) {
    const pill = $(pillId);
    if (pill) {
      if (pill.classList.contains('ok')) return true;
      const t = (pill.textContent || '').trim();
      if (/^Connected:/.test(t) || t === 'Ready') return true;
    }
  }
  return false;
}

function resetConvoPlaceholder() {
  const ph = $('convo-placeholder');
  if (!ph) return;
  ph.classList.remove('connect-prompt');
  ph.replaceChildren();
  ph.appendChild(document.createTextNode('Select a conversation.'));
}

function showConnectPrompt(sourceId) {
  const source = SOURCES.find(s => s.id === sourceId);
  const ph = $('convo-placeholder');
  if (!ph || !source) return;
  ph.classList.add('connect-prompt');
  ph.replaceChildren();
  ph.appendChild(el('p', 'connect-prompt-lead', source.label + ' is not connected yet.'));
  const btn = el('button', 'primary connect-settings-btn', 'Connect in Settings');
  btn.type = 'button';
  btn.addEventListener('click', () => openPlatformConnect(sourceId));
  ph.appendChild(btn);
}

function highlightBridgeCard(sourceId) {
  const card = $('bridge-card-' + sourceId);
  if (!card) return;
  card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  card.classList.add('bridge-card-highlight');
  window.setTimeout(() => card.classList.remove('bridge-card-highlight'), 2200);
}

function openPlatformConnect(sourceId) {
  closeSettingsPopover();
  setActiveNav('settings');
  showSection('view-workspace');
  showSettingsHub('connections', sourceId);
}

function platformRailClick(sourceId) {
  if (sourceConnected(sourceId)) navTo('source:' + sourceId);
  else openPlatformConnect(sourceId);
}

function showListMode(show) {
  const lm = $('list-mode');
  if (lm) lm.classList.toggle('hidden', !show);
}

function setListMode(mode) {
  listMode = mode === 'proposals' ? 'proposals' : 'chats';
  const chatsBtn = $('list-mode-chats');
  const propBtn = $('list-mode-proposals');
  if (chatsBtn) chatsBtn.classList.toggle('active', listMode === 'chats');
  if (propBtn) propBtn.classList.toggle('active', listMode === 'proposals');
  renderHomeLayer();
}

function renderHomeLayer() {
  if (listMode === 'proposals') {
    showListSearch(null);
    showListMode(true);
    setDetailMode('empty');
    if (proposalsViewHook) proposalsViewHook();
    return;
  }
  showListSearch('home');
  showListMode(true);
  setDetailMode(S.openRoomId ? 'chat' : 'empty');
  renderHome();
  const convoPane = $('msgr-convo');
  if (S.openRoomId) {
    if (convoPane) convoPane.classList.remove('no-selection');
    setActiveConvoRow(S.openRoomId);
  } else {
    if (convoPane) convoPane.classList.add('no-selection');
    setActiveConvoRow(null);
  }
}

function showListSearch(key) {
  for (const input of document.querySelectorAll('.list-search')) input.classList.add('hidden');
  if (key) {
    const id = LIST_SEARCH[key];
    if (id) { const node = $(id); if (node) node.classList.remove('hidden'); }
  }
  const actions = $('list-head-actions');
  if (actions) actions.classList.toggle('hidden', key !== 'people');
  const shareSlot = $('source-share-switch');
  if (shareSlot) shareSlot.classList.toggle('hidden', !(key && key.indexOf('source:') === 0));
}

function setDetailMode(mode) {
  const pane = $('msgr-convo');
  const chat = $('detail-chat');
  const proposal = $('detail-proposal');
  const contact = $('detail-contact');
  const admin = $('detail-admin');
  if (chat) chat.classList.toggle('hidden', mode !== 'chat');
  if (proposal) proposal.classList.toggle('hidden', mode !== 'proposal');
  if (contact) contact.classList.toggle('hidden', mode !== 'contact');
  if (admin) admin.classList.toggle('hidden', mode !== 'admin');
  if (pane) {
    const empty = mode === 'empty' || mode === 'admin';
    pane.classList.toggle('no-selection', empty);
  }
  if (mode !== 'empty') resetConvoPlaceholder();
}

function setWorkspaceLayout(twoPane) {
  const listPane = $('list-pane');
  if (listPane) listPane.classList.toggle('hidden', !twoPane);
  const ws = $('workspace');
  if (ws) ws.classList.toggle('admin-only', !twoPane);
}

function showSettingsSection(section) {
  const id = section || 'platforms';
  for (const b of document.querySelectorAll('.settings-tab')) {
    b.classList.toggle('active', b.dataset.section === id);
  }
  for (const name of ['platforms', 'connections', 'sharing', 'advanced']) {
    const pane = $('settings-section-' + name);
    if (pane) pane.classList.toggle('hidden', name !== id);
  }
}

function wireSettingsHub() {
  const tabs = $('settings-tabs');
  if (tabs && !tabs.dataset.wired) {
    tabs.dataset.wired = '1';
    for (const b of tabs.querySelectorAll('.settings-tab')) {
      b.addEventListener('click', () => showSettingsSection(b.dataset.section));
    }
  }
  const back = $('settings-back');
  if (back && !back.dataset.wired) {
    back.dataset.wired = '1';
    back.addEventListener('click', () => navTo('home'));
  }
}

function showSettingsHub(section, sourceId) {
  closeSettingsPopover();
  setWorkspaceLayout(false);
  setDetailMode('admin');
  wireSettingsHub();
  showSettingsSection(section || 'platforms');
  ensureConnections();
  ensureSettings();
  if (sourceId) highlightBridgeCard(sourceId);
  if (sharingViewHook) sharingViewHook();
}

function showAuth(signedIn) {
  $('shell').classList.toggle('hidden', !signedIn);
  $('view-signin').classList.toggle('hidden', signedIn);
  closeSettingsPopover();
}

let activeNavKey = 'home';

function setActiveNav(key) {
  activeNavKey = key;
  S.activeNavKey = key;                               // shared so the feed render can tell which view is active
  for (const b of document.querySelectorAll('.navitem')) {
    b.classList.toggle('active', b.dataset.navkey === key);
  }
}

function showSection(id) {
  for (const s of document.querySelectorAll('#content .view')) s.classList.toggle('hidden', s.id !== id);
}

function closeSettingsPopover() {
  const pop = $('settings-popover');
  const btn = $('nav-settings-toggle');
  if (pop) pop.classList.add('hidden');
  if (btn) btn.setAttribute('aria-expanded', 'false');
}

function toggleSettingsPopover() {
  const pop = $('settings-popover');
  const btn = $('nav-settings-toggle');
  if (!pop || !btn) return;
  const open = pop.classList.toggle('hidden');
  btn.setAttribute('aria-expanded', open ? 'false' : 'true');
}

function buildPlatformList() {
  const host = $('settings-platforms-list');
  if (!host) return;
  host.replaceChildren();
  for (const s of SOURCES) {
    if (s.kind === 'all') continue;
    const key = 'source:' + s.id;
    const btn = el('button', 'settings-row');
    btn.type = 'button';
    btn.dataset.navkey = key;
    const ic = platformLogoBadge(s.id);
    ic.classList.add('settings-row-ic');
    btn.appendChild(ic);
    const meta = el('span', 'settings-row-meta');
    meta.appendChild(el('span', 'settings-row-title', s.label));
    btn.appendChild(meta);
    btn.appendChild(el('span', 'settings-row-chev', '›'));
    btn.addEventListener('click', () => navTo(key));
    host.appendChild(btn);
  }
}

async function navTo(key) {
  closeSettingsPopover();
  const chatKeys = new Set(['home', 'people']);
  for (const s of SOURCES) if (s.kind !== 'all') chatKeys.add('source:' + s.id);

  if (S.openRoomId && !chatKeys.has(key) && key !== 'all' && key !== 'settings') stopConvoWatch();

  if (key === 'settings') {
    setActiveNav('settings');
    showSection('view-workspace');
    showSettingsHub();
    return;
  }

  setActiveNav(key);
  showSection(key === 'all' ? 'view-chats' : 'view-workspace');

  if (key === 'all') return;

  if (key === 'home') {
    listMode = 'chats';
    setWorkspaceLayout(true);
    setListMode('chats');
  } else if (key.indexOf('source:') === 0) {
    const sourceId = key.slice(7);
    setWorkspaceLayout(true);
    showListMode(false);
    showListSearch(key);
    if (!sourceConnected(sourceId)) {
      setDetailMode('empty');
      showConnectPrompt(sourceId);
      await loadSourceList(sourceId);
      return;
    }
    setDetailMode(S.openRoomId ? 'chat' : 'empty');
    await loadSourceList(sourceId);
  } else if (key === 'people') {
    setWorkspaceLayout(true);
    showListMode(false);
    showListSearch('people');
    setDetailMode('empty');
    try { await refreshConvos(); } catch (e) {}
    if (contactsViewHook) contactsViewHook();
    else renderPeople();
  }
}

function wireListMode() {
  const chatsBtn = $('list-mode-chats');
  const propBtn = $('list-mode-proposals');
  if (chatsBtn && !chatsBtn.dataset.wired) {
    chatsBtn.dataset.wired = '1';
    chatsBtn.addEventListener('click', () => setListMode('chats'));
  }
  if (propBtn && !propBtn.dataset.wired) {
    propBtn.dataset.wired = '1';
    propBtn.addEventListener('click', () => setListMode('proposals'));
  }
}

function wireSettingsMenu() {
  const toggle = $('nav-settings-toggle');
  if (toggle && !toggle.dataset.wired) {
    toggle.dataset.wired = '1';
    toggle.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleSettingsPopover();
    });
  }
  const openHub = $('settings-open-hub');
  if (openHub && !openHub.dataset.wired) {
    openHub.dataset.wired = '1';
    openHub.addEventListener('click', () => navTo('settings'));
  }
  if (!window.__settingsPopoverCloser) {
    window.__settingsPopoverCloser = true;
    document.addEventListener('click', (e) => {
      const pop = $('settings-popover');
      const btn = $('nav-settings-toggle');
      if (!pop || pop.classList.contains('hidden')) return;
      if (pop.contains(e.target) || (btn && btn.contains(e.target))) return;
      closeSettingsPopover();
    });
  }
}

function buildPlatformRail() {
  const rail = $('nav-platforms');
  if (!rail) return;
  rail.replaceChildren();
  for (const s of SOURCES) {
    if (s.kind === 'all') continue;
    const key = 'source:' + s.id;
    const btn = el('button', 'navitem nav-icon platform-rail-btn platform-off');
    btn.type = 'button';
    btn.dataset.navkey = key;
    btn.dataset.sourceId = s.id;
    btn.title = s.label + ' (not connected)';
    btn.setAttribute('aria-label', s.label);
    btn.appendChild(platformLogoBadge(s.id));
    btn.addEventListener('click', () => platformRailClick(s.id));
    rail.appendChild(btn);
  }
  refreshPlatformRail();
}

function refreshPlatformRail() {
  for (const btn of document.querySelectorAll('.platform-rail-btn')) {
    const id = btn.dataset.sourceId;
    const on = sourceConnected(id);
    btn.classList.toggle('platform-off', !on);
    btn.classList.toggle('platform-on', on);
    btn.setAttribute('aria-disabled', on ? 'false' : 'true');
    const source = SOURCES.find(s => s.id === id);
    const label = source ? source.label : id;
    btn.title = on ? label : label + ' — tap to connect in Settings';
  }
}

function buildNav() {
  buildPlatformRail();
  buildPlatformList();
  wireListMode();
  wireSettingsMenu();
  wireTool('nav-home', 'home');
  wireTool('nav-people', 'people');
}

function wireTool(id, key) {
  const b = $(id);
  if (!b) return;
  b.dataset.navkey = key;
  b.addEventListener('click', () => navTo(key));
}

export {
  showAuth, setActiveNav, showSection, navTo, buildNav, wireTool,
  setSharingViewHook, setProposalsViewHook, setContactsViewHook,
  setDetailMode, showListSearch, setListMode, renderHomeLayer, closeSettingsPopover,
  refreshPlatformRail, openPlatformConnect,
};
