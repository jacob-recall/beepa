// Teammate proposal inbox — thread list (left) + detail (right).

import { ROOMID_RE, api } from '../../shared/matrix/client.js';
import { $, el, sanitizeLine } from '../../shared/ui/el.js';
import { openConvo, sendConvoMessage } from '../../shared/ui/chat.js';
import { setProposalsViewHook, setDetailMode } from '../../shared/ui/nav.js';
import { feedRelTime } from '../../shared/ui/search.js';
import { S, feedModel } from '../../shared/state.js';

const HANDLED_KEY = 'com.jkali.proposals_handled';
let activeProposal = null;
let cachedProposals = [];

function loadHandled() {
  try { return new Set(JSON.parse(localStorage.getItem(HANDLED_KEY) || '[]')); }
  catch (e) { return new Set(); }
}
function saveHandled(set) {
  try { localStorage.setItem(HANDLED_KEY, JSON.stringify([...set])); } catch (e) { /* ignore */ }
}
function markHandled(p) { const s = loadHandled(); s.add(p.eventId); saveHandled(s); }

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
    if (!ROOMID_RE.test(rid)) continue;
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

function targetName(p) {
  const rec = feedModel.get(p.targetRoom);
  return sanitizeLine((rec && rec.name) || p.targetRoom);
}

function setDetailError(err, msg) {
  if (!err) return;
  err.textContent = msg || '';
  err.classList.toggle('hidden', !msg);
}

function buildThreadRow(p) {
  const rec = feedModel.get(p.targetRoom);
  const name = targetName(p);
  const preview = sanitizeLine(p.body).replace(/\s+/g, ' ').slice(0, 80);
  const row = el('div', 'convo thread-row');
  row.dataset.proposalId = p.eventId;
  row.setAttribute('role', 'button');
  row.tabIndex = 0;
  row.appendChild(el('div', 'avatar', 'P'));
  const meta = el('div', 'meta');
  meta.appendChild(el('div', 'title', name));
  meta.appendChild(el('div', 'preview', preview));
  row.appendChild(meta);
  if (p.ts) row.appendChild(el('span', 'when', feedRelTime(p.ts)));
  row.appendChild(el('span', 'thread-badge', 'Draft'));
  const open = () => selectProposal(p);
  row.addEventListener('click', open);
  row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  return row;
}

function markActiveThread(eventId) {
  for (const row of document.querySelectorAll('.thread-row')) {
    row.classList.toggle('active', row.dataset.proposalId === eventId);
  }
}

async function sendProposal(p, body, err, btn) {
  setDetailError(err, '');
  const text = (body || '').trim();
  if (!text) { setDetailError(err, 'Type a message before sending.'); return; }
  if (btn) btn.disabled = true;
  await openConvo(p.targetRoom);
  const ok = await sendConvoMessage(p.targetRoom, text);
  if (btn) btn.disabled = false;
  if (ok) { markHandled(p); renderProposalsView(); return; }
  setDetailError(err, 'Could not send — conversation unavailable.');
}

async function prefillProposal(p, body, err) {
  setDetailError(err, '');
  await openConvo(p.targetRoom);
  if (S.openRoomId !== p.targetRoom) {
    setDetailError(err, 'Conversation not available.');
    return;
  }
  const input = $('convo-input');
  if (input) { input.value = body || ''; input.focus(); }
}

function renderProposalDetail(p) {
  const host = $('proposal-detail-body');
  if (!host) return;
  host.replaceChildren();

  const note = el('p', 'muted proposal-detail-note',
    p.template ? 'Template suggestion — review before sending.' : 'Suggested by your manager — not sent yet.');
  host.appendChild(note);

  const to = el('div', 'proposal-to');
  to.appendChild(el('span', 'proposal-to-label', 'To'));
  to.appendChild(el('span', 'proposal-to-name', targetName(p)));
  host.appendChild(to);

  const ta = el('textarea', 'proposal-body');
  ta.value = p.body;
  ta.rows = 6;
  host.appendChild(ta);

  const err = el('div', 'proposal-card-error hidden');
  host.appendChild(err);

  const actions = el('div', 'proposal-actions');
  const sendBtn = el('button', 'proposal-btn proposal-send primary', 'Send');
  sendBtn.type = 'button';
  sendBtn.addEventListener('click', () => sendProposal(p, ta.value, err, sendBtn));
  const editBtn = el('button', 'proposal-btn', 'Open in chat');
  editBtn.type = 'button';
  editBtn.addEventListener('click', () => prefillProposal(p, ta.value, err));
  const dropBtn = el('button', 'proposal-btn proposal-dismiss', 'Dismiss');
  dropBtn.type = 'button';
  dropBtn.addEventListener('click', () => { markHandled(p); renderProposalsView(); });
  actions.appendChild(sendBtn);
  actions.appendChild(editBtn);
  actions.appendChild(dropBtn);
  host.appendChild(actions);

  $('proposal-detail-title').textContent = targetName(p);
}

function selectProposal(p) {
  activeProposal = p;
  markActiveThread(p.eventId);
  setDetailMode('proposal');
  renderProposalDetail(p);
}

function wireProposalNav() {
  const back = $('proposal-back');
  if (back && !back.dataset.wired) {
    back.dataset.wired = '1';
    back.addEventListener('click', () => {
      activeProposal = null;
      markActiveThread(null);
      setDetailMode('empty');
    });
  }
}

async function renderProposalsView() {
  wireProposalNav();
  const list = $('list-body');
  if (!list) return;
  list.replaceChildren();
  list.appendChild(el('p', 'muted', 'Loading…'));

  let proposals;
  try { proposals = await fetchProposals(); }
  catch (e) {
    list.replaceChildren();
    list.appendChild(el('p', 'error', 'Could not load: ' + String(e.message || e)));
    return;
  }

  const handled = loadHandled();
  cachedProposals = proposals.filter(p => !handled.has(p.eventId));
  cachedProposals.sort((a, b) => b.ts - a.ts);

  list.replaceChildren();
  if (!cachedProposals.length) {
    list.appendChild(el('p', 'list-empty', 'No suggestions right now.'));
    setDetailMode('empty');
    return;
  }
  for (const p of cachedProposals) list.appendChild(buildThreadRow(p));
  if (activeProposal && cachedProposals.some(p => p.eventId === activeProposal.eventId)) {
    selectProposal(activeProposal);
  } else {
    activeProposal = null;
    setDetailMode('empty');
  }
}

function initProposalsUI() { setProposalsViewHook(renderProposalsView); }

export { initProposalsUI, renderProposalsView };
