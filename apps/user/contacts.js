// Contact profiles — list (left) + detail (right).

import {
  readProfiles, writeProfiles,
  upsertProfile, removeProfile, linkRoom, unlinkRoom, setProfileShare, newProfileId,
  linkHandle, unlinkHandle,
  suggestions,
} from '../../shared/model/contacts.js';
import { loadConsentState } from './consent.js';
import { $, el, sanitizeLine } from '../../shared/ui/el.js';
import { setContactsViewHook, setDetailMode } from '../../shared/ui/nav.js';
import { appendDirectoryRows } from '../../shared/ui/search.js';
import { SOURCES, validHandle } from '../../shared/ui/sources.js';
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

// F13 copy fix (direct-share-level plan): since D1, a profile's share-state
// has NO effect on whether its linked conversations mirror to the manager —
// that is decided per-conversation only (see consent.js). This label is
// purely a tag on the contact profile itself; it is not even consulted by
// resolveContactShare() (the address-book/contact-info sharing dimension,
// which is a separate global/per-source policy — see shared/model/consent.js).
// The control is kept (its storage shape is retained for compatibility) but
// the copy states plainly that it does not control what your manager can see.
function buildShareToggle(profile) {
  const wrap = el('div', 'contact-share-row');
  wrap.appendChild(el('span', 'muted', 'Label (does not affect what your manager can see)'));
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

  // Address-book handles — the cross-platform identity anchor (source +
  // network_id). A handle belongs to at most one profile; linkHandle enforces
  // that. Manual entry only (the user types the phone/email) — never fetched
  // from the imported-contacts DB. Persists through the SAME persist() path the
  // rest of this view uses, so the People view stays consistent.
  const handles = el('div', 'contact-handles');
  handles.appendChild(el('div', 'list-section', 'Handles'));
  const existing = Array.isArray(profile.handleIds) ? profile.handleIds : [];
  if (!existing.length) {
    handles.appendChild(el('p', 'muted', 'No handles linked.'));
  } else {
    for (const h of existing) {
      const row = el('div', 'contact-linked-row');
      const src = SOURCES.find((s) => s.id === h.source);
      const meta = el('span', 'contact-row-meta');
      meta.appendChild(el('span', 'title',
        (src ? src.label : sanitizeLine(h.source)) + ' · ' + sanitizeLine(h.network_id)));
      row.appendChild(meta);
      const unlink = el('button', 'contact-unlink', 'Unlink');
      unlink.type = 'button';
      unlink.addEventListener('click', async (e) => {
        e.stopPropagation();
        await persist(unlinkHandle(store.profiles, h.source, h.network_id));
      });
      row.appendChild(unlink);
      handles.appendChild(row);
    }
  }

  const addHandle = el('div', 'contact-attach');
  const picker = document.createElement('select');
  for (const s of SOURCES) {
    if (s.kind === 'all') continue;
    const opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = s.label;
    picker.appendChild(opt);
  }
  const handleInput = el('input');
  handleInput.placeholder = 'Phone (+15551234567) or email';
  handleInput.spellcheck = false;
  handleInput.autocomplete = 'off';
  const addBtn = el('button', null, 'Link handle');
  addBtn.type = 'button';
  const handleWarn = el('p', 'error hidden');
  const addHandleNow = async () => {
    handleWarn.classList.add('hidden');
    handleWarn.textContent = '';
    const source = picker.value;
    const identifier = handleInput.value.trim();
    if (!validHandle(identifier)) {
      handleWarn.textContent = 'Enter a valid phone (+15551234567) or email address.';
      handleWarn.classList.remove('hidden');
      return;
    }
    // persist() re-renders this detail view, which rebuilds the form fresh.
    await persist(linkHandle(store.profiles, profile.id, source, identifier));
  };
  addBtn.addEventListener('click', addHandleNow);
  handleInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); addHandleNow(); }
  });
  addHandle.appendChild(picker);
  addHandle.appendChild(handleInput);
  addHandle.appendChild(addBtn);
  addHandle.appendChild(handleWarn);
  handles.appendChild(addHandle);
  host.appendChild(handles);

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

// Reuse the shared modal shell instead of hand-styling a second backdrop:
// #modal-backdrop already carries the fixed/inset/flex/z-index positioning in
// CSS, and `.card narrow` the card chrome. We add our own card as a sibling of
// the default confirm-modal card (#modal) inside that same backdrop, and hide
// #modal while ours is open so the two never render at once (confirmModal and
// this picker are never open simultaneously). Only minimal inline sizing here.
function buildAddContactModal() {
  const backdrop = $('modal-backdrop');
  if (!backdrop) return null;
  const card = el('div', 'card narrow');
  card.style.cssText = 'margin:auto;width:380px;max-height:80vh;overflow-y:auto;';
  card.classList.add('hidden');
  backdrop.appendChild(card);
  return { backdrop, card };
}

function closeAddToContact() {
  if (!addContactBackdrop) return;
  addContactBackdrop.card.classList.add('hidden');
  const dflt = $('modal');
  if (dflt) dflt.classList.remove('hidden');       // restore the confirm-modal card
  addContactBackdrop.backdrop.classList.add('hidden');
}

async function openAddToContact(roomId) {
  if (typeof roomId !== 'string' || !roomId) return;
  if (!addContactBackdrop) addContactBackdrop = buildAddContactModal();
  if (!addContactBackdrop) return;
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
  // view re-reads the store the next time it is opened. We DO refresh consent.js's
  // module-level profileMap via loadConsentState() — persist() is otherwise the
  // only path that does, so without it a room linked/created here would not be
  // reflected in row Share/Private badges until the next natural refresh — but
  // we deliberately do NOT re-render the People view from here.
  const saveFromModal = async (next) => {
    store = await writeProfiles(next);
    try { await loadConsentState(); } catch (e) { /* consent-cache refresh is best-effort */ }
  };
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

  const dflt = $('modal');
  if (dflt) dflt.classList.add('hidden');          // hide the confirm-modal card while ours shows
  card.classList.remove('hidden');
  backdrop.classList.remove('hidden');
  searchInput.focus();
}

function initContactsUI() {
  setContactsViewHook(renderPeopleView);
}

export { initContactsUI, renderPeopleView, openAddToContact };
