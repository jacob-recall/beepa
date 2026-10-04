// Plain-node test for apps/master/main.js parseSnapshot(data, base): the
// console now syncs INCREMENTALLY (a full initial sync over ~1000 mirror rooms
// costs the homeserver ~20s), so a delta must merge onto the previous rooms
// map — latest state wins, space children link/unlink, contacts keyed by
// state_key with tombstones, corrections accumulate, left rooms drop.
// Run: node tests/unit/master_snapshot_merge.test.js
import assert from 'node:assert/strict';
import { parseSnapshot, healthText, hopLagMs } from '../../apps/master/main.js';

const create = { type: 'm.room.create', state_key: '', sender: '@david:master', content: { 'com.jkali.mirror_of': '!local:localhost' } };
const full = { rooms: { join: {
  '!a:master': { state: { events: [
    create,
    { type: 'm.room.name', state_key: '', content: { name: 'Maya' } },
    { type: 'com.jkali.source', state_key: '', content: { source: 'whatsapp' } },
    { type: 'com.jkali.read_state', state_key: '', content: { teammate_read_ts: 10, remote_read_ts: 5 } },
  ] }, timeline: { events: [
    { type: 'm.room.message', event_id: '$1', origin_server_ts: 100, content: { msgtype: 'm.text', body: 'first' } },
  ] } },
  '!space:master': { state: { events: [
    { type: 'm.room.create', state_key: '', sender: '@david:master', content: { type: 'm.space' } },
    { type: 'm.space.child', state_key: '!a:master', content: { via: ['master'] } },
    { type: 'm.space.child', state_key: '!b:master', content: { via: ['master'] } },
  ] } },
  '!contacts:master': { state: { events: [
    { type: 'm.room.create', state_key: '', sender: '@david:master', content: {} },
    { type: 'com.jkali.contacts', state_key: '', content: {} },
    { type: 'com.jkali.contact', state_key: 'h1', content: { source: 'whatsapp', network_id: '+1', display_name: 'A' } },
    { type: 'com.jkali.contact', state_key: 'h2', content: { source: 'imessage', network_id: '+2', display_name: 'B' } },
  ] } },
} } };

const base = parseSnapshot(full, {});
assert.equal(base['!a:master'].name, 'Maya');
assert.equal(base['!a:master'].lastBody, 'first');
assert.equal(base['!a:master'].lastTs, 100);
assert.equal(base['!a:master'].mirrorOf, '!local:localhost');
assert.deepEqual(base['!a:master'].readState, { teammate_read_ts: 10, remote_read_ts: 5 });
assert.deepEqual(base['!space:master'].children.sort(), ['!a:master', '!b:master']);
assert.equal(base['!contacts:master'].contacts.length, 2);

// Delta: rename, newer message, read state advance, one child unlinked, one
// contact tombstoned, a brand-new room, and a left room.
const delta = { rooms: { join: {
  '!a:master': { state: { events: [] }, timeline: { events: [
    { type: 'm.room.name', state_key: '', content: { name: 'Maya Rodriguez' } },
    { type: 'com.jkali.read_state', state_key: '', content: { teammate_read_ts: 300, remote_read_ts: 5 } },
    { type: 'm.room.message', event_id: '$2', origin_server_ts: 200, content: { msgtype: 'm.text', body: 'second' } },
  ] } },
  '!space:master': { timeline: { events: [
    { type: 'm.space.child', state_key: '!b:master', content: {} },
  ] } },
  '!contacts:master': { timeline: { events: [
    { type: 'com.jkali.contact', state_key: 'h2', content: { source: 'imessage', network_id: '+2', deleted: true } },
  ] } },
  '!new:master': { state: { events: [create, { type: 'com.jkali.source', state_key: '', content: { source: 'instagram' } }] },
    timeline: { events: [{ type: 'm.room.message', event_id: '$n', origin_server_ts: 50, content: { msgtype: 'm.text', body: 'hey' } }] } },
}, leave: { '!gone:master': {} } } };

const merged = parseSnapshot(delta, Object.assign({ '!gone:master': { id: '!gone:master' } }, base));
assert.equal(merged['!a:master'].name, 'Maya Rodriguez', 'rename applied');
assert.equal(merged['!a:master'].lastBody, 'second');
assert.equal(merged['!a:master'].lastTs, 200);
assert.equal(merged['!a:master'].sourceId, 'whatsapp', 'untouched state survives the delta');
assert.equal(merged['!a:master'].mirrorOf, '!local:localhost');
assert.equal(merged['!a:master'].readState.teammate_read_ts, 300);
assert.deepEqual(merged['!space:master'].children, ['!a:master'], 'empty m.space.child content unlinks');
assert.equal(merged['!contacts:master'].contacts.length, 2, 'tombstone replaces, never duplicates');
assert.equal(merged['!contacts:master'].contacts.find(c => c.network_id === '+2').deleted, true);
assert.equal(merged['!new:master'].sourceId, 'instagram', 'new room built from its delta state');
assert.equal(merged['!new:master'].createSender, '@david:master');
assert.equal('!gone:master' in merged, false, 'left room dropped');

// An older message in a delta (backfill) never regresses lastTs.
const older = parseSnapshot({ rooms: { join: { '!a:master': { timeline: { events: [
  { type: 'm.room.message', event_id: '$0', origin_server_ts: 10, content: { msgtype: 'm.text', body: 'old' } }] } } } } }, merged);
assert.equal(older['!a:master'].lastBody, 'second');

// F0: uplink health rides on the space as state; mirror lag comes from the hop stamp.
const h = parseSnapshot({ rooms: { join: { '!space:master': { timeline: { events: [
  { type: 'com.jkali.uplink_health', state_key: '', content: { pending_events: 3, delivery_refused: 1, updated_at: 1000, connected: true, stage_errors: ['delivery'], room_id: 'never kept' } },
] } } } } }, base);
assert.deepEqual(h['!space:master'].uplinkHealth.stage_errors, ['delivery']);
assert.equal(h['!space:master'].uplinkHealth.room_id, undefined, 'only whitelisted fields survive');
assert.equal(healthText(h['!space:master'].uplinkHealth, 1030 * 1000), 'synced 30s ago · 3 queued · 1 refused · retrying delivery');
assert.equal(healthText(h['!space:master'].uplinkHealth, 2000 * 1000), 'sync STALE (17m ago) · 3 queued · 1 refused · retrying delivery');
assert.equal(healthText(null, 0), 'no sync report yet');
const lagged = parseSnapshot({ rooms: { join: { '!a:master': { timeline: { events: [
  { type: 'm.room.message', event_id: '$h', origin_server_ts: 10350, content: { msgtype: 'm.text', body: 'x', 'com.jkali.hops': { local_ts: 10000, uplink_ts: 10100 } } },
] } } } } }, h);
assert.equal(lagged['!a:master'].lastLagMs, 350);
assert.equal(hopLagMs({ origin_server_ts: 5, content: { 'com.jkali.hops': { local_ts: 10 } } }), null, 'negative lag is implausible');
console.log('master_snapshot_merge.test.js: ok');
