import { paintReceipt, mergeReceipts, MESSAGE_RECEIPT_TYPE } from '../../shared/model/message_receipts.js';
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { messageTimestamp, timestampCorrections, CORRECTION_TYPE } from '../../shared/model/message_timestamps.js';
import { displayName, parseNames, dumpNames, rename, sharingUsers, visibleUsers, visibleFeed } from '../../apps/master/hidden.js';
import { initials, indicatorsFor, pendingDrafts, clusterFeed } from '../../shared/model/attention.js';

// Independently exercise exact CRM message context using the actual master code and a
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
  setAttribute(key, value) { (this.attributes ||= {})[key] = value; }
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
const ctx = vm.createContext({ paintReceipt, mergeReceipts, MESSAGE_RECEIPT_TYPE, S, Set, Map, Date, JSON, encodeURIComponent, setTimeout,
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
  MS.rooms['!A:master'] = { ...freshRoomInfo('!A:master'), name: 'A', userLabel: 'owner', sourceId: 'imessage' };
  MS.rooms['!B:master'] = { ...freshRoomInfo('!B:master'), name: 'B', userLabel: 'owner', sourceId: 'imessage' };
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


Element.prototype.scrollIntoView = function(options) { this.scrolled = options; };
ctx.ms.rooms['!A:master'].createSender = '@owner:master';
const contextRequest = () => requests.filter(r => r.path.includes('/context/')).at(-1);
const exact = (id, extra = {}) => ({ ...msg(id), sender: '@owner:master', room_id: '!A:master', ...extra });
const target = ctx.openRoom('!A:master', { eventId: '$target' });
assert.ok(contextRequest().path.includes('/context/%24target?limit=50'));
contextRequest().resolve({ event: exact('$target', { origin_server_ts: 2000 }),
  events_before: [exact('$earlier'), exact('$foreign', { sender: '@foreign:master' })],
  events_after: [exact('$later', { origin_server_ts: 3000 }), exact('$other-room', { room_id: '!B:master' })] });
await target;
assert.deepEqual(ids(), ['$earlier', '$target', '$later']);
assert.equal(ctx.ms.roomRows.get('$target').scrolled.block, 'center');
assert.equal(polls().length, 1, 'exact context starts the same normal tail');
const old = ctx.openRoom('!A:master', { eventId: '$old' }), oldRequest = contextRequest();
const fresh = ctx.openRoom('!A:master', { eventId: '$fresh' }), freshRequest = contextRequest();
oldRequest.resolve({ event: exact('$old') }); await old; assert.deepEqual(ids(), []);
freshRequest.resolve({ event: exact('$fresh') }); await fresh; assert.deepEqual(ids(), ['$fresh']);
for (const mutation of [{ event_id: '$other' }, { sender: '@foreign:master' }, { room_id: '!B:master' }]) {
  const attempt = ctx.openRoom('!A:master', { eventId: '$expected' });
  contextRequest().resolve({ event: exact('$expected', mutation), events_before: [exact('$neighbor')] });
  await attempt; assert.deepEqual(ids(), []); assert.match(elements.get('room-status').textContent, /referenced message is not available/);
}
const logout = ctx.openRoom('!A:master', { eventId: '$logout' }), logoutRequest = contextRequest(), beforePolls = polls().length;
S.token = 'replaced-session'; logoutRequest.resolve({ event: exact('$logout') }); await logout;
assert.deepEqual(ids(), []); assert.equal(polls().length, beforePolls);
const beforeRequests = requests.length;
await ctx.openRoom('!A:master', { eventId: 'not-event' }); assert.equal(requests.length, beforeRequests);
console.log('master_crm_context: exact context, ordinary rendering/tail, owner/room/reference guards, same-room races and session invalidation pass');
