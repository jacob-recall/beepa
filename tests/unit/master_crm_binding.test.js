import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { acceptedSpaces, localpart, ROOM_SHAPE_RE } from '../../apps/master/invites.js';
import { mergeReceipts, MESSAGE_RECEIPT_TYPE } from '../../shared/model/message_receipts.js';
import { timestampCorrections, CORRECTION_TYPE } from '../../shared/model/message_timestamps.js';

// Execute actual snapshot parsing/projection, not a second capability parser.
const source = fs.readFileSync(new URL('../../apps/master/main.js', import.meta.url), 'utf8')
  .replace(/^import[\s\S]*?;\n/gm, '').replace(/^export .*;\n/gm, '');
const context = vm.createContext({ S: {}, Set, Map, Date, JSON, encodeURIComponent,
  acceptedSpaces, localpart, ROOM_SHAPE_RE, mergeReceipts, MESSAGE_RECEIPT_TYPE, timestampCorrections, CORRECTION_TYPE,
  masterTransport: () => ({}), configureMatrixBase() {}, setOnUnauthorized() {},
  sanitize: s => s, sanitizeLine: s => s, ROOMID_RE: /^![A-Za-z0-9._=/+-]+:master$/, PLATFORM_LABEL: {}, PLATFORM_ICON: {},
});
vm.runInContext(source, context);
const cap = { schema: 1, owner_mxid: '@elliot:master', generation: 'generation-1', journal_epoch: 'journal-1',
  ordered_policy: true, direct_prepare_commit: true, batch: false };
const info = () => context.freshRoomInfo('!proposals:master');
const apply = (record, sender, content, extra = {}) => context.applyRoom(record, { state: { events: [
  { type: 'm.room.create', state_key: '', sender: '@elliot:master', content: {} },
  { type: 'com.jkali.crm.capability', state_key: '', sender, content, ...extra },
] } });
let record = info(); apply(record, '@elliot:master', cap);
assert.equal(record.crmCapability.generation, 'generation-1'); assert.equal(record.crmCapability.batch, false);
record = info(); apply(record, '@david:master', cap); assert.ok(!record.crmCapability, 'foreign account cannot advertise a capability');
record = info(); apply(record, '@elliot:master', { ...cap, owner_mxid: '@david:master' }); assert.equal(record.crmCapability, null);
record = info(); apply(record, '@elliot:master', cap, { state_key: 'not-the-capability' }); assert.ok(!record.crmCapability);
for (const changed of [{ schema: 2 }, { generation: '' }, { journal_epoch: undefined }, { ordered_policy: 'true' }, { direct_prepare_commit: false }]) {
  record = info(); apply(record, '@elliot:master', { ...cap, ...changed }); assert.equal(record.crmCapability, null);
}
record = info(); apply(record, '@elliot:master', cap); apply(record, '@elliot:master', {});
assert.equal(record.crmCapability, null, 'revoked capability clears prior evidence');
const contact = { source: 'imessage', network_id: 'synthetic@example.test', display_name: 'Synthetic', contact_version: '17' };
assert.equal(context.parseContact(contact, 'elliot').contact_version, '17');
for (const version of [null, undefined, 17, '', 'next', '-1']) {
  assert.equal(context.parseContact({ ...contact, contact_version: version }, 'elliot').contact_version, null,
    'legacy or invalid contact version is never invented');
}
console.log('master_crm_binding: authenticated capability parsing, revoke, account isolation and exact source versions pass');

// Exact source-confirmed route projection, including no-profile contacts.
const ms = vm.runInContext('MS', context);
const binding = { binding_id: 'binding', person_id: 'person', owner_mxid: cap.owner_mxid,
  generation: cap.generation, availability: 'shared', contact: { source: 'imessage', network_id: contact.network_id } };
const request = { binding_id: 'binding', owner_mxid: cap.owner_mxid, generation: cap.generation,
  journal_epoch: cap.journal_epoch, command_id: 'membership-op' };
const result = { schema: 1, command_id: request.command_id, owner_mxid: cap.owner_mxid,
  generation: cap.generation, journal_epoch: cap.journal_epoch, rooms: [{ room_id: '!local:elliot', state: 'applied' }] };
