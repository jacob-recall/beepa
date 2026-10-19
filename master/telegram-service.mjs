// Dedicated, loopback-published control process. Credentials only in mounted
// mode-0600 config; never command-line arguments, URLs in logs, or model inputs.
import { readFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { AsyncLocalStorage } from 'node:async_hooks';
import { ControlDomain, defaultMonitoring, hash } from './control-domain.mjs';
import { ControlCore } from './control-core.mjs';

import {pathToFileURL} from 'node:url';

// The HTTP/domain lifecycle is independent of Telegram identity and polling.
// Export the real runtime so outage/reconnect tests exercise production wiring.
export function createControlService({config,domain,core,telegram,authenticate,host='0.0.0.0',port=8023,monitorInterval=15000,retryInterval=5000,commandInterval=250}){
// Domain/core mutations stay serialized. Only the remote model wait releases
// this lease; the continuation reacquires its FIFO place before touching state.
let serial=Promise.resolve();const leases=new AsyncLocalStorage();
async function acquire(){let release;const previous=serial;serial=new Promise(resolve=>{release=resolve;});await previous;return release;}
async function exclusive(fn){
  const lease={release:await acquire()};
  return leases.run(lease,async()=>{try{return await fn();}finally{lease.release();}});
}
core.modelWait=async fn=>{
  const lease=leases.getStore();if(!lease)return fn();
  lease.release();
  try{return await fn();}finally{lease.release=await acquire();}
};
let bot,domainReady=false,transportReady=false,stopped=false,tasks=[];
let commandRunning=false;
const sleepers=new Map();
const sleep=ms=>new Promise(resolve=>{const id=setTimeout(()=>{sleepers.delete(id);resolve();},ms);sleepers.set(id,resolve);});
async function follow({room,all_messages=false,enabled=true}){
  if(typeof all_messages!=='boolean')throw new Error('Invalid notification mode');
  await domain.refresh();const r=domain.room(room);
  const listId='browser-followed-conversations';
  if(!domain.state().lists[listId])await domain.apply(domain.operation('list.create',listId,0,{name:'Followed conversations',purpose:'monitoring'},'control-browser-list'));
  const l=domain.list(listId);
  if(!l.monitoring)await domain.apply(domain.preparePolicy({list_id:listId,enabled:true,channels:['browser','telegram']}));
  // A legacy Follow control edits the canonical list, never an independent rule.
  const previous=domain.list(listId).watches?.[room];
  await domain.apply(domain.prepareWatch({list_id:listId,room_id:room,...previous,enabled,all_messages}));
  return {following:enabled,list_id:listId,owner:r.owner};
}
async function handle(data){
  if(data.action==='status')return {...core.status(),ready:domainReady&&transportReady,domain_ready:domainReady,transport_ready:transportReady,bot:bot?.username};
  if(data.action==='command')return core.enqueueCommand(data);
  if(data.action==='command_status')return core.commandStatus(data.request_id);
  if(data.action==='command_cancel')return core.cancelCommand(data.request_id);
  if(data.action==='disconnect')return core.disconnect();
  if(!domainReady)throw new Error('Master lists are reconnecting. Try again shortly.');
  if(data.action==='settings'||data.action==='settings_update'){
    await domain.store.refresh();
    if(data.action==='settings_update'){
      if(!Number.isSafeInteger(data.revision))throw new Error('Current settings revision required');
      const operation=domain.operation('control.settings','control',data.revision,data.settings);
      await domain.validateOperation(operation);await domain.apply(operation);
      await domain.refresh();await core.invalidateReviews();
    }
    return {revision:domain.state().control?.revision||0,settings:domain.settings(),accounts:Object.keys(config.roster),model:config.model};
  }
  if(['pair','test'].includes(data.action)&&!transportReady)throw new Error('Telegram is reconnecting. Browser controls remain available.');
  if(data.action==='pair')return core.pairLink(bot.username);
  if(data.action==='follow'||data.action==='unfollow')return follow({...data,enabled:data.action==='follow'});
  if(data.action==='migrate_follows'){
    if(!core.value('follows_migrated')){for(const f of data.follows||[])await follow({room:f.room,all_messages:!!f.all_messages});core.set('follows_migrated',true);}return {migrated:true};
  }
  if(data.action==='cancel')return core.cancel(data.id);
  await domain.refresh();
  if(data.action==='ping')return core.tool('account_health',data.account===undefined?{}:{account:data.account});
  await domain.hydrateAccounts();
  if(data.action==='rules')return {rules:domain.rules()};
  if(data.action==='contact_rules')return {rules:domain.contactRules()};
  if(data.action==='contact_notification'){
    const {rules,ready}=await core.contactNotificationValidation(data.source);
    return {rules:rules.filter(r=>ready.includes(r.list_id)),deferred_rules:rules.filter(r=>!ready.includes(r.list_id))};
  }
  if(data.action==='contact_obligation'){
    if(data.revision!==domain.list(data.list_id).revision)throw new Error('List changed. Refresh before editing this deadline.');
    if(!data.changes||typeof data.changes!=='object'||Array.isArray(data.changes)||Object.keys(data.changes).some(k=>!['due_at','snooze_until','resolved'].includes(k)))throw new Error('Invalid contact task changes');
    const operation=await domain.prepareContactObligation({list_id:data.list_id,binding_id:data.binding_id,...data.changes});
    await domain.validateReviewScope({kind:'list',operation});await domain.apply(operation);
    return {saved:true};
  }
  if(data.action==='lists')return {lists:domain.lists({include_archived:data.include_archived??false})};
  if(data.action==='updates')return {updates:await domain.updates(data.list_id,{room_id:data.room_id,owner:data.owner,source:data.source,since:data.since})};
  if(data.action==='contact_updates')return {updates:await domain.contactUpdates(data.list_id,{owner:data.owner,source:data.source,since:data.since})};
  if(data.action==='tool')return core.tool(data.name,data.arguments||{});
  if(data.action==='confirm')return core.confirm(data.id);
  if(data.action==='draft_status')return core.tool('send_status',{draft_id:data.id});
  if(data.action==='test'){core.enqueue('test:'+String(data.request_id||Date.now()),'Beepa Telegram notification test. Your master connection is working.');await core.flush();return {queued:!!core.value('operator')};}
  throw new Error('Unknown control action');
}
const dispatch=data=>['status','command','command_status','command_cancel'].includes(data.action)?handle(data):exclusive(()=>handle(data));
const server=createServer(async(req,res)=>{
  const reply=(status,value)=>{res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(value));};
  if(req.url==='/health'&&req.method==='GET'){reply(200,{running:true});return;}
  if(req.url!=='/control'||req.method!=='POST'){reply(404,{error:'Not found'});return;}
  const token=req.headers.authorization?.match(/^Bearer (.+)$/)?.[1];
  if(!token){reply(401,{error:'Authentication required'});return;}
  try{const who=await authenticate(token);if(who.user_id!==config.manager){reply(403,{error:'Manager required'});return;}}
  catch{reply(401,{error:'Authentication failed'});return;}
  try{let body='';for await(const chunk of req){body+=chunk;if(Buffer.byteLength(body)>65536){reply(413,{error:'Request too large'});return;}}
    const data=JSON.parse(body||'{}');if(!data||Array.isArray(data)||typeof data!=='object')throw new Error('Invalid request');
    reply(200,await dispatch(data));
  }catch(e){reply(400,{error:e.message});}
});
server.requestTimeout=10000;server.headersTimeout=10000;

async function monitorOnce(){
  return exclusive(async()=>{
    try{
      if(!domainReady){await domain.refresh();await domain.hydrateAccounts();domainReady=true;core.setHealth('startup');}
      else{await core.monitor();core.setHealth('monitoring');if(transportReady)await core.flush();}
    }catch{
      core.setHealth(domainReady?'monitoring':'startup',domainReady?'Monitoring is retrying; check account connectivity.':'Master lists are reconnecting.');
    }
  });
}
async function pollOnce(){
  try{
    if(!transportReady){
      const candidate=await telegram('getMe');
      const webhook=await telegram('getWebhookInfo');
      if(webhook.url)throw new Error('Existing webhook');
      if(stopped)return;
      await exclusive(()=>{core.bindTransport(candidate);bot=candidate;});
      if(stopped)return;
      transportReady=true;core.setHealth('telegram_setup');
      try{await telegram('setMyCommands',{commands:[{command:'start',description:'Connect to Beepa'},{command:'ping',description:'Check account sync: /ping all or /ping Elliot'},{command:'lists',description:'Show your central lists'},{command:'settings',description:'Show monitoring settings'},{command:'help',description:'How to use Beepa'}]});}
      catch{core.setHealth('telegram_setup','Telegram command menu unavailable. Text commands still work.');}
    }
    const updates=await telegram('getUpdates',{offset:core.value('offset',0),timeout:20,allowed_updates:['message','callback_query']});
    if(stopped)return;
    core.setHealth('telegram_poll');
    // Ingestion is a synchronous SQLite transaction, independent of slow model
    // work. Advancing the provider offset never precedes durable storage.
    core.ingest(updates);
  }catch{
    transportReady=false;
    core.setHealth('telegram_setup','Telegram is reconnecting. Browser controls remain available.');
    core.setHealth('telegram_poll','Telegram polling is retrying.');
  }
}
async function commandsOnce(){
  if(commandRunning||!domainReady||!transportReady||!core.nextCommandWork())return;
  commandRunning=true;
  try{return await exclusive(async()=>{
    if(stopped||!transportReady)return;
    try{
      const kind=core.nextCommandWork();
      if(kind==='telegram')await core.process(1);
      else if(kind==='api')await core.processCommand();
      if(!stopped&&transportReady)await core.flush();core.setHealth('commands');
    }catch{core.setHealth('commands','Command processing is retrying.');}
  });}finally{commandRunning=false;}
}
async function start(){
  core.setHealth('startup','Master lists are connecting.');
  core.setHealth('telegram_setup','Telegram is connecting.');
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(port,host,()=>{server.off('error',reject);resolve();});});
  tasks=[(async()=>{while(!stopped){await monitorOnce();if(!stopped)await sleep(monitorInterval);}})(),
    (async()=>{while(!stopped){await pollOnce();if(!stopped&&!transportReady)await sleep(retryInterval);}})(),
    (async()=>{while(!stopped){await commandsOnce();if(!stopped)await sleep(commandInterval);}})()];
  return server.address();
}
async function stop(){
  stopped=true;transportReady=false;
  for(const [id,resolve] of sleepers){clearTimeout(id);resolve();}sleepers.clear();
  await new Promise(resolve=>server.close(resolve));await Promise.allSettled(tasks);
}
return {server,start,stop,monitorOnce,pollOnce,commandsOnce,handle:dispatch};
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
const config=JSON.parse(readFileSync(process.env.BEEPA_CONTROL_CONFIG||'/state/config.json','utf8'));
const json=async(url,options={})=>{
  let response;try{response=await fetch(url,{...options,redirect:'error',signal:AbortSignal.timeout(options.timeout||20000)});}catch{throw new Error('Service connection unavailable');}
  let data;try{data=await response.json();}catch{throw new Error('Service returned an invalid response');}
  if(!response.ok){const error=new Error('Service request failed (HTTP '+response.status+')');error.status=response.status;throw error;}
  return data;
};
const matrix=(method,path,data)=>json(config.matrix+path,{method,timeout:path.startsWith('/_matrix/client/v3/sync?')?90000:20000,headers:{Authorization:'Bearer '+config.manager_token,'Content-Type':'application/json'},...(data===undefined?{}:{body:JSON.stringify(data)})});
const admin=data=>json(config.enroll+'/admin/crm-room',{method:'POST',headers:{Authorization:'Bearer '+config.manager_token,'Content-Type':'application/json'},body:JSON.stringify(data)});
const telegram=async(method,data={})=>{
  if(!['getMe','getUpdates','sendMessage','answerCallbackQuery','getWebhookInfo','setMyCommands','setMessageReaction'].includes(method))throw new Error('Unsupported Telegram operation');
  const result=await json('https://api.telegram.org/bot'+config.telegram_token+'/'+method,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data),timeout:method==='setMessageReaction'?3000:35000});
  if(!result.ok){const error=new Error('Telegram request failed');error.status=result.error_code;throw error;}return result.result;
};
let core;
const model=async(messages,tools)=>{
  try{
  const result=await json('https://openrouter.ai/api/v1/chat/completions',{method:'POST',headers:{Authorization:'Bearer '+config.openrouter_key,'Content-Type':'application/json'},body:JSON.stringify({model:config.model,messages,tools,tool_choice:'auto',max_tokens:1800,provider:{require_parameters:true,allow_fallbacks:false}}),timeout:60000});
  const m=result.choices?.[0]?.message;if(!m)throw new Error('Model response unavailable');core.setHealth('model');return m;
  }catch(error){core.setHealth('model','Language model request failed. Text controls remain available.');throw error;}
};

const domain=new ControlDomain({api:matrix,bootstrap:()=>admin({}),manager:config.manager,roster:async()=>config.roster,syncDiscovery:true,accountNames:config.account_names||{}});
const browser=data=>json(config.enroll+'/admin/notifications',{method:'POST',headers:{Authorization:'Bearer '+config.manager_token,'Content-Type':'application/json'},body:JSON.stringify(data)});
core=new ControlCore({path:'/state/control.db',domain,telegram,model,browser,historyTimezone:config.timezone||'UTC'});core.set('model',config.model);
const service=createControlService({config,domain,core,telegram,authenticate:token=>json(config.matrix+'/_matrix/client/v3/account/whoami',{headers:{Authorization:'Bearer '+token}})});
process.on('SIGTERM',()=>{void service.stop();});process.on('SIGINT',()=>{void service.stop();});
service.start().catch(()=>{console.error('Beepa control listener failed.');process.exitCode=1;});
}
