// Actual list/control components with fully synthetic intercepted services.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright-core');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 try{
  const page=await browser.newPage({viewport:{width:1200,height:900}}),errors=[],calls=[];
  let offline=false,loseCommandResponse=true,jobs=[],preferences={revision:0,model:'fixture-model',accounts:['@david:test'],settings:{allow_model_context:true,notifications_enabled:true,sending_mode:'draft_confirm',allowed_accounts:null,retention_days:30}};
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
   const req=route.request(),u=new URL(req.url());assert.equal(u.origin,'http://127.0.0.1:9876');
   const reply=data=>route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
   if(u.pathname==='/admin/control'){
    assert.equal(req.headers().authorization,'Bearer fixture-token');const d=req.postDataJSON();calls.push(d);
    if(d.action==='status')return reply({paired:true,routing_ready:true,transport_ready:!offline,commands:jobs,health:{telegram_poll:{error:'Telegram polling is retrying.',stale:false},monitoring:{error:null,stale:false}}});
    if(d.action==='command'){
     let job=jobs.find(j=>j.request_id===d.request_id);if(!job){job={request_id:d.request_id,state:'queued',delivery:[],drafts:[]};jobs.unshift(job);}
     if(loseCommandResponse){loseCommandResponse=false;return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'Fixture response lost'})});}
     return reply(job);
    }
    if(d.action==='command_cancel'){const job=jobs.find(j=>j.request_id===d.request_id);job.state='canceled';return reply(job);}
    if(d.action==='settings')return reply(preferences);
    if(d.action==='settings_update'){assert.equal(d.revision,preferences.revision);preferences={...preferences,revision:preferences.revision+1,settings:d.settings};return reply(preferences);}
    if(d.action==='test')return reply({queued:true});
    if(d.action==='tool')return reply([{id:'!chat:test',owner:'@david:test',title:'Ada',source:'imessage'}]);
    if(d.action==='updates'){const saved=await page.evaluate(()=>window.ops.filter(o=>o.type==='list.obligation').at(-1)?.payload||{});return reply({updates:[{id:'!chat:test',owner:'@david:test',title:'Ada',source:'imessage',waiting:'waiting_for_us',fresh:true,last:{id:'$last'},coverage:'native_history',latest_messages:[{id:'$last',text:'Fixture question'}],obligations:[{event_id:'$last',timestamp:100000,waiting:'waiting_for_us',due_at:saved.due_at??null,snooze_until:saved.snooze_until??null,answered_by:null}]}]});}
    throw Error('unexpected control action');
   }
   if(u.pathname==='/')return route.fulfill({contentType:'text/html',body:'<!doctype html><meta name="viewport" content="width=device-width"><link rel="stylesheet" href="/shared/style/organic.css"><link rel="stylesheet" href="/apps/master/style.css"><link rel="stylesheet" href="/shared/style/beepa.css"><button id="telegram">Telegram settings</button><div id="list"></div><div id="detail"></div><script type="module" src="/fixture.js"></script>'});
   if(u.pathname==='/fixture.js')return route.fulfill({contentType:'text/javascript',body:`
    import {createControl} from '/apps/master/control.js';import {createCRMView} from '/apps/master/crm-view.js';import {createCRMState,applyCRMOperation} from '/shared/model/crm.js';
    let state=createCRMState({workspace_id:'!crm:test',manager_mxid:'@manager:test'});window.ops=[];
    const store={snapshot:()=>({state,ready:true,roomId:'!crm:test'}),refresh:async()=>store.snapshot(),append:async op=>{window.ops.push(op);const r=applyCRMOperation(state,op);state=r.state;return r.outcome;}};
    const control=createControl({base:location.origin,token:'fixture-token',current:()=>true,openRoom:id=>window.opened=id});
    const view=createCRMView({getStore:()=>store,listHost:document.getElementById('list'),detailHost:document.getElementById('detail'),isVisible:()=>true,showDetail:()=>{},describeBinding:()=>({}),renderMonitoring:(...args)=>control.renderMonitoring(...args)});
    document.getElementById('telegram').onclick=()=>control.open();view.open();`});
   const file=path.join(root,u.pathname);if(req.method()==='GET'&&/^\/(apps\/master|shared)\//.test(u.pathname)&&!u.pathname.includes('..')&&fs.existsSync(file))return route.fulfill({path:file});
   throw Error('unexpected route '+u.pathname);
  });
  await page.goto('http://127.0.0.1:9876/');await page.getByLabel('New list name',{exact:true}).fill('Following');await page.getByLabel('List use',{exact:true}).selectOption('monitoring');await page.getByRole('button',{name:'Create list',exact:true}).click();
  await page.getByText('Alerts & waiting',{exact:true}).click();await page.getByLabel('Notify me about this list',{exact:true}).check();
  assert((await page.getByLabel('Notify me about this list',{exact:true}).boundingBox()).width<=24,'checkbox must not inherit full-width text-input styling');
  assert.equal(await page.getByLabel('Delivery hours',{exact:true}).inputValue(),'none');assert.equal(await page.getByLabel('Repeat overdue reminders (hours)',{exact:true}).inputValue(),'');assert.equal(await page.getByLabel('Timezone',{exact:true}).isVisible(),false);
  assert.equal(await page.getByLabel('Message updates',{exact:true}).inputValue(),'immediate');assert.equal(await page.getByLabel('Collection window (minutes)',{exact:true}).isVisible(),false);
  await page.getByLabel('Message updates',{exact:true}).selectOption('digest');await page.getByLabel('Collection window (minutes)',{exact:true}).fill('30');
  await page.getByLabel('Delivery hours',{exact:true}).selectOption('quiet');await page.getByLabel('Timezone',{exact:true}).fill('America/Los_Angeles');
  await page.getByLabel('Repeat overdue reminders (hours)',{exact:true}).fill('2');
  await page.getByLabel('Delivery preference',{exact:true}).selectOption('browser');await page.getByLabel('Use the other channel if delivery fails',{exact:true}).check();
  if(process.env.SCHEDULE_SCREENSHOT)await page.screenshot({path:process.env.SCHEDULE_SCREENSHOT,fullPage:true});
  await page.getByRole('button',{name:'Save alerts',exact:true}).click();
  await page.waitForFunction(()=>window.ops.some(o=>o.type==='list.monitoring'));
  await page.getByText('Alerts & waiting',{exact:true}).click();await page.getByLabel('Add a conversation to monitoring').fill('Ada');await page.getByRole('button',{name:'Add Ada · @david:test · imessage',exact:true}).click();
  await page.waitForFunction(()=>window.ops.some(o=>o.type==='list.watch'));
  await page.getByRole('button',{name:'Show waiting conversations',exact:true}).click();await page.getByText('waiting for us',{exact:true}).waitFor();await page.getByRole('button',{name:'Ada · @david:test · imessage',exact:true}).click();assert.equal(await page.evaluate(()=>window.opened),'!chat:test');
  const ops=await page.evaluate(()=>window.ops);assert.deepEqual(ops.map(o=>o.type),['list.create','list.monitoring','list.watch']);assert.equal(ops[1].payload.waiting_us_ms,null);assert.equal(ops[1].payload.waiting_them_ms,null);
  assert.equal(ops[1].payload.repeat_ms,7200000);assert.deepEqual(ops[1].payload.schedule,{timezone:'America/Los_Angeles',mode:'quiet',days:[1,2,3,4,5,6,7],start_minute:1320,end_minute:480});
  assert.deepEqual(ops[1].payload.presentation,{mode:'digest',interval_ms:1800000});
  assert.deepEqual(ops[1].payload.routing,{primary:'browser',fallback:true});
  await page.locator('.control-obligation summary').click();await page.getByText('Fixture question',{exact:true}).waitFor();
  await page.getByLabel('Date for message $last',{exact:true}).fill('2026-10-20T15:30');await page.getByRole('button',{name:'Set deadline',exact:true}).click();
  await page.waitForFunction(()=>window.ops.some(o=>o.type==='list.obligation'));
  const change=await page.evaluate(()=>window.ops.at(-1));assert.equal(change.type,'list.obligation');assert.equal(change.payload.event_id,'$last');assert.equal(change.payload.owner_mxid,'@david:test');assert(Number.isSafeInteger(change.payload.due_at));
  await page.getByRole('button',{name:'Show waiting conversations',exact:true}).click();
  await page.locator('.control-obligation summary').click();await page.getByLabel('Date for message $last',{exact:true}).fill('2026-10-21T15:30');
  await page.getByRole('button',{name:'Snooze until date',exact:true}).click();
  await page.waitForFunction(()=>window.ops.at(-1).type==='list.obligation'&&window.ops.at(-1).payload.snooze_until);
  const snoozed=await page.evaluate(()=>window.ops.at(-1).payload);assert.equal(snoozed.due_at,change.payload.due_at);assert(Number.isSafeInteger(snoozed.episode_at));
  await page.getByRole('button',{name:'Show waiting conversations',exact:true}).click();await page.locator('.control-obligation summary').click();await page.getByRole('button',{name:'Clear snooze',exact:true}).click();
  await page.waitForFunction(()=>window.ops.at(-1).type==='list.obligation'&&window.ops.at(-1).payload.snooze_until===null);
  const cleared=await page.evaluate(()=>window.ops.at(-1).payload);assert.equal(cleared.due_at,change.payload.due_at);assert(cleared.episode_at>snoozed.episode_at);
  await page.getByRole('button',{name:'Show waiting conversations',exact:true}).click();
  const custom=page.getByText('Conversation alert settings',{exact:true}).locator('..');await custom.locator('summary').click();
  await custom.getByLabel('Conversation Delivery preference',{exact:true}).selectOption('telegram');await custom.getByLabel('Conversation Use the other channel if delivery fails',{exact:true}).uncheck();
  await custom.getByLabel('Delivery hours',{exact:true}).selectOption('none');await custom.getByLabel('Repeat overdue reminders (hours)',{exact:true}).fill('');await custom.getByLabel('Waiting for us (hours)',{exact:true}).fill('4');
  await custom.getByLabel('New-message alerts',{exact:true}).uncheck();await custom.getByRole('button',{name:'Save conversation alerts',exact:true}).click();
  await page.waitForFunction(()=>window.ops.at(-1).type==='list.watch'&&window.ops.at(-1).payload.waiting_us_ms===14400000);
  const override=await page.evaluate(()=>window.ops.at(-1).payload);assert.equal(override.schedule,null);assert.equal(override.repeat_ms,null);assert.equal(override.activity_enabled,false);
  assert.deepEqual(override.routing,{primary:'telegram',fallback:false});
  await page.getByRole('button',{name:'Show waiting conversations',exact:true}).click();await page.getByText('Conversation alert settings',{exact:true}).click();await page.getByRole('button',{name:'Use list alert settings',exact:true}).click();
  await page.waitForFunction(()=>window.ops.at(-1).type==='list.watch'&&!('schedule' in window.ops.at(-1).payload));
  assert(!Object.hasOwn(await page.evaluate(()=>window.ops.at(-1).payload),'routing'));assert(!Object.hasOwn(await page.evaluate(()=>window.ops.at(-1).payload),'channels'));
  await page.getByRole('button',{name:'Telegram settings',exact:true}).click();await page.getByText('Your Telegram account is connected.',{exact:true}).waitFor();await page.getByRole('button',{name:'Send test alert',exact:true}).click();await page.getByText('Test requested. Check Telegram.',{exact:true}).waitFor();assert(calls.some(d=>d.action==='test'));
  await page.getByText('Receiving Telegram messages: Telegram polling is retrying.',{exact:true}).waitFor();
  await page.getByText('Conversation monitoring: Connected',{exact:true}).waitFor();
  await page.getByLabel('Ask Beepa',{exact:true}).fill('/lists');await page.getByRole('button',{name:'Send request',exact:true}).click();
  await page.getByText('Fixture response lost Retry the same text to check the same request.',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Send request',exact:true}).click();await page.getByText('Request saved. You can close this window.',{exact:true}).waitFor();
  const attempts=calls.filter(c=>c.action==='command');assert.equal(attempts.length,2);assert.equal(attempts[0].request_id,attempts[1].request_id);assert.equal(attempts[0].session_id,attempts[1].session_id);assert.equal(jobs.length,1);
  await page.getByRole('button',{name:'Cancel request',exact:true}).click();await page.locator('.control-command-job').filter({hasText:'Canceled'}).waitFor();
  await page.getByLabel('Ask Beepa',{exact:true}).fill('/help');await page.getByRole('button',{name:'Send request',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('[aria-label="Ask Beepa"]').value==='');jobs[0].state='processing';await page.locator('.control-command-job').filter({hasText:'Working'}).waitFor();
  jobs[0].state='processed';jobs[0].delivery=[{state:'pending',count:1}];await page.locator('.control-command-job').filter({hasText:'Reply waiting for delivery'}).waitFor();
  jobs[0].delivery=[{state:'accepted',count:1}];await page.locator('.control-command-job').filter({hasText:'Reply accepted by Telegram'}).waitFor();
  if(process.env.COMMAND_SCREENSHOT)await page.screenshot({path:process.env.COMMAND_SCREENSHOT,fullPage:true});
  await page.getByLabel('Ask Beepa',{exact:true}).fill('Another queued request');await page.getByRole('button',{name:'Send request',exact:true}).click();
  await page.getByRole('button',{name:'Cancel request',exact:true}).waitFor();const submissionCount=calls.filter(c=>c.action==='command').length;
  await page.getByRole('button',{name:'Close',exact:true}).click();await page.getByRole('button',{name:'Telegram settings',exact:true}).click();
  await page.getByRole('button',{name:'Cancel request',exact:true}).waitFor();assert.equal(calls.filter(c=>c.action==='command').length,submissionCount);
  await page.getByRole('button',{name:'Cancel request',exact:true}).click();
  await page.getByText('Access & retention',{exact:true}).click();await page.getByLabel('Sending access',{exact:true}).selectOption('read_only');await page.getByLabel('Keep cached control content for days',{exact:true}).fill('7');
  await page.getByRole('button',{name:'Save access settings',exact:true}).click();await page.getByText('Access & retention',{exact:true}).click();
  assert.equal(await page.getByLabel('Sending access',{exact:true}).inputValue(),'read_only');assert.equal(await page.getByLabel('Keep cached control content for days',{exact:true}).inputValue(),'7');
  assert(calls.some(d=>d.action==='settings_update'&&d.settings.retention_days===7&&d.settings.sending_mode==='read_only'));
  await page.getByRole('button',{name:'Close',exact:true}).click();assert.deepEqual(errors,[]);
  offline=true;await page.getByRole('button',{name:'Telegram settings',exact:true}).click();
  await page.getByText('Your Telegram account is paired. Telegram is reconnecting; browser controls remain available.',{exact:true}).waitFor();
  assert(await page.getByRole('button',{name:'Send test alert',exact:true}).isDisabled());
  assert(await page.getByRole('button',{name:'Disconnect Telegram',exact:true}).isEnabled());
  await page.getByText('Access & retention',{exact:true}).click();assert(await page.getByRole('button',{name:'Save access settings',exact:true}).isEnabled());
  await page.getByRole('button',{name:'Close',exact:true}).click();assert.deepEqual(errors,[]);
  if(process.env.SCREENSHOT)await page.screenshot({path:process.env.SCREENSHOT,fullPage:true});
  console.log('Control browser: canonical list creation, alerts, no default deadlines, inline search/watch, regular conversation navigation and Telegram test passed.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
