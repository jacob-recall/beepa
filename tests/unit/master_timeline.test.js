import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { messageTimestamp, timestampCorrections, CORRECTION_TYPE } from '../../shared/model/message_timestamps.js';
import { displayName, parseNames, dumpNames, rename, sharingUsers, visibleUsers, visibleFeed } from '../../apps/master/hidden.js';
import { initials, indicatorsFor, pendingDrafts, clusterFeed } from '../../shared/model/attention.js';

// Run the actual master history, renderer, overlay and live-loop code with a
// tiny DOM and deferred transport. No browser session or real send is used.
class Element {
  constructor(tag = 'div', cls = '', text = '') {
    this.tagName = tag; this.className = cls; this.textContent = text;
    this.children = []; this.dataset = {}; this.attributes = {};
    this.classList = { add() {}, remove() {}, toggle() {} };
  }
  appendChild(child) { child.remove(); this.children.push(child); child.parent = this; return child; }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); child.parent = null; }
  remove() { if (this.parent) this.parent.removeChild(this); }
  replaceChildren() { for (const child of [...this.children]) child.remove(); }
  get firstElementChild() { return this.children[0]; }
  get lastElementChild() { return this.children.at(-1); }
  get childElementCount() { return this.children.length; }
  get parentNode() { return this.parent || null; }
  contains(node) { return this === node || this.children.some(child => child.contains(node)); }
  getAttribute(key) { return this.attributes[key] ?? null; }
  addEventListener() {}
  querySelector(selector) {
    const classes = selector.split('.').filter(Boolean);
    for (const child of this.children) {
      if (classes.every(c => child.className.split(' ').includes(c))) return child;
      const found = child.querySelector(selector); if (found) return found;
    }
    return null;
  }
}
const source = fs.readFileSync(new URL('../../apps/master/main.js', import.meta.url), 'utf8')
  .replace(/^import[\s\S]*?;\n/gm, '').replace(/^export .*;\n/gm, '');
const requests = [];
const elements = new Map([
  'room-messages', 'room-title', 'room-owner', 'room-badge', 'room-source-label', 'room-status',
  'proposal-stack', 'room-read', 'nav-teammates-rail',
].map(id => [id, new Element()]));
const S = { token: 'session-1' };
const ctx = vm.createContext({ S, Set, Map, Date, JSON, encodeURIComponent, setTimeout,
  displayName, parseNames, dumpNames, rename, sharingUsers, visibleUsers, visibleFeed,
  messageTimestamp, timestampCorrections, CORRECTION_TYPE,
  initials, indicatorsFor, pendingDrafts, clusterFeed,
  $: id => elements.get(id) || null, el: (tag, cls, text) => new Element(tag, cls, text),
  sanitize: s => s, sanitizeLine: s => s,
  appendLinkified: (node, text) => { node.textContent = text; },
  ROOMID_RE: /^!/, MXC_RE: /^mxc:/, PLATFORM_ICON: {}, PLATFORM_LABEL: {}, localpart: () => 'owner',
  masterTransport: () => ({}), configureMatrixBase() {}, setOnUnauthorized() {},
  api: (method, path) => new Promise((resolve, reject) => requests.push({ method, path, resolve, reject })),
});
vm.runInContext(source, ctx);
vm.runInContext(`
  buildPlatBadge = () => el('span', 'badge', 'iMessage');
  platformLabel = () => 'iMessage';
  showWorkspace = () => {}; setDetailMode = () => {};
  MS.rooms['!A:master'] = { name: 'A', userLabel: 'owner', sourceId: 'imessage' };
  MS.rooms['!B:master'] = { name: 'B', userLabel: 'owner', sourceId: 'imessage' };
  globalThis.ms = MS;
`, ctx);
const tick = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };
const history = () => requests.filter(r => r.path.includes('/messages?')).at(-1);
const polls = () => requests.filter(r => r.path.includes('/sync?'));
const msg = (id, body = 'hello', extra = {}) => ({ event_id: id, type: 'm.room.message',
  origin_server_ts: 1000, content: { msgtype: 'm.text', body, 'com.jkali.from_me': true, ...extra } });
