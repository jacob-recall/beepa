import assert from 'node:assert/strict';
import { feedPreviewFromEvent, feedRelTime } from '../../shared/model/message_preview.js';
const event = content => ({type:'m.room.message', content, origin_server_ts:42});
assert.deepEqual(feedPreviewFromEvent(event({msgtype:'m.image', body:'secret filename', formatted_body:'<script>bad</script>'})), {body:'Photo', ts:42});
assert.deepEqual(feedPreviewFromEvent(event({'m.relates_to':{rel_type:'m.replace'}, 'm.new_content':{msgtype:'m.text', body:'edited'}})), {body:'edited', ts:42});
assert.equal(feedPreviewFromEvent({type:'m.reaction',content:{body:'not a message'}}), null);
assert.equal(feedPreviewFromEvent(event({formatted_body:'<b>untrusted</b>',msgtype:'m.text'})), null);
assert.equal(feedRelTime(Date.now()+60000), '');
console.log('Pure preview preserves text/media/edit whitelist without importing UI or send paths.');
