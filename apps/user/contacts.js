// Contact profiles — list (left) + detail (right).

import {
  readProfiles, writeProfiles,
  upsertProfile, removeProfile, linkRoom, unlinkRoom, setProfileShare, newProfileId,
  suggestions,
} from '../../shared/model/contacts.js';
import { loadConsentState } from './consent.js';
import { $, el, sanitizeLine } from '../../shared/ui/el.js';
import { setContactsViewHook, setDetailMode } from '../../shared/ui/nav.js';
import { appendDirectoryRows } from '../../shared/ui/search.js';
import { SOURCES } from '../../shared/ui/sources.js';
import { convosBySource, feedModel } from '../../shared/state.js';
import { openConvo } from '../../shared/ui/chat.js';
import { buildPlatBadge } from '../../shared/ui/rows.js';

let store = { profiles: [] };
let activeProfileId = null;

const IGNORE_KEY = 'com.jkali.contact_suggestions_ignored';
function loadIgnored() {
  try { return new Set(JSON.parse(localStorage.getItem(IGNORE_KEY) || '[]')); }
  catch (e) { return new Set(); }
}
function saveIgnored(set) {
  try { localStorage.setItem(IGNORE_KEY, JSON.stringify([...set])); } catch (e) { /* ignore */ }
}

function allConvos() {
  const seen = new Set();
  const out = [];
  for (const s of SOURCES) {
    if (s.kind === 'all') continue;
    for (const c of (convosBySource[s.id] || [])) {
      if (seen.has(c.id)) continue;
      seen.add(c.id);
      out.push(c);
    }
  }
  return out;
}
function convoById(id) { return allConvos().find((c) => c.id === id) || null; }
function roomToProfileId() {
  const map = {};
  for (const p of store.profiles) for (const rid of p.roomIds) map[rid] = p.id;
  return map;
}

async function persist(next) {
  store = await writeProfiles(next);
  try { await loadConsentState(); } catch (e) { /* ok */ }
  renderPeopleList();
  if (activeProfileId && store.profiles.some(p => p.id === activeProfileId)) {
    renderContactDetail(activeProfileId);
  } else {
    activeProfileId = null;
    setDetailMode('empty');
  }
}

function profilePreview(p) {
  const convos = p.roomIds.map((rid) => convoById(rid)).filter(Boolean);
  convos.sort((a, b) => ((feedModel.get(b.id) || {}).lastTs || 0) - ((feedModel.get(a.id) || {}).lastTs || 0));
  const latest = convos[0];
  if (latest) {
    const prev = (feedModel.get(latest.id) || {}).lastBody;
    return prev ? sanitizeLine(prev) : sanitizeLine(latest.title || '');
  }
  return p.roomIds.length ? p.roomIds.length + ' linked' : 'No conversations';
}

function buildContactRow(p) {
  const name = sanitizeLine(p.displayName || 'Unnamed');
  const row = el('div', 'convo contact-row');
  row.dataset.profileId = p.id;
  row.setAttribute('role', 'button');
  row.tabIndex = 0;
  row.appendChild(el('div', 'avatar', (name || '?').slice(0, 1).toUpperCase()));
  const meta = el('div', 'meta');
  meta.appendChild(el('div', 'title', name));
  meta.appendChild(el('div', 'preview', profilePreview(p)));
  row.appendChild(meta);
  row.appendChild(el('span', 'when', String(p.roomIds.length)));
  const open = () => selectContact(p.id);
  row.addEventListener('click', open);
  row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  return row;
}

