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

// Distinct platform icons for a profile: dedupe the sourceIds reached by
// mapping each roomId through convoById, rendered in SOURCES order (skipping
// the synthetic 'all' entry) via the shared buildPlatBadge — no reinvented
// icon rendering. A profile with no linked/resolvable rooms renders no icons.
function contactPlatformIds(p) {
  const seen = new Set();
  for (const rid of p.roomIds) {
    const c = convoById(rid);
    if (c && c.sourceId) seen.add(c.sourceId);
  }
  const out = [];
  for (const s of SOURCES) {
    if (s.kind === 'all') continue;
    if (seen.has(s.id)) out.push(s.id);
  }
  return out;
}

function buildContactPlatRow(p) {
  const strip = el('div', 'contact-plat-row');
  for (const sourceId of contactPlatformIds(p)) strip.appendChild(buildPlatBadge(sourceId));
  return strip;
}

// One accordion entry per roomId: platform badge + sanitized title, opens the
// conversation via the shared, validated openConvo — no second nav path.
// A stale roomId with no matching convo falls back to showing the id itself.
function buildAccordionEntry(rid) {
  const c = convoById(rid);
  const row = el('div', 'contact-accordion-row');
  row.setAttribute('role', 'button');
  row.tabIndex = 0;
  if (c && c.sourceId) row.appendChild(buildPlatBadge(c.sourceId));
  row.appendChild(el('span', 'title', sanitizeLine((c && c.title) || rid)));
  const open = () => openConvo(rid);
  row.addEventListener('click', open);
  row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  return row;
}

// Inline accordion body: every conversation linked to the profile, plus a
// "Manage" control that reaches the existing selectContact/renderContactDetail
// panel (share toggle, rename, link/unlink) — kept reachable, just moved off
// the row's own click, which now toggles this accordion instead.
function buildContactAccordion(p) {
  const acc = el('div', 'contact-accordion hidden');
  if (!p.roomIds.length) {
    acc.appendChild(el('p', 'muted', 'No conversations linked.'));
  } else {
    for (const rid of p.roomIds) acc.appendChild(buildAccordionEntry(rid));
  }
  const manage = el('button', 'contact-manage-btn', 'Manage contact');
  manage.type = 'button';
  manage.addEventListener('click', (e) => { e.stopPropagation(); selectContact(p.id); });
  acc.appendChild(manage);
  return acc;
}

// Collapse every open accordion (and its chevron) other than the one passed.
// Keeps "only one open at a time" without any hidden state beyond the DOM.
function collapseOtherAccordions(exceptAcc, exceptChevron) {
  for (const other of document.querySelectorAll('.contact-accordion')) {
    if (other !== exceptAcc) other.classList.add('hidden');
  }
  for (const chevron of document.querySelectorAll('.contact-chevron')) {
    if (chevron !== exceptChevron) chevron.classList.remove('expanded');
  }
}

function buildContactRow(p) {
  const name = sanitizeLine(p.displayName || 'Unnamed');
  const wrap = el('div', 'contact-row-wrap');
  const row = el('div', 'convo contact-row');
  row.dataset.profileId = p.id;
  row.setAttribute('role', 'button');
  row.tabIndex = 0;
  row.setAttribute('aria-expanded', 'false');
  row.appendChild(el('div', 'avatar', (name || '?').slice(0, 1).toUpperCase()));
  const meta = el('div', 'meta');
  meta.appendChild(el('div', 'title', name));
  meta.appendChild(el('div', 'preview', profilePreview(p)));
  row.appendChild(meta);
  row.appendChild(buildContactPlatRow(p));
  const chevron = el('span', 'contact-chevron', '▸');
  row.appendChild(chevron);
  wrap.appendChild(row);

  const accordion = buildContactAccordion(p);
  wrap.appendChild(accordion);

  const toggle = () => {
    const willOpen = accordion.classList.contains('hidden');
    collapseOtherAccordions(accordion, chevron);
    accordion.classList.toggle('hidden', !willOpen);
    chevron.classList.toggle('expanded', willOpen);
    row.setAttribute('aria-expanded', String(willOpen));
  };
  row.addEventListener('click', toggle);
  row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } });
  return wrap;
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

// ===========================================================================
// In-conversation "Add to contact" picker. Reuses the same model helpers and
// persist() path as the People view — never a parallel storage/link scheme.
// ===========================================================================
let addContactBackdrop = null;

