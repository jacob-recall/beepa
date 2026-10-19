// Connect canonical list membership to the source's ordered Direct protocol.
// Native destinations and consent are resolved by the source agent, never here.
import { canonicalCRMJSON, membershipKey } from '../../shared/model/crm.js';
import { readCRMAccount } from './crm-account.js';

const enc = encodeURIComponent;
const equal = (a, b) => canonicalCRMJSON(a) === canonicalCRMJSON(b);
const sameAuthority = (a, b) => ['generation', 'journal_epoch', 'proposal_room_id'].every(k => a?.[k] === b?.[k]);
const fail = text => { throw new Error(text); };
const freeze = value => {
  if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value); }
  return value;
};

export function createCRMMembers({ store, api, managerMxid, accountFor, contacts,
  isCurrent, uuid = () => crypto.randomUUID(), wait = ms => new Promise(r => setTimeout(r, ms)), polls = 40 }) {
  const check = () => { if (!isCurrent()) fail('CRM session changed'); };
  const snapshot = () => { check(); const s = store.snapshot(); if (!s.ready) fail('Contact lists are still loading'); return s.state; };
  const operation = (type, entity_id, base_revision, payload) => ({ schema: 1, op_id: uuid(), workspace_id: snapshot().workspace_id, type, entity_id, base_revision, payload });
  function currentContact(binding) {
    return contacts().find(c => c.owner_mxid === binding.owner_mxid && c.source === binding.contact.source
      && c.network_id === binding.contact.network_id && c.contact_version === binding.contact_version);
  }
  function currentAccount(binding, cancel = false) {
    const account = accountFor(binding.owner_mxid);
    if (!account || account.generation !== binding.generation || (!cancel && !currentContact(binding))) fail('Contact or sending account changed. Refresh and select it again.');
    return account;
  }
  async function freshRoom(binding, cancel = false) {
    check();
    const account = currentAccount(binding, cancel);
    const room = await readCRMAccount({ api, account, ownerMxid: binding.owner_mxid, managerMxid, isCurrent });
    if (cancel && room.capability.direct_cancel !== true) fail('Sending account is not ready for CRM cancellation');
    // A snapshot may change while the point-read is in flight.
    if (!equal(account, currentAccount(binding, cancel))) fail('Sending account changed');
    return room;
  }
  function readResult(state, command, owner, account) {
    const matches = state.filter(e => e.type === 'com.jkali.crm.direct_result' && e.state_key === command);
    if (!matches.length) return null;
    const event = matches[0], result = event.content;
    if (matches.length !== 1 || event.sender !== owner || result?.schema !== 1 || result.command_id !== command
        || result.owner_mxid !== owner || result.generation !== account.generation
        || result.journal_epoch !== account.journal_epoch) fail('Permission result belongs to a different account or session');
    return result;
  }
  async function request(plan, phase, ticketId) {
    let room = await freshRoom(plan.binding, phase === 'cancel');
    const sameAccount = () => sameAuthority(plan.account, room.account);
    if (!sameAccount()) fail('Sending account changed. Recovery is required for its earlier Direct setup.');
    let result = readResult(room.state, plan.member.op_id, plan.binding.owner_mxid, room.account);
    const terminal = r => r && (phase === 'cancel' ? r.phase === 'cancel' && r.safe_to_change_membership === true
      : r.phase === 'cancel' || (phase === 'prepare'
        ? ['prepared', 'approval_required', 'applied', 'partial', 'rejected', 'canceled'].includes(r.state)
        : ['applied', 'partial', 'rejected', 'approval_required', 'canceled'].includes(r.state)));
    if (terminal(result)) return result;
    const content = { schema: 1, command_id: plan.member.op_id, phase,
      owner_mxid: plan.binding.owner_mxid, generation: plan.binding.generation,
      journal_epoch: plan.account.journal_epoch, binding_id: plan.binding.binding_id,
      contact: plan.binding.contact, contact_version: plan.binding.contact_version,
      membership_operation_id: plan.member.op_id, ...(phase === 'commit' ? { ticket_id: ticketId } : {}) };
    // Room verification yields to sync. Re-read canonical membership after it
    // before authorizing a grant; removal or archival must invalidate the plan.
    if (phase === 'commit') {
      await store.refresh(); check(); assertMembership(plan, false);
      if (!sameAuthority(plan.account, currentAccount(plan.binding))) fail('Sending account changed');
    }
    await api('PUT', '/_matrix/client/v3/rooms/' + enc(plan.account.proposal_room_id)
      + '/send/com.jkali.crm.direct_request/' + enc('crm_direct_' + plan.member.op_id + '_' + phase), content);
    check();
    for (let n = 0; n < polls; n++) {
      room = await freshRoom(plan.binding, phase === 'cancel');
      if (!sameAccount()) fail('Sending account changed. Recovery is required for its earlier Direct setup.');
      result = readResult(room.state, plan.member.op_id, plan.binding.owner_mxid, room.account);
      if (terminal(result)) return result;
      if (n + 1 < polls) await wait(500);
      check();
    }
    fail(phase === 'cancel' ? 'Earlier Direct setup is still being reconciled. The list assignment has not changed. Retry when the account is connected.'
      : 'Direct setup is awaiting the user’s sync service. Retry to check the same request.');
  }
  async function append(op) {
    check(); const result = await store.append(op); check();
    if (!result.accepted) fail('List change refused: ' + result.code);
  }
  function assertMembership(plan, before, checkBinding = true) {
    const state = snapshot(), list = state.lists[plan.member.payload.list_id];
    if (!list || list.archived || list.revision !== plan.listRevision) fail('List changed. Review the current list before adding this contact.');
    const member = state.memberships[membershipKey(list.list_id, plan.binding.person_id)];
    if (before) {
      if (!(member && member.operation_id === plan.member.op_id && !member.removed)
        && (member?.revision || 0) !== plan.member.base_revision) fail('This person’s list assignment changed. Refresh before continuing.');
    } else if (!member || member.removed || member.operation_id !== plan.member.op_id
      || member.binding_id !== plan.binding.binding_id) fail('This person’s list assignment changed. Direct setup was not continued.');
    const binding = state.bindings[plan.binding.binding_id];
    if (checkBinding && binding && (!equal(binding.contact, plan.binding.contact) || binding.contact_version !== plan.binding.contact_version
      || binding.generation !== plan.binding.generation || binding.availability !== 'shared')) fail('Contact binding changed');
  }
  function planContact({ listId, contact, personId = null }, renewing = null) {
    const state = snapshot(), list = state.lists[listId], account = accountFor(contact.owner_mxid);
    if (!list || list.archived) fail('Choose an active list');
    if (!account || !contact.contact_version) fail('This account needs the CRM sync update before contacts can be added');
    const identity = { source: contact.source, network_id: contact.network_id };
    if (!currentContact({ owner_mxid: contact.owner_mxid, contact: identity,
      contact_version: contact.contact_version })) fail('Contact is no longer shared. Refresh and select it again.');
    let binding = Object.values(state.bindings).find(b => b.owner_mxid === contact.owner_mxid
      && b.generation === account.generation && equal(b.contact, identity));
    if (binding && personId && binding.person_id !== personId) fail('This account contact is already linked to another person');
    if (personId && !state.persons[personId]) fail('Choose an existing person or create a new one');
    personId ||= binding?.person_id || uuid();
    if (personId && !state.persons[personId] && binding) fail('Person no longer exists');
    const setup = [];
    if (!state.persons[personId]) setup.push(operation('person.create', personId, 0,
      { label: (contact.person_display || contact.display_name || contact.network_id).slice(0, 160) }));
    if (!binding) {
      binding = { binding_id: uuid(), person_id: personId, owner_mxid: contact.owner_mxid,
        generation: account.generation, contact: identity, contact_version: contact.contact_version, availability: 'shared' };
      const { binding_id, availability, ...payload } = binding;
      setup.push(operation('binding.create', binding_id, 0, payload));
    } else if (binding.contact_version !== contact.contact_version || binding.availability !== 'shared') {
      setup.push(operation('binding.update', binding.binding_id, binding.revision,
        { contact_version: contact.contact_version, availability: 'shared' }));
      binding = { ...binding, contact_version: contact.contact_version, availability: 'shared' };
    }
    const old = state.memberships[membershipKey(listId, personId)];
    if (renewing) {
      // Explicit renewal replaces this exact membership. A stale dialog must
      // not follow a reassigned sender or revive a removed member.
      if (!old || old.removed || !equal(old, renewing) || old.binding_id !== binding.binding_id)
        fail('This person’s list assignment changed. Refresh before enabling Direct again.');
    } else if (old && !old.removed && old.binding_id === binding.binding_id) fail('This person is already on the list through this account');
    const member = operation('member.set', old?.membership_id || uuid(), old?.revision || 0,
      { list_id: listId, person_id: personId, binding_id: binding.binding_id });
    member.payload.direct_request = { schema: 1, command_id: member.op_id,
      owner_mxid: binding.owner_mxid, generation: binding.generation, journal_epoch: account.journal_epoch,
      binding_id: binding.binding_id, contact: binding.contact, contact_version: binding.contact_version,
      membership_operation_id: member.op_id, proposal_room_id: account.proposal_room_id };
    // Freeze caller input now. Retries cannot mutate the reviewed contact, source
    // version, command ID or expected membership revision.
    return freeze(JSON.parse(canonicalCRMJSON({ setup, member, binding, account, listRevision: list.revision,
      previous: old && !old.removed ? old : null })));
  }
  async function planAddition(input) {
    check(); await store.refresh(); check();
    return planContact(input);
  }
  async function planRenewal(member) {
    // Planning is read-only. The caller retains this immutable plan before
    // execute(), so a lost acknowledgment retries one command, not a new grant.
    check();
    const reviewed = JSON.parse(canonicalCRMJSON(member));
    await store.refresh(); check();
    const state = snapshot(), current = state.memberships[membershipKey(reviewed.list_id, reviewed.person_id)];
    if (!current || current.removed || !equal(current, reviewed))
      fail('This person’s list assignment changed. Refresh before enabling Direct again.');
    const binding = state.bindings[current.binding_id], account = binding && accountFor(binding.owner_mxid);
    if (!binding || !account || account.generation !== binding.generation)
      fail('Sending account changed. Review this contact’s sender before enabling Direct again.');
    const contact = contacts().find(c => c.owner_mxid === binding.owner_mxid
      && c.source === binding.contact.source && c.network_id === binding.contact.network_id);
    if (!contact) fail('Contact is no longer shared. Refresh and select it again.');
    return planContact({ listId: current.list_id, personId: current.person_id, contact }, reviewed);
  }
  async function execute(plan) {
    check(); await store.refresh(); check(); assertMembership(plan, true, false);
    if (!sameAuthority(plan.account, currentAccount(plan.binding))) fail('Sending account changed');
    if (plan.previous) await cancelMember(plan.previous);
    for (const op of plan.setup) {
      currentAccount(plan.binding);
      await append(op);
    }
    assertMembership(plan, true);
    const prepared = await request(plan, 'prepare');
    if (prepared.phase === 'cancel' || !['prepared', 'approval_required', 'applied', 'partial'].includes(prepared.state)) fail('Direct setup refused: ' + (prepared.reason || prepared.state));
    await store.refresh(); check(); assertMembership(plan, true);
    await append(plan.member);
    assertMembership(plan, false);
    if (prepared.state === 'approval_required') return prepared;
    if (prepared.state === 'applied' || prepared.state === 'partial') return prepared;
    if (typeof prepared.ticket_id !== 'string' || !/^[a-f0-9]{64}$/.test(prepared.ticket_id)) fail('Invalid Direct setup ticket');
    const committed = await request(plan, 'commit', prepared.ticket_id);
    if (committed.phase === 'cancel' || !['applied', 'partial'].includes(committed.state)) fail('Direct setup refused: ' + (committed.reason || committed.state));
    return committed;
  }
  async function resume(member) {
    const state = snapshot();
    const saved = state.operations[member.operation_id];
    if (!saved?.outcome.accepted) fail('Membership is not confirmed');
    const memberOp = JSON.parse(saved.serialized);
    return execute({ ...memberPlan(member), setup: [], member: memberOp, listRevision: state.lists[member.list_id].revision });
  }
  function memberPlan(member) {
    const state = snapshot(), current = state.bindings[member.binding_id], saved = member.direct_request;
    if (!current) fail('Contact binding is unavailable');
    const binding = saved ? { ...current, owner_mxid: saved.owner_mxid, generation: saved.generation,
      contact: saved.contact, contact_version: saved.contact_version } : current;
    const account = saved ? { generation: saved.generation, journal_epoch: saved.journal_epoch, proposal_room_id: saved.proposal_room_id }
      : currentAccount(binding, true);
    return { binding, account, member: { op_id: member.operation_id } };
  }
  async function cancelMember(member) {
    const result = await request(memberPlan(member), 'cancel');
    if (result.safe_to_change_membership !== true) fail('Earlier Direct setup has not finished reconciling');
  }
  async function applyChange(op) {
    check(); await store.refresh(); check();
    let state = snapshot();
    if (state.operations[op.op_id]) return store.append(op);
    if (op.type === 'member.remove') {
      const member = state.memberships[membershipKey(op.payload.list_id, op.payload.person_id)];
      if (!member || member.revision !== op.base_revision) fail('List assignment changed. Refresh before continuing.');
      await cancelMember(member);
    } else if (op.type === 'list.archive') {
      if (state.lists[op.entity_id]?.revision !== op.base_revision) fail('List changed. Refresh before archiving.');
      const members = Object.values(state.memberships).filter(m => m.list_id === op.entity_id && !m.removed);
      if (!equal(members.map(m => m.operation_id).sort(), op.payload.membership_operations)) fail('List membership changed. Review the list before archiving.');
      for (const member of members) await cancelMember(member);
    } else fail('Unsupported membership change');
    check();
    return store.append(op);
  }
  async function validate(plan) {
    await store.refresh(); check(); assertMembership(plan, true, false);
    const room = await freshRoom(plan.binding);
    if (!sameAuthority(plan.account, room.account)) fail('Sending account changed');
  }
  return { planAddition, planRenewal, execute, resume, applyChange, validate };
}
