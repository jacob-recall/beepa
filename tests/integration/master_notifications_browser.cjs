// Actual master notification UI, fully intercepted synthetic service/Matrix.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright-core');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 try{
  const page=await browser.newPage({viewport:{width:1200,height:900}});const errors=[],unknown=[],calls=[];let follows=[],history=[],archive={};
  const room='!chat:master',owner='@elliot:master';const state=(type,content,key='')=>({type,content,state_key:key,sender:owner});
  const rooms={'!space:master':{state:{events:[state('m.room.create',{type:'m.space'}),state('m.room.name',{name:'space:elliot'}),state('m.space.child',{via:['master']},room)]}},[room]:{state:{events:[state('m.room.create',{'com.jkali.mirror_of':'!source:local'}),state('m.room.name',{name:'Ada'}),state('com.jkali.share_level',{level:'direct'})]},timeline:{events:[]}}};
  const other='!second:master',otherOwner='@david:master',otherState=(type,content,key='')=>({type,content,state_key:key,sender:otherOwner});
  rooms['!david-space:master']={state:{events:[otherState('m.room.create',{type:'m.space'}),otherState('m.room.name',{name:'space:david'}),otherState('m.space.child',{via:['master']},other)]}};
  rooms[other]={state:{events:[otherState('m.room.create',{'com.jkali.mirror_of':'!second-native:local'}),otherState('m.room.name',{name:'Bea'}),otherState('com.jkali.share_level',{level:'direct'})]},timeline:{events:[]}};
  await page.addInitScript(()=>{
   window.Notification={permission:'default',requestPermission:async()=>{Notification.permission='granted';return 'granted'}};window.PushManager=function(){};
   window.testPush={registered:[],subscribed:0,unsubscribed:0};
   const sub={toJSON:()=>({endpoint:'https://updates.push.services.mozilla.com/wpush/v2/test',keys:{auth:'test',p256dh:'test'}}),unsubscribe:async()=>testPush.unsubscribed++};
   const registration={pushManager:{getSubscription:async()=>testPush.subscribed?sub:null,subscribe:async()=>{testPush.subscribed++;return sub}}};
   Object.defineProperty(navigator,'serviceWorker',{value:{register:async url=>{testPush.registered.push(String(url));return registration},ready:Promise.resolve(registration),getRegistration:async()=>({pushManager:{getSubscription:async()=>sub}})}});
  });
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
   const req=route.request(),url=new URL(req.url()),p=decodeURIComponent(url.pathname);const reply=data=>route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
   if(url.origin!=='http://127.0.0.1:9876'){unknown.push(req.method()+' foreign');return route.abort();}
   if(p==='/_matrix/client/v3/login')return reply({access_token:'synthetic-token',user_id:'@manager:master'});
   if(p==='/_matrix/client/v3/sync')return reply({next_batch:'n',rooms:{join:rooms}});
   if(p==='/admin/notifications'){
    assert.equal(req.headers().authorization,'Bearer synthetic-token');const d=req.postDataJSON();calls.push(d);
    if(d.action==='client_status')return reply({recorded:true});
    if(d.action==='status')return reply({follows,history,devices:1});
    if(d.action==='key')return reply({public_key:Buffer.from([4,...Array(64).fill(1)]).toString('base64url')});
    if(d.action==='subscribe')return reply({device:'fixture-device'});
    if(d.action==='follow'){follows=[{room,owner,title:'Ada',all_messages:d.all_messages||false}];return reply({following:true});}
    if(d.action==='unfollow'){follows=[];return reply({following:false});}
    if(d.action==='seen'){history=history.map(h=>h.id===d.id?{...h,seen:1}:h);if(archive[d.id])archive[d.id].seen=1;return reply({seen:true});}
    if(d.action==='delivery'){const item=history.find(h=>h.id===d.id)||archive[d.id];assert(item);return reply(item);}
    if(['test','unsubscribe'].includes(d.action))return reply({accepted:true});
    if(d.action==='sync')return reply({follows,history,devices:1});
    throw Error('unexpected action');
   }
   if(p===`/_matrix/client/v3/rooms/${room}/messages`)return reply({chunk:[]});
   if(p===`/_matrix/client/v3/rooms/${other}/messages`)return reply({chunk:[]});
   if(p===`/_matrix/client/v3/rooms/${other}/context/$second`)return reply({event:{type:'m.room.message',event_id:'$second',sender:otherOwner,origin_server_ts:Date.now(),content:{msgtype:'m.text',body:'Synthetic second-account message'}},events_before:[],events_after:[]});
   if(p===`/_matrix/client/v3/rooms/${room}/context/$incoming`)return reply({event:{type:'m.room.message',event_id:'$incoming',sender:owner,origin_server_ts:Date.now(),content:{msgtype:'m.text',body:'Synthetic incoming reply'}},events_before:[],events_after:[]});
   if(p==='/apps/master/config.js')return route.fulfill({contentType:'text/javascript',body:''});
   if(req.method()==='GET'&&/^(\/apps\/master\/|\/shared\/)/.test(p)&&/\.(js|html|css|svg|png)$/.test(p)&&!p.includes('..')){const file=path.join(root,p);if(fs.existsSync(file))return route.fulfill({path:file});}
   unknown.push(req.method()+' '+p);return route.abort();
  });
  await page.goto('http://127.0.0.1:9876/apps/master/index.html');await page.locator('#in-user').fill('manager');await page.locator('#in-pass').fill('synthetic-password');await page.locator('#btn-signin').click();
  await page.locator('#list-body .convo').filter({hasText:'Ada'}).click();await page.locator('#room-follow').click();await page.getByRole('button',{name:'Following',exact:true}).waitFor();
  await page.getByRole('button',{name:'Notifications',exact:true}).click();await page.getByRole('button',{name:'Enable browser notifications',exact:true}).click();await page.getByText('This browser is connected. Use Send test notification to check Mac pop-ups.',{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>testPush.subscribed),1);assert.match((await page.evaluate(()=>testPush.registered))[0],/notification-sw.js\?api=/);
  await page.getByRole('button',{name:'Send test notification',exact:true}).click();await page.getByText('Test queued. Delivery status appears below.',{exact:true}).waitFor();
  await page.getByLabel('Notification mode for Ada',{exact:true}).selectOption('all');await page.waitForFunction(()=>document.querySelector('#notification-follows select')?.value==='all');
  history=[{id:'entry',room,event:'$incoming',title:'Ada',created:Date.now(),seen:0}];await page.getByRole('button',{name:'Close',exact:true}).click();await page.getByRole('button',{name:'Notifications',exact:true}).click();await page.getByRole('button',{name:'● Ada',exact:true}).click();
  await page.getByText('Synthetic incoming reply',{exact:true}).waitFor();assert.equal(history[0].seen,1);
  assert.equal(calls.some(c=>c.action==='follow'&&c.all_messages===true),true);
  assert.equal(calls.some(c=>c.action==='seen'),true);
  const firstId='a'.repeat(64),secondId='b'.repeat(64),unrelatedId='c'.repeat(64);
  history=[{id:firstId,room,event:'$incoming',title:'Ada · Elliot',created:Date.now(),seen:0},{id:unrelatedId,room,event:'$incoming',title:'Unrelated notification',created:Date.now(),seen:0}];
  archive[secondId]={id:secondId,room:other,event:'$second',title:'Bea · David',created:Date.now()-86400000,seen:0};
  await page.evaluate(hash=>location.hash=hash,'#notification-group='+firstId+','+secondId);await page.getByText('Choose a conversation from this digest.',{exact:true}).waitFor();
  assert.equal(await page.locator('#notification-history .notification-entry').count(),2);assert.equal(await page.getByRole('button',{name:'● Unrelated notification',exact:true}).count(),0);
  assert.equal(await page.locator('#notification-controls').isVisible(),false);
  if(process.env.DIGEST_SCREENSHOT)await page.screenshot({path:process.env.DIGEST_SCREENSHOT,fullPage:true});
  await page.getByRole('button',{name:'● Bea · David',exact:true}).click();await page.getByText('Synthetic second-account message',{exact:true}).waitFor();
  assert.equal(archive[secondId].seen,1);assert.equal(history[0].seen,0);assert(calls.some(c=>c.action==='delivery'&&c.id===secondId));
  await page.getByRole('button',{name:'Notifications',exact:true}).click();await page.getByRole('button',{name:'Unfollow',exact:true}).click();await page.getByText('Open a conversation and choose Follow.',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Disable this browser',exact:true}).click();await page.getByText('Notifications disabled for this browser.',{exact:true}).waitFor();assert.equal(await page.evaluate(()=>testPush.unsubscribed),1);
  await page.setViewportSize({width:390,height:844});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  assert.deepEqual(errors,[]);assert.deepEqual(unknown,[]);console.log('master_notifications_browser: actual UI follow/unfollow, permission/subscription, test push request, incoming/all choice, exact message navigation, notification-only seen, disconnect and mobile layout passed. Browser push provider mocked; no live network.');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