context.bindingState = { memberships: { 'list/person': { direct_request: request } } };
vm.runInContext('crmStore = {snapshot: () => ({state: bindingState})}', context);
function setupRoutes(overrides = {}) {
  ms.contacts = [{ ...contact, owner_mxid: cap.owner_mxid, label: 'elliot' }];
  const proposals = info(); apply(proposals, cap.owner_mxid, cap);
  context.applyRoom(proposals, { state: { events: [{ type: 'com.jkali.crm.direct_result',
    state_key: request.command_id, sender: cap.owner_mxid, content: { ...result, ...overrides } }] } });
  ms.rooms = { '!proposals:master': proposals,
    '!good:master': { createSender: cap.owner_mxid }, '!foreign:master': { createSender: '@david:master' },
    '!wrong-source:master': { createSender: cap.owner_mxid }, '!title-match:master': { createSender: cap.owner_mxid } };
  ms.crmCapabilities = new Map([[cap.owner_mxid, { ...cap, proposal_room_id: '!proposals:master' }]]);
  ms.feed = [
    { id: '!good:master', sourceId: 'imessage', mirrorOf: '!local:elliot' },
    { id: '!foreign:master', sourceId: 'imessage', mirrorOf: '!local:elliot' },
    { id: '!wrong-source:master', sourceId: 'whatsapp', mirrorOf: '!local:elliot' },
    { id: '!title-match:master', sourceId: 'imessage', mirrorOf: '!guessed:elliot', title: contact.network_id, lastBody: contact.network_id },
  ];
  context.bindingState = { memberships: { 'list/person': { direct_request: { ...request } } } };
}
const routeIds = () => Array.from(context.crmBindingRooms(binding), room => room.id);
setupRoutes(); assert.deepEqual(routeIds(), ['!good:master'], 'exact local room proof isolates owner and source without a profile');
for (const changes of [{ generation: 'old' }, { journal_epoch: 'old' }, { owner_mxid: '@david:master' },
  { rooms: [{ room_id: '!local:elliot', state: 'queued' }] }, { rooms: [{ room_id: '!local:elliot', state: 'outcome_unknown' }] }]) {
  setupRoutes(changes); assert.deepEqual(routeIds(), [], 'unconfirmed/wrong-authority result is not a conversation route');
}
for (const changes of [{ binding_id: 'other' }, { owner_mxid: '@david:master' }, { generation: 'old' }, { journal_epoch: 'old' }]) {
  setupRoutes(); Object.assign(context.bindingState.memberships['list/person'].direct_request, changes);
  assert.deepEqual(routeIds(), [], 'frozen request must bind this identity and account epoch');
}
setupRoutes(); ms.contacts = []; assert.deepEqual(routeIds(), [], 'unshared contact clears route availability');
setupRoutes(); assert.deepEqual(Array.from(context.crmBindingRooms({ ...binding, availability: 'revoked' })), []);
setupRoutes(); ms.crmCapabilities.clear(); assert.deepEqual(routeIds(), [], 'no-profile route needs current authority');
setupRoutes();
context.applyRoom(ms.rooms['!proposals:master'], { state: { events: [{ type: 'com.jkali.crm.direct_result',
  state_key: request.command_id, sender: cap.owner_mxid, content: {} }] } });
assert.deepEqual(routeIds(), [], 'revoked owner-authenticated result clears prior room proof');
setupRoutes();
context.applyRoom(ms.rooms['!proposals:master'], { state: { events: [{ type: 'com.jkali.crm.direct_result',
  state_key: 'foreign', sender: '@david:master', content: { ...result, command_id: 'foreign' } }] } });
assert.equal(ms.rooms['!proposals:master'].crmDirectResults.has('foreign'), false);
// Preserved normal profile routing also isolates the sending account.
setupRoutes(); ms.contacts[0].person_id = 'profile';
ms.feed[0].profileId = ms.feed[1].profileId = 'profile'; ms.crmCapabilities.clear();
assert.deepEqual(routeIds(), ['!good:master']);
// The actual injected view action uses the normal single-room viewer or chooser.
const navigations = [];
context.createCRMView = options => options;
context.$ = id => ({ id });
context.openRoom = id => navigations.push(['room', id]);
context.selectContact = (ct, rooms) => navigations.push(['contact', ct, rooms]);
setupRoutes(); const view = context.getCRMView(); view.openPerson({}, binding);
assert.deepEqual(navigations[0], ['room', '!good:master']);
ms.rooms['!second:master'] = { createSender: cap.owner_mxid };
ms.feed.push({ id: '!second:master', sourceId: 'imessage', mirrorOf: '!local:elliot' });
view.openPerson({}, binding);
assert.equal(navigations[1][0], 'contact');
assert.deepEqual(Array.from(navigations[1][2], room => room.id), ['!good:master', '!second:master']);
ms.contacts = []; view.openPerson({}, binding); assert.equal(navigations.length, 2);
console.log('master_crm_binding: exact no-profile routes, owner/source/epoch isolation, revocation and regular conversation navigation pass');

setupRoutes(); context.bindingState.bindings = { binding };
const trackedItem = { binding_id: binding.binding_id, owner_mxid: binding.owner_mxid, generation: cap.generation, route: { source: 'imessage' } };
const trackedOutcome = { sent_event_id: '$sent', sent_room_id: '!local:elliot', tracking: { revision: 1,
  delivery: { sent_event_id: '$sent', mirror_room_id: '!good:master', mirror_event_id: '$sent-mirror' },
  response: { state: 'replied', incoming_event_id: '$reply', mirror_room_id: '!good:master', mirror_event_id: '$reply-mirror' } } };
