import assert from 'node:assert/strict';
import { createCRMView } from '../../apps/master/crm-view.js';
import { createCRMState, applyCRMOperation } from '../../shared/model/crm.js';

// Minimal DOM fixture: text-only construction, native control event boundaries,
// and bubbling. Browser layout remains a separate acceptance gate.
class Element {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.attributes = {}; this.listeners = {}; this.parentNode = null; this._text = ''; this.className = ''; this.disabled = false; this.value = ''; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set innerHTML(_) { throw new Error('HTML injection sink used'); }
  append(...children) { for (const child of children) { child.parentNode = this; this.children.push(child); } }
  appendChild(child) { this.append(child); return child; }
  replaceChildren(...children) { this._text = ''; for (const child of this.children) child.parentNode = null; this.children = []; this.append(...children); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  closest(selectors) { const tags = selectors.split(',').map(s => s.trim().toUpperCase()); for (let node = this; node; node = node.parentNode) if (tags.includes(node.tagName)) return node; return null; }
  contains(node) { for (; node; node = node.parentNode) if (node === this) return true; return false; }
  fire(type, extra = {}) {
    if (type === 'click' && this.disabled) return;
    const event = { type, target: this, currentTarget: this, defaultPrevented: false, stopped: false,
      preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; }, ...extra };
    for (let node = this; node; node = node.parentNode) {
      event.currentTarget = node; for (const handler of node.listeners[type] || []) handler(event);
      if (event.stopped) break;
    }
    return event;
  }
}
const oldDocument = globalThis.document;
globalThis.document = { createElement: tag => new Element(tag) };
const flush = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const ROOM = '!crm:master';
function fixture({ seeded = false, membershipService = false, batchService = false } = {}) {
  let sequence = 0;
  const h = { state: createCRMState({ workspace_id: ROOM, manager_mxid: '@manager:master' }), ready: true, visible: true, calls: [], details: 0, opened: [], appendHook: null, refreshHook: null };
  h.op = (type, entity_id, payload, base_revision = 0) => ({ schema: 1, workspace_id: ROOM, op_id: 'seed-' + ++sequence, type, entity_id, base_revision, payload });
  h.apply = operation => { const result = applyCRMOperation(h.state, operation); h.state = result.state; return result.outcome; };
  if (seeded) {
    for (const operation of [h.op('person.create', 'maya', { label: '<img src=x onerror=evil()>' }),
      h.op('binding.create', 'binding', { person_id: 'maya', owner_mxid: '@elliot:master', generation: 'g', contact: { source: 'imessage', network_id: 'maya@example.com' }, contact_version: 'v1' }),
      h.op('list.create', 'founders', { name: 'Founders' }), h.op('member.set', 'membership', { list_id: 'founders', person_id: 'maya', binding_id: 'binding' })]) assert.equal(h.apply(operation).accepted, true);
  }
  h.store = { snapshot: () => ({ state: h.state, ready: h.ready, roomId: ROOM }),
    refresh: async () => h.refreshHook ? h.refreshHook() : h.store.snapshot(),
    append: async operation => { h.calls.push(operation); return h.appendHook ? h.appendHook(operation) : h.apply(operation); } };
  h.memberCalls = []; h.contacts = [
    { owner_mxid: '@elliot:master', ownerLabel: 'Elliot', source: 'imessage', network_id: 'maya@example.com', display_name: 'Maya', contact_version: 'v1', crmReady: true },
    { owner_mxid: '@david:master', ownerLabel: 'David', source: 'imessage', network_id: 'maya-other@example.com', display_name: 'Maya alternate', contact_version: 'v2', crmReady: true },
    { owner_mxid: '@old:master', ownerLabel: 'Needs update', source: 'imessage', network_id: 'old@example.com', display_name: 'Old account', crmReady: false },
  ];
  h.members = {
    async planRenewal(member) { h.memberCalls.push(['renew', member]); return h.renewHook ? h.renewHook(member) : Object.freeze({ id: 'renewal-plan', member }); },
    async planAddition(args) { h.memberCalls.push(['plan', args]); return h.planHook ? h.planHook(args) : Object.freeze({ id: 'frozen-plan', ...args }); },
    async execute(plan) { h.memberCalls.push(['execute', plan]); return h.executeHook ? h.executeHook(plan) : { state: 'applied' }; },
    async applyChange(op) { h.memberCalls.push(['change', op]); return h.changeHook ? h.changeHook(op) : h.apply(op); },
    async resume(member) { h.memberCalls.push(['resume', member]); return h.resumeHook ? h.resumeHook(member) : { state: 'applied' }; },
  };
  h.batchCalls = []; h.batches = { routes: async (...args) => { h.batchCalls.push(args); return [{ label: 'Existing conversation', route: { mode: 'direct_existing', room_id: '!conversation:local' } }]; } };
  h.list = new Element('div'); h.detail = new Element('div');
  h.view = createCRMView({ getStore: () => h.store, listHost: h.list, detailHost: h.detail, isVisible: () => h.visible,
    showDetail: () => h.details++, openPerson: (...args) => h.opened.push(args),
    ...(membershipService ? { getMembers: () => h.members, getContacts: () => h.contacts } : {}),
    ...(batchService ? { getBatches: () => h.batches } : {}),
    describeBinding: binding => h.bindingHook ? h.bindingHook(binding) : ({ owner: binding.owner_mxid, status: 'Direct', available: true }), uuid: () => 'uuid-' + ++sequence });
  h.nodes = (root = null) => { const result = []; const visit = node => { result.push(node); node.children.forEach(visit); }; if (root) visit(root); else { visit(h.list); visit(h.detail); } return result; };
  h.button = text => { const value = h.nodes().find(n => n.tagName === 'BUTTON' && n.textContent === text); assert.ok(value, 'button exists: ' + text); return value; };
  h.input = label => { const value = h.nodes().find(n => n.tagName === 'INPUT' && n.getAttribute('aria-label') === label); assert.ok(value, 'input exists: ' + label); return value; };
  h.fill = (label, value) => { const input = h.input(label); input.value = value; input.fire('input'); return input; };
  h.create = async name => { h.fill('New list name', name).parentNode.fire('submit'); await flush(); };
  h.click = async text => { h.button(text).fire('click'); await flush(); };
  return h;
}
let failures = 0, passed = 0;
async function test(name, run) { try { await run(); passed++; } catch (error) { failures++; console.error('FAIL master_crm_view:', name, '\n', error.stack); } }
await test('real metadata create rename archive restore workflow', async () => {
  const h = fixture(); await h.view.open(); await h.create('  Launch list  ');
  const list = Object.values(h.state.lists)[0]; assert.equal(list.name, 'Launch list'); assert.equal(h.input('New list name').value, '');
  const rename = h.fill('List name', 'Renamed'); rename.parentNode.fire('submit'); await flush(); assert.equal(h.state.lists[list.list_id].name, 'Renamed');
  await h.click('Archive list'); assert.equal(h.state.lists[list.list_id].archived, true); await h.click('Archived lists');
  await h.click('Restore list'); assert.equal(h.state.lists[list.list_id].archived, false); await h.click('Active lists');
  assert.deepEqual(h.calls.map(o => o.type), ['list.create', 'list.rename', 'list.archive', 'list.restore']);
});
await test('membership remove preserves binding and opens no conversation', async () => {
  const h = fixture({ seeded: true }); await h.view.open(); const binding = h.state.bindings.binding;
  await h.click('Remove'); assert.equal(h.state.memberships['founders/maya'].removed, true); assert.equal(h.state.bindings.binding, binding); assert.equal(h.opened.length, 0);
  assert.equal(h.calls[0].type, 'member.remove'); assert.deepEqual(h.calls[0].payload, { list_id: 'founders', person_id: 'maya' });
});
await test('contact name uses regular conversation callback and text never parses as HTML', async () => {
  const h = fixture({ seeded: true }); await h.view.open(); const name = '<img src=x onerror=evil()>';
  await h.click(name); assert.equal(h.opened.length, 1); assert.equal(h.opened[0][0], h.state.persons.maya); assert.equal(h.opened[0][1], h.state.bindings.binding);
  assert.equal(h.nodes().filter(n => n.tagName === 'IMG').length, 0); assert.ok(h.detail.textContent.includes(name)); assert.equal(h.calls.length, 0);
});
await test('noninteractive contact row opens same regular conversation', async () => {
  const h = fixture({ seeded: true }); await h.view.open();
  const row = h.nodes().find(n => n.tagName === 'TR' && n.children.some(c => c.tagName === 'TD'));
  row.children[1].fire('click'); await flush(); assert.equal(h.opened.length, 1); assert.equal(h.opened[0][1], h.state.bindings.binding);
});
await test('lost response retry preserves exact operation identity and payload', async () => {
  const h = fixture(); await h.view.open(); let lost = true;
  h.appendHook = operation => { const result = h.apply(operation); if (lost) { lost = false; throw new Error('lost response'); } return result; };
  await h.create('One list'); assert.equal(Object.keys(h.state.lists).length, 1); assert.equal(h.button('Create list').disabled, true);
  await h.click('Reconcile saved change'); assert.equal(h.calls.length, 2); assert.equal(h.calls[0], h.calls[1]);
  assert.equal(Object.keys(h.state.lists).length, 1); assert.equal(h.button('Create list').disabled, false); assert.ok(h.detail.textContent.includes('Saved'));
});
await test('refresh reconciles accepted creation and clears original draft', async () => {
  const h = fixture({ seeded: true }); await h.view.open();
  h.appendHook = operation => { h.apply(operation); throw new Error('lost response'); };
  await h.create('Newly created'); await h.click('Refresh');
  assert.equal(h.input('New list name').value, ''); assert.equal(h.input('List name').value, 'Newly created');
});
await test('refresh cannot claim success for different intent under same operation ID', async () => {
  const h = fixture(); await h.view.open();
  h.appendHook = operation => { h.apply({ ...operation, payload: { name: 'Different intent' } }); throw new Error('lost response'); };
  await h.create('My intended name'); await h.click('Refresh');
  assert.ok(h.nodes().some(node => node.getAttribute('role') === 'alert'), 'collision must be reported as a failed reconciliation');
  assert.equal(h.calls.length, 1, 'refresh must not automatically create a replacement operation');
  assert.ok(!h.detail.textContent.includes('Saved'), 'different accepted payload is not this saved change');
});
await test('revision conflicts refresh visible state without claiming success', async () => {
  const h = fixture({ seeded: true }); await h.view.open(); h.appendHook = () => ({ accepted: false, code: 'revision-conflict' });
  h.fill('List name', 'Conflict').parentNode.fire('submit'); await flush();
  assert.ok(h.detail.textContent.includes('changed in another session')); assert.equal(h.state.lists.founders.name, 'Founders');
});
await test('loading/partial history cannot create or mutate lists', async () => {
  const h = fixture({ seeded: true }); h.ready = false; await h.view.open();
  for (const text of ['Create list', 'Rename', 'Archive list', 'Remove']) assert.equal(h.button(text).disabled, true, text);
  await h.create('Cannot write'); assert.equal(h.calls.length, 0);
});
await test('failed initial load stops loading copy, explains failure and refresh recovers', async () => {
  const h = fixture(); h.ready = false;
  h.refreshHook = async () => { throw new Error('Contact lists require the updated master service. Restart the master admin service, then refresh.'); };
  await h.view.open();
  assert.match(h.detail.textContent, /updated master service/);
  assert.match(h.list.textContent, /Lists unavailable/);
  assert.doesNotMatch(h.list.textContent, /Lists are loading/);
  assert.doesNotMatch(h.detail.textContent, /Create a central list/);
  assert.equal(h.button('Create list').disabled, true);
  h.refreshHook = null; h.ready = true; await h.click('Refresh');
  assert.equal(h.button('Create list').disabled, false);
  await h.create('Recovered list');
  assert.equal(Object.values(h.state.lists)[0].name, 'Recovered list');
});
await test('leaving CRM during request cannot replace destination view', async () => {
  const h = fixture(), response = deferred(); h.refreshHook = () => response.promise;
  const request = h.view.open(); h.visible = false; const sentinel = new Element('p'); sentinel.textContent = 'Regular conversation'; h.detail.replaceChildren(sentinel);
  response.resolve(h.store.snapshot()); await request; assert.equal(h.detail.textContent, 'Regular conversation');
});
await test('close while write pending prevents stale render and clears hosts', async () => {
  const h = fixture(), response = deferred(); await h.view.open(); h.appendHook = () => response.promise;
  await h.create('Pending'); h.view.close(); response.resolve({ accepted: true }); await flush();
  assert.equal(h.list.children.length, 0); assert.equal(h.detail.children.length, 0);
});
await test('closed view open is a no-op and cannot navigate former session', async () => {
  const h = fixture(); await h.view.open(); h.view.close(); const details = h.details; await h.view.open();
  assert.equal(h.details, details); assert.equal(h.list.children.length, 0); assert.equal(h.detail.children.length, 0);
});
await test('stale contact button and row events cannot navigate a closed session', async () => {
  const h = fixture({ seeded: true }); await h.view.open();
  const name = h.button('<img src=x onerror=evil()>');
  const row = h.nodes().find(n => n.tagName === 'TR' && n.children.some(c => c.tagName === 'TD'));
  h.view.close(); name.fire('click'); row.children[1].fire('click'); await flush();
  assert.equal(h.opened.length, 0);
});
await test('background render preserves unsaved rename text', async () => {
  const h = fixture({ seeded: true }); await h.view.open(); h.fill('List name', 'Typing a new name');
  h.view.render(); assert.equal(h.input('List name').value, 'Typing a new name');
});
await test('retained rename draft keeps original revision across another editor update', async () => {
  const h = fixture({ seeded: true }); await h.view.open(); h.fill('List name', 'My pending rename');
  h.apply(h.op('list.rename', 'founders', { name: 'Another editor name' }, 1)); h.view.render();
  const input = h.input('List name'); assert.equal(input.value, 'My pending rename'); input.parentNode.fire('submit'); await flush();
  assert.equal(h.calls[0].base_revision, 1, 'background render must not silently refresh an old draft authorization');
  assert.equal(h.state.lists.founders.name, 'Another editor name'); assert.ok(h.detail.textContent.includes('changed in another session'));
});
await test('a rejected rename can be edited again against the reviewed current version', async () => {
  const h = fixture({ seeded: true }); await h.view.open(); h.fill('List name', 'First attempt');
  h.apply(h.op('list.rename', 'founders', { name: 'Current remote name' }, 1)); h.view.render();
  h.input('List name').parentNode.fire('submit'); await flush(); assert.equal(h.calls[0].base_revision, 1);
  h.fill('List name', 'Fresh deliberate edit').parentNode.fire('submit'); await flush();
  assert.equal(h.calls[1].base_revision, 2, 'terminal conflict must not permanently pin all subsequent new drafts to stale revision');
  assert.equal(h.state.lists.founders.name, 'Fresh deliberate edit');
});

