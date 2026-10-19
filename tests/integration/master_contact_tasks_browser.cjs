// Real contact-task and notification components; all services are synthetic.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright-core');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 try{
  const page=await browser.newPage({viewport:{width:1200,height:900}}),calls=[],errors=[];
  let revision=3,contact={kind:'contact',binding_id:'ada-david',owner:'@david:test',generation:'g1',title:'Ada',source:'imessage',status_label:'No shared conversation linked',fresh:true,conversation_ids:[],due_at:null,snooze_until:null,resolved:false};
  const nid='a'.repeat(64),ref={contact:{kind:'contact_reminder',binding:'ada-david',owner:'@david:test',generation:'g1',created:100,episode:100,due:200,slot:200},rules:[{id:'outreach',revision:3,slot:200}],due:200,slot:200};
  const notification={id:nid,kind:'contact_reminder',room:'',event:'',title:'Contact-by reminder',created:Date.now(),rule_refs:JSON.stringify(ref),seen:0,deliveries:[{state:'accepted'}]};
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
   const req=route.request(),u=new URL(req.url());assert.equal(u.origin,'http://127.0.0.1:9876');
   const reply=data=>route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
   if(u.pathname==='/admin/control'){
    assert.equal(req.headers().authorization,'Bearer fixture-token');const d=req.postDataJSON();calls.push(d);
    if(d.action==='status')return reply({routing_ready:true});
    if(d.action==='contact_updates')return reply({updates:[contact]});
    if(d.action==='contact_obligation'){
     assert.equal(d.list_id,'outreach');assert.equal(d.binding_id,'ada-david');assert.equal(d.revision,revision);revision++;
     assert.deepEqual(Object.keys(d).sort(),['action','binding_id','changes','list_id','revision']);
     contact={...contact,...d.changes};return reply({saved:true});
    }
    throw Error('Unexpected control action '+d.action);
   }
   if(u.pathname==='/admin/notifications'){
    const d=req.postDataJSON();calls.push(d);
    if(d.action==='status')return reply({history:[notification],follows:[],devices:0});
    if(d.action==='seen'){assert.equal(d.id,nid);notification.seen=1;return reply({seen:true});}
    throw Error('Unexpected notification action');
   }
   if(u.pathname==='/')return route.fulfill({contentType:'text/html',body:`<!doctype html><link rel="stylesheet" href="/apps/master/style.css"><div id="tasks"></div><button id="nav-notifications">Notifications</button><button id="room-follow"></button><dialog id="notifications-dialog"><h2 id="notification-title"></h2><div id="notification-controls"></div><h3 id="notification-follow-title"></h3><div id="notification-follows"></div><h3 id="notification-history-title"></h3><div id="notification-history"></div><p id="notification-status"></p></dialog><script type="module" src="/fixture.js"></script>`});
   if(u.pathname==='/fixture.js')return route.fulfill({contentType:'text/javascript',body:`
    import {createControl} from '/apps/master/control.js';import {createNotifications} from '/apps/master/notifications.js';
    let revision=3;const host=document.getElementById('tasks');
    const control=createControl({base:location.origin,token:'fixture-token',current:()=>true,openRoom:id=>window.openedRoom=id});
    const render=()=>{host.replaceChildren();control.renderMonitoring(host,{list_id:'outreach',revision,monitoring:{enabled:true}},
     {locked:false,perform:()=>{throw Error('Not a conversation operation')},operation:()=>{throw Error('No fake room')},refresh:async()=>{revision++;render();}});};render();
    window.notifs=createNotifications({base:location.origin,token:'fixture-token',userId:'@manager:test',current:()=>true,getRoom:()=>null,getVerifiedRooms:()=>[],openMessage:()=>{throw Error('Contact notification opened a fake room')},openContact:async refs=>{window.contactOpened=refs;}});
    window.notifs.start();`});
   const file=path.join(root,u.pathname);if(req.method()==='GET'&&/^\/(apps\/master|shared)\//.test(u.pathname)&&!u.pathname.includes('..')&&fs.existsSync(file))return route.fulfill({path:file});
   throw Error('Unexpected route '+u.pathname);
  });
  await page.goto('http://127.0.0.1:9876/');
  const show=async()=>page.getByRole('button',{name:'Uncontacted & contact tasks',exact:true}).click();
  await show();await page.getByText('Ada · @david:test · imessage',{exact:true}).waitFor();
  assert.equal(await page.getByRole('button',{name:'Open conversation',exact:true}).count(),0);
  await page.getByRole('button',{name:'Set contact-by date',exact:true}).click();await page.getByText('Choose a date and time',{exact:true}).waitFor();assert.equal(calls.filter(c=>c.action==='contact_obligation').length,0);
  await page.getByLabel('Contact date for Ada · @david:test',{exact:true}).fill('2030-01-02T10:30');await page.getByRole('button',{name:'Set contact-by date',exact:true}).click();
  await page.getByRole('button',{name:'Uncontacted & contact tasks',exact:true}).waitFor();assert(Number.isSafeInteger(contact.due_at));
  const due=contact.due_at;await show();await page.getByLabel('Contact date for Ada · @david:test',{exact:true}).fill('2030-01-03T12:00');await page.getByRole('button',{name:'Snooze contact until date',exact:true}).click();
  await page.getByRole('button',{name:'Uncontacted & contact tasks',exact:true}).waitFor();assert.equal(contact.due_at,due);assert(contact.snooze_until>due);
  await show();await page.getByRole('button',{name:'Resolve contact task',exact:true}).click();await page.getByRole('button',{name:'Uncontacted & contact tasks',exact:true}).waitFor();assert.equal(contact.resolved,true);
  await show();await page.getByRole('button',{name:'Reopen contact task',exact:true}).click();await page.getByRole('button',{name:'Uncontacted & contact tasks',exact:true}).waitFor();assert.equal(contact.resolved,false);
  await show();await page.getByRole('button',{name:'Clear contact-by date',exact:true}).click();await page.getByRole('button',{name:'Uncontacted & contact tasks',exact:true}).waitFor();assert.equal(contact.due_at,null);
  contact.conversation_ids=['!real:test'];await show();await page.getByRole('button',{name:'Open conversation',exact:true}).click();assert.equal(await page.evaluate(()=>window.openedRoom),'!real:test');
  await page.evaluate(async id=>{location.hash='#notification='+id;await window.notifs.handleLink();},nid);assert.deepEqual(await page.evaluate(()=>window.contactOpened),ref);assert.equal(notification.seen,1);
  assert.deepEqual(errors,[]);console.log('Chromium contact tasks: exact account/binding edits, dates/snooze/resolve, real-room navigation and typed notification link passed.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
