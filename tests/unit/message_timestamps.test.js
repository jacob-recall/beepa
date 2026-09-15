import assert from 'node:assert/strict';
import { CORRECTION_TYPE, messageTimestamp, timestampCorrections } from '../../shared/model/message_timestamps.js';
import { feedPreviewFromEvent } from '../../shared/model/message_preview.js';

const native = 1704110400000, imported = 1788908400000;
const old = { event_id: '$old', type: 'm.room.message', origin_server_ts: imported,
  content: { msgtype: 'm.text', body: 'unchanged' } };
const correction = { type: CORRECTION_TYPE, state_key: '$old', sender: '@owner:test',
  content: { version: 1, source: 'imessage', origin_ts: native } };
const corrections = timestampCorrections([correction], '@owner:test');
assert.equal(messageTimestamp(old, corrections), native);
assert.equal(feedPreviewFromEvent(old, corrections).ts, native);
assert.equal(messageTimestamp(old), imported);
assert.equal(messageTimestamp({ ...old, content: { ...old.content, 'com.jkali.origin_ts': native } }), native);
assert.equal(timestampCorrections([correction], '@other:test').size, 0);
assert.equal(timestampCorrections([{ ...correction, content: { ...correction.content, origin_ts: true } }], '@owner:test').size, 0);
assert.equal(feedPreviewFromEvent(correction), null, 'repair metadata must not become a message bubble');
assert.equal(old.content['com.jkali.origin_ts'], undefined, 'display overlay must not mutate the event');
console.log('message_timestamps: native times, trusted repair overlays, previews and fallback pass');
