import assert from 'node:assert/strict';
import { sourceRoomIds } from '../../shared/model/source_rooms.js';
import { SOURCES } from '../../shared/model/source_catalog.js';
import { bridgeInvitesToJoin } from '../../apps/user/invites.js';

const discord = SOURCES.find(source => source.id === 'discord');
const room = (name, children = [], isSpace = true) => ({ name, children, isSpace });
const rooms = {
  '!root:localhost': room('Discord', ['!dms:localhost', '!guild:localhost']),
  '!dms:localhost': room('Direct Messages', ['!one:localhost', '!group:localhost', '!gone:localhost', '!dms:localhost', '!mgmt:localhost']),
  '!guild:localhost': room('Server', ['!channel:localhost']),
  '!one:localhost': room('One', [], false),
  '!group:localhost': room('Group', [], false),
  '!channel:localhost': room('Server channel', [], false),
  '!mgmt:localhost': room('Management', [], false),
};
for (const [roomId, value] of Object.entries(rooms)) value.id = roomId;
assert.deepEqual(sourceRoomIds(discord, rooms, new Set(['!mgmt:localhost'])).sort(), ['!group:localhost', '!one:localhost']);
assert.equal(discord.canStartChat, false);
const self = '@casey:localhost';
function invite(creator, name) {
  return { invite_state: { events: [
    { type: 'm.room.create', state_key: '', sender: creator, content: { type: 'm.space' } },
    { type: 'm.room.name', state_key: '', sender: creator, content: { name } },
    { type: 'm.room.member', state_key: self, sender: creator, content: { membership: 'invite' } },
  ] } };
}
const bots = SOURCES.map(source => source.botMxid).filter(Boolean);
assert.deepEqual(bridgeInvitesToJoin({ '!dm:localhost': invite(discord.botMxid, 'Direct Messages') }, bots, self, SOURCES).join, ['!dm:localhost']);
assert.deepEqual(bridgeInvitesToJoin({ '!dm:localhost': invite('@whatsappbot:localhost', 'Direct Messages') }, bots, self, SOURCES).join, []);
assert.deepEqual(bridgeInvitesToJoin({ '!dm:localhost': invite(discord.botMxid, 'WhatsApp') }, bots, self, SOURCES).join, []);
console.log('Discord nested DMs, group DMs, cycles, exclusions and invite identity checks pass.');
