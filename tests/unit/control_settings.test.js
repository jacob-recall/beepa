import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {ControlCore} from '../../master/control-core.mjs';
import {ControlDomain} from '../../master/control-domain.mjs';
import {createCRMState,applyCRMOperation,defaultControlSettings} from '../../shared/model/crm.js';

{
 let state=createCRMState({workspace_id:'!crm:test',manager_mxid:'@manager:test'});
 const op={schema:1,op_id:'settings',workspace_id:'!crm:test',type:'control.settings',entity_id:'control',base_revision:0,payload:{...defaultControlSettings(),sending_mode:'read_only',allowed_accounts:['@david:test'],retention_days:7}};
 const result=applyCRMOperation(state,op);assert(result.outcome.accepted);state=result.state;
 assert.deepEqual(state.control.settings.allowed_accounts,['@david:test']);assert.equal(state.control.revision,1);
 assert(applyCRMOperation(state,op).outcome.duplicate);
 assert.equal(applyCRMOperation(state,{...op,op_id:'stale'}).outcome.code,'revision-conflict');
 for(const patch of [{retention_days:0},{retention_days:366},{allowed_accounts:['@x:test','@x:test']},{allowed_accounts:['not-an-account']},{sending_mode:'auto_send'},{allow_model_context:'yes'},{secret:'never'}])assert.equal(applyCRMOperation(state,{...op,op_id:'invalid',base_revision:1,payload:{...op.payload,...patch}}).outcome.accepted,false);
}
{
 const state={workspace_id:'!crm:test',lists:{},control:{revision:1,settings:{...defaultControlSettings(),allowed_accounts:['@david:test']}}},reads=[];
 const d=new ControlDomain({api:async(_,path)=>{reads.push(path);return [{type:'m.room.create',sender:'@david:test',state_key:'',content:{type:'m.space'}}];},bootstrap:async()=>{},manager:'@manager:test',roster:async()=>({'@david:test':'!david:test','@elliot:test':'!elliot:test'})});
 d.store={refresh:async()=>{},snapshot:()=>({ready:true,state})};d.histories.set('!old:test',{events:new Map()});await d.refresh();
 assert.equal(reads.length,1);assert(!reads[0].includes('elliot'));assert.equal(d.histories.size,0);
 state.control.settings.notifications_enabled=false;assert.deepEqual(d.rules(),[]);
}
let now=1700000000000;
const dir=mkdtempSync(join(tmpdir(),'control-settings-'));
try{
 let settings={...defaultControlSettings(),retention_days:1},modelCalls=0;
 const domain={settings:()=>settings,refresh:async()=>{},hydrateAccounts:async()=>{},rooms:new Map(),lists:()=>[]};
 const options={path:join(dir,'control.db'),domain,clock:()=>now,telegram:async()=>({message_id:1}),model:async()=>{modelCalls++;return {content:'Hello'};}};
 let c=new ControlCore(options);c.set('operator',{user:1,chat:1});
 settings.allow_model_context=false;await assert.rejects(c.command('Please summarize my conversations',null,'command'),/disabled/);assert.equal(modelCalls,0);
 settings.sending_mode='read_only';await assert.rejects(c.tool('prepare_send',{room_id:'!room:test',body:'No send'}),/read-only/);
 c.draft({id:'pending',kind:'send',created:now,body:'pending private body'});await assert.rejects(c.confirm('pending'),/read-only/);
 c.draft({id:'unknown',kind:'send',created:now,body:'reconciliation body'});c.db.prepare("UPDATE drafts SET state='unknown' WHERE id='unknown'").run();
 c.draft({id:'submitted',kind:'send',owner:'@david:test',created:now,body:'submitted private body'});c.db.prepare("UPDATE drafts SET state='submitted',result=? WHERE id='submitted'").run(JSON.stringify({proposal_id:'$proposal'}));
 c.draft({id:'batch',kind:'batch',created:now,manifest:{body:'needed for cancellation'}});c.db.prepare("UPDATE drafts SET state='submitted' WHERE id='batch'").run();
 c.ingest([{update_id:1,message:{text:'private command'}}]);c.db.prepare("UPDATE inbox SET state='done'").run();
 c.enqueue('delivered','private reply',{draft:'submitted'});await c.flush();
 now+=2*86400000;assert(c.maintain().cleared_records>=4);
 assert.equal(c.db.prepare('SELECT payload FROM inbox WHERE id=1').get().payload,'{}');
 assert.equal(JSON.parse(c.db.prepare("SELECT payload FROM drafts WHERE id='submitted'").get().payload).body,undefined);
 assert.equal(JSON.parse(c.db.prepare("SELECT payload FROM drafts WHERE id='unknown'").get().payload).body,'reconciliation body');
 assert.equal(JSON.parse(c.db.prepare("SELECT payload FROM drafts WHERE id='batch'").get().payload).manifest.body,'needed for cancellation');
 assert.equal(c.ref(1,1).unknown_quote,true);
 assert.equal((await c.confirm('pending')).state,'review_required');
 c.db.close();c=new ControlCore(options);c.ingest([{update_id:1,message:{text:'must not resurrect'}}]);c.enqueue('delivered','must not resend');
 assert.equal(c.db.prepare('SELECT payload FROM inbox WHERE id=1').get().payload,'{}');assert.equal(c.status().pending,0);
 assert.equal((await c.confirm('submitted')).proposal_id,'$proposal');assert.equal(c.maintain().cleared_records,0);c.db.close();
}finally{rmSync(dir,{recursive:true,force:true});}
console.log('Control settings: canonical revision/replay, account scope, hosted-model/read-only gates and retention with restart-safe deduplication passed.');
