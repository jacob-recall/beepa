// PLAN-MASTER-SYNC §12 phase 5 — Contact management UI (apps/user only).
//
// A ContactProfile (shared/model/contacts.js) links several conversations
// (across sources/rooms) to one person, stored in the user's own account-data
// (com.jkali.contact_profiles). This file is the ONLY place that renders that
// data: create a profile, search-and-attach/detach conversations (reusing the
// existing convosBySource search data), view every one of a person's threads
// grouped in one card, a per-profile SHARE toggle (share/private/inherit) that
// feeds the existing 4-level consent resolver (shared/model/consent.js), and
// non-auto merge suggestions the teammate can accept or ignore.
//
// Wires into shared/ui/nav.js via its app-injection hook (setContactsViewHook),
// the same pattern consent.js/proposals.js already use, so shared/ never
// imports from apps/. textContent-only (el()/sanitizeLine), no innerHTML, no
// CSP change. Linking/unlinking here is the ONLY mutation path for profiles —
// suggestions() is advisory and never called except in response to the
// teammate pressing "Create profile" below.

import {
  readProfiles, writeProfiles,
  upsertProfile, removeProfile, linkRoom, unlinkRoom, setProfileShare, newProfileId,
  suggestions,
} from '../../shared/model/contacts.js';
import { loadConsentState } from './consent.js';
import { $, el, sanitizeLine } from '../../shared/ui/el.js';
import { setContactsViewHook } from '../../shared/ui/nav.js';
import { SOURCES } from '../../shared/ui/sources.js';
import { convosBySource, feedModel } from '../../shared/state.js';
import { openConvo } from '../../shared/ui/chat.js';
import { buildPlatBadge } from '../../shared/ui/rows.js';

// Local cache of the one consent-storage read. Writes below replace it with
// the normalized result writeProfiles() returns, so the view re-renders from
// authoritative data with no extra round trip.
let store = { profiles: [] };

// Per-viewer "don't show this merge suggestion again" set, keyed by the
// suggestion's grouping key. Convenience state only (never mutates/merges
// anything itself) — plain localStorage, tolerant of failure, same pattern as
// proposals.js's HANDLED_KEY / consent.js's SEEN_KEY.
const IGNORE_KEY = 'com.jkali.contact_suggestions_ignored';
function loadIgnored() {
  try { return new Set(JSON.parse(localStorage.getItem(IGNORE_KEY) || '[]')); }
  catch (e) { return new Set(); }
}
function saveIgnored(set) {
  try { localStorage.setItem(IGNORE_KEY, JSON.stringify([...set])); } catch (e) { /* ignore */ }
}

// All known conversations across sources, deduped by room id (same
// first-SOURCES-order-wins rule consent.js's allConvos() / seedFeed() use).
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

// Write, refresh the local cache from the normalized result, tell consent.js
// to re-read profiles (so Sharing-view badges reflect the change immediately),
// and re-render this view. The single mutation-and-refresh path every action
// below goes through.
async function persist(next) {
  store = await writeProfiles(next);
  try { await loadConsentState(); } catch (e) { /* Sharing view refreshes on its own next visit */ }
  renderContactsBody();
}

// ---- create ----

function wireCreate() {
  const btn = $('contact-new-btn');
  const input = $('contact-new-name');
  if (!btn || !input || btn.dataset.wired) return;
  btn.dataset.wired = '1';
  const go = async () => {
    const name = input.value.trim();
    if (!name) return;
    btn.disabled = true;
    try { await persist(upsertProfile(store, { id: newProfileId(), displayName: name, share: 'inherit' })); }
    finally { btn.disabled = false; }
    input.value = '';
  };
  btn.addEventListener('click', go);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
}

// ---- one profile card ----

function buildShareToggle(profile) {
  const wrap = el('span', 'share-toggle');
  const opts = [['share', 'Share'], ['inherit', 'Auto'], ['private', 'Private']];
  for (const [val, label] of opts) {
    const b = el('button', 'share-opt' + (profile.share === val ? ' active' : ''), label);
    b.type = 'button';
    b.setAttribute('aria-label', label + ' ' + sanitizeLine(profile.displayName || 'this contact'));
    b.addEventListener('click', async () => { await persist(setProfileShare(store, profile.id, val)); });
    wrap.appendChild(b);
  }
  return wrap;
}

