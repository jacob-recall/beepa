import assert from 'node:assert/strict';
import {ControlDomain,DiscoveryStateCache} from '../../master/control-domain.mjs';
import {createCRMState,defaultControlSettings} from '../../shared/model/crm.js';

let sequence=0;
const event=(type,content,sender='@source:test',state_key='')=>({type,content,sender,state_key,event_id:'$state'+(++sequence)});
function fixture(){
 const h={reads:[],syncs:[],changes:{join:{'!space:test':{},'!room:test':{}}},states:new Map(),token:0};
 h.states.set('!space:test',[event('m.room.create',{type:'m.space'}),event('m.space.child',{via:['test']},'@source:test','!room:test')]);
 h.states.set('!room:test',[event('m.room.create',{'com.jkali.mirror_of':'!native:test'}),event('com.jkali.share_level',{level:'share'}),event('m.room.name',{name:'Initial'})]);
 h.api=async(method,path)=>{
  assert.equal(method,'GET');
  if(path.startsWith('/_matrix/client/v3/sync?')){
   const u=new URL('https://fixture'+path);h.syncs.push(u);if(h.syncError)throw Error('Sync unavailable');if(h.malformed)return h.malformed;
   const rooms=h.changes;h.changes={};return {next_batch:'position-'+(++h.token),rooms};
  }
  const id=decodeURIComponent(path.split('/')[5]);h.reads.push(id);if(h.failed===id)throw Error('State unavailable');
  assert(h.states.has(id),'Unexpected state read '+id);return structuredClone(h.states.get(id));
 };
 let state=createCRMState({workspace_id:'!crm:test',manager_mxid:'@manager:test'});
 h.domain=new ControlDomain({api:h.api,bootstrap:async()=>{},manager:'@manager:test',roster:async()=>({'@source:test':'!space:test'}),syncDiscovery:true});
 h.domain.store={refresh:async()=>{},snapshot:()=>({ready:true,state})};
 h.setSettings=settings=>state={...state,control:{revision:1,settings:{...defaultControlSettings(),...settings}}};
 h.change=(room,section='join')=>h.changes={[section]:{[room]:{timeline:{limited:true,events:[{type:'m.room.message',content:{body:'Never a state authority'}}]}}}};
 return h;
}
{
 const h=fixture();await h.domain.refresh();assert.equal(h.domain.rooms.size,1);assert.deepEqual(h.reads,['!space:test','!room:test']);
 h.reads=[];await h.domain.refresh();assert.equal(h.reads.length,0);assert.equal(h.domain.discoveryStatus().cache_hits,2);
 const filter=JSON.parse(h.syncs[0].searchParams.get('filter'));assert.equal(filter.room.timeline.limit,1);assert(!filter.room.timeline.types&&!filter.room.timeline.not_types);assert.deepEqual(filter.room.rooms,[]);assert(!Object.hasOwn(JSON.parse(h.syncs[1].searchParams.get('filter')).room,'rooms'));assert.equal(h.syncs[0].searchParams.get('set_presence'),'offline');assert(!h.syncs[0].searchParams.has('since'));assert.equal(h.syncs[1].searchParams.get('since'),'position-1');
 h.states.get('!room:test')[2]=event('m.room.name',{name:'Changed'});h.change('!room:test');await h.domain.refresh();assert.deepEqual(h.reads,['!room:test']);assert.equal(h.domain.room('!room:test').title,'Changed');
 h.domain.histories.set('!room:test',{private:'cached history'});h.states.get('!room:test')[1]=event('com.jkali.share_level',{level:'private'});h.change('!room:test');await h.domain.refresh();assert.equal(h.domain.rooms.size,0);assert.equal(h.domain.histories.size,0);
}
// Unlinking a room or restricting accounts drops both derived discovery and cache.
{
 const h=fixture();await h.domain.refresh();h.states.get('!space:test')[1]=event('m.space.child',{},'@source:test','!room:test');h.change('!space:test');await h.domain.refresh();assert.equal(h.domain.rooms.size,0);assert(!h.domain.discoveryCache.states.has('!room:test'));
 h.setSettings({allowed_accounts:[]});await h.domain.refresh();assert.equal(h.domain.discoveryCache.states.size,0);
}
// An unrelated joined room can invalidate a cache entry but cannot add a source
// or supply authorization through its sync payload.
{
 const h=fixture();await h.domain.refresh();h.changes={join:{'!attacker:test':{state:{events:[event('com.jkali.share_level',{level:'direct'},'@attacker:test')]}}}};await h.domain.refresh();assert.equal(h.domain.rooms.size,1);assert(!h.reads.includes('!attacker:test'));
}
// Membership leave + endpoint denial never falls back to previously shared state.
{
 const h=fixture();await h.domain.refresh();h.change('!room:test','leave');h.failed='!room:test';await h.domain.refresh();assert.equal(h.domain.rooms.size,0);assert(!h.domain.discoveryCache.states.has('!room:test'));
 h.failed=null;await h.domain.refresh();assert.equal(h.domain.rooms.size,1,'a failed fetch is retried even without a new delta');
}
// Lost/invalid sync invalidates the entire cache/token. A subsequent complete
// refresh proves permissions again; the failed refresh cannot claim success.
for(const broken of ['network','malformed']){
 const h=fixture();await h.domain.refresh();if(broken==='network')h.syncError=true;else h.malformed={rooms:{join:{}}};
 await assert.rejects(h.domain.refresh());assert.equal(h.domain.discoveryCache.since,null);assert.equal(h.domain.discoveryCache.states.size,0);
 h.syncError=false;h.malformed=null;h.states.get('!room:test')[1]=event('com.jkali.share_level',{level:'private'});await h.domain.refresh();assert.equal(h.domain.rooms.size,0);assert(!h.syncs.at(-1).searchParams.has('since'));
}
// Point-of-use checks remain fresh even if a new event has not been synced yet.
{
 const h=fixture();await h.domain.refresh();h.states.get('!room:test')[1]=event('com.jkali.share_level',{level:'private'});await assert.rejects(h.domain.liveRoom('!room:test'),/permissions changed/);
}
// Room data cannot be used before any successful sync barrier, including after
// restart. Unexpected response shapes must not advance the position.
{
 const c=new DiscoveryStateCache(async()=>({next_batch:'bad',rooms:{join:[]}}));await assert.rejects(c.state('!room:test'),/sync required/);await assert.rejects(c.refresh());assert.equal(c.since,null);
}
console.log('control discovery: incremental invalidation, full-state authority, limited deltas, revocation, unlink, leave, failures, account scope, and fresh permission checks passed');
{
 let now=100000,reads=0,changes={},refused=true;
 const c=new DiscoveryStateCache(async(_,path)=>{
  if(path.includes('/sync?')){const rooms=changes;changes={};return {next_batch:'position',rooms};}
  reads++;if(refused)throw Object.assign(Error('Not shared'),{status:403});return [];
 },()=>now);
 await c.refresh();await assert.rejects(c.state('!old:test'));await c.refresh();await assert.rejects(c.state('!old:test'));assert.equal(reads,1,'definitive refusal is reused without reviving prior state');
 changes={join:{'!old:test':{}}};refused=false;await c.refresh();assert.deepEqual(await c.state('!old:test'),[]);assert.equal(reads,2,'new membership invalidates a cached refusal immediately');
 changes={leave:{'!old:test':{}}};refused=true;await c.refresh();await assert.rejects(c.state('!old:test'));now+=60001;await c.refresh();await assert.rejects(c.state('!old:test'));assert.equal(reads,4,'negative cache expires even without another room delta');
}
