import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const handlers={},shown=[],navigated=[],receipts=[];
const self={location:{origin:'https://master.test',href:'https://master.test/apps/master/notification-sw.js'},registration:{scope:'https://master.test/apps/master/',showNotification:async(...args)=>shown.push(args)},addEventListener:(name,fn)=>handlers[name]=fn,clients:{matchAll:async()=>[],openWindow:async url=>navigated.push(url)}};
vm.runInNewContext(fs.readFileSync('apps/master/notification-sw.js','utf8'),{self,URL,fetch:async(url,options)=>receipts.push({url:String(url),body:JSON.parse(options.body)})});
async function push(value){let pending;handlers.push({data:{json:()=>value},waitUntil:p=>pending=p});await pending;}
await push({title:'Ada via Elliot',body:'New message',url:'index.html#notification=one',tag:'beepa-one',receipt_token:'a'.repeat(43)});
assert.equal(receipts[0].body.stage,'shown');assert.equal(receipts[0].url,'https://master.test/admin/notifications/receipt');
assert.equal(shown.length,1);assert.equal(shown[0][1].data.url,'https://master.test/apps/master/index.html#notification=one');
for(const url of ['https://evil.test/x','/apps/user/index.html','javascript:alert(1)'])await push({title:'Bad route',url});
assert.equal(shown.length,1,'push cannot create off-origin/other-app navigation');
let pending,closed=false;handlers.notificationclick({notification:{data:shown[0][1].data,close:()=>closed=true},waitUntil:p=>pending=p});await pending;assert.equal(receipts[1].body.stage,'clicked');assert.equal(closed,true);assert.deepEqual(navigated,['https://master.test/apps/master/index.html#notification=one']);
let focused=false;self.clients.matchAll=async()=>[{url:'https://master.test/apps/master/index.html',navigate:async url=>navigated.push(url),focus:async()=>focused=true}];handlers.notificationclick({notification:{data:shown[0][1].data,close:()=>{}},waitUntil:p=>pending=p});await pending;assert.equal(focused,true);
console.log('master_notification_worker: push display, exact click target, existing-window focus and unsafe URL rejection passed');

self.registration.showNotification=async()=>{throw Error('permission denied')};
const count=receipts.length;await assert.rejects(push({title:'Denied',receipt_token:'b'.repeat(43)}),/permission denied/);assert.equal(receipts.length,count,'failed display never acknowledged');
