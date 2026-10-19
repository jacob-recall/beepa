// Synthetic source-owned data only. The API below cannot reach any real account.
import assert from 'node:assert/strict';
import {ControlCore} from '../../master/control-core.mjs';
import {ControlDomain,defaultMonitoring} from '../../master/control-domain.mjs';
import {createCRMState,applyCRMOperation} from '../../shared/model/crm.js';
export function acceptanceFixture(model=async()=>{throw Error('No model configured');}){
 const h={now:Date.parse('2026-10-18T12:00:00Z'),sent:[],writes:[],events:new Map()};
 const manager='@manager:fixture';let state=createCRMState({workspace_id:'!crm:fixture',manager_mxid:manager});
 const d=new ControlDomain({manager,clock:()=>h.now,bootstrap:async()=>{},roster:async()=>({}),api:async()=>{throw Error('Unexpected synthetic API call');}});
 d.state=()=>state;d.refresh=async()=>{};d.hydrateAccounts=async()=>{};
 d.account=async owner=>{assert(d.accounts.has(owner));return {state:[]};};
 h.apply=op=>{const result=applyCRMOperation(state,op);assert(result.outcome.accepted,result.outcome.code);state=result.state;return result.outcome;};
 d.store={refresh:async()=>{},append:async op=>h.apply(op)};
 h.apply(d.operation('list.create','followups',0,{name:'Follow-ups',purpose:'monitoring'}));
 h.apply(d.operation('list.monitoring','followups',1,{...defaultMonitoring(),enabled:true}));
 for(const [name,source,level,incoming,unread] of [['david','imessage','direct',true,3],['elliot','whatsapp','share',false,0]]){
  const owner='@'+name+':fixture',id='!ada-'+name+':fixture',native='!native-'+name+':fixture';
  d.accounts.set(owner,{generation:name+'-gen',journal_epoch:name+'-journal',proposal_room_id:'!proposals-'+name+':fixture'});
  d.health.set(owner,{connected:true,updated_at:h.now});
  const events=Array.from({length:20},(_,i)=>({event_id:'$'+name+'-'+i,sender:owner,type:'m.room.message',origin_server_ts:h.now-20000+i,
   content:{msgtype:'m.text',body:name+' exact message '+i,'com.jkali.from_me':!incoming}}));h.events.set(id,events);
  const event=(type,content)=>({type,state_key:'',sender:owner,content});
  const tracking={schema:1,state:'ready',owner_mxid:owner,generation:name+'-gen',journal_epoch:name+'-journal',bridge_epoch:name+'-bridge',room_id:native,mirror_room_id:id,source,updated_ts:h.now,coverage:'native_history',complete:true,total_messages:events.length,
   messages:events.map((e,i)=>({event_id:e.event_id,incoming,order:String(i+1),timestamp:e.origin_server_ts,answered_by:null}))};
  const room={id,owner,native,source,title:'Ada',level,consent:{level,revision:1},unread,profile:'ada-profile',
   state:[event('m.room.create',{'com.jkali.mirror_of':native}),event('com.jkali.share_level',{level,revision:1}),event('com.jkali.conversation_tracking',tracking)]};d.rooms.set(id,room);
  h.apply(d.operation('list.watch','followups',d.list('followups').revision,{room_id:id,owner_mxid:owner,enabled:true,due_at:null,snooze_until:null,resolved_event:null}));
 }
 d.api=async(method,path,body)=>{
  const url=new URL('https://fixture.invalid'+path),room=decodeURIComponent(url.pathname.split('/')[5]);
  if(method==='PUT'){assert(path.includes('/send/com.jkali.proposal/'));h.writes.push({path,body});return {event_id:'$proposal-'+h.writes.length};}
  assert.equal(method,'GET');assert(d.rooms.has(room),'Only synthetic rooms are readable');
  if(path.endsWith('/state'))return d.rooms.get(room).state;
  const events=h.events.get(room),from=Number(url.searchParams.get('from')||0);return {start:String(from),end:String(events.length),chunk:events.slice(from)};
 };
 const core=new ControlCore({path:':memory:',domain:d,clock:()=>h.now,model,telegram:async(method,payload)=>{assert.equal(method,'sendMessage');h.sent.push(payload);return {message_id:h.sent.length};}});
 core.set('operator',{user:1,chat:1});
 return Object.assign(h,{core,domain:d,state:()=>state,close:()=>core.db.close(),ask:async(text,key,ref=null)=>{await core.command(text,ref,key);await core.flush();}});
}
