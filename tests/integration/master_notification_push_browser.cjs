// Real Chromium service worker + Notifications API + CSP, isolated HTTP only.
// CDP supplies a synthetic push event; this does not claim live provider/OS delivery.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright-core');
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..'),receipts=[],unknown=[];
const csp=fs.readFileSync(path.join(root,'apps/master/index.html'),'utf8').match(/http-equiv="Content-Security-Policy" content="([^"]+)"/)[1];
const nginx=fs.readFileSync(path.join(root,'views/nginx.conf'),'utf8');
const headerPolicy=nginx.split('location /apps/master/ {')[1].match(/add_header Content-Security-Policy "([^"]+)"/)[1];
assert.equal(headerPolicy,csp,'nginx header and HTML CSP must agree on worker policy');
const server=http.createServer(async(req,res)=>{
 const u=new URL(req.url,'http://fixture');
 if(u.pathname==='/favicon.ico'){res.statusCode=204;res.end();return;}
 if(req.method==='POST'&&u.pathname==='/admin/notifications/receipt'){
  let body='';for await(const chunk of req)body+=chunk;
  receipts.push({body:JSON.parse(body),authorization:req.headers.authorization});res.setHeader('Content-Type','application/json');res.end('{"recorded":true}');return;
 }
 if(u.pathname==='/apps/master/test.html'){
  res.setHeader('Content-Type','text/html');res.setHeader('Content-Security-Policy',csp);res.end('<!doctype html><title>Isolated worker test</title><script type="module" src="./bootstrap.js"></script>');return;
 }
 if(u.pathname==='/apps/master/bootstrap.js'){
  res.setHeader('Content-Type','text/javascript');res.end("import {registerNotificationWorker} from './notifications.js'; window.workerReady=registerNotificationWorker(location.origin).then(()=>navigator.serviceWorker.ready);");return;
 }
 if(u.pathname.startsWith('/apps/master/')||u.pathname.startsWith('/shared/')){
  const file=path.resolve(root,'.'+u.pathname);
  if(file.startsWith(root+path.sep)&&fs.existsSync(file)&&/\.(js|png)$/.test(file)){res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':'image/png');res.end(fs.readFileSync(file));return;}
 }
 unknown.push(req.url);res.statusCode=404;res.end();
});
(async()=>{
 await new Promise(r=>server.listen(0,'127.0.0.1',r));const origin='http://127.0.0.1:'+server.address().port;
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 try{
  const context=await browser.newContext({permissions:['notifications']});const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const cdp=await context.newCDPSession(page);const versions=[];cdp.on('ServiceWorker.workerVersionUpdated',e=>versions.push(...e.versions));await cdp.send('ServiceWorker.enable');
  await page.goto(origin+'/apps/master/test.html');await page.evaluate(()=>window.workerReady.then(()=>true));
  // Page readiness and the CDP activation event travel over separate channels.
  // Wait for that exact event rather than asserting telemetry arrived first.
  for(let i=0;i<100&&!versions.some(v=>v.status==='activated');i++)await new Promise(r=>setTimeout(r,20));
  const worker=versions.findLast(v=>v.status==='activated');assert.ok(worker,'actual worker activated under production CSP');
  await cdp.send('ServiceWorker.deliverPushMessage',{origin,registrationId:worker.registrationId,data:JSON.stringify({title:'Beepa isolated push',body:'Synthetic test',url:'index.html#notification=fixture',tag:'fixture',receipt_token:'a'.repeat(43)})});
  let displayed=[];
  for(let i=0;i<100&&!displayed.length;i++) {
    displayed=await page.evaluate(async()=>{const r=await navigator.serviceWorker.ready;return (await r.getNotifications()).map(n=>({title:n.title,url:n.data.url}))});
    if(!displayed.length)await new Promise(r=>setTimeout(r,20));
  }
  assert.equal(displayed[0].title,'Beepa isolated push');assert.equal(displayed[0].url,origin+'/apps/master/index.html#notification=fixture');
  // Await receipt without assumptions about network scheduling.
  for(let i=0;i<100&&!receipts.length;i++)await new Promise(r=>setTimeout(r,20));
  assert.deepEqual(receipts,[{body:{token:'a'.repeat(43),stage:'shown'},authorization:undefined}]);
  const groupUrl='index.html#notification-group='+'a'.repeat(64)+','+'b'.repeat(64);
  await cdp.send('ServiceWorker.deliverPushMessage',{origin,registrationId:worker.registrationId,data:JSON.stringify({title:'Beepa isolated digest',body:'Two synthetic conversations',url:groupUrl,tag:'digest-fixture',receipt_token:'b'.repeat(43)})});
  for(let i=0;i<100&&receipts.length<2;i++)await new Promise(r=>setTimeout(r,20));
  displayed=await page.evaluate(async()=>{const r=await navigator.serviceWorker.ready;return (await r.getNotifications()).map(n=>({title:n.title,url:n.data.url}))});
  assert(displayed.some(n=>n.title==='Beepa isolated digest'&&n.url===origin+'/apps/master/'+groupUrl));
  assert.deepEqual(receipts[1],{body:{token:'b'.repeat(43),stage:'shown'},authorization:undefined});
  assert.deepEqual(errors,[]);assert.deepEqual(unknown,[]);
  console.log('master_notification_push_browser: real service worker registered under Trusted Types CSP, push event displayed via Notifications API, scoped acknowledgement reached HTTP endpoint; no manager bearer in worker. Provider/OS banner not simulated as evidence.');
 }finally{await browser.close();await new Promise(r=>server.close(r))}
})().catch(e=>{console.error(e);server.close();process.exitCode=1});