const sync = (events, since = 'cursor') => ({ next_batch: since,
  rooms: { join: { '!A:master': { timeline: { events } } } } });
const box = elements.get('room-messages');
const ids = () => box.children.filter(c => c.dataset.eventId).map(c => c.dataset.eventId);

// Same-room reopen must invalidate the earlier history request, not just compare IDs.
const obsolete = ctx.openRoom('!A:master'); const oldHistory = history();
const selected = ctx.openRoom('!A:master');
oldHistory.resolve({ chunk: [msg('$obsolete')] }); await obsolete;
assert.deepEqual(ids(), []); assert.equal(polls().length, 0);
history().resolve({ chunk: [msg('$one')] }); await selected;
assert.equal(elements.get('room-status').textContent, '', 'history must render without exceptions');
assert.deepEqual(ids(), ['$one']); assert.equal(polls().length, 1);

// Initial /sync replays the /messages window. Distinct real messages with the
// same body must survive; only the repeated Matrix event ID is ignored.
polls().at(-1).resolve(sync([msg('$one'), msg('$two')])); await tick();
assert.deepEqual(ids(), ['$one', '$two']);
assert.equal(polls().length, 2);
const oldPoll = polls().at(-1);

// The old live loop cannot overwrite a new loop's cursor or append its events.
const reopened = ctx.openRoom('!A:master');
history().resolve({ chunk: [msg('$fresh')] }); await reopened;
const currentPoll = polls().at(-1);
oldPoll.resolve(sync([msg('$stale')], 'wrong-cursor')); await tick();
assert.deepEqual(ids(), ['$fresh']); assert.equal(ctx.ms.tailSince, null);
assert.equal(ctx.ms.tailRunning, true); assert.equal(polls().at(-1), currentPoll);

// A mirrored outgoing event acknowledges its EXACT proposal, not another
// proposal containing identical text; the stack shows pending vs sent.
vm.runInContext(`
  MS.proposalsByUser.set('owner', '!proposals:master');
  MS.proposalsRoomSet.add('!proposals:master');
  MS.rooms['!A:master'].mirrorOf = '!local:source';
  MS.proposalsByRoom.set('!local:source', [
    { eventId: '$different-proposal', body: 'hello', ts: 900 },
    { eventId: '$proposal', body: 'hello', ts: 800 },
  ]);
`, ctx);
const stackOpen = ctx.openRoom('!A:master');
// ts=850 sits between the two proposals (800/900): it acknowledges $proposal
// without being newer than $different-proposal, so the latter is still
// 'pending' here — only the LATER message below (ts=5000) retires it.
history().resolve({ chunk: [{ ...msg('$ack', 'hello', { 'com.jkali.auto_sent_from_proposal': '$proposal' }), origin_server_ts: 850 }] }); await stackOpen;
const stack = { get children() { const g = box.querySelector('.ghosts'); return g ? g.children.filter(c => c.className.startsWith('ghost ')) : []; } };
const cardStates = () => stack.children.map(c => c.className.replace('ghost ', ''));
assert.deepEqual(cardStates(), ['auto', 'pending'], 'exact id acknowledged; same text alone is not acknowledgement');
// A later mirrored message retires the still-pending suggestion, never marks it sent.
ctx.renderBubble({ ...msg('$later', 'anything'), origin_server_ts: 5000 });
ctx.renderSuggestionStack();
assert.deepEqual(cardStates(), ['auto', 'retired']);
ctx.stopTail(); polls().at(-1).resolve({ next_batch: 'done', rooms: {} }); await tick();

