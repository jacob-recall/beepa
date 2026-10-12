import assert from 'node:assert/strict';
import { parseMessageReceipts, mergeReceipts, receiptPresentation, paintReceipt } from '../../shared/model/message_receipts.js';
import { parseSnapshot } from '../../apps/master/main.js';
const self = new Set(['@me:local']), bots = new Set(['@whatsappbot:local', '@imessagebot:local']);
const bot = '@whatsappbot:local';
const status = (id, content = {}, sender = bot) => ({ type: 'com.beeper.message_send_status', sender,
  content: { 'm.relates_to': { rel_type: 'm.reference', event_id: id }, status: 'SUCCESS', ...content } });
const parse = events => parseMessageReceipts({ timeline: { events } }, self, bots, bot);
assert.equal(receiptPresentation({}).glyph, '✓');
assert.equal(receiptPresentation(parse([status('$a')]).get('$a')).state, 'sending', 'bridge acceptance is not recipient delivery');
assert.equal(receiptPresentation(parse([status('$a', { delivered_to_users: [] })]).get('$a')).state, 'sending');
assert.equal(parse([status('$a', { delivered_to_users: ['@peer:local'] }, '@evil:local')]).size, 0, 'forged status ignored');
assert.equal(parse([status('$a', { delivered_to_users: ['@peer:local'] }, '@imessagebot:local')]).size, 0, 'wrong bridge ignored');
let receipts = parse([status('$a', { delivered_to_users: ['@peer:local'] })]);
assert.equal(receiptPresentation(receipts.get('$a')).state, 'delivered');
const read = { ephemeral: { events: [{ type: 'm.receipt', content: {
  '$a': { 'm.read': { '@peer:local': { ts: 9999999999999 } } },
  '$b': { 'm.read': { [bot]: { ts: 9999999999999 }, '@me:local': { ts: 9999999999999 } } },
  '$thread': { 'm.read': { '@peer:local': { ts: 1, thread_id: '$thread-root' } } },
} }] } };
receipts = mergeReceipts(receipts, parseMessageReceipts(read, self, bots, bot));
assert.equal(receiptPresentation(receipts.get('$a')).state, 'read');
assert.equal(receiptPresentation(receipts.get('$b')).state, 'sending', 'late A receipt and own/bot receipts never mark B read');
assert.equal(receipts.has('$thread'), false);
receipts = mergeReceipts(receipts, parse([status('$a', { status: 'FAIL_PERMANENT' })]));
assert.equal(receiptPresentation(receipts.get('$a')).state, 'read', 'old failure cannot erase read evidence');
assert.equal(receiptPresentation(parse([status('$failed', { status: 'FAIL_PERMANENT' })]).get('$failed')).glyph, '!');
const node = { setAttribute(k,v) { this[k] = v; } };
paintReceipt(node, receipts.get('$a'));
assert.equal(node.className, 'message-receipt read');
assert.equal(node.textContent, '✓✓');
assert.match(node['aria-label'], /Read/);
const create = { type: 'm.room.create', state_key: '', sender: '@me:master', content: {} };
const stamp = { type: 'com.jkali.message_receipt', state_key: '$a', sender: '@me:master', content: { read: true } };
let rooms = parseSnapshot({ rooms: { join: { '!r:master': { state: { events: [stamp, create] } } } } }, {});
assert.equal(rooms['!r:master'].receipts.get('$a').read, true, 'state order independent');
rooms = parseSnapshot({ rooms: { join: { '!r:master': { timeline: { events: [{...stamp, state_key: '$b', sender: '@evil:master'}] } } } } }, rooms);
assert.equal(rooms['!r:master'].receipts.has('$b'), false, 'master rejects non-owner stamp');
console.log('message_receipts: sending, delivery, read, late target, bot/self/thread exclusions, forged status, accessibility, master merge pass');
