// Master list UI. The injected store owns canonical metadata; the app owns
// verified contacts and the regular conversation viewer. No network/send code.
import { el, sanitize } from '../../shared/ui/el.js';
import { canonicalCRMJSON, membershipKey, reminderOperationPayload } from '../../shared/model/crm.js';
import { createCRMCompose } from './crm-compose.js';

export function createCRMView({ getStore, listHost, detailHost, isVisible,
  showDetail, openPerson, openMessage, renderMonitoring, describeBinding, getMembers, getBatches, getContacts = () => [], uuid = () => crypto.randomUUID() }) {
  let selected = null, archived = false, busy = false, closed = false;
  let pending = null, notice = '', failed = false, nameDraft = '';
  const renameDrafts = new Map();
  let picker = false, query = '', linkPerson = '', pendingAddition = null;
  let memberQuery = '', memberOwner = '', pickerOwner = '';
  const senderChoices = new Map();
  let focusTargets = new Map(), focusSearchList = null, undoChange = null, pickerList = null;
  const trackFocus = (node, key) => { node.setAttribute('data-crm-focus', key); focusTargets.set(key, node); };
  function rememberUndo(op) {
    if (op.type === 'member.remove') {
      const m = snapshot().state.memberships[membershipKey(op.payload.list_id, op.payload.person_id)];
      if (m?.removed && m.operation_id === op.op_id) undoChange = operation('member.set', m.membership_id, m.revision,
        { list_id: m.list_id, person_id: m.person_id, binding_id: m.binding_id });
    }
  }
  const expandedPeople = new Set();
  let renewalMember = null;
  let store = null;
  const outreach = getBatches ? createCRMCompose({ getBatches, onChange: () => { if (current()) render(); }, describeBinding, openMessage,
    selectList: id => { selected = id; archived = !!snapshot().state?.lists[id]?.archived; } }) : null;
  const current = () => !closed && isVisible();
  const snapshot = () => store?.snapshot() || { state: null, ready: false };
  const button = (text, action, disabled = false) => {
    const node = el('button', 'btn', text); node.type = 'button'; node.disabled = disabled;
    node.addEventListener('click', event => { if (!closed) action(event); }); return node;
  };
  function operation(type, entityId, base, payload) {
    payload=reminderOperationPayload(type,snapshot().state?.lists[entityId],payload);
    return { schema: 1, op_id: uuid(), workspace_id: snapshot().roomId,
      type, entity_id: entityId, base_revision: base, payload };
  }
  async function perform(op) {
    if (closed || busy || (pending && pending !== op)) return;
    pending = op; undoChange = null; busy = true; failed = false; notice = 'Saving…'; render();
    try {
      const outcome = getMembers && ['member.remove', 'list.archive'].includes(op.type)
        ? await getMembers().applyChange(op) : await store.append(op);
      if (closed) return;
      pending = null;
      if (op.type === 'list.rename') renameDrafts.delete(op.entity_id);
      if (!outcome.accepted) {
        failed = true; notice = outcome.code === 'revision-conflict'
          ? 'This list changed in another session. Review the current version before editing again.'
          : 'Change refused: ' + outcome.code;
      } else {
        notice = op.type === 'member.remove' ? 'Contact removed. Direct stays unchanged.' : 'Saved';
        rememberUndo(op);
        if (op.type === 'list.create') { selected = op.entity_id; nameDraft = ''; }
      }
    } catch (error) {
      if (closed) return;
      // Preserve exact intent/transaction ID. A lost response is not a failed
      // write and must never turn a retry into a second list or membership.
      failed = true; notice = 'Save not confirmed. Reconcile this change before making another edit.'
        + (getMembers && ['member.remove', 'list.archive'].includes(op.type) ? ' ' + error.message : '');
    } finally { busy = false; if (current()) render(); }
  }
  async function refresh() {
    if (closed || busy) return;
    busy = true; notice = 'Loading lists…'; failed = false; render();
    try {
      store ||= getStore();
      const value = await store.refresh();
      if (closed) return;
      if (value.ready && outreach) await outreach.refreshHistory(value.state);
      if (closed) return;
      notice = value.ready ? '' : 'Loading earlier changes. Refresh to continue.';
      if (pending && value.ready) {
        const record = value.state.operations[pending.op_id];
        if (record) {
          const outcome = record.serialized === canonicalCRMJSON(pending)
            ? record.outcome : { accepted: false, code: 'operation-id-collision' };
          notice = outcome.accepted ? 'Saved' : 'Change refused: ' + outcome.code;
          failed = !outcome.accepted;
          if (outcome.accepted) rememberUndo(pending);
          if (outcome.accepted && pending.type === 'list.create') { selected = pending.entity_id; nameDraft = ''; }
          if (pending.type === 'list.rename') renameDrafts.delete(pending.entity_id);
          pending = null;
        }
      }
    } catch (error) { if (!closed) { notice = 'Contact lists could not be loaded. ' + sanitize(error?.message || 'Refresh to try again.'); failed = true; } }
    finally { busy = false; if (current()) render(); }
  }
  async function addContact(contact, personId = linkPerson || null) {
    if (closed || busy || pending || !snapshot().ready) return;
    const additionList = selected;
    undoChange = null; busy = true; failed = false; notice = 'Verifying contact and preparing Direct…'; render();
    try {
      const members = getMembers();
      if (!pendingAddition) pendingAddition = await members.planAddition({ listId: additionList, contact, personId });
      if (closed) return;
      const result = await members.execute(pendingAddition);
      if (closed) return;
      notice = result.state === 'approval_required' ? 'Contact added. Starting a new conversation still needs user approval.'
        : result.state === 'applied' ? 'Contact added. Direct confirmed by the sending account.'
        : 'Contact added. Some conversations could not become Direct; review the user’s sharing settings.';
      const plan = pendingAddition;
      const m = plan.member?.payload && snapshot().state.memberships[membershipKey(plan.member.payload.list_id, plan.member.payload.person_id)];
      if (m && !m.removed && m.operation_id === plan.member.op_id && !plan.previous) undoChange = operation('member.remove', m.membership_id, m.revision, { list_id: m.list_id, person_id: m.person_id });
      pendingAddition = null;
      if (selected === additionList) { picker = true; linkPerson = ''; query = ''; focusSearchList = selected; }
    } catch (error) { if (!closed) { notice = error.message; failed = true; } }
    finally { busy = false; if (current()) render(); }
  }
  async function finishDirect(member) {
    if (closed || busy || pendingAddition || pending) return;
    busy = true; notice = 'Checking Direct setup…'; failed = false; render();
    try {
      const result = await getMembers().resume(member);
      if (!closed) notice = result.state === 'applied' ? 'Original Direct setup completed. The user’s latest sharing settings still apply.' : result.state === 'approval_required'
        ? 'Starting this conversation requires user approval.' : 'Some conversations need attention in the user’s sharing settings.';
    } catch (error) { if (!closed) { notice = error.message; failed = true; } }
    finally { busy = false; if (current()) render(); }
  }
  async function renewDirect(member) {
    if (closed || busy || pendingAddition || pending) return;
    busy = true; failed = false; notice = 'Preparing a new Direct setup…'; render();
    try {
      pendingAddition = await getMembers().planRenewal(member);
      if (closed) return;
      renewalMember = null;
    } catch (error) { if (!closed) { notice = error.message; failed = true; } }
    finally { busy = false; if (current()) render(); }
    if (!closed && pendingAddition) await addContact(null);
  }
  // Group only canonical linked people. Display names and handles alone never
  // authorize a cross-account person merge or a sender reassignment.
  function contactGroups(state) {
    const groups = new Map(), indexed = new Map();
    const bindingKey = (owner, generation, source, handle) => JSON.stringify([owner, generation, source, handle]);
    for (const binding of Object.values(state.bindings)) {
      for (const generation of [binding.generation, null]) {
        const key = bindingKey(binding.owner_mxid, generation, binding.contact.source, binding.contact.network_id);
        if (!indexed.has(key)) indexed.set(key, []);
        indexed.get(key).push(binding);
      }
    }
    for (const contact of getContacts()) {
      if (pickerOwner && contact.owner_mxid !== pickerOwner) continue;
      const bindings = indexed.get(bindingKey(contact.owner_mxid, contact.generation || null, contact.source, contact.network_id)) || [];
      const binding = bindings.length === 1 ? bindings[0] : null;
      const identity = JSON.stringify([contact.owner_mxid, contact.generation, contact.source, contact.network_id]);
      const key = binding ? 'person:' + binding.person_id : identity;
      if (!groups.has(key)) groups.set(key, { key, personId: binding?.person_id, contacts: [] });
      const group = groups.get(key);
      if (!group.contacts.some(c => c.identity === identity)) group.contacts.push({ ...contact, identity, binding });
    }
    const needle = query.trim().toLowerCase();
    return [...groups.values()].filter(group => group.contacts.some(c =>
      [state.persons[group.personId]?.label, c.person_display, c.display_name, c.network_id, c.ownerLabel, c.source]
        .some(value => typeof value === 'string' && value.toLowerCase().includes(needle))));
  }
  function renderPicker(state, locked) {
    const panel = el('section', 'crm-picker crm-inline-add');
    const heading = el('div', 'crm-add-heading');
    heading.append(el('h3', '', linkPerson ? 'Choose a sender' : 'Add people'), el('span', 'muted', 'From shared contacts')); panel.append(heading);
    const search = el('form', 'crm-inline-search'), input = el('input', 'input');
    input.value = query; input.placeholder = 'Type a name, handle, or account…'; input.disabled = locked;
    input.setAttribute('aria-label', 'Find shared contacts'); input.setAttribute('aria-controls', 'crm-contact-results');
    trackFocus(input, 'contact-search');
    const filter = el('select', 'input'); filter.setAttribute('aria-label', 'Filter shared contacts by account');
    trackFocus(filter, 'contact-account'); filter.disabled = locked;
    const all = el('option', '', 'All accounts'); all.value = ''; filter.append(all);
    const owners = new Map(getContacts().map(c => [c.owner_mxid, c.ownerLabel]));
    if (pickerOwner && !owners.has(pickerOwner)) pickerOwner = '';
    for (const [owner, name] of owners) { const option = el('option', '', sanitize(name)); option.value = owner; filter.append(option); }
    filter.value = pickerOwner; search.append(input, filter); panel.append(search);
    const toolbar = el('div', 'crm-picker-toolbar'), count = el('span', 'muted');
    count.setAttribute('role', 'status'); count.setAttribute('aria-live', 'polite');
    const browse = button(picker ? 'Hide results' : 'Browse contacts', () => { picker = !picker; updateResults(); }, locked);
    browse.setAttribute('aria-controls', 'crm-contact-results'); toolbar.append(count, browse); panel.append(toolbar);
    const results = el('div', 'crm-contact-results'); results.id = 'crm-contact-results'; panel.append(results);
    // Linking is an explicit, secondary action, not a mandatory step per add.
    const advanced = el('details', 'crm-picker-link'); advanced.open = !!linkPerson;
    advanced.append(el('summary', '', 'Link to an existing CRM person'));
    const label = el('label', 'crm-link-person', 'Use this person’s existing history');
    const select = el('select', 'input'); select.setAttribute('aria-label', 'Link shared contact to person'); trackFocus(select, 'contact-person');
    const fresh = el('option', '', 'Use contact’s identity'); fresh.value = ''; select.append(fresh);
    for (const person of Object.values(state.persons)) { const option = el('option', '', sanitize(person.label)); option.value = person.person_id; select.append(option); }
    select.value = linkPerson; select.disabled = locked;
    select.addEventListener('change', () => { if (!closed) { linkPerson = select.value; picker = true; render(); } });
    label.append(select); advanced.append(label, el('p', 'muted', 'Link only when you know these contacts are the same person. Choosing another account for a listed person changes their sender.'));
    if (linkPerson) advanced.append(button('Cancel sender change', () => { linkPerson = ''; render(); }, locked));
    panel.append(advanced);
    const permissions = el('details', 'crm-picker-permissions');
    permissions.append(el('summary', '', 'Adding enables Direct for the selected account.'),
      el('p', 'muted', 'Adding enables Direct for this account’s existing one-to-one conversations, including Private ones. Their existing message history may become visible to the master. New conversations still need approval. Removing a person leaves Direct unchanged.'));
    panel.append(permissions);
    function updateResults() {
      results.replaceChildren(); results.hidden = !picker; advanced.hidden = !picker && !linkPerson;
      browse.textContent = picker ? 'Hide results' : 'Browse contacts'; browse.setAttribute('aria-expanded', String(picker));
      input.setAttribute('aria-expanded', String(picker));
      const matches = contactGroups(state);
      count.textContent = picker ? matches.length + (matches.length === 1 ? ' matching person' : ' matching people') : 'Search across your managed accounts';
      if (!picker) return;
      for (const group of matches.slice(0, 100)) {
        const personId = linkPerson || group.personId;
        const member = personId && state.memberships[ membershipKey(selected, personId) ];
        const active = member && !member.removed;
        let choice = group.contacts.find(c => c.identity === senderChoices.get(group.key));
        if (active && !linkPerson) choice = group.contacts.find(c => c.binding?.binding_id === member.binding_id);
        if (!choice && group.contacts.length === 1) choice = group.contacts[0];
        const sample = choice || group.contacts[0];
        const display = state.persons[group.personId]?.label || sample.person_display || sample.display_name || sample.network_id;
        const row = el('div', 'crm-contact-choice'), text = el('div', 'crm-choice-person');
        text.append(el('strong', '', sanitize(display)), el('div', 'muted', sanitize(choice?.network_id || 'Choose a sending account')));
        const sender = el('div', 'crm-choice-sender');
        if (group.contacts.length > 1 && !(active && !linkPerson)) {
          const options = el('select', 'input'); options.setAttribute('aria-label', 'Send from for ' + sanitize(display));
          trackFocus(options, 'sender:' + group.key); options.disabled = locked || (!!active && !linkPerson);
          const empty = el('option', '', 'Send from…'); empty.value = ''; options.append(empty);
          for (const contact of group.contacts) { const option = el('option', '', sanitize(contact.ownerLabel) + ' · ' + sanitize(contact.source) + ' · ' + sanitize(contact.network_id)); option.value = contact.identity; options.append(option); }
          options.value = choice?.identity || '';
          options.addEventListener('change', () => { if (!closed) { senderChoices.set(group.key, options.value); render(); } }); sender.append(options);
        } else sender.append(el('span', '', 'From ' + sanitize(sample.ownerLabel) + ' · ' + sanitize(sample.source)));
        const same = active && choice?.binding?.binding_id === member.binding_id;
        const incompatible = linkPerson && choice?.binding && choice.binding.person_id !== linkPerson;
        const already = active && (!linkPerson || same);
        if (already) {
          sender.append(el('div', 'muted', 'On this list · from ' + describeBinding(state.bindings[member.binding_id]).owner));
          row.append(text, sender, el('span', 'crm-choice-added', '✓ Added'));
        } else {
          const hint = incompatible ? 'Already linked to another person' : !choice ? 'Choose who sends'
            : !choice.crmReady ? 'Account sync update needed' : choice.binding ? describeBinding(choice.binding).status : 'Direct setup checked when added';
          sender.append(el('div', 'muted', hint));
          const add = button(active ? 'Use sender' : choice && !choice.crmReady ? 'Sync update needed' : 'Add to list',
            () => addContact(choice, personId || null), locked || !choice || !choice.crmReady || !!incompatible);
          add.setAttribute('aria-label', (active ? 'Use sender for ' : 'Add ') + sanitize(display));
          trackFocus(add, 'add:' + group.key); row.append(text, sender, add);
        }
        results.append(row);
      }
      if (!matches.length) results.append(el('p', 'muted crm-picker-empty', 'No shared contacts match. Try another name or account.'));
      if (matches.length > 100) results.append(el('p', 'muted', 'Showing 100 contacts. Narrow your search to find more.'));
    }
    input.addEventListener('input', () => { if (!closed) { query = input.value; picker = true; updateResults(); } });
    input.addEventListener('focus', () => { if (!closed && !locked && !picker) { picker = true; updateResults(); } });
    input.addEventListener('keydown', event => { if (event.key === 'Escape') { picker = false; updateResults(); } });
    search.addEventListener('submit', event => { event.preventDefault(); });
    filter.addEventListener('change', () => { if (!closed) { pickerOwner = filter.value; picker = true; render(); } });
    updateResults(); detailHost.append(panel);
  }
  function locations(person, state) {
    const box = el('details', 'crm-locations'); box.open = expandedPeople.has(person.person_id);
    const bindings = Object.values(state.bindings).filter(b => b.person_id === person.person_id);
    box.append(el('summary', '', 'Where we have them · ' + bindings.length));
    box.addEventListener('toggle', () => { if (!closed) box.open ? expandedPeople.add(person.person_id) : expandedPeople.delete(person.person_id); });
    for (const binding of bindings) {
      const detail = describeBinding(binding), row = el('div', 'crm-location');
      row.append(button(detail.owner + ' · ' + sanitize(binding.contact.source || 'Linked profile'),
        () => openPerson(person, binding), !detail.available),
      el('div', 'muted', sanitize(binding.contact.network_id || binding.contact.profile_id)), el('div', 'muted', detail.status));
      box.append(row);
    }
    const memberships = Object.values(state.memberships).filter(m => m.person_id === person.person_id && !m.removed);
    for (const member of memberships) {
      const list = state.lists[member.list_id];
      if (list) box.append(button(sanitize(list.name) + ' · Send from ' + describeBinding(state.bindings[member.binding_id]).owner + (list.archived ? ' · Archived' : ''), () => {
        archived = list.archived; selected = list.list_id; outreach?.showList(selected); showDetail(); render();
      }));
    }
    if (outreach) box.append(button('Outreach history', () => outreach.openHistory(state, person.person_id)));
    return box;
  }
  function render() {
    if (!current()) return;
    const focused = document.activeElement;
    let focusKey = focused?.getAttribute?.('data-crm-focus');
    const selection = [focused?.selectionStart, focused?.selectionEnd];
    focusTargets = new Map();
    const { state, ready } = snapshot(), locked = busy || !ready || !!pending || !!pendingAddition;
    const lists = Object.values(state?.lists || {}).filter(l => l.archived === archived);
    if (!lists.some(l => l.list_id === selected)) selected = lists[0]?.list_id || null;
    if (pickerList !== selected) {
      pickerList = selected; picker = false; query = ''; linkPerson = ''; pickerOwner = '';
      senderChoices.clear(); renewalMember = null; focusKey = null; focusSearchList = null;
    }
    listHost.replaceChildren();
    const tools = el('div', 'crm-list-tools');
    tools.append(button(archived ? 'Active lists' : 'Archived lists', () => { archived = !archived; render(); }));
    tools.append(button('Refresh', refresh, busy)); listHost.append(tools);
    if (outreach) listHost.append(button('Outreach across accounts', () => { showDetail(); outreach.openHistory(state); }, !ready));
    const create = el('form', 'crm-list-tools');
    const name = el('input', 'input'); name.placeholder = 'New list name'; name.maxLength = 160;
    name.setAttribute('aria-label', 'New list name'); name.value = nameDraft; name.disabled = locked;
    name.addEventListener('input', () => { nameDraft = name.value; });
    const add = button('Create list', () => {}, locked); add.type = 'submit';
    const purpose = el('select', 'input'); purpose.setAttribute('aria-label', 'List use'); purpose.disabled = locked;
    for (const [value,label] of [['outreach','Outreach & monitoring'],['monitoring','Monitoring only']]) { const option=el('option','',label);option.value=value;purpose.append(option); }
    create.append(name, purpose, add);
    create.addEventListener('submit', e => {
      e.preventDefault(); const value = nameDraft.trim();
      if (!value || locked) return;
      archived = false; perform(operation('list.create', uuid(), 0, { name: value, ...(purpose.value==='monitoring'?{purpose:'monitoring'}:{}) }));
    });
    listHost.append(create);
    for (const list of lists) {
      const count = list.purpose==='monitoring' ? Object.values(list.watches||{}).filter(w=>w.enabled).length : Object.values(state.memberships).filter(m => m.list_id === list.list_id && !m.removed).length;
      const row = button(sanitize(list.name) + ' · ' + count, () => { selected = list.list_id; outreach?.showList(selected); showDetail(); render(); });
      row.className = 'crm-list-row' + (selected === list.list_id ? ' active' : '');
      row.setAttribute('aria-pressed', String(selected === list.list_id)); listHost.append(row);
    }
    if (!lists.length) listHost.append(el('p', 'muted crm-padding', failed ? 'Lists unavailable.' : ready ? 'No lists yet.' : 'Lists are loading.'));
    detailHost.replaceChildren();
    const status = el('div', 'crm-status' + (failed ? ' error' : ''), notice);
    status.setAttribute('role', failed ? 'alert' : 'status'); detailHost.append(status);
    if (undoChange?.payload.list_id === selected) detailHost.append(button('Undo', () => { const op = undoChange; if (op) perform(op); }, locked));
    if (pending) detailHost.append(button('Reconcile saved change', () => perform(pending), busy));
    if (pendingAddition) {
      detailHost.append(button('Continue this contact addition', () => addContact(null), busy));
      detailHost.append(button('Return to list', () => {
        pendingAddition = null; picker = false; failed = false;
        notice = 'Saved contacts remain on the list. Use Check Direct setup to resume any unfinished setup.'; render();
      }, busy));
    }
    const list = state?.lists[selected];
    if (state && outreach?.render(detailHost, list, state, locked)) return;
    if (!list && failed && !ready) return;
    if (!list) { detailHost.append(el('p', 'muted', 'Create a central list for contacts across your managed accounts.')); return; }
    if (renewalMember) {
      const warning = el('section', 'crm-picker');
      warning.append(el('h3', '', 'Enable Direct again'), el('p', 'muted',
        'This changes the selected account’s existing one-to-one conversations to Direct, including conversations manually set to Private. Their existing message history may become visible to the master. Starting a new conversation still needs user approval.'),
      button('Confirm new Direct setup', () => renewDirect(renewalMember), locked),
      button('Keep current settings', () => { renewalMember = null; render(); }, busy)); detailHost.append(warning);
    }
    const head = el('div', 'crm-heading'); head.append(el('h2', '', sanitize(list.name)));
    head.append(button(list.archived ? 'Restore list' : 'Archive list', () => perform(operation(
      list.archived ? 'list.restore' : 'list.archive', list.list_id, list.revision,
      getMembers && !list.archived ? { membership_operations: Object.values(state.memberships)
        .filter(m => m.list_id === list.list_id && !m.removed).map(m => m.operation_id).sort() } : {})), locked));
    detailHost.append(head);
    if (!list.archived && renderMonitoring) renderMonitoring(detailHost,list,{perform,operation,locked,refresh});
    if (getMembers && !list.archived && list.purpose !== 'monitoring') renderPicker(state, locked);
    if (!list.archived) {
      const renameDetails = el('details', 'crm-rename'); renameDetails.append(el('summary', '', 'Rename list'));
      const rename = el('form', 'crm-list-tools');
      const input = el('input', 'input'); input.value = renameDrafts.get(list.list_id)?.value ?? list.name; input.maxLength = 160;
      input.addEventListener('input', () => renameDrafts.set(list.list_id,
        { value: input.value, base: renameDrafts.get(list.list_id)?.base ?? list.revision }));
      input.setAttribute('aria-label', 'List name'); input.disabled = locked;
      const save = button('Rename', () => {}, locked); save.type = 'submit'; rename.append(input, save);
      rename.addEventListener('submit', e => { e.preventDefault(); if (!closed && !locked && input.value.trim()) perform(operation('list.rename', list.list_id,
        renameDrafts.get(list.list_id)?.base ?? list.revision, { name: input.value.trim() })); });
      renameDetails.append(rename); detailHost.append(renameDetails);
    }
    if (list.purpose === 'monitoring') return;
    const members = Object.values(state.memberships).filter(m => m.list_id === selected && !m.removed);
    const filters = el('form', 'crm-list-tools crm-member-filters'), search = el('input', 'input'), sender = el('select', 'input');
    search.value = memberQuery; search.placeholder = 'Find a contact in this list'; search.setAttribute('aria-label', 'Find list contacts');
    sender.setAttribute('aria-label', 'Filter list by sending account');
    const all = el('option', '', 'All sending accounts'); all.value = ''; sender.append(all);
    const owners = new Map();
    for (const member of members) { const binding = state.bindings[member.binding_id]; owners.set(binding.owner_mxid, describeBinding(binding).owner); }
    for (const [owner, label] of owners) { const option = el('option', '', label); option.value = owner; sender.append(option); }
    if (memberOwner && !owners.has(memberOwner)) memberOwner = '';
    sender.value = memberOwner;
    sender.addEventListener('change', () => { if (!closed) { memberOwner = sender.value; memberQuery = search.value; render(); } });
    const find = button('Filter', () => {}); find.type = 'submit'; filters.append(search, sender, find);
    filters.addEventListener('submit', event => { event.preventDefault(); if (!closed) { memberQuery = search.value; render(); } });
    detailHost.append(filters);
    const needle = memberQuery.trim().toLowerCase();
    const visibleMembers = members.filter(member => {
      const binding = state.bindings[member.binding_id];
      return (!memberOwner || binding.owner_mxid === memberOwner) && (!needle ||
        [state.persons[member.person_id]?.label, binding.contact.network_id, binding.contact.source, describeBinding(binding).owner]
          .some(value => typeof value === 'string' && value.toLowerCase().includes(needle)));
    });
    const table = el('table', 'crm-table');
    const heading = el('tr');
    for (const text of [...(outreach ? ['Select'] : []), 'Contact', 'Send from', 'Conversation', ...(outreach ? ['Latest outreach'] : []), '']) { const th = el('th', '', text); th.scope = 'col'; heading.append(th); }
    const thead = el('thead'); thead.append(heading); table.append(thead);
    const body = el('tbody'); table.append(body);
    for (const member of visibleMembers) {
      const person = state.persons[member.person_id], binding = state.bindings[member.binding_id];
      const detail = describeBinding(binding), row = el('tr');
      const contact = el('td', 'crm-person'), owner = el('td', '', detail.owner), access = el('td', '', detail.status), actions = el('td');
      if (outreach) row.append(outreach.selectionCell(list.list_id, member, locked || list.archived, person.label));
      contact.append(button(sanitize(person.label), () => openPerson(person, binding), !detail.available));
      contact.append(locations(person, state));
      row.addEventListener('click', event => {
        if (!closed && detail.available && !event.target.closest('button,input,select,textarea,a,label,details,summary')) openPerson(person, binding);
      });
      if (!list.archived) actions.append(button('Remove', () => perform(operation('member.remove', member.membership_id,
        member.revision, { list_id: list.list_id, person_id: member.person_id })), locked));
      if (getMembers && !list.archived) actions.append(button('Check Direct setup', () => finishDirect(member), locked));
      if (getMembers && !list.archived) actions.append(button('Enable Direct again', () => { renewalMember = member; render(); }, locked));
      if (getMembers && !list.archived) actions.append(button('Change sender', () => {
        picker = true; linkPerson = member.person_id; query = ''; pickerOwner = ''; focusSearchList = selected; render();
      }, locked));
      const management = el('details', 'crm-management'); management.append(el('summary', '', 'Manage'));
      management.append(...Array.from(actions.children)); actions.replaceChildren(management);
      row.append(contact, owner, access);
      owner.setAttribute('data-label', 'Send from'); access.setAttribute('data-label', 'Conversation');
      if (outreach) row.append(outreach.latestCell(state, member));
      row.append(actions); body.append(row);
    }
    detailHost.append(table);
    if (outreach) detailHost.append(outreach.controls(list, state, locked));
    if (!members.length) detailHost.append(el('p', 'muted', 'This list has no contacts.'));
    else if (!visibleMembers.length) detailHost.append(el('p', 'muted', 'No contacts match these filters. Your selected recipients are kept.'));
    detailHost.append(el('p', 'muted crm-footnote', 'Removing a contact or archiving a list keeps its Direct setting unchanged. Undo restores membership only; it does not enable Direct again.'));
    const target = focusTargets.get(focusSearchList === selected ? 'contact-search' : focusKey);
    focusSearchList = null;
    if (target && !target.disabled) {
      target.focus?.({ preventScroll: true });
      if (focusKey === 'contact-search' && selection[0] != null && target.value === query) target.setSelectionRange?.(...selection);
    }
  }
  return {
    open() { if (closed) return; showDetail(); render(); return refresh(); },
    async openList(id) { if(closed)return;await refresh();const list=snapshot().state?.lists[id];if(!list||list.archived)throw new Error('This list is no longer available.');selected=id;archived=false;showDetail();render(); },
    render,
    close() { closed = true; outreach?.close(); pending = null; pendingAddition = null; store = null; renameDrafts.clear(); expandedPeople.clear(); listHost.replaceChildren(); detailHost.replaceChildren(); },
  };
}