function buildSuggestionRow(group, ignored) {
  const row = el('div', 'convo contact-row contact-suggestion-row');
  const label = group.convos.map(c => sanitizeLine(c.title || c.id)).join(', ');
  row.appendChild(el('div', 'avatar', '?'));
  const meta = el('div', 'meta');
  meta.appendChild(el('div', 'title', 'Same person?'));
  meta.appendChild(el('div', 'preview', label));
  row.appendChild(meta);
  const btn = el('button', 'contact-merge-btn', 'Link');
  btn.type = 'button';
  btn.addEventListener('click', async (e) => {
    e.stopPropagation();
    const id = newProfileId();
    let next = upsertProfile(store, { id, displayName: group.convos[0].title || group.key, share: 'inherit' });
    for (const c of group.convos) next = linkRoom(next, id, c.id);
    await persist(next);
    selectContact(id);
  });
  row.appendChild(btn);
  const skip = el('button', 'contact-ignore', '×');
  skip.type = 'button';
  skip.addEventListener('click', (e) => {
    e.stopPropagation();
    ignored.add(group.key);
    saveIgnored(ignored);
    renderPeopleList();
  });
  row.appendChild(skip);
  return row;
}

function markActiveContact(id) {
  for (const row of document.querySelectorAll('.contact-row')) {
    row.classList.toggle('active', row.dataset.profileId === id);
  }
}

function buildShareToggle(profile) {
  const wrap = el('div', 'contact-share-row');
  wrap.appendChild(el('span', 'muted', 'Sharing'));
  const toggle = el('span', 'share-toggle');
  for (const [val, label] of [['share', 'Share'], ['inherit', 'Auto'], ['private', 'Private']]) {
    const b = el('button', 'share-opt' + (profile.share === val ? ' active' : ''), label);
    b.type = 'button';
    b.addEventListener('click', async () => { await persist(setProfileShare(store, profile.id, val)); });
    toggle.appendChild(b);
  }
  wrap.appendChild(toggle);
  return wrap;
}

function renderContactDetail(profileId) {
  const profile = store.profiles.find(p => p.id === profileId);
  const host = $('contact-detail-body');
  const nameInput = $('contact-detail-name');
  if (!profile || !host) return;

  if (nameInput) {
    nameInput.value = profile.displayName || '';
    if (!nameInput.dataset.wired) {
      nameInput.dataset.wired = '1';
      nameInput.addEventListener('blur', async () => {
        const v = nameInput.value.trim();
        if (!v || v === profile.displayName) return;
        await persist(upsertProfile(store, { id: profile.id, displayName: v }));
      });
    }
  }

  host.replaceChildren();
  host.appendChild(buildShareToggle(profile));

  const linked = el('div', 'contact-linked-list');
  const convos = profile.roomIds
    .map((rid) => convoById(rid) || { id: rid, title: rid, sourceId: '' })
    .sort((a, b) => ((feedModel.get(b.id) || {}).lastTs || 0) - ((feedModel.get(a.id) || {}).lastTs || 0));

  if (!convos.length) {
    linked.appendChild(el('p', 'muted', 'No conversations linked.'));
  } else {
    for (const c of convos) {
      const row = el('div', 'contact-linked-row');
      row.appendChild(buildPlatBadge(c.sourceId));
      const meta = el('span', 'contact-row-meta');
      meta.appendChild(el('span', 'title', sanitizeLine(c.title || c.id)));
      row.appendChild(meta);
      const unlink = el('button', 'contact-unlink', 'Remove');
      unlink.type = 'button';
      unlink.addEventListener('click', async (e) => {
        e.stopPropagation();
        await persist(unlinkRoom(store, c.id));
      });
      row.appendChild(unlink);
      row.addEventListener('click', () => openConvo(c.id));
      linked.appendChild(row);
    }
  }
  host.appendChild(linked);

  const attach = el('div', 'contact-attach');
  const attachInput = el('input');
  attachInput.placeholder = 'Search to link a conversation…';
  attachInput.spellcheck = false;
  const attachResults = el('div', 'contact-attach-results');
  attachInput.addEventListener('input', () => {
    const q = attachInput.value.trim().toLowerCase();
    attachResults.replaceChildren();
    if (!q) return;
    const linkedByRoom = roomToProfileId();
    const cands = allConvos().filter((c) =>
      sanitizeLine(c.title || '').toLowerCase().includes(q)).slice(0, 12);
    for (const c of cands) {
      const row = el('div', 'contact-attach-row');
      row.appendChild(el('span', 'title', sanitizeLine(c.title || c.id)));
      const btn = el('button', null, linkedByRoom[c.id] === profile.id ? 'Linked' : 'Link');
      btn.type = 'button';
      btn.disabled = linkedByRoom[c.id] === profile.id;
      btn.addEventListener('click', async () => {
        await persist(linkRoom(store, profile.id, c.id));
        attachInput.value = '';
        attachResults.replaceChildren();
      });
      row.appendChild(btn);
      attachResults.appendChild(row);
    }
  });
  attach.appendChild(attachInput);
  attach.appendChild(attachResults);
  host.appendChild(attach);

  const del = el('button', 'danger contact-delete-block', 'Delete contact');
  del.type = 'button';
  del.addEventListener('click', async () => {
    await persist(removeProfile(store, profile.id));
  });
  host.appendChild(del);
}

