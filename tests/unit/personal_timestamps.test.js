import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { messageTimestamp, timestampCorrections, CORRECTION_TYPE } from '../../shared/model/message_timestamps.js';

class Element {
  constructor(tag, cls = '', text = '') { this.className = cls; this.textContent = text; this.children = []; this.dataset = {}; }
  appendChild(child) { this.children.push(child); return child; }
  insertBefore(child, next) { this.children.splice(this.children.indexOf(next), 0, child); return child; }
  get childElementCount() { return this.children.length; }
}
const room = '!personal:local';
const correction = { type: CORRECTION_TYPE, state_key: '$old', sender: '@imessagebot:local',
  content: { version: 1, source: 'imessage', origin_ts: 1704110400000 } };
const box = new Element('div');
const S = { userId: '@owner:local', openRoomId: room, selfMxids: new Set(),
  timestampCorrections: new Map([[room, timestampCorrections([correction], '@imessagebot:local')]]) };
const ctx = vm.createContext({ S, messageTimestamp, IMSG_BOT_MXID: '@imessagebot:local',
  convoSeen: new Set(), convoNames: new Map(), convoNamePending: new Set(),
  $: () => box, el: (tag, cls, text) => new Element(tag, cls, text),
  sanitize: text => text, sanitizeLine: text => text,
  appendLinkified: (node, text) => { node.textContent = text; } });
const code = fs.readFileSync(new URL('../../shared/ui/render.js', import.meta.url), 'utf8')
  .replace(/^import .*;\n/gm, '').replace(/^export .*;\n/gm, '');
vm.runInContext(code, ctx);
const message = (id, ts) => ({ event_id: id, sender: S.userId, type: 'm.room.message', origin_server_ts: ts,
  content: { msgtype: 'm.text', body: id } });
ctx.renderMessageEvent(message('$new', 1704110460000));
ctx.renderMessageEvent(message('$old', 1788908400000));
ctx.renderMessageEvent(correction);
ctx.renderMessageEvent(message('$old', 1788908400000));
assert.deepEqual(box.children.map(node => node.dataset.eventId), ['$old', '$new']);
assert.equal(box.children[0].dataset.messageTs, '1704110400000');
assert.equal(box.children[0].children[1].textContent, '$old');
assert.equal(box.children[0].children[2].textContent, ctx.convoTime(1704110400000));
console.log('personal_timestamps: original date, chronological insertion, unchanged body and no repair bubbles pass');