function buildAddContactModal() {
  const backdrop = el('div');
  backdrop.style.cssText = 'position:fixed;inset:0;display:flex;z-index:100;';
  backdrop.classList.add('hidden');
  const card = el('div', 'card narrow');
  card.style.cssText = 'margin:auto;width:380px;max-height:80vh;overflow-y:auto;padding:18px;';
  backdrop.appendChild(card);
  document.body.appendChild(backdrop);
  return { backdrop, card };
}

function closeAddToContact() {
  if (addContactBackdrop) addContactBackdrop.backdrop.classList.add('hidden');
}

async function openAddToContact(roomId) {
  if (typeof roomId !== 'string' || !roomId) return;
  if (!addContactBackdrop) addContactBackdrop = buildAddContactModal();
  const { backdrop, card } = addContactBackdrop;

  try { store = await readProfiles(); } catch (e) { /* keep cache */ }

  const convo = convoById(roomId);
  const convoTitle = sanitizeLine((convo && convo.title) || roomId);

  card.replaceChildren();
  card.appendChild(el('h3', null, 'Add to contact'));
  card.appendChild(el('p', 'muted', convoTitle));

  const currentId = roomToProfileId()[roomId];
  if (currentId) {
    const current = store.profiles.find((p) => p.id === currentId);
    if (current) {
      card.appendChild(el('p', 'muted',
        'Currently linked to ' + (sanitizeLine(current.displayName) || 'Unnamed') + '. Picking a different contact below will move it.'));
    }
  }

  const searchInput = el('input');
  searchInput.placeholder = 'Search contacts…';
  searchInput.spellcheck = false;
  searchInput.autocomplete = 'off';
  card.appendChild(searchInput);

  const results = el('div', 'contact-attach-results');
  results.style.cssText = 'max-height:320px;overflow-y:auto;margin:6px 0;';
  card.appendChild(results);

  const feedback = el('p', 'muted');
  card.appendChild(feedback);

  // Save WITHOUT persist()'s People-view re-renders: this modal is opened from a
  // conversation, and renderPeopleList()/setDetailMode('empty') inside persist()
  // would wipe the open conversation out from under the user (that is why "New
  // contact" appeared broken). Just write + refresh the store cache; the People
  // view re-reads the store the next time it is opened.
  const saveFromModal = async (next) => { store = await writeProfiles(next); };
  const finishLink = async (profileId) => {
    try {
      await saveFromModal(linkRoom(store, profileId, roomId));
      feedback.textContent = 'Added.';
      setTimeout(closeAddToContact, 700);
    } catch (e) {
      feedback.textContent = 'Could not save — try again.';
    }
  };

  const renderResults = () => {
    const q = searchInput.value.trim().toLowerCase();
    results.replaceChildren();
    for (const p of filterProfiles(q)) {
      if (p.id === currentId) continue;
      const row = el('div', 'contact-attach-row');
      row.appendChild(el('span', 'title', sanitizeLine(p.displayName || 'Unnamed')));
      const btn = el('button', null, 'Link');
      btn.type = 'button';
      btn.addEventListener('click', () => finishLink(p.id));
      row.appendChild(btn);
      results.appendChild(row);
    }
  };
  searchInput.addEventListener('input', renderResults);
  renderResults();

  const newRow = el('div', 'row');
  const newBtn = el('button', 'primary', 'New contact from this conversation');
  newBtn.type = 'button';
  newBtn.addEventListener('click', async () => {
    try {
      const id = newProfileId();
      let next = upsertProfile(store, { id, displayName: convoTitle, share: 'inherit' });
      next = linkRoom(next, id, roomId);
      await saveFromModal(next);
      feedback.textContent = 'Created “' + convoTitle + '”.';
      setTimeout(closeAddToContact, 900);
    } catch (e) {
      feedback.textContent = 'Could not create — try again.';
    }
  });
  newRow.appendChild(newBtn);
  card.appendChild(newRow);

  const closeRow = el('div', 'row');
  const cancelBtn = el('button', null, 'Cancel');
  cancelBtn.type = 'button';
  cancelBtn.addEventListener('click', closeAddToContact);
  closeRow.appendChild(cancelBtn);
  card.appendChild(closeRow);

  backdrop.classList.remove('hidden');
  searchInput.focus();
}

function initContactsUI() {
  setContactsViewHook(renderPeopleView);
}

export { initContactsUI, renderPeopleView, openAddToContact };