function buildLinkedRow(convo) {
  const row = el('div', 'contact-linked-row');
  row.setAttribute('role', 'button');
  row.tabIndex = 0;
  row.appendChild(buildPlatBadge(convo.sourceId));
  const meta = el('span', 'contact-row-meta');
  meta.appendChild(el('span', 'title', sanitizeLine(convo.title || convo.id)));
  const preview = (feedModel.get(convo.id) || {}).lastBody;
  if (preview) meta.appendChild(el('span', 'sub', sanitizeLine(preview)));
  row.appendChild(meta);
  const btn = el('button', 'contact-unlink', 'Detach');
  btn.type = 'button';
  btn.addEventListener('click', async (e) => { e.stopPropagation(); await persist(unlinkRoom(store, convo.id)); });
  row.appendChild(btn);
  const open = () => openConvo(convo.id);
  row.addEventListener('click', open);
  row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  return row;
}

// Reuses convosBySource (the same data the Directory search filters) as a
// local, client-side filter — no new endpoint, no command sent.
function buildAttachSearch(profile) {
  const wrap = el('div', 'contact-attach');
  const input = el('input');
  input.placeholder = 'Search conversations to attach…';
  input.spellcheck = false;
  input.autocomplete = 'off';
  const results = el('div', 'contact-attach-results');
  function render() {
    const q = input.value.trim().toLowerCase();
    results.replaceChildren();
    if (!q) return;
    const linkedByRoom = roomToProfileId();
    const cands = allConvos().filter((c) => linkedByRoom[c.id] !== profile.id &&
      (sanitizeLine(c.title || '').toLowerCase().includes(q) ||
       sanitizeLine(c.sub || '').toLowerCase().includes(q))).slice(0, 20);
    if (!cands.length) { results.appendChild(el('p', 'muted', 'No matches.')); return; }
    for (const c of cands) {
      const row = el('div', 'contact-attach-row');
      row.appendChild(el('span', 'badge', sanitizeLine(c.sourceLabel || c.sourceId || '')));
      row.appendChild(el('span', 'title', sanitizeLine(c.title || c.id)));
      const already = linkedByRoom[c.id];
      if (already) row.appendChild(el('span', 'muted', 'in another contact'));
      const btn = el('button', 'primary', already ? 'Move here' : 'Attach');
      btn.type = 'button';
      btn.addEventListener('click', async () => {
        await persist(linkRoom(store, profile.id, c.id));
        input.value = '';
        results.replaceChildren();
      });
      row.appendChild(btn);
      results.appendChild(row);
    }
  }
  input.addEventListener('input', render);
  wrap.appendChild(input);
  wrap.appendChild(results);
  return wrap;
}