Object.assign(ms.rooms['!good:master'], { id: '!good:master', mirrorOf: '!local:elliot', sourceId: 'imessage' });
const messageNavigations = []; context.openRoom = (...args) => messageNavigations.push(args);
context.openCRMMessage(trackedItem, trackedOutcome, 'delivery');
context.openCRMMessage(trackedItem, trackedOutcome, 'response');
assert.equal(messageNavigations.length, 2);
assert.equal(messageNavigations[0][1].eventId, '$sent-mirror'); assert.equal(messageNavigations[1][1].eventId, '$reply-mirror');
for (const mutation of [o => o.sent_event_id = '$other', o => o.sent_room_id = '!other:elliot',
  o => o.tracking.response.incoming_event_id = null, o => o.tracking.response.mirror_event_id = 'invalid',
  o => o.tracking.response.mirror_room_id = '!foreign:master']) {
  const value = JSON.parse(JSON.stringify(trackedOutcome)); mutation(value);
  context.openCRMMessage(trackedItem, value, 'response');
}
assert.equal(messageNavigations.length, 2, 'wrong exact message and room references cannot navigate');
for (const change of [{ owner_mxid: '@david:master' }, { generation: 'old' }, { route: { source: 'whatsapp' } }]) {
  context.openCRMMessage({ ...trackedItem, ...change }, trackedOutcome, 'delivery');
}
assert.equal(messageNavigations.length, 2, 'message links cannot cross sending owner, source or generation');
ms.feed = []; context.openCRMMessage(trackedItem, trackedOutcome, 'delivery');
assert.equal(messageNavigations.length, 2, 'a removed or hidden mirror cannot be opened by tracking metadata');
console.log('master_crm_binding: exact delivery/reply navigation validates message, local room, owner, generation, source and visible feed');

// Current UI permission labels use current visible mirrors, never an old grant
// alone; renewal does not relabel an account until source evidence exists.
function setupStatus(changes={}) {
  setupRoutes(changes);
  Object.assign(context.bindingState.memberships['list/person'],{binding_id:'binding',removed:false});
}
setupStatus();ms.rooms['!good:master'].shareLevelContent={level:'direct'};
assert.equal(context.crmBindingStatus(binding),'Direct conversation');
ms.rooms['!good:master'].shareLevelContent={level:'private'};
assert.equal(context.crmBindingStatus(binding),'Direct not enabled','manual privacy wins over old applied Direct grant');
setupStatus();ms.rooms['!good:master'].shareLevelContent={level:'direct'};
ms.rooms['!second:master']={createSender:cap.owner_mxid,shareLevelContent:{level:'share'}};
ms.feed.push({id:'!second:master',sourceId:'imessage',mirrorOf:'!local:elliot'});
assert.equal(context.crmBindingStatus(binding),'Mixed sharing settings');
setupStatus({state:'approval_required',phase:'prepare',rooms:[]});
assert.equal(context.crmBindingStatus(binding),'New chat approval required');
setupStatus({state:'canceled',phase:'cancel',rooms:[]});
assert.equal(context.crmBindingStatus(binding),'List Direct setup canceled');
setupStatus({state:'prepared',phase:'prepare',rooms:[]});
assert.equal(context.crmBindingStatus(binding),'Direct conversation not confirmed');
setupStatus({state:'approval_required',phase:'prepare',rooms:[],journal_epoch:'foreign'});
assert.equal(context.crmBindingStatus(binding),'Direct conversation not confirmed','stale result cannot advertise approval availability');
setupStatus();ms.contacts=[];assert.equal(context.crmBindingStatus(binding),'Contact no longer shared');
setupStatus();ms.crmCapabilities.clear();assert.equal(context.crmBindingStatus(binding),'Account sync update needed');
setupStatus();ms.crmCapabilities.get(cap.owner_mxid).generation='relinked';assert.equal(context.crmBindingStatus(binding),'Account sync update needed');
setupStatus({state:'approval_required',phase:'prepare',rooms:[]});context.bindingState.memberships['list/person'].removed=true;
assert.equal(context.crmBindingStatus(binding),'Direct conversation not confirmed','removed membership cannot supply new-chat approval status');
setupStatus();ms.rooms['!foreign:master'].shareLevelContent={level:'direct'};
assert.equal(context.crmBindingStatus(binding),'Direct not enabled','another account mirror cannot supply Direct permission');
console.log('master_crm_binding: current Direct, mixed/manual privacy, approval, cancellation, removed membership and relink status verified');