// The stack renders ONLY the open room's target: switching rooms clears it.
const otherOpen = ctx.openRoom('!B:master'); history().resolve({ chunk: [] }); await otherOpen;
assert.equal(stack.children.length, 0);
assert.equal(ctx.ms.openRoomId, '!B:master');
ctx.stopTail(); polls().at(-1).resolve({ next_batch: 'done', rooms: {} }); await tick();

// The actual incident shape: one proposal send, then native text and URL
// components. Keep all three records, but show one main timeline entry with
// an expandable disclosure. Never collapse arbitrary equal message bodies.
const original = msg('$invitation', 'Invitation\nhttps://example.com/event', {
  'com.jkali.source': 'imessage', 'com.jkali.auto_sent_from_proposal': '$invite-proposal',
});
const copy = (id, body, ts = 2000) => ({ ...msg(id, body, {
  'com.jkali.source': 'imessage', 'com.jkali.origin_sender': 'iMessage bridge bot',
}), origin_server_ts: ts });
const textCopy = copy('$text', 'Invitation');
const linkCopy = copy('$link', 'https://example.com/event', 2001);
assert.equal(ctx.nativeEchoGroups([original, textCopy]).length, 0, 'partial match stays visible');
assert.equal(ctx.nativeEchoGroups([original, textCopy, linkCopy]).length, 1);
assert.equal(ctx.nativeEchoGroups([textCopy, copy('$repeat', 'Invitation')]).length, 0);
assert.equal(ctx.nativeEchoGroups([original, { ...textCopy, content: { ...textCopy.content,
  'com.jkali.from_me': false } }, linkCopy]).length, 0, 'received text is not an echo');
assert.equal(ctx.nativeEchoGroups([original, textCopy, { ...linkCopy, origin_server_ts: 90000 }]).length, 0);
assert.equal(ctx.nativeEchoGroups([original, textCopy, copy('$interleaved', 'different'), linkCopy]).length, 0);
const grouped = ctx.openRoom('!B:master');
history().resolve({ chunk: [linkCopy, textCopy, original] }); await grouped;
assert.deepEqual(ids(), ['$invitation']);
const disclosure = box.querySelector('.message-copies');
assert.ok(disclosure);
assert.deepEqual(disclosure.children.filter(c => c.dataset.eventId).map(c => c.dataset.eventId), ['$text', '$link']);
ctx.renderBubble(textCopy); ctx.renderBubble(linkCopy); ctx.reconcileNativeEchoes();
assert.equal(disclosure.children.length, 3, 'sync replay cannot duplicate grouped copies');
ctx.stopTail(); polls().at(-1).resolve({ next_batch: 'done', rooms: {} }); await tick();
// Metadata-only history repair changes dates/order, never adds a message.
vm.runInContext(`MS.rooms['!A:master'].createSender = '@owner:master'; MS.proposalsByUser.clear();`, ctx);
const repairOpen = ctx.openRoom('!A:master');
history().resolve({ chunk: [msg('$imported', 'old', { 'com.jkali.origin_ts': 5000 }),
  msg('$recent', 'new', { 'com.jkali.origin_ts': 2000 })] }); await repairOpen;
assert.deepEqual(ids(), ['$recent', '$imported']);
const repairEvent = { event_id: '$correction', type: CORRECTION_TYPE, state_key: '$imported',
  sender: '@owner:master', content: { version: 1, source: 'imessage', origin_ts: 1000 } };
polls().at(-1).resolve(sync([repairEvent])); await tick();
assert.deepEqual(ids(), ['$imported', '$recent']);
assert.equal(ctx.mirrorTs(msg('$imported')), 1000);
polls().at(-1).resolve(sync([repairEvent])); await tick();
assert.deepEqual(ids(), ['$imported', '$recent'], 'repair replay must not duplicate messages');
ctx.stopTail(); polls().at(-1).resolve({ next_batch: 'done', rooms: {} }); await tick();
console.log('master_timeline: history, sync dedup, timestamp repair, stale reads, sessions and proposal acknowledgements pass');
