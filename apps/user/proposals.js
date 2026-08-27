// PLAN-MASTER-SYNC §2 (v2) / §7 — the teammate's proposal inbox (apps/user only).
//
// SECURITY MODEL (the whole point of V2): the manager can PROPOSE a message but
// MUST NEVER cause an external send. This file reads the teammate's dedicated
// local proposals room (the room the uplink created and marked
// com.jkali.proposals) and shows each com.jkali.proposal as a clearly-labelled
// DRAFT. A proposal is rendered in this SEPARATE inbox region ONLY — it is never
// passed through renderMessageEvent and never appears in #convo-messages, so it
// can never be mistaken for a real received/sent message, and the from_me
// anti-spoof gate in the renderer is untouched.
//
// The ONLY way a proposal ever leaves the device is the teammate pressing send,
// which goes through the EXISTING guarded local send path sendConvoMessage() in
// shared/ui/chat.js — the same function typing into a chat uses. That path
// re-validates the room (ROOMID_RE ∩ feedModel ∩ S.joinedSet) and REFUSES the
// six bridge management rooms. This file adds NO new send endpoint and never
// PUTs /send/… itself: an invalid or management-room target is rejected by that
// guard, not by anything here. "Prefill in chat" just fills the composer for the
// teammate to send themselves; "Dismiss" marks the proposal handled locally
// without sending.

import { ROOMID_RE, api } from '../../shared/matrix/client.js';
import { $, el, sanitizeLine } from '../../shared/ui/el.js';
import { openConvo, sendConvoMessage } from '../../shared/ui/chat.js';
import { setProposalsViewHook } from '../../shared/ui/nav.js';
import { S, feedModel } from '../../shared/state.js';

// Per-viewer "already handled" set (sent or dismissed), keyed by the local
// proposal event id. Convenience state only — never an authorization decision
// (the send guard always is) — so plain localStorage, tolerant of failure.
const HANDLED_KEY = 'com.jkali.proposals_handled';
function loadHandled() {
  try { return new Set(JSON.parse(localStorage.getItem(HANDLED_KEY) || '[]')); }
  catch (e) { return new Set(); }
}
function saveHandled(set) {
  try { localStorage.setItem(HANDLED_KEY, JSON.stringify([...set])); } catch (e) { /* ignore */ }
}
function markHandled(p) { const s = loadHandled(); s.add(p.eventId); saveHandled(s); }

// ---- read the local proposals room -----------------------------------------

// Whitelist one com.jkali.proposal timeline event into the fields the inbox
// uses. target_room is a teammate-LOCAL room id; it is NOT trusted here — the
// send guard re-validates it against the live joined set at send time.
function parseProposal(e) {
  if (!e || e.type !== 'com.jkali.proposal' || !e.content) return null;
  const c = e.content;
  const target = c.target_room;
  const body = c.body;
  if (typeof target !== 'string' || !target) return null;
  if (typeof body !== 'string' || !body.trim()) return null;
  return {
    eventId: e.event_id,
    targetRoom: target,
    body: body,
    template: c.template === true,
    ts: typeof c.origin_ts === 'number' ? c.origin_ts
        : (typeof e.origin_server_ts === 'number' ? e.origin_server_ts : 0),
  };
}

// One filtered snapshot: find the joined room(s) marked com.jkali.proposals and
// read their com.jkali.proposal events. Read-only; touches no command/console
// path and never writes.
async function fetchProposals() {
  const filter = encodeURIComponent(JSON.stringify({
    room: {
      timeline: { limit: 100, types: ['com.jkali.proposal'] },
      state: { types: ['com.jkali.proposals'], lazy_load_members: true },
      account_data: { types: [] },
    },
    presence: { types: [] }, account_data: { types: [] },
  }));
  const data = await api('GET', '/_matrix/client/v3/sync?timeout=0&filter=' + filter);
  const join = (data.rooms && data.rooms.join) || {};
  const out = [];
  for (const rid of Object.keys(join)) {
    if (!ROOMID_RE.test(rid)) continue;               // own local room-id shape
    const r = join[rid];
    const stateEvents = ((r.state && r.state.events) || [])
      .concat((r.timeline && r.timeline.events) || []);
    const isProposals = stateEvents.some(e => e.type === 'com.jkali.proposals' && e.state_key === '');
    if (!isProposals) continue;
    for (const e of ((r.timeline && r.timeline.events) || [])) {
      const p = parseProposal(e);
      if (p) out.push(p);
    }
  }
  return out;
}

// ---- actions ----------------------------------------------------------------

function setCardError(err, msg) {
  if (!err) return;
  err.textContent = msg || '';
  err.classList.toggle('hidden', !msg);
}