function selectContact(id) {
  activeProfileId = id;
  markActiveContact(id);
  setDetailMode('contact');
  renderContactDetail(id);
}

function filterProfiles(q) {
  if (!q) return store.profiles;
  q = q.toLowerCase();
  return store.profiles.filter(p =>
    sanitizeLine(p.displayName || '').toLowerCase().includes(q));
}

function renderPeopleList() {
  const list = $('list-body');
  if (!list) return;
  const q = (($('people-search') && $('people-search').value) || '').trim().toLowerCase();
  list.replaceChildren();

  const withName = allConvos().map((c) => Object.assign({}, c, { name: c.title }));
  const groups = suggestions(withName, store);
  const ignored = loadIgnored();
  for (const g of groups.filter(x => !ignored.has(x.key)).slice(0, 3)) {
    list.appendChild(buildSuggestionRow(g, ignored));
  }

  const profiles = filterProfiles(q);
  if (profiles.length) {
    list.appendChild(el('div', 'list-section', 'Contacts'));
    for (const p of profiles) list.appendChild(buildContactRow(p));
  }

  const dirTotal = appendDirectoryRows(list, q);
  if (!profiles.length && !dirTotal && !list.childElementCount) {
    list.appendChild(el('p', 'list-empty', q ? 'No matches.' : 'No people or conversations yet.'));
  }
}

function wireContactsNav() {
  const back = $('contact-back');
  if (back && !back.dataset.wired) {
    back.dataset.wired = '1';
    back.addEventListener('click', () => {
      activeProfileId = null;
      markActiveContact(null);
      setDetailMode('empty');
    });
  }
  const search = $('people-search');
  if (search && !search.dataset.wiredPeople) {
    search.dataset.wiredPeople = '1';
    search.addEventListener('input', renderPeopleViewBody);
  }
  const btn = $('contact-new-btn');
  const input = $('contact-new-name');
  if (btn && input && !btn.dataset.wired) {
    btn.dataset.wired = '1';
    const go = async () => {
      const name = input.value.trim();
      if (!name) return;
      btn.disabled = true;
      try {
        const id = newProfileId();
        await persist(upsertProfile(store, { id, displayName: name, share: 'inherit' }));
        selectContact(id);
        input.value = '';
      } finally { btn.disabled = false; }
    };
    btn.addEventListener('click', go);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
  }
}

async function renderPeopleView() {
  wireContactsNav();
  try { store = await readProfiles(); } catch (e) { /* keep cache */ }
  renderPeopleViewBody();
}

function renderPeopleViewBody() {
  renderPeopleList();
  if (activeProfileId) selectContact(activeProfileId);
  else setDetailMode('empty');
}

function initContactsUI() {
  setContactsViewHook(renderPeopleView);
}

export { initContactsUI, renderPeopleView };
