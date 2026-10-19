// A durable reviewed parent and one source-owned child per sending account.
// Staging writes existing proposals; only the source's accepted child queue
// can dispatch them through the existing proposal/approval paths.
import { canonicalCRMJSON, crmDigest, createReviewedBatch, createCRMChildEnvelope, membershipKey } from '../../shared/model/crm.js';
import { readCRMAccount } from './crm-account.js';
import { sanitize } from '../../shared/ui/el.js';

const enc = encodeURIComponent;
const same = (a, b) => canonicalCRMJSON(a) === canonicalCRMJSON(b);
const fail = message => { throw new Error(message); };
const EVENT = /^\$[^\s]{1,255}$/;
const ROOM = /^![^\s:]+:[^\s]+$/;
const ITEM_STATES = new Set(['accepted', 'waiting_for_approval', 'review_required', 'approval_pending',
  'attempted', 'sent', 'outcome_unknown', 'refused', 'declined', 'canceled']);
const ITEM = 'com.jkali.crm.item';

export function createCRMBatches({ store, api, managerMxid, accountFor, contacts, roomFor,
  isCurrent, uuid = () => crypto.randomUUID(), now = Date.now }) {
  const check = () => { if (!isCurrent()) fail('CRM session changed'); };
  const snapshot = () => { check(); const s = store.snapshot(); if (!s.ready) fail('Contact lists are still loading'); return s.state; };
  function current(binding, epoch) {
    const account = accountFor(binding.owner_mxid);
    if (!account || account.generation !== binding.generation || account.journal_epoch !== epoch
      || !contacts().some(c => c.owner_mxid === binding.owner_mxid && c.source === binding.contact.source
        && c.network_id === binding.contact.network_id && c.contact_version === binding.contact_version)) fail('Contact or sending account changed. Review again.');
    return account;
  }
  async function accountRoom(binding, epoch) {
    const account = current(binding, epoch);
    const room = await readCRMAccount({ api, account, ownerMxid: binding.owner_mxid, managerMxid, isCurrent });
    check();
    if (room.capability.batch !== true || room.capability.direct_cancel !== true
      || room.capability.journal_epoch !== epoch || room.account.proposal_room_id !== current(binding, epoch).proposal_room_id) fail('This sending account needs the batch sync update.');
    return room;
  }
  function memberState(listId, personId) {
    const state = snapshot(), list = state.lists[listId];
    const member = state.memberships[membershipKey(listId, personId)], binding = member && state.bindings[member.binding_id];
    if (!list || list.archived || !member || member.removed || !binding || binding.availability !== 'shared'
      || !member.direct_request) fail('This list assignment is unavailable. Refresh before reviewing.');
    return { member, binding };
  }
  async function refreshBinding(listId, personId) {
    const before = memberState(listId, personId), binding = before.binding;
    const matches = contacts().filter(c => c.owner_mxid === binding.owner_mxid && c.source === binding.contact.source
      && c.network_id === binding.contact.network_id);
    if (matches.length !== 1 || !matches[0].contact_version) fail('Contact is no longer uniquely shared. Refresh before reviewing.');
    const version = matches[0].contact_version;
    if (version === binding.contact_version) return;
    const operationId = 'refresh-' + await crmDigest([snapshot().workspace_id, binding.binding_id, binding.revision, version]);
    check();
    // Catalog versions change even when this exact contact does not. Refresh
    // metadata only; never replay its historical Direct permission grant.
    const updated = { ...binding, contact_version: version };
    await accountRoom(updated, before.member.direct_request.journal_epoch);
    await store.refresh(); check();
    const latest = memberState(listId, personId);
    if (!same(before.member, latest.member) || !same(binding, latest.binding)) fail('Contact assignment changed while refreshing. Try again.');
    current(updated, before.member.direct_request.journal_epoch);
    const result = await store.append({ schema: 1, op_id: operationId,
      workspace_id: snapshot().workspace_id, type: 'binding.update', entity_id: binding.binding_id, base_revision: binding.revision,
      payload: { contact_version: version, availability: 'shared' } }); check();
    if (!result.accepted) fail('Contact refresh conflicted with another change. Refresh and try again.');
  }
  async function routes(listId, personId) {
    await refreshBinding(listId, personId);
    const { member, binding } = memberState(listId, personId), grant = member.direct_request;
    const room = await accountRoom(binding, grant.journal_epoch);
    const currentMember = memberState(listId, personId);
    if (!same(member, currentMember.member) || !same(binding, currentMember.binding)) fail('List assignment changed. Review again.');
    const results = room.state.filter(e => e.type === 'com.jkali.crm.direct_result' && e.state_key === member.operation_id);
    const event = results[0], result = event?.content;
    if (results.length !== 1 || event.sender !== binding.owner_mxid || result?.schema !== 1
      || result.command_id !== member.operation_id || result.owner_mxid !== binding.owner_mxid
      || result.generation !== binding.generation || result.journal_epoch !== grant.journal_epoch
      || result.phase === 'cancel') fail('Direct setup is not confirmed. Check it before reviewing this contact.');
    const base = { source: binding.contact.source, network_id: binding.contact.network_id };
    if (result.state === 'approval_required') {
      // The current source approval dispatcher supports identifier-only
      // iMessage sends. Other providers need an existing bridged conversation.
      if (base.source !== 'imessage') fail('Starting a new ' + base.source + ' conversation from CRM is not supported. Start it in the user account, then refresh this list.');
      return [{ label: 'New conversation · user approval required',
        route: { ...base, mode: 'request_new_conversation_approval', consent_revision: 0 } }];
    }
    const choices = (result.rooms || []).filter(r => r.state === 'applied' && Number.isSafeInteger(r.revision) && r.revision >= 0)
      .map(r => ({ result: r, room: roomFor(binding, r.room_id) }))
      .filter(r => r.room && r.room.sourceId === base.source && r.room.direct === true)
      .map(r => ({ label: r.room.label, route: { ...base, mode: 'direct_existing', room_id: r.result.room_id, consent_revision: r.result.revision } }));
    if (!choices.length) fail('No confirmed Direct conversation is visible yet. Refresh after the account syncs.');
    return choices;
  }
  async function review({ listId, items }) {
    check(); await store.refresh(); check();
    for (const item of items) await refreshBinding(listId, item.personId);
    const state = snapshot(), prepared = [];
    for (const item of items) {
      const body = typeof item.body === 'string' ? item.body.trim() : '';
      // Reuse the existing visible-text sanitizer without its 4k display clamp.
      // Refuse changes here so the review always shows exactly what will send.
      if (!body || body.length > 8000 || sanitize(body.slice(0, 4000)) + sanitize(body.slice(4000)) !== body || body.startsWith('!')) fail('Enter a message of up to 8,000 characters without hidden controls or a leading bridge command.');
      if (/\{\{[^{}]+\}\}/.test(body)) fail('Fill every {{placeholder}} before reviewing the batch.');
      const choices = await routes(listId, item.personId);
      const choice = item.route ? choices.find(c => same(c.route, item.route)) : choices.length === 1 ? choices[0] : null;
      if (!choice) fail('Choose the conversation for every selected contact.');
      const { member, binding } = memberState(listId, item.personId);
      prepared.push({ item_id: uuid(), person_id: item.personId, proposal_room_id: member.direct_request.proposal_room_id,
        journal_epoch: member.direct_request.journal_epoch, route: choice.route, body });
      current(binding, member.direct_request.journal_epoch);
    }
    // Replayed metadata, not a pre-await snapshot, decides the final review.
    await store.refresh(); check();
    if (snapshot() !== state) {
      for (const item of items) {
        const before = state.memberships[membershipKey(listId, item.personId)], after = memberState(listId, item.personId);
        if (!same(before, after.member) || !same(state.bindings[before.binding_id], after.binding)) fail('List assignment changed while reviewing.');
      }
    }
    return createReviewedBatch(snapshot(), { batch_id: uuid(), list_id: listId, items: prepared, reviewed_at: now() });
  }
  async function stillCurrent(manifest) {
    await store.refresh(); check();
    if (snapshot().batch_cancellations?.[manifest.batch_id]) fail('Cancellation was requested for this batch. Reconcile cancellation instead.');
    const rebuilt = await createReviewedBatch(snapshot(), { batch_id: manifest.batch_id, list_id: manifest.list_id,
      reviewed_at: manifest.reviewed_at, items: manifest.items.map(i => ({ item_id: i.item_id, person_id: i.person_id,
        proposal_room_id: i.proposal_room_id, journal_epoch: manifest.children.find(c => c.child_id === i.child_id).journal_epoch,
        route: i.route, body: i.body })) });
    check();
    if (!same(rebuilt, manifest)) fail('The reviewed list or sender assignments changed. Create a fresh review.');
    if (now() - manifest.reviewed_at > 600000) fail('This review expired. Review the messages again.');
  }
  function annotation(child, item) {
    return { schema: 1, batch_id: child.batch_id, child_id: child.child_id, item_id: item.item_id,
      owner_mxid: child.owner_mxid, generation: child.generation, journal_epoch: child.journal_epoch,
      payload_hash: child.payload_hash, child_hash: child.child_hash };
  }
  function proposal(child, item) {
    return { body: item.body, [ITEM]: annotation(child, item), ...(item.route.mode === 'direct_existing'
      ? { target_room: item.route.room_id } : { target_source: item.route.source, target_identifier: item.route.network_id }) };
  }
  function resultFrom(room, child) {
    const events = room.state.filter(e => e.type === 'com.jkali.crm.batch_result' && e.state_key === child.child_id);
    if (!events.length) return null;
    const event = events[0], c = event.content;
    if (events.length !== 1 || event.sender !== child.owner_mxid || c?.schema !== 1
      || !['batch_id', 'child_id', 'owner_mxid', 'generation', 'journal_epoch', 'child_hash'].every(k => c[k] === child[k])) fail('Batch acknowledgment does not match the reviewed account.');
    if (!['accepted', 'canceled', 'rejected'].includes(c.state) || !Array.isArray(c.items)
      || (c.state === 'accepted' && c.items.length !== child.items.length)
      || (c.state === 'rejected' && (c.items.length || typeof c.reason !== 'string' || !c.reason))
      || c.items.some(i => !i || !ITEM_STATES.has(i.state)
        || (i.reason != null && typeof i.reason !== 'string')
        || (i.state === 'sent' && (!EVENT.test(i.sent_event_id || '') || !ROOM.test(i.sent_room_id || '')))
        || !child.items.some(expected => expected.item_id === i.item_id))
      || new Set(c.items.map(i => i.item_id)).size !== c.items.length) fail('Batch acknowledgment is incomplete.');
    return c;
  }
  async function staged(room, child) {
    let cursor = '', seen = new Set();
    const found = new Map();
    for (let page = 0; page < 200; page++) {
      const data = await api('GET', room.path + '/messages?dir=f&limit=200&filter=' + enc(JSON.stringify({ types: ['com.jkali.proposal'] }))
        + (cursor ? '&from=' + enc(cursor) : '')); check();
      if (!Array.isArray(data?.chunk)) fail('Could not reconcile staged messages.');
      for (const e of data.chunk) {
        if (e.sender !== managerMxid || e.type !== 'com.jkali.proposal' || Object.hasOwn(e, 'state_key') || !EVENT.test(e.event_id || '')
          || (e.room_id !== undefined && e.room_id !== child.items[0].proposal_room_id)) continue;
        const item = child.items.find(i => i.item_id === e.content?.[ITEM]?.item_id);
        if (item && same(e.content, proposal(child, item)) && !found.has(item.item_id)) found.set(item.item_id, e.event_id);
      }
      if (!data.end || data.end === cursor) {
        if (data.chunk.length) fail('Staged message history is incomplete.');
        return found;
      }
      if (seen.has(data.end)) fail('Staged message history did not advance.');
      seen.add(data.end); cursor = data.end;
    }
    fail('Staged message history is still loading. Retry to continue.');
  }
  async function submit(manifest) {
    check();
    // Detach caller input before the first await. The saved parent, every
    // staged proposal and every retry must describe the same reviewed text.
    manifest = JSON.parse(canonicalCRMJSON(manifest));
    const outcome = await store.append({ schema: 1, op_id: 'review-' + manifest.batch_id, workspace_id: manifest.workspace_id,
      type: 'batch.review', entity_id: manifest.batch_id, base_revision: 0, payload: manifest }); check();
    if (!outcome.accepted) fail('Review could not be saved: ' + outcome.code);
    if (snapshot().batch_cancellations?.[manifest.batch_id]) return cancel(manifest);
    const results = [];
    // Children are independent: an offline account must not hide another
    // account's accepted queue. Retry always reconciles each exact child first.
    for (const group of manifest.children) {
      try {
        const child = await createCRMChildEnvelope(manifest, group.child_id); check();
        const binding = snapshot().bindings[child.items[0].binding_id];
        let room = await accountRoom(binding, child.journal_epoch);
        const prior = resultFrom(room, child);
        if (prior) { results.push(prior); continue; }
        await stillCurrent(manifest);
        const found = await staged(room, child);
        for (const item of child.items) {
          if (found.has(item.item_id)) continue;
          await stillCurrent(manifest);
          room = await accountRoom(snapshot().bindings[item.binding_id], child.journal_epoch);
          await stillCurrent(manifest);
          current(snapshot().bindings[item.binding_id], child.journal_epoch);
          const sent = await api('PUT', '/_matrix/client/v3/rooms/' + enc(group.proposal_room_id)
            + '/send/com.jkali.proposal/' + enc('crm_proposal_' + group.child_id + '_' + item.item_id), proposal(child, item)); check();
          if (!EVENT.test(sent?.event_id || '')) fail('Staged message acknowledgment is missing. Reconcile before retrying.');
          found.set(item.item_id, sent.event_id);
        }
        await stillCurrent(manifest);
        room = await accountRoom(snapshot().bindings[child.items[0].binding_id], child.journal_epoch);
        const accepted = resultFrom(room, child);
        if (accepted) { results.push(accepted); continue; }
        await stillCurrent(manifest);
        for (const item of child.items) current(snapshot().bindings[item.binding_id], child.journal_epoch);
        if (new Set(child.items.map(i => found.get(i.item_id))).size !== child.items.length) fail('Staged message IDs conflict. Reconcile before submitting this account.');
        const content = { ...child, items: child.items.map(i => ({ ...i, proposal_event_id: found.get(i.item_id) })) };
        await api('PUT', '/_matrix/client/v3/rooms/' + enc(group.proposal_room_id)
          + '/send/com.jkali.crm.batch_request/' + enc('crm_batch_' + group.child_id + '_submit'), content); check();
        room = await accountRoom(snapshot().bindings[child.items[0].binding_id], child.journal_epoch);
        results.push(resultFrom(room, child) || { child_id: group.child_id, state: 'awaiting_acknowledgment', items: [] });
      } catch (error) { check(); results.push({ child_id: group.child_id, state: 'not_confirmed', reason: error.message, items: [] }); }
    }
    return results;
  }
  async function cancel(manifest) {
    check(); manifest = JSON.parse(canonicalCRMJSON(manifest));
    await store.refresh(); check();
    if (!same(snapshot().batches[manifest.batch_id], manifest)) fail('Cancellation must match the saved batch review.');
    const intent = await store.append({ schema: 1, op_id: 'cancel-' + manifest.batch_id, workspace_id: manifest.workspace_id,
      type: 'batch.cancel_requested', entity_id: manifest.batch_id, base_revision: 0, payload: { payload_hash: manifest.payload_hash } }); check();
    if (!intent.accepted) fail('Cancellation could not be saved: ' + intent.code);
    const results = [];
    for (const group of manifest.children) {
      try {
        const child = await createCRMChildEnvelope(manifest, group.child_id); check();
        // Use the original account envelope even if the shared contact was
        // revoked or its list archived. Cancellation grants no send authority.
        const read = () => readCRMAccount({ api, account: group, ownerMxid: group.owner_mxid, managerMxid, isCurrent });
        let room = await read(); check();
        const prior = resultFrom(room, child);
        if (['canceled', 'rejected'].includes(prior?.state)) { results.push(prior); continue; }
        if (room.capability.batch !== true && room.capability.batch_cancel !== true) fail('This account cannot acknowledge batch cancellation yet.');
        const content = { schema: 1, phase: 'cancel', owner_mxid: child.owner_mxid, generation: child.generation,
          journal_epoch: child.journal_epoch, batch_id: child.batch_id, child_id: child.child_id, child_hash: child.child_hash };
        await api('PUT', '/_matrix/client/v3/rooms/' + enc(group.proposal_room_id)
          + '/send/com.jkali.crm.batch_request/' + enc('crm_batch_' + group.child_id + '_cancel'), content); check();
        room = await read(); check();
        const result = resultFrom(room, child);
        results.push(['canceled', 'rejected'].includes(result?.state) ? result : { child_id: child.child_id, state: 'cancellation_requested',
          reason: 'Waiting for the sending account to confirm. Messages already attempted remain visible.', items: result?.items || [] });
      } catch (error) { check(); results.push({ child_id: group.child_id, state: 'cancellation_unconfirmed', reason: error.message, items: [] }); }
    }
    return results;
  }
  async function statuses(manifest, reads = new Map()) {
    manifest = JSON.parse(canonicalCRMJSON(manifest));
    const results = [];
    for (const group of manifest.children) {
      try {
        const child = await createCRMChildEnvelope(manifest, group.child_id); check();
        const key = sameAccountKey(group);
        if (!reads.has(key)) reads.set(key, readCRMAccount({ api, account: group, ownerMxid: group.owner_mxid, managerMxid, isCurrent }));
        const room = await reads.get(key); check();
        results.push(resultFrom(room, child) || { child_id: group.child_id, state: 'awaiting_acknowledgment', items: [] });
      } catch (error) { check(); results.push({ child_id: group.child_id, state: 'unavailable', reason: error.message, items: [] }); }
    }
    return results;
  }
  function sameAccountKey(group) {
    return canonicalCRMJSON([group.owner_mxid, group.generation, group.journal_epoch, group.proposal_room_id]);
  }
  async function validate(manifest) {
    await stillCurrent(manifest);
    for (const item of manifest.items) {
      const { binding } = memberState(manifest.list_id, item.person_id);
      const child = manifest.children.find(c => c.child_id === item.child_id);
      await accountRoom(binding, child.journal_epoch);
    }
  }
  return { routes, review, submit, cancel, statuses, validate };
}