// Approve + send. Opens the target conversation through the SAME validated path
// the app uses (so the optimistic echo lands in the right pane and the teammate
// watches their message go out), then sends through the EXISTING guarded
// sendConvoMessage(target, body). No send happens unless that guard accepts.
async function sendProposal(p, body, err, btn) {
  setCardError(err, '');
  const text = (body || '').trim();
  if (!text) { setCardError(err, 'Type a message before sending.'); return; }
  if (btn) btn.disabled = true;
  await openConvo(p.targetRoom);                       // validated open; no-op if unavailable
  const ok = await sendConvoMessage(p.targetRoom, text);  // EXISTING guarded send path — the only send
  if (btn) btn.disabled = false;
  if (ok) { markHandled(p); renderProposalsView(); return; }
  setCardError(err, (S.openRoomId === p.targetRoom)
    ? 'The conversation refused this message. Nothing was sent.'
    : 'This conversation is not available to you, so nothing was sent.');
}

// Apply / prefill: open the target conversation and drop the draft into its
// composer for the teammate to edit and send THEMSELVES via the normal Send
// button (which is the same guarded path). Only prefills if the validated open
// actually succeeded, so text is never left staged against the wrong room.
async function prefillProposal(p, body, err) {
  setCardError(err, '');
  await openConvo(p.targetRoom);
  if (S.openRoomId !== p.targetRoom) {
    setCardError(err, 'This conversation is not available to you.');
    return;
  }
  const input = $('convo-input');
  if (input) { input.value = (body || ''); input.focus(); }
}

function dismissProposal(p) { markHandled(p); renderProposalsView(); }

// ---- rendering (SEPARATE region; never a message bubble) --------------------

function buildProposalCard(p) {
  const card = el('div', 'proposal-card');

  // Unmistakable DRAFT banner — this is a suggestion, not a message.
  const flag = el('div', 'proposal-flag');
  flag.appendChild(el('span', 'proposal-chip', 'DRAFT'));
  flag.appendChild(el('span', 'proposal-flag-text', p.template
    ? 'Template suggested by your manager. Not sent to anyone — review, edit, then send it yourself.'
    : 'Suggested by your manager. Not sent to anyone — review, edit, then send it yourself.'));
  card.appendChild(flag);

  // Which of the teammate's own conversations this suggestion is for.
  const rec = feedModel.get(p.targetRoom);
  const known = feedModel.has(p.targetRoom) && S.joinedSet.has(p.targetRoom);
  const to = el('div', 'proposal-to');
  to.appendChild(el('span', 'proposal-to-label', 'To:'));
  to.appendChild(el('span', 'proposal-to-name', sanitizeLine((rec && rec.name) || p.targetRoom)));
  if (!known) to.appendChild(el('span', 'proposal-warn', 'conversation not available'));
  card.appendChild(to);

  // Editable draft body.
  const ta = el('textarea', 'proposal-body');
  ta.value = p.body;
  ta.rows = 3;
  ta.setAttribute('aria-label', 'Edit this suggested message before sending');
  card.appendChild(ta);

  const err = el('div', 'proposal-card-error hidden');
  card.appendChild(err);

  const actions = el('div', 'proposal-actions');
  const sendBtn = el('button', 'proposal-btn proposal-send', 'Send to conversation');
  sendBtn.type = 'button';
  sendBtn.addEventListener('click', () => sendProposal(p, ta.value, err, sendBtn));
  const prefillBtn = el('button', 'proposal-btn', p.template ? 'Use in composer' : 'Edit in chat');
  prefillBtn.type = 'button';
  prefillBtn.addEventListener('click', () => prefillProposal(p, ta.value, err));
  const dropBtn = el('button', 'proposal-btn proposal-dismiss', 'Dismiss');
  dropBtn.type = 'button';
  dropBtn.addEventListener('click', () => dismissProposal(p));
  actions.appendChild(sendBtn);
  actions.appendChild(prefillBtn);
  actions.appendChild(dropBtn);
  card.appendChild(actions);
  return card;
}

async function renderProposalsView() {
  const host = $('proposals-list');
  if (!host) return;
  host.replaceChildren();
  host.appendChild(el('p', 'muted', 'Loading suggestions…'));
  let proposals;
  try {
    proposals = await fetchProposals();
  } catch (e) {
    host.replaceChildren();
    host.appendChild(el('p', 'error', 'Could not load suggestions: ' + String(e.message || e)));
    return;
  }
  const handled = loadHandled();
  const active = proposals.filter(p => !handled.has(p.eventId));
  active.sort((a, b) => b.ts - a.ts);                  // newest first
  host.replaceChildren();
  if (!active.length) {
    host.appendChild(el('p', 'muted',
      'No suggestions right now. When your manager suggests a message it appears here as a draft for you to review — nothing is ever sent automatically.'));
    return;
  }
  for (const p of active) host.appendChild(buildProposalCard(p));
}

// Entry point — call once from apps/user/main.js after sign-in.
function initProposalsUI() { setProposalsViewHook(renderProposalsView); }

export { initProposalsUI, renderProposalsView };