function buildProfileCard(profile) {
  const card = el('div', 'card contact-card');

  const head = el('div', 'contact-head');
  const nameInput = el('input', 'contact-name-input');
  nameInput.value = profile.displayName;
  nameInput.spellcheck = false;
  nameInput.autocomplete = 'off';
  nameInput.setAttribute('aria-label', 'Contact name');
  const saveName = async () => {
    const v = nameInput.value.trim();
    if (v === profile.displayName) return;
    await persist(upsertProfile(store, { id: profile.id, displayName: v }));
  };
  nameInput.addEventListener('blur', saveName);
  nameInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') nameInput.blur(); });
  head.appendChild(nameInput);
  head.appendChild(buildShareToggle(profile));
  const delBtn = el('button', 'danger contact-delete', 'Delete');
  delBtn.type = 'button';
  delBtn.setAttribute('aria-label', 'Delete contact ' + sanitizeLine(profile.displayName || ''));
  delBtn.addEventListener('click', async () => { await persist(removeProfile(store, profile.id)); });
  head.appendChild(delBtn);
  card.appendChild(head);

  // Conversations with this person: the LATEST is shown on top; clicking it
  // opens that conversation in the main window AND accordion-expands the others.
  const linked = el('div', 'contact-linked');
  const convos = profile.roomIds
    .map((rid) => convoById(rid) || { id: rid, title: rid, sourceLabel: '', sourceId: '' })
    .sort((a, b) => ((feedModel.get(b.id) || {}).lastTs || 0) - ((feedModel.get(a.id) || {}).lastTs || 0));
  if (!convos.length) {
    linked.appendChild(el('p', 'muted', 'No conversations attached yet.'));
  } else {
    const latest = convos[0];
    const rest = convos.slice(1);
    const primary = buildLinkedRow(latest);
    primary.classList.add('contact-primary');
    const acc = el('div', 'contact-accordion hidden');
    for (const c of rest) acc.appendChild(buildLinkedRow(c));
    if (rest.length) {
      const chev = el('span', 'contact-chev', '▾');
      primary.insertBefore(chev, primary.firstChild);
      // primary's own click (buildLinkedRow) opens the latest; this also toggles
      // the accordion of the person's other conversations.
      primary.addEventListener('click', () => { acc.classList.toggle('hidden'); chev.classList.toggle('open'); });
    }
    linked.appendChild(primary);
    linked.appendChild(acc);
  }
  card.appendChild(linked);
  card.appendChild(buildAttachSearch(profile));
  return card;
}

// ---- merge suggestions (advisory only — never auto-merges) ----

function buildSuggestionCard(group, ignored) {
  const card = el('div', 'card contact-suggestion');
  card.appendChild(el('h3', '', 'Possibly the same person'));
  const names = group.convos.map((c) => sanitizeLine(c.title || c.id)).join(', ');
  card.appendChild(el('p', 'muted', names));
  const actions = el('div', 'row contact-suggestion-actions');
  const createBtn = el('button', 'primary', 'Create contact');
  createBtn.type = 'button';
  createBtn.addEventListener('click', async () => {
    const id = newProfileId();
    let next = upsertProfile(store, { id, displayName: group.convos[0].title || group.key, share: 'inherit' });
    for (const c of group.convos) next = linkRoom(next, id, c.id);
    await persist(next);
  });
  const ignoreBtn = el('button', 'contact-ignore', 'Ignore');
  ignoreBtn.type = 'button';
  ignoreBtn.addEventListener('click', () => {
    ignored.add(group.key);
    saveIgnored(ignored);
    renderSuggestions();
  });
  actions.appendChild(createBtn);
  actions.appendChild(ignoreBtn);
  card.appendChild(actions);
  return card;
}

function renderSuggestions() {
  const host = $('contacts-suggestions');
  if (!host) return;
  host.replaceChildren();
  // suggestions() groups by handle/displayName/name; convos here only carry
  // `title`, so pass it through as `name` for grouping purposes only.
  const withName = allConvos().map((c) => Object.assign({}, c, { name: c.title }));
  const groups = suggestions(withName, store);
  const ignored = loadIgnored();
  const active = groups.filter((g) => !ignored.has(g.key));
  if (!active.length) return;
  for (const g of active) host.appendChild(buildSuggestionCard(g, ignored));
}

// ---- entry ----

function renderContactsBody() {
  wireCreate();
  const list = $('contacts-list');
  if (list) {
    list.replaceChildren();
    if (!store.profiles.length) {
      list.appendChild(el('p', 'muted', 'No contacts yet. Create one above, or accept a suggestion below.'));
    } else {
      for (const p of store.profiles) list.appendChild(buildProfileCard(p));
    }
  }
  renderSuggestions();
}

// Rendered whenever the 'contacts' nav view opens, via setContactsViewHook (nav.js).
async function renderContactsView() {
  wireCreate();
  try { store = await readProfiles(); } catch (e) { /* keep previous cache on read failure */ }
  renderContactsBody();
}

// Entry point — call once from apps/user/main.js after sign-in.
function initContactsUI() { setContactsViewHook(renderContactsView); }

export { initContactsUI, renderContactsView };