await test('shared contact picker shows sending accounts and disables unsupported accounts', async () => {
  const h = fixture({ seeded: true, membershipService: true }); await h.view.open(); await h.click('Browse contacts');
  assert.ok(h.detail.textContent.includes('Elliot')); assert.ok(h.detail.textContent.includes('David'));
  assert.ok(h.detail.textContent.includes('including Private ones')); assert.ok(h.detail.textContent.includes('New conversations still need approval'));
  assert.equal(h.button('Sync update needed').disabled, true);
  h.button('Sync update needed').fire('click'); await flush(); assert.equal(h.memberCalls.length, 0);
  await h.click('Add to list');
  assert.deepEqual(h.memberCalls.map(c => c[0]), ['plan', 'execute']);
  assert.equal(h.memberCalls[0][1].listId, 'founders'); assert.equal(h.memberCalls[0][1].contact.owner_mxid, '@david:master');
  assert.ok(h.detail.textContent.includes('Direct confirmed by the sending account.'));
});
await test('Change sender links selected contact to exact existing person without opening conversation', async () => {
  const h = fixture({ seeded: true, membershipService: true }); await h.view.open(); await h.click('Change sender');
  const selector = h.nodes().find(n => n.getAttribute('aria-label') === 'Link shared contact to person'); assert.equal(selector.value, 'maya');
  h.fill('Find shared contacts', 'David').parentNode.fire('submit'); await h.click('Use sender');
  assert.equal(h.memberCalls[0][1].personId, 'maya'); assert.equal(h.memberCalls[0][1].contact.owner_mxid, '@david:master');
  assert.equal(h.opened.length, 0); assert.equal(h.calls.length, 0);
});
await test('picker search narrows text and renders contact labels without HTML', async () => {
  const h = fixture({ seeded: true, membershipService: true }); h.contacts[0].display_name = '<script>hostile()</script>'; h.contacts[0].network_id = 'unlinked@example.com';
  await h.view.open(); await h.click('Browse contacts'); h.fill('Find shared contacts', 'elliot').parentNode.fire('submit'); await flush();
  assert.ok(h.detail.textContent.includes('<script>hostile()</script>'));
  assert.equal(h.nodes().filter(n => n.tagName === 'SCRIPT').length, 0);
  assert.equal(h.nodes().filter(n => n.tagName === 'BUTTON' && n.textContent === 'Add to list').length, 1);
});
await test('picker limits visible choices without treating hidden matches as absent', async () => {
  const h = fixture({ seeded: true, membershipService: true });
  h.contacts = Array.from({ length: 101 }, (_, i) => ({ ...h.contacts[0], display_name: 'Contact ' + i, network_id: 'person' + i + '@example.com' }));
  await h.view.open(); await h.click('Browse contacts');
  assert.equal(h.nodes().filter(n => n.tagName === 'BUTTON' && n.textContent === 'Add to list').length, 100);
  assert.ok(h.detail.textContent.includes('Showing 100 contacts.'));
  h.fill('Find shared contacts', 'person100@').parentNode.fire('submit'); await flush();
  assert.equal(h.nodes().filter(n => n.tagName === 'BUTTON' && n.textContent === 'Add to list').length, 1);
});
await test('logout during addition planning prevents execution and obsolete rendering', async () => {
  const h = fixture({ seeded: true, membershipService: true }), response = deferred(); h.planHook = () => response.promise;
  await h.view.open(); await h.click('Browse contacts'); await h.click('Add to list'); h.view.close();
  response.resolve({ id: 'old-session-plan' }); await flush();
  assert.deepEqual(h.memberCalls.map(c => c[0]), ['plan']); assert.equal(h.detail.children.length, 0); assert.equal(h.list.children.length, 0);
});
await test('logout during addition execution cannot render its confirmed result', async () => {
  const h = fixture({ seeded: true, membershipService: true }), response = deferred(); h.executeHook = () => response.promise;
  await h.view.open(); await h.click('Browse contacts'); await h.click('Add to list'); h.view.close();
  response.resolve({ state: 'applied' }); await flush();
  assert.equal(h.detail.children.length, 0); assert.equal(h.list.children.length, 0);
});
await test('navigation during addition leaves regular conversation view intact', async () => {
  const h = fixture({ seeded: true, membershipService: true }), response = deferred(); h.executeHook = () => response.promise;
  await h.view.open(); await h.click('Browse contacts'); await h.click('Add to list'); h.visible = false;
  const sentinel = new Element('p'); sentinel.textContent = 'Regular conversation'; h.detail.replaceChildren(sentinel);
  response.resolve({ state: 'applied' }); await flush(); assert.equal(h.detail.textContent, 'Regular conversation');
});
await test('lost addition response retries exact plan across list selection and contact refresh', async () => {
  const h = fixture({ seeded: true, membershipService: true }); h.apply(h.op('list.create', 'other', { name: 'Other list' }));
  let first = true; h.executeHook = () => { if (first) { first = false; throw new Error('Direct setup awaits confirmation'); } return { state: 'applied' }; };
  await h.view.open(); await h.click('Browse contacts'); await h.click('Add to list');
  const frozen = h.memberCalls.find(c => c[0] === 'execute')[1];
  for (const label of ['Create list', 'Archive list', 'Remove', 'Change sender', 'Check Direct setup']) assert.equal(h.button(label).disabled, true, label);
  await h.click('Other list · 0'); h.contacts = []; await h.click('Refresh'); await h.click('Continue this contact addition');
  assert.equal(h.memberCalls.filter(c => c[0] === 'plan').length, 1);
  const executions = h.memberCalls.filter(c => c[0] === 'execute'); assert.equal(executions.length, 2);
  assert.equal(executions[0][1], frozen); assert.equal(executions[1][1], frozen); assert.equal(frozen.listId, 'founders');
  assert.equal(h.input('List name').value, 'Other list', 'old operation must not navigate away from current selection');
});
await test('failed planning permits deliberate new contact selection without retrying nonexistent plan', async () => {
  const h = fixture({ seeded: true, membershipService: true }); h.planHook = () => { throw new Error('Contact is no longer shared'); };
  await h.view.open(); await h.click('Browse contacts'); await h.click('Add to list');
  assert.ok(h.detail.textContent.includes('Contact is no longer shared')); assert.equal(h.button('Add to list').disabled, false);
  assert.equal(h.nodes().some(n => n.textContent === 'Continue this contact addition'), false);
  h.planHook = null; h.fill('Find shared contacts', 'David').parentNode.fire('submit'); await h.click('Add to list');
  assert.equal(h.memberCalls.filter(c => c[0] === 'plan').length, 2); assert.equal(h.memberCalls.at(-1)[1].contact.owner_mxid, '@david:master');
});
await test('remove and archive use cancellation service with exact immutable operation scope', async () => {
  const h = fixture({ seeded: true, membershipService: true }); await h.view.open();
  await h.click('Remove'); assert.equal(h.calls.length, 0); assert.equal(h.memberCalls[0][0], 'change');
  assert.equal(h.memberCalls[0][1].type, 'member.remove'); assert.equal(h.state.memberships['founders/maya'].removed, true);
  await h.click('Archive list'); assert.equal(h.memberCalls[1][1].type, 'list.archive');
  assert.deepEqual(h.memberCalls[1][1].payload.membership_operations, []); assert.equal(h.state.lists.founders.archived, true);
});
await test('archive freezes active membership operation identities for cancellation and CAS', async () => {
  const h = fixture({ seeded: true, membershipService: true }); await h.view.open();
  const memberOp = h.state.memberships['founders/maya'].operation_id;
  h.changeHook = () => { throw new Error('Earlier Direct setup is unresolved'); };
  await h.click('Archive list'); const op = h.memberCalls[0][1];
  assert.deepEqual(op.payload.membership_operations, [memberOp]); assert.equal(h.state.lists.founders.archived, false);
  assert.ok(h.detail.textContent.includes('Earlier Direct setup is unresolved')); await h.click('Reconcile saved change');
  assert.equal(h.memberCalls[1][1], op); assert.equal(h.calls.length, 0);
});
await test('lost remove response retries original member despite navigating to another list', async () => {
  const h = fixture({ seeded: true, membershipService: true }); h.apply(h.op('list.create', 'other', { name: 'Other list' }));
  let first = true; h.changeHook = op => { const value = h.apply(op); if (first) { first = false; throw new Error('lost cancellation or metadata reply'); } return value; };
  await h.view.open(); await h.click('Remove'); const original = h.memberCalls[0][1];
  await h.click('Other list · 0'); await h.click('Reconcile saved change');
  assert.equal(h.memberCalls[1][1], original); assert.equal(original.payload.list_id, 'founders'); assert.equal(h.input('List name').value, 'Other list');
});
await test('resume uses exact selected membership and routes no row click into conversations', async () => {
  const h = fixture({ seeded: true, membershipService: true }); await h.view.open();
  const member = h.state.memberships['founders/maya']; await h.click('Check Direct setup');
  assert.deepEqual(h.memberCalls, [['resume', member]]); assert.equal(h.opened.length, 0);
  assert.ok(h.detail.textContent.includes('latest sharing settings still apply'));
});
await test('resume errors retain membership and never claim Direct success', async () => {
  const h = fixture({ seeded: true, membershipService: true }); h.resumeHook = () => { throw new Error('Sending account changed; recovery required'); };
  await h.view.open(); await h.click('Check Direct setup');
  assert.ok(h.detail.textContent.includes('recovery required')); assert.ok(h.nodes().some(n => n.getAttribute('role') === 'alert'));
  assert.equal(h.state.memberships['founders/maya'].removed, false); assert.equal(h.button('Check Direct setup').disabled, false);
});
await test('partial metadata disables every permission-affecting action', async () => {
  const h = fixture({ seeded: true, membershipService: true }); h.ready = false; await h.view.open();
  for (const label of ['Browse contacts', 'Change sender', 'Check Direct setup', 'Remove', 'Archive list']) assert.equal(h.button(label).disabled, true, label);
  await h.click('Check Direct setup'); await h.click('Change sender'); assert.equal(h.memberCalls.length, 0);
});
await test('rows remain semantic compact tables with named account and conversation controls', async () => {
  const h = fixture({ seeded: true, membershipService: true }); await h.view.open();
  const table = h.nodes().find(n => n.tagName === 'TABLE'); assert.ok(table.className.includes('crm-table'));
  const headers = h.nodes(table).filter(n => n.tagName === 'TH'); assert.deepEqual(headers.map(n => n.textContent), ['Contact', 'Send from', 'Conversation', '']);
  assert.ok(headers.every(n => n.getAttribute('scope') === null && n.scope === 'col'));
  const row = h.nodes(table).find(n => n.tagName === 'TR' && n.children.some(c => c.tagName === 'TD'));
  assert.equal(row.children.length, 4); assert.equal(row.children[1].textContent, '@elliot:master');
});
function addAlternate(h) {
  assert.equal(h.apply(h.op('binding.create', 'alternate', { person_id: 'maya', owner_mxid: '@david:master', generation: 'g2', contact: { source: 'imessage', network_id: '<script>alternate@example.com</script>' }, contact_version: 'v2' })).accepted, true);
}
await test('locations expose all account bindings without changing selected sender', async () => {
  const h = fixture({ seeded: true }); addAlternate(h); await h.view.open();
  assert.ok(h.detail.textContent.includes('Where we have them · 2'));
  await h.click('@david:master · imessage');
  assert.equal(h.opened.length, 1); assert.equal(h.opened[0][0], h.state.persons.maya);
  assert.equal(h.opened[0][1], h.state.bindings.alternate);
  assert.equal(h.state.memberships['founders/maya'].binding_id, 'binding'); assert.equal(h.calls.length, 0);
  assert.ok(h.detail.textContent.includes('<script>alternate@example.com</script>'));
  assert.equal(h.nodes().filter(n => n.tagName === 'SCRIPT').length, 0);
});
await test('location summary and nested details do not bubble into conversation navigation', async () => {
  const h = fixture({ seeded: true }); await h.view.open();
  const details = h.nodes().find(n => n.className === 'crm-locations');
  details.children[0].fire('click'); details.children[1].children[1].fire('click');
  assert.equal(h.opened.length, 0);
  details.open = true; details.fire('toggle'); h.view.render();
  assert.equal(h.nodes().find(n => n.className === 'crm-locations').open, true);
});
await test('revoked alternate location remains visible but cannot navigate', async () => {
  const h = fixture({ seeded: true }); addAlternate(h);
  h.bindingHook = b => ({ owner: b.owner_mxid, status: b.binding_id === 'alternate' ? 'Contact no longer shared' : 'Direct', available: b.binding_id !== 'alternate' });
  await h.view.open(); assert.ok(h.detail.textContent.includes('Contact no longer shared'));
  assert.equal(h.button('@david:master · imessage').disabled, true);
  await h.click('@david:master · imessage'); assert.equal(h.opened.length, 0);
});
await test('location list link navigates exact list without opening conversation', async () => {
  const h = fixture({ seeded: true }); h.apply(h.op('list.create', 'other', { name: 'Other' }));
  h.apply(h.op('member.set', 'other-member', { list_id: 'other', person_id: 'maya', binding_id: 'binding' }));
  await h.view.open(); await h.click('Other · Send from @elliot:master');
  assert.equal(h.input('List name').value, 'Other'); assert.equal(h.opened.length, 0); assert.equal(h.calls.length, 0);
});
await test('removed memberships are excluded from location list links', async () => {
  const h = fixture({ seeded: true }); h.apply(h.op('list.create', 'other', { name: 'Removed list' }));
  h.apply(h.op('member.set', 'other-member', { list_id: 'other', person_id: 'maya', binding_id: 'binding' }));
  const member = h.state.memberships['other/maya']; h.apply(h.op('member.remove', member.membership_id, { list_id: 'other', person_id: 'maya' }, member.revision));
  await h.view.open(); assert.ok(!h.nodes(h.detail).some(n => n.tagName === 'BUTTON' && n.textContent === 'Removed list'));
});
await test('location controls retained after logout cannot navigate or reopen lists', async () => {
  const h = fixture({ seeded: true }); addAlternate(h); await h.view.open();
  const alternate = h.button('@david:master · imessage'), list = h.button('Founders · Send from @elliot:master');
  h.view.close(); const oldDetails = h.details; alternate.fire('click'); list.fire('click');
  assert.equal(h.opened.length, 0); assert.equal(h.details, oldDetails); assert.equal(h.detail.children.length, 0);
});
await test('filtering keeps selected hidden recipients through personalization and back', async () => {
  const h = fixture({ seeded: true, batchService: true }); await h.view.open();
  const checkbox = h.input('Select <img src=x onerror=evil()>'); checkbox.checked = true; checkbox.fire('change');
  h.fill('Find list contacts', 'no matching contact').parentNode.fire('submit'); await flush();
  assert.ok(h.detail.textContent.includes('No contacts match')); assert.ok(h.detail.textContent.includes('1 selected'));
  await h.click('Personalize messages'); assert.deepEqual(h.batchCalls, [['founders', 'maya']]);
  assert.equal(h.nodes().filter(n => n.tagName === 'TEXTAREA').length, 1);
  await h.click('Back to list'); assert.equal(h.input('Find list contacts').value, 'no matching contact');
  h.fill('Find list contacts', '').parentNode.fire('submit'); await flush();
  assert.equal(h.input('Select <img src=x onerror=evil()>').checked, true); assert.equal(h.opened.length, 0);
});
await test('sending-account filter matches membership owner rather than alternate profile', async () => {
  const h = fixture({ seeded: true }); addAlternate(h); await h.view.open();
  const selector = h.nodes().find(n => n.getAttribute('aria-label') === 'Filter list by sending account');
  assert.deepEqual(selector.children.map(n => n.value), ['', '@elliot:master']);
  h.fill('Find list contacts', '@david:master').parentNode.fire('submit'); await flush();
  assert.ok(h.detail.textContent.includes('No contacts match'));
});
await test('Direct renewal requires explicit warning confirmation and preserves cancel choice',async()=>{
  const h=fixture({seeded:true,membershipService:true});await h.view.open();await h.click('Enable Direct again');
  assert.match(h.detail.textContent,/manually set to Private/);assert.match(h.detail.textContent,/existing message history may become visible/);
  assert.match(h.detail.textContent,/new conversation still needs user approval/);assert.equal(h.memberCalls.length,0);
  await h.click('Keep current settings');assert.equal(h.memberCalls.length,0);
  await h.click('Enable Direct again');await h.click('Confirm new Direct setup');
  assert.deepEqual(h.memberCalls.map(c=>c[0]),['renew','execute']);assert.equal(h.opened.length,0);
  assert.equal(h.memberCalls[0][1].binding_id,'binding');assert.equal(Object.isFrozen(h.memberCalls[1][1]),true);
});
await test('renewal with a lost execution reply retries the same retained plan',async()=>{
  const h=fixture({seeded:true,membershipService:true});await h.view.open();let lost=true;
  h.executeHook=async()=>{if(lost){lost=false;throw Error('Direct acknowledgment lost');}return {state:'applied'};};
  await h.click('Enable Direct again');await h.click('Confirm new Direct setup');assert.match(h.detail.textContent,/Direct acknowledgment lost/);
  await h.click('Continue this contact addition');
  assert.equal(h.memberCalls.filter(c=>c[0]==='renew').length,1);const executes=h.memberCalls.filter(c=>c[0]==='execute');
  assert.equal(executes.length,2);assert.equal(executes[0][1],executes[1][1]);
});
await test('failed current-contact renewal check never reaches permission execution',async()=>{
  const h=fixture({seeded:true,membershipService:true});h.renewHook=async()=>{throw Error('Contact no longer shared');};await h.view.open();
  await h.click('Enable Direct again');await h.click('Confirm new Direct setup');
  assert.match(h.detail.textContent,/Contact no longer shared/);assert.equal(h.memberCalls.filter(c=>c[0]==='execute').length,0);
});
await test('closing during renewal planning suppresses permission execution',async()=>{
  const h=fixture({seeded:true,membershipService:true}),gate=deferred();h.renewHook=()=>gate.promise;await h.view.open();
  await h.click('Enable Direct again');h.button('Confirm new Direct setup').fire('click');h.view.close();gate.resolve(Object.freeze({id:'late'}));await flush();
  assert.equal(h.memberCalls.filter(c=>c[0]==='execute').length,0);assert.equal(h.detail.children.length,0);
});
await test('restoring a list does not request a fresh Direct setup automatically',async()=>{
  const h=fixture({seeded:true,membershipService:true});await h.view.open();await h.click('Archive list');await h.click('Archived lists');
  await h.click('Restore list');assert.deepEqual(h.memberCalls.map(c=>c[0]),['change']);assert.equal(h.state.lists.founders.archived,false);
});
await test('central outreach is accessible with no active lists or an archived list',async()=>{
  for(const seeded of [false,true]){
    const h=fixture({seeded,batchService:true});if(seeded)h.apply(h.op('list.archive','founders',{},1));await h.view.open();
    await h.click('Outreach across accounts');assert.match(h.detail.textContent,/Outreach across accounts/);assert.match(h.detail.textContent,/No outreach matches/);
    assert.equal(h.opened.length,0);assert.equal(h.calls.length,0);
  }
});
await test('person history opens from compact row without opening a conversation or changing sender',async()=>{
  const h=fixture({seeded:true,batchService:true});await h.view.open();const before=h.state.memberships['founders/maya'].binding_id;
  assert.match(h.detail.textContent,/Latest outreach/);assert.match(h.detail.textContent,/No reviewed outreach/);
  await h.click('Outreach history');assert.match(h.detail.textContent,/Outreach history/);assert.equal(h.opened.length,0);
  assert.equal(h.state.memberships['founders/maya'].binding_id,before);await h.click('Back to list');assert.equal(h.input('List name').value,'Founders');
});
await test('inline search filters on each keystroke without replacing the input', async () => {
  const h = fixture({seeded:true,membershipService:true}); await h.view.open();
  const input = h.input('Find shared contacts'); input.value = 'David'; input.fire('input');
  assert.equal(h.input('Find shared contacts'),input);
  const rows = h.nodes().filter(n=>n.className==='crm-contact-choice');
  assert.equal(rows.length,1); assert.match(rows[0].textContent,/David/);
  assert.equal(h.nodes().some(n=>n.tagName==='BUTTON'&&n.textContent==='Find'),false);
  input.value='missing'; input.fire('input'); assert.match(h.detail.textContent,/No shared contacts match/);
});
await test('continuous additions retain the inline search and clear its query', async () => {
  const h=fixture({seeded:true,membershipService:true}); await h.view.open();
  h.fill('Find shared contacts','David'); await h.click('Add to list');
  assert.equal(h.input('Find shared contacts').value,''); assert.ok(h.button('Hide results'));
  h.contacts.push({...h.contacts[1],network_id:'next@example.com',display_name:'Next'});
  h.view.render(); h.fill('Find shared contacts','Next'); await h.click('Add to list');
  assert.equal(h.memberCalls.filter(c=>c[0]==='execute').length,2);
});
await test('same names across accounts stay separate until explicitly linked',async()=>{
  const h=fixture({seeded:true,membershipService:true}); h.contacts[0].display_name=h.contacts[1].display_name='Same Name';
  await h.view.open();h.fill('Find shared contacts','Same Name');
  assert.equal(h.nodes().filter(n=>n.className==='crm-contact-choice').length,2);
  assert.equal(h.nodes().filter(n=>n.textContent==='✓ Added').length,1);
});
await test('linked person groups account choices and requires an explicit sender',async()=>{
  const h=fixture({seeded:true,membershipService:true});
  h.apply(h.op('binding.create','david',{person_id:'maya',owner_mxid:'@david:master',generation:'g2',contact:{source:'imessage',network_id:'maya-other@example.com'},contact_version:'v2'}));
  h.apply(h.op('list.create','empty',{name:'Empty'}));await h.view.open();await h.click('Empty · 0');h.fill('Find shared contacts','Maya');
  assert.equal(h.nodes().filter(n=>n.className==='crm-contact-choice').length,1);
  assert.equal(h.button('Add to list').disabled,true);
  const select=h.nodes().find(n=>n.getAttribute('aria-label')?.startsWith('Send from for'));
  select.value=select.children[2].value;select.fire('change');await h.click('Add to list');
  assert.equal(h.memberCalls[0][1].personId,'maya');assert.equal(h.memberCalls[0][1].contact.owner_mxid,'@david:master');
});
await test('already-listed contacts cannot silently replace a sender',async()=>{
  const h=fixture({seeded:true,membershipService:true});await h.view.open();h.fill('Find shared contacts','maya@example.com');
  assert.match(h.detail.textContent,/✓ Added/);assert.equal(h.nodes().some(n=>n.tagName==='BUTTON'&&n.textContent==='Add to list'),false);
  assert.equal(h.memberCalls.length,0);
});
await test('Undo removal restores only membership and never grants Direct',async()=>{
  const h=fixture({seeded:true,membershipService:true});await h.view.open();await h.click('Remove');await h.click('Undo');
  assert.equal(h.state.memberships['founders/maya'].removed,false);
  assert.deepEqual(h.memberCalls.map(c=>c[0]),['change']);
  assert.equal(h.calls[0].type,'member.set');assert.equal(h.calls[0].payload.direct_request,undefined);
});
await test('stale Undo cannot overwrite another editor or revive an archived list',async()=>{
  for(const archive of [false,true]){
    const h=fixture({seeded:true,membershipService:true});await h.view.open();await h.click('Remove');
    if(archive)h.apply(h.op('list.archive','founders',{},1));
    else h.apply(h.op('member.set','membership',{list_id:'founders',person_id:'maya',binding_id:'binding'},2));
    await h.click('Undo');assert.match(h.detail.textContent,/changed in another session|list-archived/);
    assert.equal(h.memberCalls.filter(c=>c[0]==='execute').length,0);
  }
});
await test('Undo addition cancels only the exact accepted membership',async()=>{
  const h=fixture({seeded:true,membershipService:true});
  h.apply(h.op('list.create','empty',{name:'Empty'}));
  const member=h.op('member.set','new-member',{list_id:'empty',person_id:'maya',binding_id:'binding'});
  h.planHook=()=>({member,previous:null});h.executeHook=()=>{h.apply(member);return {state:'applied'}};
  await h.view.open();await h.click('Empty · 0');h.fill('Find shared contacts','maya@example.com');await h.click('Add to list');await h.click('Undo');
  assert.equal(h.state.memberships['empty/maya'].removed,true);
  const change=h.memberCalls.find(c=>c[0]==='change')[1];assert.equal(change.entity_id,'new-member');assert.equal(change.base_revision,1);
});
globalThis.document = oldDocument;
console.log(`master_crm_view: ${passed} independent view scenarios passed; ${failures} failed`);
if (failures) process.exitCode = 1;
