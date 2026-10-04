// Plain-node test for siblingRooms (shared/ui/chat.js): rooms cluster ONLY via
// S.roomProfile (the stored contact profiles), newest first, including self.
// Run: docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/convo_tabs.test.js
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('../../shared/ui/chat.js', import.meta.url), 'utf8')
  .replace(/^import .*;\n/gm, '').replace(/^export .*;\n/gm, '');
const feedModel = new Map([
  ['!wa:l', { id: '!wa:l', name: 'Maya (WA)', lastTs: 50, sourceId: 'whatsapp', unread: 2 }],
  ['!ig:l', { id: '!ig:l', name: 'Maya (IG)', lastTs: 10, sourceId: 'instagram', unread: 0 }],
  ['!g:l', { id: '!g:l', name: 'Group', lastTs: 40, sourceId: 'imessage', unread: 0 }],
]);
const S = { token: 't', joinedSet: new Set(feedModel.keys()), roomProfile: new Map([
  ['!wa:l', { id: 'p', displayName: 'Maya Rodriguez' }], ['!ig:l', { id: 'p', displayName: 'Maya Rodriguez' }]]) };
const ctx = vm.createContext({ S, ROOMID_RE: /^!/, convoSeen: new Set(), feedModel, runtime: {}, setTimeout,
  $: () => null, el: () => ({ appendChild() {}, classList: { add() {}, toggle() {} }, dataset: {}, setAttribute() {}, addEventListener() {} }),
  sanitizeLine: s => s, messageTimestamp: () => 0, timestampCorrections: () => new Map(), IMSG_BOT_MXID: '@b:l',
  setActiveNav() {}, showSection() {}, setDetailMode() {}, setActiveConvoRow() {}, renderMessageEvent() {},
  convoResolveContent: () => true, applyReadCaption() {}, parseReadState: () => ({ unread: 0, remoteReadTs: 0 }),
  scheduleFeedRender() {}, buildPlatBadge: () => ({ className: '', textContent: '', classList: { add() {} } }),
  SOURCES: [], feedRelTime: () => '', document: { visibilityState: 'visible' }, api: () => new Promise(() => {}) });
vm.runInContext(source, ctx);
assert.deepEqual([...ctx.siblingRooms('!ig:l').map(r => r.id)], ['!wa:l', '!ig:l'], 'newest first, includes self');
assert.deepEqual([...ctx.siblingRooms('!g:l').map(r => r.id)], ['!g:l'], 'unlinked room is its own only sibling');
assert.deepEqual([...ctx.siblingRooms('!nope:l')], []);
console.log('convo_tabs.test.js: ok');
