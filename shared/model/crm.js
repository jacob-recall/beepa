// Pure CRM metadata and attribution. Transport authenticates the room and sender
// before replaying operations in canonical Matrix timeline order. Nothing here
// grants consent, discovers contacts, sends messages, or interprets display names.
export const CRM_SCHEMA = 1;
export const CRM_OPERATION_TYPE = 'com.jkali.crm.operation';
export const CRM_MAX_BATCH_ITEMS = 50;
export const CRM_MAX_BYTES = 48000;
export const defaultControlSettings = () => ({allow_model_context:true,sending_mode:'draft_confirm',allowed_accounts:null,notifications_enabled:true,retention_days:30});
const ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const MXID = /^@[^\s:]+:[^\s]+$/;
const ROOM = /^![^\s:]+:[^\s]+$/;
const SOURCE = /^[a-z][a-z0-9_-]{0,63}$/;
const encoder = new TextEncoder();
const own = (o, k) => Object.hasOwn(o, k);
const plain = x => x !== null && typeof x === 'object' && !Array.isArray(x) && (Object.getPrototypeOf(x) === Object.prototype || Object.getPrototypeOf(x) === null);
const fail = code => { throw new Error(code); };
const requireValue = (ok, code = 'invalid-payload') => { if (!ok) fail(code); };
const text = (x, max = 256) => typeof x === 'string' && x.length > 0 && x.length <= max && !/[\u0000-\u001f]/.test(x);
const id = x => typeof x === 'string' && ID.test(x) && !['__proto__', 'constructor', 'prototype'].includes(x);
const revision = x => Number.isSafeInteger(x) && x >= 0;
const boundedIdentity = (x, re) => typeof x === 'string' && x.length <= 256 && re.test(x);
const generation = x => text(x, 128);
const compare = (a, b) => a < b ? -1 : a > b ? 1 : 0;
function keys(value, required, optional = []) {
  requireValue(plain(value) && required.every(k => own(value, k)) && Object.keys(value).every(k => required.includes(k) || optional.includes(k)));
}
const monitoringOptions=['schedule','repeat_ms','activity_enabled','reminders_enabled','presentation','routing'];
// Explicit timer edits begin a new cycle. Legacy operations remain byte-for-byte
// legacy on replay, so upgrading cannot replay already attempted reminders.
export function withReminderEpisode(previous, payload, now = Date.now()) {
  const result = { ...payload };
  if (previous?.episode_at !== undefined) result.episode_at = previous.episode_at;
  const fields = ['due_at', 'snooze_until', 'resolved', 'resolved_event'];
  const value = (v, key) => v?.[key] ?? (key === 'resolved' ? false : null);
  const changed = fields.some(key => value(previous, key) !== value(payload, key));
  // A first deadline already has its own identity, shared across overlapping
  // lists. A first snooze/resolution, or a later timer edit, needs a new cycle.
  if (changed && (previous || payload.snooze_until != null || payload.resolved || payload.resolved_event != null)) {
    result.episode_at = Math.max(now, (previous?.episode_at || 0) + 1);
  }
  return result;
}
export function reminderOperationPayload(type, list, payload, now = Date.now()) {
  if (!['list.watch', 'list.obligation'].includes(type)) return payload;
  const watch = list?.watches?.[payload.room_id];
  const obligations = list?.obligations || {};
  const roomEpisodes = Object.values(obligations).filter(o => o.room_id === payload.room_id).map(o => o.episode_at || 0);
  // Room-wide and per-message edits can happen in the same millisecond. A
  // later explicit edit must advance beyond either contributing cycle.
  const at = Math.max(now, (watch?.episode_at || 0) + 1, ...roomEpisodes.map(n => n + 1));
  const previous = type === 'list.watch' ? watch : obligations[JSON.stringify([payload.room_id, payload.event_id])];
  return withReminderEpisode(previous, payload, at);
}
function validateReminderEpisode(payload, previous) {
  requireValue(!own(payload, 'episode_at') || Number.isSafeInteger(payload.episode_at) && payload.episode_at > 0);
  const changed = ['due_at', 'snooze_until', 'resolved', 'resolved_event'].some(k => (payload[k] ?? null) !== (previous?.[k] ?? null));
  requireValue(previous?.episode_at === undefined || (changed ? payload.episode_at > previous.episode_at : payload.episode_at >= previous.episode_at), 'reminder-episode-regression');
}
export function monitoringRouting(rule){
  requireValue(Array.isArray(rule.channels)&&rule.channels.length<=2&&new Set(rule.channels).size===rule.channels.length&&rule.channels.every(c=>['browser','telegram'].includes(c)),'invalid-notification-channels');
  const selected=['browser','telegram'].filter(c=>rule.channels.includes(c)),r=rule.routing;
  if(r===undefined||r===null)return {channels:selected,primary:null,fallback:false};
  keys(r,['primary','fallback']);
  requireValue(selected.includes(r.primary)&&typeof r.fallback==='boolean'&&(!r.fallback||selected.length===2),'invalid-notification-routing');
  return {channels:selected,primary:r.primary,fallback:r.fallback};
}
function validateMonitoringOptions(p){
  if(p.routing!==undefined&&p.routing!==null){keys(p.routing,['primary','fallback']);requireValue(['browser','telegram'].includes(p.routing.primary)&&typeof p.routing.fallback==='boolean');}
  if(p.presentation!==undefined&&p.presentation!==null){
    const v=p.presentation;keys(v,['mode','interval_ms']);
    requireValue(['immediate','grouped','digest'].includes(v.mode));
    requireValue(v.mode==='immediate'?v.interval_ms===null:Number.isSafeInteger(v.interval_ms)&&v.interval_ms>=60000&&v.interval_ms<=(v.mode==='grouped'?3600000:86400000));
  }
  for(const key of ['activity_enabled','reminders_enabled'])if(own(p,key))requireValue(typeof p[key]==='boolean');
  for(const key of ['repeat_ms','waiting_them_ms','waiting_us_ms'])if(own(p,key))requireValue(p[key]===null||Number.isSafeInteger(p[key])&&p[key]>=60000&&p[key]<=31536000000);
  if(p.schedule!==undefined&&p.schedule!==null){
    const s=p.schedule;keys(s,['timezone','mode','days','start_minute','end_minute']);
    requireValue(text(s.timezone,100)&&/^(?:UTC|GMT|[A-Za-z_]+(?:\/[A-Za-z0-9_+-]+)+)$/.test(s.timezone)&&['quiet','working'].includes(s.mode));
    try{new Intl.DateTimeFormat('en-US',{timeZone:s.timezone}).format(0);}catch{fail('invalid-timezone');}
    requireValue(Array.isArray(s.days)&&s.days.length>0&&s.days.length<=7&&new Set(s.days).size===s.days.length&&s.days.every(d=>Number.isInteger(d)&&d>=1&&d<=7));
    requireValue(Number.isInteger(s.start_minute)&&s.start_minute>=0&&s.start_minute<1440&&Number.isInteger(s.end_minute)&&s.end_minute>=0&&s.end_minute<=1440&&s.start_minute!==s.end_minute);
  }
}
// Window days identify the local start day; overnight windows extend into the
// following day. Evaluate the actual instant so DST gaps/repeated hours require
// no guessed UTC offset. Schedules defer alerts, not elapsed-time deadlines.
export function monitoringAllows(schedule,at){
  if(schedule===undefined||schedule===null)return true;
  try{
    validateMonitoringOptions({schedule});
    const parts=Object.fromEntries(new Intl.DateTimeFormat('en-US',{timeZone:schedule.timezone,weekday:'short',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(at).map(p=>[p.type,p.value]));
    const day=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].indexOf(parts.weekday)+1,minute=Number(parts.hour)*60+Number(parts.minute),s=schedule;
    const inside=s.start_minute<s.end_minute?s.days.includes(day)&&minute>=s.start_minute&&minute<s.end_minute:
      s.days.includes(day)&&minute>=s.start_minute||s.days.includes(day===1?7:day-1)&&minute<s.end_minute;
    return s.mode==='working'?inside:!inside;
  }catch{return false;}
}
export function monitoringReminderSlot(due,repeat,at){
  if(!Number.isSafeInteger(due)||due<=0||at<due)return null;
  if(repeat===undefined||repeat===null)return due;
  if(!Number.isSafeInteger(repeat)||repeat<60000||repeat>31536000000)return null;
  return due+Math.floor((at-due)/repeat)*repeat;
}
// Message updates share fixed UTC collection windows. Open overlapping policies
// compete by earliest delivery; immediate wins without duplicating a later digest.
export function monitoringDeliveryPlan(rules,created,at,room){
  const choices=[];
  for(const rule of rules){
    if(!monitoringAllows(rule.schedule,at))continue;
    try{validateMonitoringOptions({presentation:rule.presentation});}catch{continue;}
    const p=rule.presentation||{mode:'immediate',interval_ms:null};
    const ready_at=p.mode==='immediate'?created:(Math.floor(created/p.interval_ms)+1)*p.interval_ms;
    const collection=p.mode==='immediate'?created:Math.max(ready_at,Math.floor(at/p.interval_ms)*p.interval_ms);
    choices.push({mode:p.mode,ready_at,key:p.mode==='grouped'?'grouped:'+room+':'+collection:p.mode==='digest'?'digest:'+rule.list_id+':'+collection:null,list_id:rule.list_id});
  }
  choices.sort((a,b)=>a.ready_at-b.ready_at||compare(String(a.key||''),String(b.key||'')));
  return choices[0]||null;
}
export function canonicalCRMJSON(value) {
  function walk(x, depth) {
    requireValue(depth <= 16, 'payload-too-deep');
    if (x === null || typeof x === 'boolean' || typeof x === 'string') return JSON.stringify(x);
    if (typeof x === 'number') { requireValue(Number.isFinite(x)); return JSON.stringify(x); }
    if (Array.isArray(x)) return '[' + x.map(v => walk(v, depth + 1)).join(',') + ']';
    requireValue(plain(x));
    return '{' + Object.keys(x).sort().map(k => JSON.stringify(k) + ':' + walk(x[k], depth + 1)).join(',') + '}';
  }
  return walk(value, 0);
}
function copy(value) { return JSON.parse(canonicalCRMJSON(value)); }
function freeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}
function bounded(value) {
  const encoded = canonicalCRMJSON(value);
  requireValue(encoder.encode(encoded).length <= CRM_MAX_BYTES, 'payload-too-large');
  return encoded;
}
function dictionary() { return Object.create(null); }
export function createCRMState({ workspace_id, manager_mxid }) {
  requireValue(boundedIdentity(workspace_id, ROOM) && boundedIdentity(manager_mxid, MXID), 'invalid-workspace');
  return freeze({ schema: CRM_SCHEMA, workspace_id, manager_mxid, persons: dictionary(), bindings: dictionary(), lists: dictionary(), memberships: dictionary(), batches: dictionary(), batch_cancellations: dictionary(), operations: dictionary() });
}
export function membershipKey(listId, personId) {
  requireValue(id(listId) && id(personId), 'invalid-id');
  return listId + '/' + personId;
}
function contactIdentity(c) {
  requireValue(plain(c));
  if (own(c, 'profile_id')) { keys(c, ['profile_id']); requireValue(text(c.profile_id, 256)); }
  else { keys(c, ['source', 'network_id']); requireValue(typeof c.source === 'string' && SOURCE.test(c.source) && text(c.network_id, 512)); }
}
function current(state, collection, entityId, base) {
  const entity = state[collection][entityId];
  requireValue(!!entity, 'not-found');
  requireValue(entity.revision === base, 'revision-conflict');
  return entity;
}
function fresh(state, collection, entityId, base) {
  requireValue(!own(state[collection], entityId) && base === 0, 'revision-conflict');
}
function setEntity(state, collection, entityId, value) {
  return { ...state, [collection]: { ...state[collection], [entityId]: freeze(value) } };
}
function validateRoute(route) {
  keys(route, ['mode', 'source', 'network_id', 'consent_revision'], ['room_id']);
  requireValue(typeof route.source === 'string' && SOURCE.test(route.source) && text(route.network_id, 512) && revision(route.consent_revision));
  if (route.mode === 'direct_existing') requireValue(boundedIdentity(route.room_id, ROOM));
  else requireValue(route.mode === 'request_new_conversation_approval' && !own(route, 'room_id'));
}
function validateManifest(state, manifest) {
  keys(manifest, ['schema', 'batch_id', 'workspace_id', 'manager_mxid', 'list_id', 'list_revision', 'reviewed_at', 'items', 'children', 'payload_hash']);
  requireValue(manifest.schema === CRM_SCHEMA && id(manifest.batch_id) && manifest.workspace_id === state.workspace_id && manifest.manager_mxid === state.manager_mxid);
  requireValue(id(manifest.list_id) && revision(manifest.list_revision) && Number.isSafeInteger(manifest.reviewed_at) && manifest.reviewed_at > 0);
  requireValue(typeof manifest.payload_hash === 'string' && /^[a-f0-9]{64}$/.test(manifest.payload_hash));
  const list = current(state, 'lists', manifest.list_id, manifest.list_revision);
  requireValue(!list.archived, 'list-archived');
  requireValue(Array.isArray(manifest.items) && manifest.items.length > 0 && manifest.items.length <= CRM_MAX_BATCH_ITEMS, 'batch-size');
  requireValue(Array.isArray(manifest.children) && manifest.children.length > 0 && manifest.children.length <= manifest.items.length);
  const people = new Set(), items = new Set(), children = new Map();
  for (const child of manifest.children) {
    keys(child, ['child_id', 'owner_mxid', 'generation', 'journal_epoch', 'proposal_room_id', 'item_ids']);
    requireValue(id(child.child_id) && boundedIdentity(child.owner_mxid, MXID) && generation(child.generation) && generation(child.journal_epoch) && boundedIdentity(child.proposal_room_id, ROOM));
    requireValue(Array.isArray(child.item_ids) && child.item_ids.length > 0 && child.item_ids.every(id));
    requireValue(!children.has(child.child_id));
    children.set(child.child_id, child);
  }
  const accountGroups = new Set();
  for (const child of children.values()) {
    const account = canonicalCRMJSON([child.owner_mxid, child.generation]);
    requireValue(!accountGroups.has(account), 'duplicate-account-child'); accountGroups.add(account);
  }
  for (const item of manifest.items) {
    keys(item, ['item_id', 'person_id', 'binding_id', 'binding_revision', 'membership_revision', 'membership_operation_id', 'contact_version', 'owner_mxid', 'generation', 'child_id', 'proposal_room_id', 'route', 'body']);
    requireValue(id(item.item_id) && id(item.person_id) && id(item.binding_id) && id(item.child_id) && revision(item.binding_revision) && revision(item.membership_revision));
    requireValue(typeof item.body === 'string' && item.body.trim().length > 0 && item.body.length <= 8000 && !/\u0000/.test(item.body), 'invalid-body');
    requireValue(!people.has(item.person_id) && !items.has(item.item_id), 'duplicate-recipient'); people.add(item.person_id); items.add(item.item_id);
    const binding = current(state, 'bindings', item.binding_id, item.binding_revision);
    const member = current(state, 'memberships', membershipKey(manifest.list_id, item.person_id), item.membership_revision);
    requireValue(!member.removed && member.binding_id === item.binding_id && binding.person_id === item.person_id && binding.availability === 'shared', 'binding-unavailable');
    requireValue(item.membership_operation_id === member.operation_id && item.contact_version === binding.contact_version, 'binding-unavailable');
    requireValue(binding.owner_mxid === item.owner_mxid && binding.generation === item.generation, 'owner-mismatch');
    validateRoute(item.route);
    if (!own(binding.contact, 'profile_id')) requireValue(binding.contact.source === item.route.source && binding.contact.network_id === item.route.network_id, 'route-mismatch');
    const child = children.get(item.child_id);
    requireValue(child && child.owner_mxid === item.owner_mxid && child.generation === item.generation && child.proposal_room_id === item.proposal_room_id && child.item_ids.filter(x => x === item.item_id).length === 1, 'child-mismatch');
    if (member.direct_request) requireValue(member.direct_request.journal_epoch === child.journal_epoch, 'child-mismatch');
  }
  requireValue([...children.values()].flatMap(c => c.item_ids).length === items.size && [...children.values()].every(c => c.item_ids.every(x => items.has(x))), 'child-mismatch');
  bounded(manifest);
}
// Rejections with a valid envelope are remembered too: replaying a stale edit
// after later state changes must never turn that original edit into an acceptance.
export function applyCRMOperation(state, operation) {
  let serialized;
  try {
    keys(operation, ['schema', 'op_id', 'workspace_id', 'type', 'entity_id', 'base_revision', 'payload']);
    requireValue(operation.schema === CRM_SCHEMA && id(operation.op_id) && id(operation.entity_id) && revision(operation.base_revision) && operation.workspace_id === state.workspace_id && plain(operation.payload), 'invalid-operation');
    serialized = bounded(operation);
  } catch (error) { return { state, outcome: { accepted: false, code: error.message } }; }
  const previous = state.operations[operation.op_id];
  if (previous) return { state, outcome: previous.serialized === serialized ? { ...previous.outcome, duplicate: true } : { accepted: false, code: 'operation-id-collision' } };
  const { type, entity_id: entityId, base_revision: base, payload: p } = operation;
  let next = state, outcome;
  try {
    switch (type) {
      case 'control.settings': {
        keys(p, ['allow_model_context', 'sending_mode', 'allowed_accounts', 'notifications_enabled', 'retention_days']);
        requireValue(entityId === 'control' && base === (state.control?.revision || 0), 'revision-conflict');
        requireValue(typeof p.allow_model_context === 'boolean' && typeof p.notifications_enabled === 'boolean');
        requireValue(['read_only', 'draft_confirm'].includes(p.sending_mode));
        requireValue(p.allowed_accounts === null || Array.isArray(p.allowed_accounts) && p.allowed_accounts.length <= 100
          && new Set(p.allowed_accounts).size === p.allowed_accounts.length && p.allowed_accounts.every(a => boundedIdentity(a, MXID)));
        requireValue(Number.isInteger(p.retention_days) && p.retention_days >= 1 && p.retention_days <= 365);
        next = { ...state, control: { revision: base + 1, settings: copy(p) } }; break;
      }
      case 'person.create':
        keys(p, ['label']); requireValue(text(p.label, 160)); fresh(state, 'persons', entityId, base);
        next = setEntity(state, 'persons', entityId, { person_id: entityId, label: p.label, revision: 1 }); break;
      case 'binding.create': {
        keys(p, ['person_id', 'owner_mxid', 'generation', 'contact', 'contact_version']);
        requireValue(id(p.person_id) && own(state.persons, p.person_id) && boundedIdentity(p.owner_mxid, MXID) && generation(p.generation) && text(p.contact_version, 256));
        contactIdentity(p.contact); fresh(state, 'bindings', entityId, base);
        requireValue(!Object.values(state.bindings).some(b => b.owner_mxid === p.owner_mxid && b.generation === p.generation && canonicalCRMJSON(b.contact) === canonicalCRMJSON(p.contact)), 'binding-already-linked');
        next = setEntity(state, 'bindings', entityId, { ...copy(p), binding_id: entityId, availability: 'shared', revision: 1 }); break;
      }
      case 'binding.update': {
        keys(p, ['contact_version', 'availability']); requireValue(text(p.contact_version, 256) && ['shared', 'unavailable', 'tombstone'].includes(p.availability));
        const old = current(state, 'bindings', entityId, base);
        // Re-sharing is an explicit new operation with the current revision and
        // fresh shared-contact evidence. Old membership/replay cannot revive it.
        requireValue(old.availability !== 'tombstone' || (p.availability === 'shared' && p.contact_version !== old.contact_version), 'binding-tombstoned');
        next = setEntity(state, 'bindings', entityId, { ...old, ...copy(p), revision: base + 1 }); break;
      }
      case 'list.create':
        keys(p, ['name'], ['purpose']); requireValue(text(p.name, 160) && (!p.purpose || ['monitoring', 'outreach'].includes(p.purpose))); fresh(state, 'lists', entityId, base);
        next = setEntity(state, 'lists', entityId, { list_id: entityId, name: p.name, archived: false, revision: 1, creator_mxid: state.manager_mxid, ...(p.purpose ? { purpose: p.purpose } : {}) }); break;
      case 'list.monitoring': {
        keys(p, ['enabled', 'all_messages', 'channels', 'waiting_them_ms', 'waiting_us_ms', 'preview'],monitoringOptions);
        validateMonitoringOptions(p);
        const old = current(state, 'lists', entityId, base);
        requireValue(!old.archived && typeof p.enabled === 'boolean' && typeof p.all_messages === 'boolean' && typeof p.preview === 'boolean');
        requireValue(Array.isArray(p.channels) && p.channels.length <= 2 && new Set(p.channels).size === p.channels.length && p.channels.every(c => ['browser', 'telegram'].includes(c)));
        monitoringRouting(p);
        for(const w of Object.values(old.watches||{}))monitoringRouting({...p,...w});
        for (const k of ['waiting_them_ms', 'waiting_us_ms']) requireValue(p[k] === null || (Number.isSafeInteger(p[k]) && p[k] >= 60000 && p[k] <= 31536000000));
        next = setEntity(state, 'lists', entityId, { ...old, monitoring: copy(p), revision: base + 1 }); break;
      }
      case 'list.watch': {
        keys(p, ['room_id', 'owner_mxid', 'enabled', 'due_at', 'snooze_until', 'resolved_event'], ['episode_at','all_messages','waiting_them_ms','waiting_us_ms','channels',...monitoringOptions]);
        validateMonitoringOptions(p);
        const old = current(state, 'lists', entityId, base);
        requireValue(!old.archived && boundedIdentity(p.room_id, ROOM) && boundedIdentity(p.owner_mxid, MXID) && typeof p.enabled === 'boolean');
        monitoringRouting({channels:['browser','telegram'],...old.monitoring,...p});
        for (const k of ['due_at', 'snooze_until']) requireValue(p[k] === null || (Number.isSafeInteger(p[k]) && p[k] > 0));
        requireValue(p.resolved_event === null || (typeof p.resolved_event === 'string' && /^\$[^\s]{1,255}$/.test(p.resolved_event)));
        requireValue(!own(p, 'all_messages') || typeof p.all_messages === 'boolean');
        requireValue(Object.keys(old.watches || {}).length < 1000 || own(old.watches || {}, p.room_id), 'watch-limit');
        validateReminderEpisode(p, old.watches?.[p.room_id]);
        next = setEntity(state, 'lists', entityId, { ...old, watches: { ...old.watches, [p.room_id]: copy(p) }, revision: base + 1 }); break;
      }
      case 'list.contact_obligation': {
        keys(p, ['binding_id', 'owner_mxid', 'generation', 'membership_operation', 'created_at', 'episode_at', 'due_at', 'snooze_until', 'resolved']);
        const old = current(state, 'lists', entityId, base), binding = state.bindings[p.binding_id];
        requireValue(!old.archived && id(p.binding_id) && boundedIdentity(p.owner_mxid, MXID) && generation(p.generation));
        requireValue(binding && binding.availability === 'shared' && binding.owner_mxid === p.owner_mxid && binding.generation === p.generation, 'binding-unavailable');
        const member = state.memberships[membershipKey(entityId, binding.person_id)];
        requireValue(member && !member.removed && member.binding_id === p.binding_id && member.operation_id === p.membership_operation, 'membership-conflict');
        requireValue(typeof p.resolved === 'boolean' && ['created_at','episode_at'].every(k=>Number.isSafeInteger(p[k])&&p[k]>0));
        for (const k of ['due_at', 'snooze_until']) requireValue(p[k] === null || (Number.isSafeInteger(p[k]) && p[k] > 0));
        const entries = old.contact_obligations || {};
        requireValue(Object.keys(entries).length < 2000 || own(entries, p.binding_id), 'obligation-limit');
        next = setEntity(state, 'lists', entityId, { ...old, contact_obligations: { ...entries, [p.binding_id]: copy(p) }, revision: base + 1 }); break;
      }
      case 'list.obligation': {
        keys(p, ['room_id', 'owner_mxid', 'event_id', 'due_at', 'snooze_until', 'resolved'], ['episode_at']);
        const old = current(state, 'lists', entityId, base);
        requireValue(!old.archived && boundedIdentity(p.room_id, ROOM) && boundedIdentity(p.owner_mxid, MXID));
        requireValue(p.event_id === 'uncontacted' || boundedIdentity(p.event_id, /^\$[^\s]{1,255}$/));
        requireValue(typeof p.resolved === 'boolean');
        for (const k of ['due_at', 'snooze_until']) requireValue(p[k] === null || (Number.isSafeInteger(p[k]) && p[k] > 0));
        const entries = old.obligations || {}, key = JSON.stringify([p.room_id, p.event_id]);
        validateReminderEpisode(p, entries[key]);
        requireValue(Object.keys(entries).length < 2000 || own(entries, key), 'obligation-limit');
        next = setEntity(state, 'lists', entityId, { ...old, obligations: { ...entries, [key]: copy(p) }, revision: base + 1 }); break;
      }
      case 'list.seen': {
        keys(p, ['room_id', 'owner_mxid', 'event_id', 'source_order', 'bridge_epoch', 'generation']);
        const old = current(state, 'lists', entityId, base);
        requireValue(!old.archived && boundedIdentity(p.room_id, ROOM) && boundedIdentity(p.owner_mxid, MXID));
        requireValue(boundedIdentity(p.event_id, /^\$[^\s]{1,255}$/) && typeof p.source_order === 'string' && /^\d{1,32}$/.test(p.source_order));
        requireValue(text(p.bridge_epoch, 512) && generation(p.generation));
        requireValue(Object.keys(old.seen || {}).length < 1000 || own(old.seen || {}, p.room_id), 'seen-limit');
        const before = old.seen?.[p.room_id];
        requireValue(!before || before.owner_mxid !== p.owner_mxid || before.bridge_epoch !== p.bridge_epoch || before.generation !== p.generation || BigInt(p.source_order) >= BigInt(before.source_order), 'seen-regression');
        next = setEntity(state, 'lists', entityId, { ...old, seen: { ...old.seen, [p.room_id]: copy(p) }, revision: base + 1 }); break;
      }
      case 'list.rename':
      case 'list.archive':
      case 'list.restore': {
        keys(p, type === 'list.rename' ? ['name'] : [], type === 'list.archive' ? ['membership_operations'] : []);
        const old = current(state, 'lists', entityId, base);
        requireValue(type === 'list.restore' ? old.archived : !old.archived, type === 'list.restore' ? 'list-not-archived' : 'list-archived');
        if (type === 'list.rename') requireValue(text(p.name, 160));
        if (type === 'list.archive' && own(p, 'membership_operations')) {
          requireValue(Array.isArray(p.membership_operations) && p.membership_operations.every(id));
          const active = Object.values(state.memberships).filter(member => member.list_id === entityId && !member.removed).map(member => member.operation_id).sort();
          requireValue(canonicalCRMJSON(p.membership_operations) === canonicalCRMJSON(active), 'membership-conflict');
        }
        next = setEntity(state, 'lists', entityId, { ...old, ...(type === 'list.rename' ? { name: p.name } : { archived: type === 'list.archive' }), revision: base + 1 }); break;
      }
      case 'member.set':
      case 'member.remove': {
        keys(p, ['list_id', 'person_id'], type === 'member.set' ? ['binding_id', 'direct_request'] : []);
        requireValue(id(p.list_id) && id(p.person_id) && own(state.persons, p.person_id));
        requireValue(state.lists[p.list_id] && !state.lists[p.list_id].archived, 'list-archived');
        const key = membershipKey(p.list_id, p.person_id), old = state.memberships[key];
        requireValue(old ? old.membership_id === entityId && old.revision === base : base === 0, 'revision-conflict');
        requireValue(!Object.values(state.memberships).some(m => m.membership_id === entityId && membershipKey(m.list_id, m.person_id) !== key), 'membership-id-collision');
        if (type === 'member.set') {
          const binding = id(p.binding_id) && state.bindings[p.binding_id];
          requireValue(binding && binding.person_id === p.person_id && binding.availability === 'shared', 'binding-unavailable');
          if (own(p, 'direct_request')) {
            const request = p.direct_request;
            keys(request, ['schema', 'command_id', 'owner_mxid', 'generation', 'journal_epoch', 'binding_id', 'contact', 'contact_version', 'membership_operation_id', 'proposal_room_id']);
            contactIdentity(request.contact);
            requireValue(request.schema === CRM_SCHEMA && request.command_id === operation.op_id && request.membership_operation_id === operation.op_id
              && request.owner_mxid === binding.owner_mxid && request.generation === binding.generation && generation(request.journal_epoch)
              && request.binding_id === binding.binding_id && request.contact_version === binding.contact_version
              && canonicalCRMJSON(request.contact) === canonicalCRMJSON(binding.contact) && !own(request.contact, 'profile_id')
              && boundedIdentity(request.proposal_room_id, ROOM), 'direct-request-mismatch');
          }
        } else requireValue(old && !old.removed, 'not-found');
        const member = { ...old, ...copy(p), membership_id: entityId, revision: base + 1, removed: type === 'member.remove', operation_id: operation.op_id };
        if (type === 'member.set' && !own(p, 'direct_request')) delete member.direct_request;
        next = setEntity(state, 'memberships', key, member); break;
      }
      case 'batch.review':
        fresh(state, 'batches', entityId, base); requireValue(p.batch_id === entityId); validateManifest(state, p);
        next = setEntity(state, 'batches', entityId, copy(p)); break;
      case 'batch.cancel_requested': {
        keys(p, ['payload_hash']);
        const batch = state.batches[entityId];
        requireValue(base === 0 && batch && p.payload_hash === batch.payload_hash, 'batch-cancel-mismatch');
        // Cancellation is durable intent, not proof that a source stopped.
        // It must remain possible after list/contact changes or review expiry.
        if (!state.batch_cancellations[entityId]) next = setEntity(state, 'batch_cancellations', entityId,
          { batch_id: entityId, payload_hash: p.payload_hash, operation_id: operation.op_id });
        break;
      }
      default: fail('unknown-operation');
    }
    outcome = { accepted: true, code: 'accepted' };
  } catch (error) { next = state; outcome = { accepted: false, code: error.message }; }
  next = setEntity(next, 'operations', operation.op_id, { serialized, outcome });
  return { state: freeze(next), outcome };
}
async function digest(value) {
  const bytes = await globalThis.crypto.subtle.digest('SHA-256', encoder.encode(canonicalCRMJSON(value)));
  return [...new Uint8Array(bytes)].map(x => x.toString(16).padStart(2, '0')).join('');
}
export { digest as crmDigest };
export async function verifyReviewedBatch(manifest) {
  try {
    const { payload_hash, ...payload } = manifest;
    bounded(manifest);
    if (await digest(payload) !== payload_hash) return false;
    for (const child of manifest.children) if (child.child_id !== await accountChildId(manifest.batch_id, child.owner_mxid, child.generation)) return false;
    return true;
  } catch { return false; }
}
// The storage entry point verifies digest and derived child IDs before the pure
// reducer. Use this for every authenticated operation, including timeline replay.
export async function applyVerifiedCRMOperation(state, operation) {
  if (id(operation?.op_id) && state.operations[operation.op_id]) return applyCRMOperation(state, operation);
  if (operation?.type === 'batch.review' && !(await verifyReviewedBatch(operation.payload))) {
    return { state, outcome: { accepted: false, code: 'invalid-manifest-hash' } };
  }
  return applyCRMOperation(state, operation);
}
async function accountChildId(batchId, owner, enrollment) {
  return 'child-' + await digest([batchId, owner, enrollment]);
}
// Callers supply exact reviewed routes. Existing consent and contact resolvers
// must verify those routes independently; metadata is never send authority.
export async function createReviewedBatch(state, { batch_id, list_id, items, reviewed_at }) {
  requireValue(id(batch_id) && id(list_id) && Array.isArray(items) && items.length > 0 && items.length <= CRM_MAX_BATCH_ITEMS, 'batch-size');
  const list = state.lists[list_id]; requireValue(list && !list.archived, 'list-archived');
  const groups = new Map(), frozenItems = [];
  for (const input of items) {
    keys(input, ['item_id', 'person_id', 'proposal_room_id', 'route', 'body'], ['journal_epoch']);
    const member = state.memberships[membershipKey(list_id, input.person_id)], binding = member && state.bindings[member.binding_id];
    requireValue(binding && !member.removed && binding.availability === 'shared', 'binding-unavailable');
    const child_id = await accountChildId(batch_id, binding.owner_mxid, binding.generation);
    const journal_epoch = member.direct_request?.journal_epoch ?? input.journal_epoch;
    requireValue(generation(journal_epoch), 'missing-journal-epoch');
    if (input.journal_epoch !== undefined) requireValue(input.journal_epoch === journal_epoch, 'child-mismatch');
    if (!groups.has(child_id)) groups.set(child_id, { child_id, owner_mxid: binding.owner_mxid, generation: binding.generation, journal_epoch, proposal_room_id: input.proposal_room_id, item_ids: [] });
    const child = groups.get(child_id); requireValue(child.proposal_room_id === input.proposal_room_id && child.journal_epoch === journal_epoch, 'child-mismatch'); child.item_ids.push(input.item_id);
    const { journal_epoch: _epoch, ...reviewedInput } = copy(input);
    frozenItems.push({ ...reviewedInput, binding_id: binding.binding_id, binding_revision: binding.revision, membership_revision: member.revision,
      membership_operation_id: member.operation_id, contact_version: binding.contact_version,
      owner_mxid: binding.owner_mxid, generation: binding.generation, child_id });
  }
  const payload = { schema: CRM_SCHEMA, batch_id, workspace_id: state.workspace_id, manager_mxid: state.manager_mxid, list_id, list_revision: list.revision, reviewed_at, items: frozenItems, children: [...groups.values()] };
  const manifest = { ...payload, payload_hash: await digest(payload) };
  validateManifest(state, manifest);
  // Leave headroom for the operation envelope under Matrix event size limits.
  requireValue(encoder.encode(canonicalCRMJSON(manifest)).length <= CRM_MAX_BYTES - 1000, 'payload-too-large');
  return freeze(manifest);
}
// Staging needs the hash before actual proposal events exist. Call once with no
// references for annotations, then again with the returned event IDs; references
// are transport data and never change the reviewed child hash.
export async function createCRMChildEnvelope(manifest, childId, proposalEvents = {}) {
  requireValue(await verifyReviewedBatch(manifest), 'invalid-manifest-hash');
  const child = manifest.children.find(value => value.child_id === childId);
  requireValue(child && generation(child.journal_epoch) && plain(proposalEvents), 'invalid-child');
  const items = manifest.items.filter(item => item.child_id === childId).map(copy);
  requireValue(items.length === child.item_ids.length && items.every(item => child.item_ids.includes(item.item_id)), 'child-mismatch');
  const payload = { schema: CRM_SCHEMA, owner_mxid: child.owner_mxid, generation: child.generation, journal_epoch: child.journal_epoch,
    batch_id: manifest.batch_id, child_id: childId, workspace_id: manifest.workspace_id, payload_hash: manifest.payload_hash,
    reviewed_at: manifest.reviewed_at, parent_item_count: manifest.items.length, items };
  const child_hash = await digest(payload);
  for (const item of items) if (own(proposalEvents, item.item_id)) {
    requireValue(typeof proposalEvents[item.item_id] === 'string' && /^\$[^\s]{1,255}$/.test(proposalEvents[item.item_id]), 'invalid-proposal-event');
    item.proposal_event_id = proposalEvents[item.item_id];
  }
  return freeze({ ...payload, phase: 'submit', child_hash });
}
export function crmMessageKey(record) {
  return canonicalCRMJSON([record.owner_mxid, record.generation, record.conversation_id, record.source_id]);
}
// `trusted` is an adapter assertion from authenticated source records, never a
// field to copy from arbitrary event content. source_order is a normalized,
// lossless per-conversation integer cursor; master arrival order is unsuitable.
// Re-run with the full ledger when missing records arrive; pass complete:false
// while history is incomplete. Retained old links carry reconciling:true and
// must be displayed as reconciling, not newly verified replies. Persist links
// and corrections with that ledger cursor atomically in the owning worker.
export function attributeResponses(records, { previousLinks = [], complete = true } = {}) {
  requireValue(Array.isArray(records) && records.length <= 100000, 'ledger-too-large');
  const unique = new Map(), ambiguous = new Set(), pending = [], groups = new Map(), blockedScopes = new Set();
  const scopeKey = r => canonicalCRMJSON([r.owner_mxid, r.generation, r.conversation_id]);
  for (const r of records) {
    if (!plain(r) || r.trusted !== true || !boundedIdentity(r.owner_mxid, MXID) || !generation(r.generation) || !text(r.conversation_id, 256)) continue;
    if (!['text', 'media'].includes(r.kind) || r.edit === true || r.self_echo === true || !['incoming', 'outgoing'].includes(r.direction)) continue;
    if (!text(r.source_id, 512)) { blockedScopes.add(scopeKey(r)); continue; }
    const key = crmMessageKey(r);
    if (!revision(r.source_order) || !Number.isSafeInteger(r.source_ts) || r.source_ts < 0) {
      pending.push(key); blockedScopes.add(scopeKey(r)); continue;
    }
    const normalized = { owner_mxid: r.owner_mxid, generation: r.generation, conversation_id: r.conversation_id, source_id: r.source_id, source_order: r.source_order, source_ts: r.source_ts, direction: r.direction, kind: r.kind, confirmed: r.confirmed === true, outreach: r.outreach === true };
    if (unique.has(key) && canonicalCRMJSON(unique.get(key)) !== canonicalCRMJSON(normalized)) {
      ambiguous.add(key); blockedScopes.add(scopeKey(r));
    }
    else unique.set(key, normalized);
  }
  for (const [key, record] of unique) {
    if (ambiguous.has(key)) { pending.push(key); continue; }
    const scope = scopeKey(record);
    if (!groups.has(scope)) groups.set(scope, []); groups.get(scope).push(record);
  }
  const links = [];
  for (const [scope, group] of groups) {
    group.sort((a, b) => a.source_order - b.source_order || compare(a.source_id, b.source_id));
    const cursors = new Map(); for (const r of group) cursors.set(r.source_order, (cursors.get(r.source_order) || 0) + 1);
    const timeInversion = group.some((record, index) => index > 0 && group[index - 1].source_ts > record.source_ts);
    if (!complete || blockedScopes.has(scope) || timeInversion || [...cursors.values()].some(count => count > 1)) {
      blockedScopes.add(scope); pending.push(...group.map(crmMessageKey)); continue;
    }
    const unanswered = [];
    for (const r of group) {
      if (r.direction === 'outgoing') { if (r.confirmed && r.outreach) unanswered.push(r); continue; }
      const index = unanswered.length - 1;
      if (index < 0) continue;
      // A contradictory newest candidate is uncertainty, not permission to
      // skip it and answer an older outreach. Wait for source reconciliation.
      if (unanswered[index].source_ts > r.source_ts) { pending.push(crmMessageKey(r)); continue; }
      const outgoing = unanswered.splice(index, 1)[0];
      links.push({ rule_version: 1, owner_mxid: r.owner_mxid, generation: r.generation, conversation_id: r.conversation_id, incoming_id: r.source_id, outgoing_id: outgoing.source_id, incoming_order: r.source_order, outgoing_order: outgoing.source_order });
    }
  }
  const linkKey = l => canonicalCRMJSON([l.owner_mxid, l.generation, l.conversation_id, l.incoming_id]);
  // A partial ledger cannot revoke previously established links. Keep them as
  // last-known results, accompanied by pending keys, until a complete rebuild.
  const pendingKeys = new Set(pending);
  for (const old of previousLinks) {
    if (!complete || blockedScopes.has(scopeKey(old)) || pendingKeys.has(linkKey(old))) {
      if (!links.some(next => linkKey(next) === linkKey(old))) links.push({ ...copy(old), reconciling: true });
      pending.push(linkKey(old));
    }
  }
  links.sort((a, b) => compare(linkKey(a), linkKey(b)));
  const before = new Map(previousLinks.map(l => [linkKey(l), l])), after = new Map(links.map(l => [linkKey(l), l]));
  const corrections = [];
  for (const key of new Set([...before.keys(), ...after.keys()])) {
    const old = before.get(key), next = after.get(key);
    if (old && (!next || old.outgoing_id !== next.outgoing_id)) corrections.push({ incoming_key: key, previous_outgoing_id: old.outgoing_id, outgoing_id: next?.outgoing_id ?? null, rule_version: 1 });
  }
  corrections.sort((a, b) => compare(a.incoming_key, b.incoming_key));
  return freeze({ links, pending: [...new Set(pending)].sort(), corrections, reconciling_scopes: [...blockedScopes].sort() });
}
