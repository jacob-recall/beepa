import { proposalLabel } from '../../shared/model/proposal-lifecycle.js';
// Presentation for selection → personalization → immutable review → outcomes.
// Source accounts own dispatch; this view never interprets a PUT as delivery.
import { el, sanitize } from '../../shared/ui/el.js';
import { paintReceipt } from '../../shared/model/message_receipts.js';

export function createCRMCompose({ getBatches, onChange, describeBinding, openMessage, selectList = () => {} }) {
  let closed = false, activeList = null, phase = 'list', busy = false, error = '', manifest = null, results = [];
  let epoch = 0;
  let historyEpoch = 0, historyBusy = false, historyPerson = null, resultSerial = 0;
  const historyResults = new Map();
  const historyFilters = { list: '', owner: '', delivery: '', response: '' };
  const selections = new Map(), drafts = new Map(), choices = new Map(), routes = new Map(), excluded = new Map();
  const selected = list => { if (!selections.has(list)) selections.set(list, new Set()); return selections.get(list); };
  const key = (list, person) => list + '/' + person;
  const button = (label, action, disabled = false) => {
    const node = el('button', 'btn', label); node.type = 'button'; node.disabled = disabled;
    node.addEventListener('click', () => { if (!closed && !node.disabled) action(); }); return node;
  };
  const redraw = () => { if (!closed) onChange(); };
  function enterList(list) {
    if (activeList !== list) { activeList = list; phase = 'list'; busy = false; error = ''; manifest = null; results = []; epoch++; }
  }
  async function compose(list, state) {
    if (busy || closed) return;
    enterList(list.list_id); phase = 'compose'; busy = true; error = ''; choices.clear(); routes.clear(); excluded.clear();
    const run = ++epoch; redraw();
    try {
      for (const person of selected(list.list_id)) {
        try {
          const options = await getBatches().routes(list.list_id, person);
          if (closed || run !== epoch) return;
          if (!options.length) throw new Error('No available conversation');
          choices.set(person, options); if (options.length === 1) routes.set(person, options[0].route);
        } catch (e) {
          if (closed || run !== epoch) return;
          excluded.set(person, e.message);
        }
      }
    } catch (e) { if (!closed && run === epoch) error = e.message; }
    finally { if (!closed && run === epoch) { busy = false; redraw(); } }
  }
  async function review(list) {
    if (closed || busy) return;
    const run = ++epoch; busy = true; error = ''; redraw();
    try {
      const items = [...selected(list.list_id)].filter(id => !excluded.has(id)).map(personId => ({ personId,
        body: drafts.get(key(list.list_id, personId)) || '', route: routes.get(personId) }));
      const value = await getBatches().review({ listId: list.list_id, items });
      if (closed || run !== epoch) return;
      manifest = value; phase = 'review';
    } catch (e) { if (!closed && run === epoch) error = e.message; }
    finally { if (!closed && run === epoch) { busy = false; redraw(); } }
  }
  async function load(value, action = 'statuses') {
    if (closed || busy) return;
    const run = ++epoch, serial = ++resultSerial; manifest = value; busy = true; error = ''; phase = 'results'; redraw();
    try {
      const response = await getBatches()[action](value);
      if (closed || run !== epoch) return;
      results = response;
      if ((historyResults.get(value.batch_id)?.serial || 0) <= serial)
        historyResults.set(value.batch_id, { hash: value.payload_hash, results: response, serial });
    } catch (e) { if (!closed && run === epoch) error = e.message; }
    finally { if (!closed && run === epoch) { busy = false; redraw(); } }
  }
  function name(state, id) { return sanitize(state.persons[id]?.label || 'Unavailable contact'); }
  function account(state, item) { return describeBinding(state.bindings[item.binding_id]).owner; }
  function evidence(outcome) {
    const value = outcome?.tracking;
    if (!Number.isSafeInteger(value?.revision) || value.revision < 1
      || typeof outcome?.sent_event_id !== 'string' || !/^\$[^\s]{1,255}$/.test(outcome.sent_event_id)
      || value.delivery?.sent_event_id !== outcome.sent_event_id) return null;
    return value;
  }
  function statusLabel(child, outcome) {
    return (outcome ? proposalLabel(outcome) : child?.state === 'accepted' ? 'Account received batch' : sanitize((child?.state || 'Status not loaded').replaceAll('_', ' ')))
      + (outcome?.reason || child?.reason ? ' · ' + sanitize(outcome?.reason || child.reason) : '');
  }
  function historyEntries(state) {
    return Object.values(state.batches).sort((a, b) => b.reviewed_at - a.reviewed_at || a.batch_id.localeCompare(b.batch_id))
      .flatMap(batch => batch.items.map(item => {
        const cached = historyResults.get(batch.batch_id);
        const child = cached?.hash === batch.payload_hash ? cached.results.find(c => c.child_id === item.child_id) : null;
        const outcome = child?.items?.find(i => i.item_id === item.item_id);
        const proof = evidence(outcome);
        const response = proof?.response;
        return { batch, item, child, outcome, proof,
          delivery: proof?.delivery?.state || outcome?.state || child?.state || 'unavailable',
          response: response?.state === 'replied' && !/^\$[^\s]{1,255}$/.test(response.incoming_event_id || '')
            ? 'reconciling' : response?.state || 'unavailable' };
      }));
  }
  async function refreshHistory(state) {
    if (closed) return;
    const run = ++historyEpoch, serial = ++resultSerial;
    historyBusy = true; historyResults.clear(); redraw();
    try {
      const reads = new Map(), service = getBatches();
      for (const batch of Object.values(state.batches)) {
        let response;
        try { response = await service.statuses(batch, reads); }
        catch (e) { response = batch.children.map(c => ({ child_id: c.child_id, state: 'unavailable', reason: e.message, items: [] })); }
        if (closed || run !== historyEpoch) return;
        if ((historyResults.get(batch.batch_id)?.serial || 0) <= serial)
          historyResults.set(batch.batch_id, { hash: batch.payload_hash, results: response, serial });
      }
    } finally { if (!closed && run === historyEpoch) { historyBusy = false; redraw(); } }
  }
  function openHistory(state, person = null) {
    if (closed) return;
    phase = 'history'; historyPerson = person; epoch++; busy = false;
    Object.keys(historyFilters).forEach(k => { historyFilters[k] = ''; });
    redraw(); return refreshHistory(state);
  }
  function latestCell(state, member) {
    const cell = el('td', 'crm-latest');
    const entry = historyEntries(state).find(e => e.item.person_id === member.person_id && e.item.binding_id === member.binding_id);
    if (!entry) { cell.append(el('span', 'muted', 'No reviewed outreach')); return cell; }
    cell.append(button('Latest reviewed outreach', () => openHistory(state, member.person_id)));
    const line = el('div', 'crm-tracking-line');
    if (['sending', 'sent', 'delivered', 'read', 'failed'].includes(entry.proof?.delivery?.state)) {
      const checks = el('span'); paintReceipt(checks, { sent: entry.delivery === 'sent', delivered: entry.delivery === 'delivered',
        read: entry.delivery === 'read', failed: entry.delivery === 'failed' }); line.append(checks);
    }
    line.append(el('span', 'muted', entry.proof ? entry.delivery.replaceAll('_', ' ') : statusLabel(entry.child, entry.outcome)));
    cell.append(line);
    if (entry.response !== 'unavailable') cell.append(el('div', 'muted', responseLabel(entry.proof.response, true)));
    return cell;
  }
  function renderHistory(host, state) {
    host.append(el('h2', '', historyPerson ? name(state, historyPerson) + ' · Outreach history' : 'Outreach across accounts'),
      button('Back to list', () => { phase = 'list'; redraw(); }), button('Refresh outreach', () => refreshHistory(state), historyBusy));
    if (historyBusy) host.append(el('p', 'muted', 'Refreshing source account outcomes…'));
    const all = historyEntries(state).filter(e => !historyPerson || e.item.person_id === historyPerson);
    const controls = el('div', 'crm-list-tools');
    const filter = (key, label, pairs) => {
      const wrap = el('label', '', label), select = el('select', 'input'); select.setAttribute('aria-label', label);
      for (const [value, text] of [['', 'All'], ...pairs]) { const option = el('option', '', text); option.value = value; select.append(option); }
      select.value = historyFilters[key];
      select.addEventListener('change', () => { if (!closed) { historyFilters[key] = select.value; redraw(); } });
      wrap.append(select); controls.append(wrap);
    };
    const owners = new Map(all.map(e => [e.item.owner_mxid, account(state, e.item)]));
    const lists = new Map(all.map(e => [e.batch.list_id, sanitize(state.lists?.[e.batch.list_id]?.name || e.batch.list_id)]));
    filter('list', 'Outreach list', [...lists]); filter('owner', 'Outreach sending account', [...owners]);
    filter('delivery', 'Outreach message state', [...new Set(all.map(e => e.delivery))].sort().map(v => [v, v.replaceAll('_', ' ')]));
    filter('response', 'Outreach response', [['waiting', 'Awaiting reply'], ['replied', 'Replied'], ['reconciling', 'Reconciling'], ['unavailable', 'Unavailable']]);
    host.append(controls, el('p', 'muted', 'Newest review first. Sending, delivery, read and response are separate source-confirmed outcomes.'));
    const entries = all.filter(e => (!historyFilters.list || e.batch.list_id === historyFilters.list)
      && (!historyFilters.owner || e.item.owner_mxid === historyFilters.owner)
      && (!historyFilters.delivery || e.delivery === historyFilters.delivery)
      && (!historyFilters.response || e.response === historyFilters.response));
    for (const { batch, item, child, outcome } of entries) {
      const panel = el('section', 'crm-compose-item');
      panel.append(el('strong', '', name(state, item.person_id)),
        el('p', 'muted', account(state, item) + ' · ' + sanitize(item.route.source) + ' · ' + sanitize(state.lists?.[batch.list_id]?.name || batch.list_id)
          + ' · Reviewed ' + new Date(batch.reviewed_at).toLocaleString()),
        el('p', 'crm-message-body', item.body), el('p', 'muted', statusLabel(child, outcome)));
      tracking(panel, item, outcome);
      panel.append(button('View batch', () => { activeList = batch.list_id; selectList(batch.list_id); load(batch); }, busy)); host.append(panel);
    }
    if (!entries.length) host.append(el('p', 'muted', 'No outreach matches these filters.'));
  }
  function tracking(panel, item, outcome) {
    const event = value => typeof value === 'string' && /^\$[^\s]{1,255}$/.test(value);
    const room = value => typeof value === 'string' && /^![^\s:]+:[^\s]+$/.test(value);
    const value = outcome?.tracking, delivery = value?.delivery, response = value?.response;
    if (!Number.isSafeInteger(value?.revision) || value.revision < 1 || !event(outcome.sent_event_id)
      || delivery?.sent_event_id !== outcome.sent_event_id) return;
    if (['sending', 'sent', 'delivered', 'read', 'failed'].includes(delivery.state)) {
      const line = el('p', 'crm-tracking-line'), checks = el('span');
      paintReceipt(checks, { sent: delivery.state === 'sent', delivered: delivery.state === 'delivered',
        read: delivery.state === 'read', failed: delivery.state === 'failed' });
      line.append(checks, el('span', '', { sending: 'Sending', sent: 'Sent · awaiting delivery confirmation',
        delivered: 'Delivered', read: 'Read', failed: 'Send failed' }[delivery.state])); panel.append(line);
    }
    if (openMessage) panel.append(button('Open message', () => openMessage(item, outcome, 'delivery'),
      !event(delivery.mirror_event_id) || !room(delivery.mirror_room_id)));
    if (!response || !['waiting', 'replied', 'reconciling'].includes(response.state)) return;
    // A response never supplies receipt evidence. A retained uncertain link
    // stays explicitly reconciling instead of asserting a new reply match.
    const linked = event(response.incoming_event_id);
    const label = response.state === 'replied' && !linked ? 'Response link is reconciling' : responseLabel(response);
    panel.append(el('p', 'muted', label + (response.reason ? ' · ' + sanitize(response.reason) : '')));
    if (response.evidence_kind === 'observed_native') panel.append(el('p', 'muted',
      'Based on messages observed by the bridge. Later history sync may revise the response link.'));
    if (openMessage && linked) panel.append(button(response.state === 'reconciling' ? 'Open previously linked reply' : 'Open reply',
      () => openMessage(item, outcome, 'response'), !event(response.mirror_event_id) || !room(response.mirror_room_id)));
  }
  function responseLabel(response, compact = false) {
    if (response.evidence_kind === 'observed_native') return { waiting: 'No reply in observed history',
      replied: 'Replied · observed history', reconciling: 'Observed response link is reconciling' }[response.state] || 'Response unavailable';
    return { waiting: 'Awaiting reply', replied: compact ? 'Replied' : 'Replied to this message',
      reconciling: compact ? 'Response reconciling' : 'Response link is reconciling' }[response.state] || 'Response unavailable';
  }
  function reviewWarnings(host, state, batch) {
    const destinations = new Map();
    for (const item of batch.items) {
      const destination = item.route.source + '/' + item.route.network_id.trim().toLowerCase();
      if (!destinations.has(destination)) destinations.set(destination, []);
      destinations.get(destination).push(item);
      if (/\{\{[^{}]+\}\}/.test(item.body)) host.append(el('p', 'error',
        name(state, item.person_id) + ' has an unresolved {{placeholder}}. Fill it before submitting.'));
    }
    for (const items of destinations.values()) if (items.length > 1) host.append(el('p', 'error',
      'Potential duplicate destination: ' + items.map(item => name(state, item.person_id) + ' via ' + account(state, item)).join(', ')
      + '. Both messages are included; check the recipients before submitting.'));
  }
  function selectionCell(listId, member, disabled, label) {
    const cell = el('td'), input = el('input'); input.type = 'checkbox'; input.checked = selected(listId).has(member.person_id); input.disabled = disabled;
    input.setAttribute('aria-label', 'Select ' + label);
    input.addEventListener('change', () => { if (closed) return; const set = selected(listId); input.checked ? set.add(member.person_id) : set.delete(member.person_id); redraw(); });
    cell.append(input); return cell;
  }
  function controls(list, state, locked) {
    enterList(list.list_id);
    const wrap = el('div', 'crm-batch-controls'), count = selected(list.list_id).size;
    wrap.append(el('span', 'muted', count + ' selected'), button('Personalize messages', () => compose(list, state), locked || !count || list.archived));
    const batches = Object.values(state.batches).filter(b => b.list_id === list.list_id).sort((a, b) => b.reviewed_at - a.reviewed_at);
    if (batches.length) {
      const history = el('details', 'crm-batch-history'); history.append(el('summary', '', 'Reviewed batches (' + batches.length + ')'));
      for (const batch of batches) history.append(button(new Date(batch.reviewed_at).toLocaleString() + ' · ' + batch.items.length + ' messages', () => load(batch), locked));
      wrap.append(history);
    }
    return wrap;
  }
  function render(host, list, state, locked) {
    if (phase === 'history') { renderHistory(host, state); return true; }
    if (!list) return false;
    enterList(list.list_id);
    if (phase === 'list') return false;
    const heading = { compose: 'Personalize messages', review: 'Review batch', results: 'Batch progress' }[phase];
    host.append(el('h2', '', heading), button('Back to list', () => { phase = 'list'; epoch++; busy = false; redraw(); }, busy));
    if (error) { const message = el('p', 'error', error); message.setAttribute('role', 'alert'); host.append(message); }
    if (busy) { const status = el('p', 'muted', phase === 'compose' ? 'Checking sending accounts…' : 'Reconciling batch…'); status.setAttribute('role', 'status'); host.append(status); }
    if (phase === 'compose') {
      host.append(el('p', 'muted', (selected(list.list_id).size - excluded.size) + ' included · ' + excluded.size + ' excluded'));
      for (const person of selected(list.list_id)) {
        const member = state.memberships[key(list.list_id, person)], binding = member && state.bindings[member.binding_id];
        const panel = el('section', 'crm-compose-item'); panel.append(el('h3', '', name(state, person)), el('p', 'muted', 'Send through ' + describeBinding(binding).owner));
        if (excluded.has(person)) {
          panel.append(el('p', 'error', 'Excluded · ' + sanitize(excluded.get(person)))); host.append(panel); continue;
        }
        const options = choices.get(person) || [], select = el('select', 'input'); select.setAttribute('aria-label', 'Conversation for ' + name(state, person)); select.disabled = busy;
        const empty = el('option', '', 'Choose conversation'); empty.value = ''; select.append(empty);
        options.forEach((option, index) => { const node = el('option', '', option.label); node.value = String(index); select.append(node); });
        const index = options.findIndex(o => o.route === routes.get(person)); select.value = index < 0 ? '' : String(index);
        select.addEventListener('change', () => { if (!closed) routes.set(person, select.value === '' ? undefined : options[Number(select.value)]?.route); });
        panel.append(select);
        const input = el('textarea', 'input'); input.value = drafts.get(key(list.list_id, person)) || ''; input.maxLength = 8000;
        input.setAttribute('aria-label', 'Message to ' + name(state, person)); input.disabled = busy;
        input.addEventListener('input', () => { if (!closed) drafts.set(key(list.list_id, person), input.value); }); panel.append(input); host.append(panel);
      }
      host.append(button('Recheck sending accounts', () => compose(list, state), busy),
        button('Review batch', () => review(list), busy || locked || !!error || !choices.size));
    } else if (manifest) {
      if (phase === 'review') {
        host.append(el('p', 'muted', manifest.items.length + ' included · ' + excluded.size + ' excluded'));
        for (const [person, reason] of excluded) host.append(el('p', 'muted', name(state, person) + ' · Excluded · ' + sanitize(reason)));
        reviewWarnings(host, state, manifest);
      }
      const cancelRequested = !!state.batch_cancellations?.[manifest.batch_id];
      if (phase === 'results' && cancelRequested) host.append(el('p', 'muted',
        'Cancellation requested. Each account must confirm; messages already attempted cannot be recalled.'));
      for (const group of manifest.children) {
        const child = results.find(r => r.child_id === group.child_id), items = manifest.items.filter(i => i.child_id === group.child_id);
        host.append(el('h3', '', 'Send through ' + account(state, items[0])));
        for (const item of items) {
          const outcome = child?.items?.find(i => i.item_id === item.item_id);
          const panel = el('section', 'crm-compose-item'); panel.append(el('strong', '', name(state, item.person_id)),
            el('p', 'muted', sanitize(item.route.source)), el('p', 'crm-message-body', item.body));
          panel.append(el('p', 'muted', phase === 'review'
            ? item.route.mode === 'direct_existing' ? 'Existing Direct conversation' : 'User approval required to start this conversation'
            : outcome ? proposalLabel(outcome) + (outcome.reason ? ' · ' + outcome.reason : '')
              : (child?.state || 'Not submitted').replaceAll('_', ' ') + (child?.reason ? ' · ' + child.reason : '')));
          if (phase === 'results') tracking(panel, item, outcome);
          host.append(panel);
        }
      }
      if (phase === 'review') host.append(button('Edit messages', () => { phase = 'compose'; manifest = null; redraw(); }, busy),
        button('Submit reviewed batch', () => load(manifest, 'submit'), busy || locked || manifest.items.some(item => /\{\{[^{}]+\}\}/.test(item.body))));
      else host.append(button('Refresh progress', () => load(manifest), busy),
        button(cancelRequested ? 'Reconcile cancellation' : 'Reconcile submission', () => load(manifest, cancelRequested ? 'cancel' : 'submit'), busy || locked),
        ...(!cancelRequested ? [button('Cancel unattempted messages', () => load(manifest, 'cancel'), busy || locked)] : []));
    }
    return true;
  }
  return { selectionCell, controls, render, latestCell, openHistory, refreshHistory,
    showList(listId) { enterList(listId); phase = 'list'; epoch++; busy = false; },
    close() { closed = true; epoch++; historyEpoch++; historyResults.clear(); selections.clear(); drafts.clear(); choices.clear(); routes.clear(); excluded.clear(); manifest = null; results = []; } };
}
