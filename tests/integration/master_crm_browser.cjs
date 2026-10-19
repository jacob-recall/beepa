// Browser integration of the actual master entry point. Every request is
// intercepted: no production homeserver, contact, credential or native send.
// PLAYWRIGHT_MODULE may point to a provided playwright-core installation.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../..');

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium',
    headless: true, args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [], writes = [], events = [], unknown = [], direct = [], order = [], contexts = [], cancellations = [];
    const rooms = {}, results = new Map(), directTransactions = new Map(), batchTransactions = new Map(), staged = [], batches = [];
    let crmBootstrapAttempts = 0, contactNotification = null, contactRows = [];
    const manager = '@manager:master';
    const state = (owner, type, content, state_key = '') => ({ type, state_key, sender: owner, content });
    const addRoom = (id, states, timeline = []) => { rooms[id] = { state: { events: states }, timeline: { events: timeline } }; };
    const contact = (name, handle, version, person = null) => ({ source: 'imessage', network_id: handle,
      kind: 'email', display_name: name, person_id: person, person_display: person ? name : '', contact_version: version, deleted: false });
    const syntheticContacts = {
      elliot: [contact('Ada Rivera', 'ada.elliot@example.invalid', '7', 'elliot-ada')],
      david: [contact('Sam Chen', 'sam.david@example.invalid', '9'), contact('Ada Rivera', 'ada.david@example.invalid', '11'),
        contact('New conversation contact', 'new.david@example.invalid', '13'),
        { ...contact('Revoked contact', 'revoked@example.invalid', '4'), deleted: true }],
    };
    for (const label of ['elliot', 'david']) {
      const owner = '@' + label + ':master', proposal = '!proposals-' + label + ':master', contacts = '!contacts-' + label + ':master';
      const capability = { schema: 1, owner_mxid: owner, generation: 'generation-' + label, journal_epoch: 'journal-' + label,
        ordered_policy: true, direct_prepare_commit: true, direct_cancel: true, batch: true };
      const children = [proposal, contacts, ...(label === 'elliot' ? ['!conversation-ada:master'] : [])];
      addRoom('!space-' + label + ':master', [state(owner, 'm.room.create', { type: 'm.space' }),
        state(owner, 'm.room.name', { name: 'space:' + label }), ...children.map(id => state(owner, 'm.space.child', { via: ['master'] }, id))]);
      addRoom(contacts, [state(owner, 'm.room.create', {}), state(owner, 'com.jkali.contacts', {}),
        ...syntheticContacts[label].map((value, index) => state(owner, 'com.jkali.contact', value, 'contact-' + index))]);
      addRoom(proposal, [state(owner, 'm.room.create', {}), state(owner, 'com.jkali.proposals', {}), state(owner, 'com.jkali.crm.capability', capability),
        state(owner, 'm.room.member', { membership: 'join' }, owner), state(owner, 'm.room.member', { membership: 'join' }, manager),
        state(owner, 'm.room.power_levels', { users: { [owner]: 100, [manager]: 50 }, users_default: 0, events_default: 100, state_default: 100,
          invite: 100, kick: 100, ban: 100, redact: 100, events: { 'com.jkali.proposal': 50, 'com.jkali.crm.direct_request': 50, 'com.jkali.crm.batch_request': 50 } }),
        state(owner, 'm.room.join_rules', { join_rule: 'invite' }), state(owner, 'm.room.history_visibility', { history_visibility: 'shared' }),
        state(owner, 'm.room.guest_access', { guest_access: 'forbidden' })]);
    }
    addRoom('!conversation-ada:master', [state('@elliot:master', 'm.room.create', { 'com.jkali.mirror_of': '!native-ada:local' }),
      state('@elliot:master', 'm.room.name', { name: 'Ada Rivera' }), state('@elliot:master', 'com.jkali.profile', { id: 'elliot-ada', displayName: 'Ada Rivera' }),
      state('@elliot:master', 'com.jkali.source', { source: 'imessage' }), state('@elliot:master', 'com.jkali.share_level', { level: 'direct' })],
      [{ event_id: '$synthetic-conversation', sender: '@elliot:master', type: 'm.room.message', origin_server_ts: 1000,
        content: { msgtype: 'm.text', body: 'Synthetic conversation shown in the regular viewer.' } }]);
    const stateFor = room => [...rooms[room].state.events, ...[...results.values()].filter(value => value.room === room).map(value => value.event)];
    await page.addInitScript(() => localStorage.setItem('beepa_teammate_names', JSON.stringify({ elliot: 'Elliot', david: 'David' })));
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url());
      const reply = data => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) });
      if (url.origin !== 'http://127.0.0.1:9876') { unknown.push(req.method() + ' foreign-origin'); return route.abort(); }
      const name = decodeURIComponent(url.pathname);
      if (name === '/_matrix/client/v3/login') return reply({ access_token: 'synthetic-session', user_id: '@manager:master' });
      if (name === '/_matrix/client/v3/sync') return reply({ next_batch: 'sync-1', rooms: { join: Object.fromEntries(Object.entries(rooms).map(([id,room]) => [id, { ...room, state: { events: stateFor(id) } }])) } });
      if (name === '/admin/notifications') {
        const data=req.postDataJSON();if(data.action==='seen'){assert.equal(data.id,contactNotification.id);contactNotification.seen=1;return reply({seen:true});}
        return reply({follows:[],history:contactNotification?[contactNotification]:[],devices:0});
      }
      if (name === '/admin/control') {
        const data=req.postDataJSON();if(data.action==='status')return reply({routing_ready:true});
        if(data.action==='contact_updates')return reply({updates:contactRows.filter(row=>row.list_id===data.list_id)});
        throw Error('Unexpected control request');
      }
      if (name === '/admin/crm-room') {
        if (++crmBootstrapAttempts === 1) return route.fulfill({status:404,contentType:'application/json',body:JSON.stringify({error:'not found'})});
        return reply({ room_id: '!crm:master', manager_mxid: '@manager:master', schema_version: 1 });
      }
      if (name === '/_matrix/client/v3/rooms/!crm:master/messages') {
        const from = Number(url.searchParams.get('from') || 0);
        return reply({ chunk: events.slice(from), ...(from < events.length ? { end: String(events.length) } : {}) });
      }
      if (name.startsWith('/_matrix/client/v3/rooms/!crm:master/send/com.jkali.crm.operation/crm_')) {
        const op = req.postDataJSON(), prior = writes.find(x => x.op_id === op.op_id);
        if (!prior) { writes.push(op); order.push({ kind: 'metadata', op }); events.push({ event_id: '$event-' + writes.length,
          sender: '@manager:master', type: 'com.jkali.crm.operation', content: op }); }
        return reply({ event_id: '$event-' + (writes.indexOf(prior || op) + 1) });
      }
      const roomPath = name.match(/^\/_matrix\/client\/v3\/rooms\/([^/]+)\/(state|messages)$/);
      if (roomPath && rooms[roomPath[1]]) {
        if (roomPath[2] === 'state') return reply(stateFor(roomPath[1]));
        const timeline = rooms[roomPath[1]].timeline.events, from = Number(url.searchParams.get('from') || 0);
        return reply({ chunk: timeline.slice(from), ...(from < timeline.length ? { end: String(timeline.length) } : {}) });
      }
      const contextPath = name.match(/^\/_matrix\/client\/v3\/rooms\/([^/]+)\/context\/(\$[^/]+)$/);
      if (contextPath && rooms[contextPath[1]]) {
        const timeline = rooms[contextPath[1]].timeline.events, index = timeline.findIndex(e => e.event_id === contextPath[2]);
        assert.ok(index >= 0, 'context targets an exact known message'); contexts.push(contextPath[2]);
        return reply({ event: timeline[index], events_before: timeline.slice(0, index).reverse(), events_after: timeline.slice(index + 1) });
      }
      const control = name.match(/^\/_matrix\/client\/v3\/rooms\/(!proposals-(elliot|david):master)\/send\/com\.jkali\.crm\.direct_request\/(crm_direct_.+)$/);
      if (control && req.method() === 'PUT') {
        const body = req.postDataJSON(), owner = '@' + control[2] + ':master';
        assert.equal(body.owner_mxid, owner); assert.equal(body.generation, 'generation-' + control[2]); assert.equal(body.journal_epoch, 'journal-' + control[2]);
        assert.ok(['prepare', 'commit', 'cancel'].includes(body.phase), 'only typed Direct protocol requests');
        const tx = control[3], key = owner + '/' + body.command_id;
        if (directTransactions.has(tx)) assert.deepEqual(directTransactions.get(tx), body);
        else {
          directTransactions.set(tx, body); direct.push({ owner, body }); order.push({ kind: 'direct', body });
          const result = { schema: 1, command_id: body.command_id, owner_mxid: owner, generation: body.generation, journal_epoch: body.journal_epoch,
            state: body.phase === 'commit' ? 'applied' : body.contact.network_id.startsWith('new.') ? 'approval_required' : 'prepared', phase: body.phase };
          if (body.phase === 'cancel') { result.state = 'canceled'; result.safe_to_change_membership = true; }
          if (result.state === 'prepared') result.ticket_id = 'a'.repeat(64);
          if (body.phase === 'commit') {
            assert.equal(body.ticket_id, 'a'.repeat(64));
            result.rooms = [{ room_id: body.contact.network_id === 'ada.elliot@example.invalid' ? '!native-ada:local' : '!native-other:local', state: 'applied', revision: 3 }];
          }
          results.set(key, { room: control[1], event: state(owner, 'com.jkali.crm.direct_result', result, body.command_id) });
        }
        return reply({ event_id: '$direct-' + directTransactions.size });
      }
      const batchWrite = name.match(/^\/_matrix\/client\/v3\/rooms\/(!proposals-(elliot|david):master)\/send\/(com\.jkali\.proposal|com\.jkali\.crm\.batch_request)\/(crm_(?:proposal|batch)_.+)$/);
      if (batchWrite && req.method() === 'PUT') {
        const body=req.postDataJSON(), owner='@'+batchWrite[2]+':master', prior=batchTransactions.get(batchWrite[4]);
        if(prior){assert.deepEqual(prior.body,body);return reply({event_id:prior.event_id});}
        const event_id='$batch-transport-'+batchTransactions.size;
        batchTransactions.set(batchWrite[4],{body,event_id});
        const parent=writes.find(op=>op.type==='batch.review'&&op.entity_id===(body.batch_id||body['com.jkali.crm.item']?.batch_id));
        assert.ok(parent,'canonical reviewed parent is durable before any source staging');
        if(batchWrite[3]==='com.jkali.proposal') {
          const annotation=body['com.jkali.crm.item'];assert.equal(annotation.owner_mxid,owner);
          assert.equal(body.body,parent.payload.items.find(item=>item.item_id===annotation.item_id).body);
          staged.push({room:batchWrite[1],body,event_id});order.push({kind:'stage',body});
          rooms[batchWrite[1]].timeline.events.push({type:'com.jkali.proposal',sender:manager,event_id,room_id:batchWrite[1],content:body});
        } else {
          assert.equal(body.owner_mxid,owner);assert.equal(body.generation,'generation-'+batchWrite[2]);assert.equal(body.journal_epoch,'journal-'+batchWrite[2]);
          if(body.phase==='cancel') {
            assert.ok(writes.some(op=>op.type==='batch.cancel_requested'&&op.entity_id===body.batch_id),'cancel intent durable before source cancel');
            const previous=results.get('batch/'+body.child_id).event.content;
            assert.equal(body.child_hash,previous.child_hash);cancellations.push(body);
            const result={...previous,state:'canceled',items:previous.items.map(item=>item.state==='sent'?item:{...item,state:'canceled'})};
            results.set('batch/'+body.child_id,{room:batchWrite[1],event:state(owner,'com.jkali.crm.batch_result',result,body.child_id)});
            return reply({event_id});
          }
          for(const item of body.items){const proposal=staged.find(p=>p.event_id===item.proposal_event_id);assert.ok(proposal);assert.equal(proposal.room,batchWrite[1]);assert.equal(proposal.body['com.jkali.crm.item'].child_hash,body.child_hash);}
          batches.push(body);order.push({kind:'batch',body});
          const result={schema:1,batch_id:body.batch_id,child_id:body.child_id,owner_mxid:owner,generation:body.generation,journal_epoch:body.journal_epoch,child_hash:body.child_hash,state:'accepted',items:body.items.map(item=>({item_id:item.item_id,state:item.route.mode==='direct_existing'?'accepted':'waiting_for_approval'}))};
          results.set('batch/'+body.child_id,{room:batchWrite[1],event:state(owner,'com.jkali.crm.batch_result',result,body.child_id)});
        }
        return reply({event_id});
      }
      if (name === '/_matrix/client/v3/logout') return reply({});
      if (name === '/apps/master/config.js') return route.fulfill({ contentType: 'text/javascript', body: '' });
      // Static allowlist deliberately excludes every session/config/secret file.
      if (req.method() === 'GET' && /^(\/apps\/master\/|\/shared\/)/.test(name)
          && /\.(html|js|css|svg|png)$/.test(name) && !name.includes('..')) {
        const file = path.join(root, name);
        if (fs.existsSync(file)) return route.fulfill({ path: file });
      }
      unknown.push(req.method() + ' ' + name); return route.abort();
    });
    await page.goto('http://127.0.0.1:9876/apps/master/index.html');
    await page.locator('#in-user').fill('manager');
    await page.locator('#in-pass').fill('synthetic-password');
    await page.locator('#btn-signin').click();
    await page.locator('#shell').waitFor({ state: 'visible' });
    await page.getByRole('button', { name: 'Contact lists', exact: true }).click();
    await page.getByRole('alert').filter({hasText:'updated master service'}).waitFor();
    assert.equal(await page.getByText('Lists are loading.',{exact:true}).count(),0);
    assert.equal(await page.getByRole('button',{name:'Create list',exact:true}).isDisabled(),true);
    await page.getByRole('button',{name:'Refresh',exact:true}).click();
    const name = page.getByRole('textbox', { name: 'New list name', exact: true });
    await name.fill('Founder introductions');
    await page.getByRole('button', { name: 'Create list', exact: true }).click();
    await page.getByRole('heading', { name: 'Founder introductions', exact: true }).waitFor();
    assert.equal(writes.length, 1); assert.equal(writes[0].type, 'list.create');
    await page.locator('.crm-rename summary').click();
    await page.getByRole('textbox', { name: 'List name', exact: true }).fill('Across Elliot and David');
    await page.getByRole('button', { name: 'Rename', exact: true }).click();
    await page.getByRole('heading', { name: 'Across Elliot and David', exact: true }).waitFor();
    await page.getByRole('button', { name: 'Archive list', exact: true }).click();
    await page.getByRole('button', { name: 'Archived lists', exact: true }).click();
    await page.getByRole('button', { name: 'Restore list', exact: true }).click();
    await page.getByRole('button', { name: 'Active lists', exact: true }).click();
    await page.getByRole('heading', { name: 'Across Elliot and David', exact: true }).waitFor();
    assert.deepEqual(writes.map(x => x.type), ['list.create', 'list.rename', 'list.archive', 'list.restore']);
    const centralList = writes[0].entity_id;
    const addContact = async (handle, personLabel = null, approval = false) => {
      await page.getByRole('textbox', { name: 'Find shared contacts', exact: true }).fill(handle);
      assert.equal(await page.locator('.crm-contact-choice').filter({ hasText: 'revoked@example.invalid' }).count(), 0);
      if (personLabel) { await page.locator('.crm-picker-link summary').click(); await page.getByRole('combobox', { name: 'Link shared contact to person', exact: true }).selectOption({ label: personLabel }); }
      const choice = page.locator('.crm-contact-choice').filter({ hasText: handle });
      await choice.getByRole('button', { name: /^Add / }).click();
      await page.getByText(approval ? 'Contact added. Starting a new conversation still needs user approval.' : 'Contact added. Direct confirmed by the sending account.', { exact: true }).waitFor();
    };
    await addContact('ada.elliot@example.invalid');
    assert.equal(await page.getByRole('textbox',{name:'Find shared contacts',exact:true}).evaluate(e=>e===document.activeElement),true,'addition returns focus to inline search');
    await page.getByRole('textbox',{name:'Find shared contacts',exact:true}).fill('ada.elliot');
    assert.match(await page.locator('.crm-contact-choice').innerText(),/Added/);
    assert.equal(await page.locator('.crm-contact-choice button').count(),0,'duplicate add absent');
    await addContact('sam.david@example.invalid');
    await addContact('new.david@example.invalid', null, true);
    assert.equal(await page.locator('.crm-table tbody tr').count(), 3);
    const samRow=page.locator('.crm-table tbody tr').filter({hasText:'Sam Chen'});
    await samRow.locator('.crm-management summary').click();
    await samRow.getByRole('button',{name:'Remove',exact:true}).click();
    await page.getByText('Contact removed. Direct stays unchanged.',{exact:true}).waitFor();
    assert.equal(await page.locator('.crm-table tbody tr').count(),2);
    const grantsBeforeUndo=direct.length;
    await page.getByRole('button',{name:'Undo',exact:true}).click();
    await page.getByText('Saved',{exact:true}).waitFor();
    assert.equal(await page.locator('.crm-table tbody tr').count(),3);
    assert.equal(direct.length,grantsBeforeUndo,'Undo removal does not grant Direct');
    assert.match(await page.locator('.crm-table').innerText(), /Elliot/);
    assert.match(await page.locator('.crm-table').innerText(), /David/);
    const adaPerson = writes.find(x => x.type === 'person.create' && x.payload.label === 'Ada Rivera').entity_id;
    const initialPeople = writes.filter(x => x.type === 'person.create').length;
    const elliotBinding = writes.find(x => x.type === 'binding.create' && x.payload.contact.network_id === 'ada.elliot@example.invalid');
    assert.equal(elliotBinding.payload.person_id, adaPerson);
    await name.fill('Shared person follow-up');
    await page.getByRole('button', { name: 'Create list', exact: true }).click();
    await page.getByRole('heading', { name: 'Shared person follow-up', exact: true }).waitFor();
    await addContact('ada.david@example.invalid', 'Ada Rivera');
    const davidBinding = writes.find(x => x.type === 'binding.create' && x.payload.contact.network_id === 'ada.david@example.invalid');
    assert.equal(davidBinding.payload.person_id, adaPerson, 'explicit shared person link retained');
    assert.equal(writes.filter(x => x.type === 'person.create').length, initialPeople, 'linking does not create another person');
    assert.notEqual(davidBinding.payload.owner_mxid, elliotBinding.payload.owner_mxid);
    assert.notEqual(davidBinding.entity_id, elliotBinding.entity_id, 'outreach histories retain separate account bindings');
    assert.equal(await page.locator('.crm-table tbody tr').count(), 1);
    assert.match(await page.locator('.crm-table').innerText(), /David/);
    for (const request of direct.filter(x => x.body.phase === 'prepare')) {
      const command = request.body.command_id;
      const prepareIndex = order.findIndex(x => x.kind === 'direct' && x.body.command_id === command && x.body.phase === 'prepare');
      const memberIndex = order.findIndex(x => x.kind === 'metadata' && x.op.op_id === command && x.op.type === 'member.set');
      const commitIndex = order.findIndex(x => x.kind === 'direct' && x.body.command_id === command && x.body.phase === 'commit');
      assert.ok(memberIndex > prepareIndex, 'source preparation precedes canonical membership');
      if (request.body.contact.network_id.startsWith('new.')) assert.equal(commitIndex, -1, 'new conversation needs user approval');
      else assert.ok(commitIndex > memberIndex, 'canonical membership precedes Direct commit');
    }
    assert.equal(direct.filter(x => x.body.phase === 'prepare').length, 4);
    assert.equal(direct.filter(x => x.body.phase === 'commit').length, 3);
    await page.reload();
    await page.getByRole('button', { name: 'Contact lists', exact: true }).click();
    await page.getByRole('heading', { name: 'Across Elliot and David', exact: true }).waitFor();
    assert.equal(await page.locator('.crm-table tbody tr').count(), 3, 'canonical cross-account membership survives reopen');
    assert.equal(writes.filter(x => x.type === 'member.set' && x.payload.list_id === centralList).length, 4);
    await page.locator('.crm-table tbody tr').filter({ hasText: 'Elliot' }).getByRole('button', { name: 'Ada Rivera', exact: true }).click();
    await page.getByText('Synthetic conversation shown in the regular viewer.', { exact: true }).last().waitFor();
    assert.equal(await page.locator('#detail-crm').isVisible(), false, 'CRM contact uses regular conversation view');
    if (process.env.CRM_SCREENSHOT) await page.screenshot({ path: process.env.CRM_SCREENSHOT.replace(/\.png$/, '-conversation.png'), fullPage: true });
    await page.getByRole('button', { name: 'Contact lists', exact: true }).click();
    await page.getByRole('heading', { name: 'Across Elliot and David', exact: true }).waitFor();
    await page.waitForFunction(() => document.querySelector('input[aria-label="New list name"]')?.disabled === false);
    if (process.env.CRM_SCREENSHOT) {
      await page.screenshot({ path: process.env.CRM_SCREENSHOT, fullPage: true });
      await page.getByRole('textbox', { name: 'Find shared contacts', exact: true }).fill('Ada');
      await page.screenshot({ path: process.env.CRM_SCREENSHOT.replace(/\.png$/, '-picker.png'), fullPage: true });
      await page.getByRole('button', { name: 'Hide results', exact: true }).click();
    }
    await page.getByRole('checkbox', { name: 'Select Ada Rivera', exact: true }).check();
    await page.getByRole('checkbox', { name: 'Select New conversation contact', exact: true }).check();
    await page.getByRole('button', { name: 'Personalize messages', exact: true }).click();
    await page.getByRole('textbox', { name: 'Message to Ada Rivera', exact: true }).fill('Hello Ada, a personal introduction through Elliot.');
    await page.getByRole('textbox', { name: 'Message to New conversation contact', exact: true }).fill('Hello, an introduction for David to approve.');
    assert.equal(staged.length,0);assert.equal(batches.length,0);
    assert.equal(await page.getByRole('button', { name: 'Review batch', exact: true }).isEnabled(),true,await page.locator('#detail-crm').innerText());
    await page.getByRole('button', { name: 'Review batch', exact: true }).click();
    await page.getByRole('heading', { name: 'Review batch', exact: true }).waitFor();
    assert.match(await page.locator('#detail-crm').innerText(),/Send through Elliot/);
    assert.match(await page.locator('#detail-crm').innerText(),/Send through David/);
    await page.getByText('User approval required to start this conversation',{exact:true}).waitFor();
    assert.equal(staged.length,0);assert.equal(batches.length,0);
    if(process.env.CRM_SCREENSHOT)await page.screenshot({path:process.env.CRM_SCREENSHOT.replace(/\.png$/,'-batch-review.png'),fullPage:true});
    await page.getByRole('button',{name:'Submit reviewed batch',exact:true}).click();
    await page.getByText('Sending through account',{exact:true}).waitFor();
    await page.getByText('Queued · awaiting user',{exact:true}).waitFor();
    assert.equal(staged.length,2);assert.equal(batches.length,2);
    assert.deepEqual(batches.map(b=>b.owner_mxid).sort(),['@david:master','@elliot:master']);
    assert.equal(writes.filter(op=>op.type==='batch.review').length,1);
    await page.getByRole('button',{name:'Reconcile submission',exact:true}).click();
    await page.getByText('Sending through account',{exact:true}).waitFor();
    await page.waitForFunction(()=>[...document.querySelectorAll('button')].some(button=>button.textContent==='Refresh progress'&&!button.disabled));
    assert.equal(staged.length,2);assert.equal(batches.length,2,'reconciliation cannot duplicate accepted children');
    if(process.env.CRM_SCREENSHOT)await page.screenshot({path:process.env.CRM_SCREENSHOT.replace(/\.png$/,'-batch-progress.png'),fullPage:true});
    const elliotBatch=batches.find(batch=>batch.owner_mxid==='@elliot:master');
    const elliotResult=results.get('batch/'+elliotBatch.child_id).event.content;
    const tracked=elliotResult.items[0];
    Object.assign(tracked,{state:'sent',sent_event_id:'$local-outreach',sent_room_id:'!native-ada:local',tracking:{revision:1,
      delivery:{state:'delivered',sent_event_id:'$local-outreach',mirror_event_id:'$mirror-outreach',mirror_room_id:'!conversation-ada:master'},
      response:{state:'replied',incoming_event_id:'$local-reply',mirror_event_id:'$mirror-reply',mirror_room_id:'!conversation-ada:master',source_order:2}}});
    const sent={event_id:'$mirror-outreach',room_id:'!conversation-ada:master',sender:'@elliot:master',type:'m.room.message',origin_server_ts:2000,
      content:{msgtype:'m.text',body:'Hello Ada, a personal introduction through Elliot.','com.jkali.from_me':true}};
    const response={event_id:'$mirror-reply',room_id:'!conversation-ada:master',sender:'@elliot:master',type:'m.room.message',origin_server_ts:3000,
      content:{msgtype:'m.text',body:'Synthetic reply linked by exact message identity.'}};
    rooms['!conversation-ada:master'].timeline.events.push(sent,response);
    rooms['!conversation-ada:master'].state.events.push(state('@elliot:master','com.jkali.message_receipt',{delivered:true},'$mirror-outreach'));
    await page.getByRole('button',{name:'Refresh progress',exact:true}).click();
    await page.getByText('Replied to this message',{exact:true}).waitFor();
    assert.equal(await page.locator('#detail-crm .message-receipt.delivered').count(),1,'reply does not turn delivered checks blue');
    assert.equal(await page.locator('#detail-crm .message-receipt.read').count(),0);
    tracked.tracking.revision=2;tracked.tracking.delivery.state='read';
    await page.getByRole('button',{name:'Refresh progress',exact:true}).click();
    await page.locator('#detail-crm .message-receipt.read').waitFor();
    if(process.env.CRM_SCREENSHOT)await page.screenshot({path:process.env.CRM_SCREENSHOT.replace(/\.png$/,'-tracking.png'),fullPage:true});
    await page.getByRole('button',{name:'Open reply',exact:true}).click();
    try { await page.locator('#room-messages [data-event-id="$mirror-reply"]').waitFor(); }
    catch(error) { console.error('Synthetic tracking navigation diagnostics',JSON.stringify({contexts,errors,unknown,room:await page.locator('#detail-room').innerText()}));throw error; }
    assert.equal(contexts.at(-1),'$mirror-reply');assert.equal(await page.locator('#detail-crm').isVisible(),false);
    await page.getByRole('button',{name:'Contact lists',exact:true}).click();
    await page.getByRole('heading',{name:'Batch progress',exact:true}).waitFor();
    await page.getByRole('button',{name:'Open message',exact:true}).click();
    await page.locator('#room-messages [data-event-id="$mirror-outreach"]').waitFor();assert.equal(contexts.at(-1),'$mirror-outreach');
    await page.getByRole('button',{name:'Contact lists',exact:true}).click();
    await page.getByRole('button',{name:'Cancel unattempted messages',exact:true}).click();
    await page.getByRole('button',{name:'Reconcile cancellation',exact:true}).waitFor();
    assert.equal(cancellations.length,2);assert.equal(writes.filter(op=>op.type==='batch.cancel_requested').length,1);
    assert.equal(await page.locator('#detail-crm .message-receipt.read').count(),1,'cancel keeps attempted message evidence');
    await page.reload();
    await page.getByRole('button',{name:'Contact lists',exact:true}).click();
    await page.locator('.crm-batch-history summary').click();
    await page.locator('.crm-batch-history button').click();
    await page.getByRole('button',{name:'Reconcile cancellation',exact:true}).waitFor();
    assert.equal(await page.getByRole('button',{name:'Reconcile submission',exact:true}).count(),0,'reopened cancellation cannot offer resubmission');
    assert.equal(batches.length,2);assert.equal(staged.length,2);
    await page.getByRole('button',{name:'Back to list',exact:true}).click();
    assert.equal(await page.locator('.crm-latest .message-receipt.read').count(),1);
    await page.getByRole('button',{name:'Outreach across accounts',exact:true}).click();
    await page.getByRole('heading',{name:'Outreach across accounts',exact:true}).waitFor();
    await page.waitForFunction(()=>[...document.querySelectorAll('button')].some(b=>b.textContent==='Refresh outreach'&&!b.disabled));
    assert.equal(await page.locator('.crm-compose-item').count(),2);
    await page.getByLabel('Outreach sending account',{exact:true}).selectOption('@elliot:master');
    assert.equal(await page.locator('.crm-compose-item').count(),1);
    assert.match(await page.locator('.crm-compose-item').innerText(),/Ada Rivera/);
    await page.getByLabel('Outreach response',{exact:true}).selectOption('replied');
    assert.equal(await page.locator('#detail-crm .message-receipt.read').count(),1);
    if(process.env.CRM_SCREENSHOT)await page.screenshot({path:process.env.CRM_SCREENSHOT.replace(/\.png$/,'-outreach.png'),fullPage:true});
    await page.getByRole('button',{name:'Back to list',exact:true}).click();
    await page.locator('.crm-table tbody tr').filter({hasText:'Ada Rivera'}).getByRole('button',{name:'Latest reviewed outreach',exact:true}).click();
    await page.getByRole('heading',{name:'Ada Rivera · Outreach history',exact:true}).waitFor();
    assert.equal(await page.locator('.crm-compose-item').count(),1);
    await page.getByRole('button',{name:'Back to list',exact:true}).click();
    await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'populated mobile list fits');
    await page.getByRole('textbox',{name:'Find shared contacts',exact:true}).fill('Ada');
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'mobile inline picker fits');
    if(process.env.CRM_SCREENSHOT)await page.screenshot({path:process.env.CRM_SCREENSHOT.replace(/\.png$/,'-mobile-picker.png'),fullPage:true});
    await name.fill('Mobile list');
    await page.getByRole('button', { name: 'Create list', exact: true }).click();
    await page.getByRole('heading', { name: 'Mobile list', exact: true }).waitFor();
    const inlineSearch=page.getByRole('textbox',{name:'Find shared contacts',exact:true});
    await inlineSearch.fill('Ada');
    const senderChoice=page.getByRole('combobox',{name:'Send from for Ada Rivera',exact:true});
    assert.equal(await page.getByRole('button',{name:'Add Ada Rivera',exact:true}).isDisabled(),true,'linked person needs deliberate sender choice');
    await senderChoice.focus();
    await senderChoice.selectOption({label:'David · imessage · ada.david@example.invalid'});
    assert.equal(await senderChoice.evaluate(e=>e===document.activeElement),true,'sender selection survives rerender');
    await page.getByRole('button',{name:'Add Ada Rivera',exact:true}).click();
    await page.getByText('Contact added. Direct confirmed by the sending account.',{exact:true}).waitFor();
    assert.equal(await inlineSearch.evaluate(e=>e===document.activeElement),true);
    assert.match(await page.locator('.crm-table').innerText(),/David/);
    await page.getByRole('combobox',{name:'Filter shared contacts by account',exact:true}).selectOption('@elliot:master');
    await inlineSearch.fill('sam.david');
    await page.getByText('No shared contacts match. Try another name or account.',{exact:true}).waitFor();
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mobile layout fits');
    await page.getByRole('button', { name: 'Feed', exact: true }).click();
    assert.equal(await page.locator('#detail-crm').isVisible(), false);
    // Open a contact reminder from a cold app load: wait for list hydration,
    // select the referenced list rather than the last viewed list, never a room.
    const member=writes.find(op=>op.type==='member.set'&&op.payload.list_id===centralList);
    const binding=writes.find(op=>op.type==='binding.create'&&op.entity_id===member.payload.binding_id);
    contactRows=[{kind:'contact',list_id:centralList,binding_id:binding.entity_id,owner:binding.payload.owner_mxid,generation:binding.payload.generation,title:'Synthetic contact task',source:'imessage',status_label:'No shared conversation linked',fresh:true,conversation_ids:[]}];
    contactNotification={id:'a'.repeat(64),kind:'contact_reminder',room:'',event:'',created:Date.now(),title:'Contact-by reminder',deliveries:[],seen:0,
      rule_refs:JSON.stringify({contact:{binding:binding.entity_id,owner:binding.payload.owner_mxid,generation:binding.payload.generation},rules:[{id:centralList}]})};
    await page.goto('http://127.0.0.1:9876/apps/master/index.html#notification='+contactNotification.id);
    await page.getByRole('heading',{name:'Across Elliot and David',exact:true}).waitFor();
    await page.getByText('No shared conversation linked',{exact:true}).waitFor();
    assert.equal(contactNotification.seen,1);assert.equal(await page.locator('#detail-crm').isVisible(),true);
    assert.deepEqual(errors, []); assert.deepEqual(unknown, []);
    console.log('master_crm_browser: actual app lists, exact regular conversations, cross-account review, source approval/queue outcomes, exact receipt/reply tracking, message context navigation, durable cancellation/reload and mobile layout passed; zero live requests');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
