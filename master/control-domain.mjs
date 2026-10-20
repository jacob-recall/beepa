// Headless master operations reuse the browser's canonical CRM reducer/store.
// Only typed proposals go to verified source-owned proposal rooms. No native sends.
import { createHash, randomUUID } from 'node:crypto';
import { createCRMStore } from '../apps/master/crm-store.js';
import { createCRMMembers } from '../apps/master/crm-members.js';
import { createCRMBatches } from '../apps/master/crm-batches.js';
import { readCRMAccount } from '../apps/master/crm-account.js';
import { canonicalCRMJSON, applyVerifiedCRMOperation, defaultControlSettings, reminderOperationPayload } from '../shared/model/crm.js';
import { messageTimestamp, timestampCorrections } from '../shared/model/message_timestamps.js';
import { sanitize } from '../shared/ui/el.js';

export const defaultMonitoring = () => ({enabled:false, all_messages:false, channels:['telegram','browser'], waiting_them_ms:null, waiting_us_ms:null, preview:false});
export const hash = value => createHash('sha256').update(typeof value === 'string' ? value : canonicalCRMJSON(value)).digest('hex');
const enc = encodeURIComponent;
const roomPath = id => '/_matrix/client/v3/rooms/' + enc(id);
const get = (state,type,key='') => state.find(e=>e.type===type && e.state_key===key);
const owned = (state,type,owner,key='') => { const e=get(state,type,key); return e?.sender===owner ? e.content : undefined; };
const assert = (test,message) => { if(!test) throw new Error(message); };
const scopeAssert = (test,message) => { if(!test) throw Object.assign(new Error(message),{code:'scope_changed'}); };
// A different conversation's watch can advance the list revision without
// changing this review. All other list fields and this exact watch stay fenced.
const notificationListScope = list => {
  if(!list)return null;
  const {revision,watches,...scope}=list;return scope;
};
const editContent = content => content?.['m.new_content'] || (typeof content?.msgtype==='string' ? content : null);
export function message(event,owner,corrections) {
  const c=event?.content;
  if(event?.sender!==owner || event.type!=='m.room.message' || event.state_key!==undefined || event.unsigned?.redacted_because || !c || c['m.relates_to']?.rel_type==='m.replace') return null;
  if(!Number.isSafeInteger(event.origin_server_ts) || typeof event.event_id!=='string') return null;
  const replacement=event.unsigned?.['m.relations']?.['m.replace'];
  const content=replacement?.sender===owner && editContent(replacement.content) ? editContent(replacement.content) : c;
  return {id:event.event_id, ts:messageTimestamp(event,corrections), from_me:c['com.jkali.from_me']===true,
    version:hash({content,from_me:c['com.jkali.from_me']===true,ts:messageTimestamp(event,corrections)}),
    text:['m.text','m.notice','m.emote'].includes(content.msgtype) ? String(content.body || '') : '['+String(content.msgtype || 'attachment')+']'+(typeof content.body==='string'&&content.body?'\n'+content.body:''),
    proposal:c['com.jkali.auto_sent_from_proposal'] || c['com.jkali.from_proposal'] || null};
}
// /sync is only an invalidation barrier. We never reconstruct authorization from
// a possibly limited timeline: every changed room is re-read via its full state
// endpoint before discovery uses it. Tokens/cache are process-local and discarded
// together on errors or restart. No stale-on-error behavior is permitted.
export class DiscoveryStateCache {
  constructor(api,clock=Date.now){this.api=api;this.clock=clock;this.states=new Map();this.unavailable=new Map();this.since=null;this.stats={};this.invites=new Map();this.joinRetry=new Map();}
  async refresh(){
    // Keep timeline event types unfiltered: even a state-only change must name
    // its room in the delta. Full state payloads here would be redundant.
    const filter={presence:{not_types:['*']},account_data:{not_types:['*']},room:{state:{not_types:['*']},timeline:{limit:1},ephemeral:{not_types:['*']},account_data:{not_types:['*']}}};
    // Bootstrap a position without joined-room payloads; Synapse still supplies
    // stripped invitations, retained for identity-checked headless joins.
    // Fetching an unused initial snapshot for every joined room costs seconds.
    // The next delta is unfiltered by room and includes changes during discovery.
    if(!this.since)filter.room.rooms=[];
    const query=new URLSearchParams({timeout:'0',set_presence:'offline',filter:JSON.stringify(filter),...(this.since?{since:this.since}:{})});
    try{
      const data=await this.api('GET','/_matrix/client/v3/sync?'+query);
      assert(data&&typeof data.next_batch==='string'&&data.next_batch.length>0&&data.next_batch.length<8192,'Discovery sync position unavailable');
      assert(data.rooms===undefined||data.rooms!==null&&typeof data.rooms==='object'&&!Array.isArray(data.rooms),'Discovery sync rooms unavailable');
      const changed=new Set();
      for(const section of ['join','leave','invite','knock']){
        const rooms=data.rooms?.[section];assert(rooms===undefined||rooms!==null&&typeof rooms==='object'&&!Array.isArray(rooms),'Discovery sync room changes unavailable');
        for(const id of Object.keys(rooms||{}))changed.add(id);
      }
      for(const id of changed){this.states.delete(id);this.unavailable.delete(id);}
      for(const section of ['join','leave'])for(const id of Object.keys(data.rooms?.[section]||{})){this.invites.delete(id);this.joinRetry.delete(id);}
      for(const [id,invite] of Object.entries(data.rooms?.invite||{}))this.invites.set(id,invite);
      this.stats={initial:!this.since,changed_rooms:changed.size,state_reads:0,cache_hits:0,unavailable_hits:0};this.since=data.next_batch;this.used=new Set();
    }catch(error){this.states.clear();this.unavailable.clear();this.since=null;this.invites.clear();this.joinRetry.clear();throw error;}
  }
  async state(id){
    assert(this.since,'Discovery sync required');this.used.add(id);
    if(this.states.has(id)){this.stats.cache_hits++;return this.states.get(id);}
    if((this.unavailable.get(id)||0)>this.clock()){this.stats.unavailable_hits++;throw Error('Room state unavailable');}
    this.unavailable.delete(id);
    this.stats.state_reads++;
    let state;
    try{state=await this.api('GET',roomPath(id)+'/state');assert(Array.isArray(state),'Room state unavailable');}
    catch(error){
      // Obsolete child links can remain in source spaces. Cache only an explicit
      // 403/404 refusal, never network/5xx failures, and never an earlier grant.
      // Membership/room deltas invalidate it immediately; a one-minute cap also
      // bounds rediscovery when the server supplies no room delta while denied.
      if([403,404].includes(error.status))this.unavailable.set(id,this.clock()+60000);
      throw error;
    }
    // Failed/absent state is never cached. Current errors cannot revive old data.
    this.states.set(id,state);return state;
  }
  prune(){for(const cache of [this.states,this.unavailable])for(const id of cache.keys())if(!this.used.has(id))cache.delete(id);}
}
export class ControlDomain {
  constructor({api, bootstrap, manager, roster, clock=Date.now,syncDiscovery=false,accountNames={}}) {
    Object.assign(this,{api,bootstrap,manager,roster,clock,accountNames}); this.rooms=new Map(); this.accounts=new Map(); this.contacts=[]; this.health=new Map();this.histories=new Map();
    this.store=createCRMStore({api,bootstrap,managerMxid:manager});
    this.discoveryCache=syncDiscovery?new DiscoveryStateCache(api,clock):null;
    const common={store:this.store,api,managerMxid:manager,accountFor:o=>this.accounts.get(o),contacts:()=>this.contacts,isCurrent:()=>true};
    this.members=createCRMMembers({...common,polls:4});
    this.batches=createCRMBatches({...common,roomFor:(b,n)=>{ const r=[...this.rooms.values()].find(r=>r.owner===b.owner_mxid && r.native===n); return r ? {sourceId:r.source,label:r.title,direct:r.level==='direct'} : null; }});
  }
  async refresh() {
    const started=performance.now();
    await this.store.refresh(); assert(this.store.snapshot().ready,'Lists are still loading');
    await this.discoveryCache?.refresh();
    const stateFor=id=>this.discoveryCache?this.discoveryCache.state(id):this.api('GET',roomPath(id)+'/state');
    const rooms=new Map(), accounts=new Map(), contacts=[], health=new Map(), contactRooms=new Map(), ownerNames=new Map();let joinBudget=20;
    const roster=await this.roster();
    for(const [owner,space] of Object.entries(roster)) {
      if(this.settings().allowed_accounts!==null&&!this.settings().allowed_accounts.includes(owner))continue;
      if(!owner || !space) continue;
      let state; try { state=await stateFor(space); } catch { continue; }
      if(get(state,'m.room.create')?.sender!==owner) continue;
      health.set(owner,owned(state,'com.jkali.uplink_health',owner));
      const member=state.find(e=>e.type==='m.room.member'&&e.state_key===owner&&e.sender===owner);
      if(typeof member?.content?.displayname==='string')ownerNames.set(owner,member.content.displayname);
      const children=state.filter(e=>e.type==='m.space.child' && e.sender===owner && Object.keys(e.content||{}).length).map(e=>e.state_key).sort();
      // The browser is not required to accept already-shared room invitations.
      // Both the current roster space and this invite must prove the same owner.
      if(this.discoveryCache&&get(state,'m.room.create')?.content?.type==='m.space')for(const id of children){
        const cache=this.discoveryCache,invite=cache.invites.get(id),events=invite?.invite_state?.events;
        if(!joinBudget||!Array.isArray(events)||(cache.joinRetry.get(id)||0)>this.clock())continue;
        const create=get(events,'m.room.create'),member=events.find(e=>e.type==='m.room.member'&&e.state_key===this.manager);
        if(!/^![^\s:]+:[^\s]+$/.test(id)||create?.sender!==owner||create.content?.type==='m.space'||member?.sender!==owner||member.content?.membership!=='invite')continue;
        joinBudget--;
        try{await this.api('POST',roomPath(id)+'/join',{});cache.invites.delete(id);cache.joinRetry.delete(id);cache.states.delete(id);cache.unavailable.delete(id);}
        catch{cache.joinRetry.set(id,this.clock()+60000);}
      }
      for(let i=0;i<children.length;i+=8) await Promise.all(children.slice(i,i+8).map(async id=>{
        let s;try{s=await stateFor(id);}catch{return;}
        const create=get(s,'m.room.create'); if(create?.sender!==owner || create.content?.type==='m.space' || s.some(e=>['m.room.tombstone','m.room.encryption'].includes(e.type))) return;
        const native=create.content?.['com.jkali.mirror_of'];
        if(!native && owned(s,'com.jkali.proposals',owner)) {
          const cap=owned(s,'com.jkali.crm.capability',owner);
          if(cap?.owner_mxid===owner && cap.schema===1 && (!accounts.has(owner) || id<accounts.get(owner).proposal_room_id)) accounts.set(owner,{...cap,proposal_room_id:id});
          return;
        }
        if(!native && owned(s,'com.jkali.contacts',owner)) {
          const catalogs=contactRooms.get(owner)||[];catalogs.push(id);contactRooms.set(owner,catalogs);
          for(const e of s.filter(e=>e.type==='com.jkali.contact' && e.sender===owner && !e.content?.deleted && e.content?.network_id)) contacts.push({...e.content,owner_mxid:owner});
          return;
        }
        const level=owned(s,'com.jkali.share_level',owner);
        if(!/^![^\s:]+:[^\s]+$/.test(native||'') || !['share','direct'].includes(level?.level)) return;
        rooms.set(id,{id,owner,native,title:owned(s,'m.room.name',owner)?.name||'Conversation',source:owned(s,'com.jkali.source',owner)?.source||'unknown',
          level:level.level,consent:level,profile:owned(s,'com.jkali.profile',owner)?.id||null,unread:owned(s,'com.jkali.read_state',owner)?.unread_count??null,state:s});
      }));
    }
    for(const id of this.histories.keys())if(!rooms.has(id))this.histories.delete(id);
    this.rosterOwners=Object.keys(roster).filter(o=>this.allowedOwner(o)).sort();
    this.ownerNames=ownerNames;this.accountStates=new Map();this.rooms=rooms;this.accounts=accounts;this.contacts=contacts;this.health=health;this.contactRooms=contactRooms;
    this.discoveryCache?.prune();
    await this.hydrateAccounts();this.discoveryMetrics={...(this.discoveryCache?.stats||{}),duration_ms:Math.round(performance.now()-started),rooms:rooms.size};return this.lists();
  }
  discoveryStatus(){return this.discoveryMetrics?{...this.discoveryMetrics}:null;}
  state(){return this.store.snapshot().state;}
  settings(){return this.state()?.control?.settings||defaultControlSettings();}
  allowedOwner(owner){return this.settings().allowed_accounts===null||this.settings().allowed_accounts.includes(owner);}
  contextStamp(){
    // Only a fingerprint leaves this method. A refreshed discovery or canonical
    // access change discards old dialogue context, including names and snippets.
    return hash({state:this.state(),roster:this.rosterOwners||[],rooms:[...this.rooms.values()].map(r=>({id:r.id,owner:r.owner,native:r.native,title:r.title,source:r.source,consent:r.consent})).sort((a,b)=>a.id.localeCompare(b.id)),
      accounts:[...this.accounts.entries()].sort(([a],[b])=>a.localeCompare(b)),contacts:this.contacts.slice().sort((a,b)=>canonicalCRMJSON(a).localeCompare(canonicalCRMJSON(b)))});
  }
  summary(r){return {id:r.id,owner:r.owner,owner_name:this.accountNames[r.owner]||this.ownerNames?.get(r.owner)||null,title:r.title,source:r.source,mode:r.level,unread:r.unread};}
  list(id){const l=this.state().lists[id];assert(l&&!l.archived,'List is unavailable');return l;}
  room(id){const r=this.rooms.get(id);assert(r,'Conversation is no longer shared');return r;}
  async liveRoom(id){
    const previous=this.rooms.get(id);scopeAssert(previous,'Conversation is no longer shared');
    const state=await this.api('GET',roomPath(id)+'/state');
    assert(Array.isArray(state),'Conversation permissions unavailable');
    const create=get(state,'m.room.create'),consent=owned(state,'com.jkali.share_level',previous.owner);
    scopeAssert(create?.sender===previous.owner&&create.content?.['com.jkali.mirror_of']===previous.native
      &&!state.some(e=>['m.room.tombstone','m.room.encryption'].includes(e.type))
      &&['share','direct'].includes(consent?.level),'Conversation permissions changed. Review again.');
    const room={...previous,state,consent,level:consent.level};this.rooms.set(id,room);return room;
  }
  lists({include_archived=false}={}){assert(typeof include_archived==='boolean','Invalid archived-list filter');return Object.values(this.state().lists).filter(l=>include_archived||!l.archived).map(l=>({...l,members:Object.values(this.state().memberships).filter(m=>m.list_id===l.list_id&&!m.removed&&this.allowedOwner(this.state().bindings[m.binding_id]?.owner_mxid)).map(m=>({...m,person:this.state().persons[m.person_id],binding:this.state().bindings[m.binding_id]})),conversations:this.listRooms(l).map(r=>this.summary(r))}));}
  contactMember(list_id,binding_id){
    const b=this.state().bindings[binding_id],l=this.list(list_id);
    const member=Object.values(this.state().memberships).find(m=>m.list_id===list_id&&!m.removed&&m.binding_id===binding_id);
    if(!member||!b||b.availability!=='shared'||!this.allowedOwner(b.owner_mxid)||this.accounts.get(b.owner_mxid)?.generation!==b.generation)return null;
    const contacts=this.contacts.filter(c=>c.owner_mxid===b.owner_mxid&&c.source===b.contact.source&&c.network_id===b.contact.network_id);
    if(contacts.length!==1||contacts[0].contact_version!==b.contact_version)return null;
    const contact=contacts[0],stored=l.contact_obligations?.[binding_id];
    const obligation=stored?.membership_operation===member.operation_id&&stored.owner_mxid===b.owner_mxid&&stored.generation===b.generation?stored:null;
    return {binding:b,member,contact,obligation};
  }
  memberRooms(member){
    const b=this.state().bindings[member.binding_id],c=b&&this.contacts.find(c=>c.owner_mxid===b.owner_mxid&&c.source===b.contact.source&&c.network_id===b.contact.network_id);
    if(!c||b.availability!=='shared'||!this.allowedOwner(b.owner_mxid)||this.accounts.get(b.owner_mxid)?.generation!==b.generation)return [];
    const grant=member.direct_request,account=this.accounts.get(b.owner_mxid),natives=new Set();
    if(grant&&grant.generation===account?.generation&&grant.journal_epoch===account?.journal_epoch){
      for(const r of this.accountStates?.get(b.owner_mxid)||[])if(r.type==='com.jkali.crm.direct_result'&&r.state_key===member.operation_id&&r.sender===b.owner_mxid&&r.content?.generation===grant.generation&&r.content?.journal_epoch===grant.journal_epoch)
        for(const item of r.content.rooms||[])if(item.state==='applied')natives.add(item.room_id);
    }
    return [...this.rooms.values()].filter(r=>r.owner===b.owner_mxid&&((c.person_id&&r.profile===c.person_id)||(r.source===c.source&&natives.has(r.native))));
  }
  listRooms(list){
    const ids=new Set(Object.values(list.watches||{}).filter(w=>w.enabled&&this.rooms.get(w.room_id)?.owner===w.owner_mxid).map(w=>w.room_id));
    for(const m of Object.values(this.state().memberships).filter(m=>m.list_id===list.list_id&&!m.removed)) {
      for(const r of this.memberRooms(m))ids.add(r.id);
    }
    return [...ids].map(id=>this.rooms.get(id)).filter(Boolean).filter(r=>list.watches?.[r.id]?.enabled!==false);
  }
  async account(owner){const a=this.accounts.get(owner);assert(a,'Sending account is unavailable');const result=await readCRMAccount({api:this.api,account:a,ownerMxid:owner,managerMxid:this.manager,isCurrent:()=>true});this.accountStates ||= new Map();this.accountStates.set(owner,result.state);return result;}
  async hydrateAccounts(){for(const o of this.accounts.keys()) try{await this.account(o);}catch{this.accounts.delete(o);}}
  sourceStatus(owner){
    const health=this.health.get(owner),time=value=>{const ms=value<1e12?value*1000:value;return Number.isSafeInteger(ms)&&ms>0&&ms<=this.clock()+30000?ms:null;};
    const reported_at=time(health?.updated_at),last_ingestion_at=time(health?.last_ingestion),last_delivery_at=time(health?.last_delivery);
    const fresh=reported_at!==null&&reported_at<=this.clock()+30000&&this.clock()-reported_at<420000&&health?.connected===true
      &&!health.history_incomplete&&!health.pending_events&&!health.history_pages_pending&&!health.delivery_incomplete&&!health.revocations_pending&&!(health.stage_errors||[]).length;
    return {fresh,connected:health?.connected===true,reported_at,last_ingestion_at,last_delivery_at};
  }
  async history(id,limit=15,cursor=null){
    const r=this.room(id);assert(Number.isInteger(limit)&&limit>0&&limit<=100,'History limit must be 1–100');
    assert(cursor===null||typeof cursor==='string'&&cursor.length<2048,'Invalid cursor');
    // Read the whole verified mirror once, then advance its source stream. A
    // freshly imported old message must never masquerade as the latest reply.
    let index=this.histories.get(id);
    if(!index){index={events:new Map(),edits:new Map(),removed:new Set(),cursor:null};this.histories.set(id,index);}
    index.used_at=this.clock();
    let complete=false;
    for(let p=0;p<200;p++){
      const page=await this.api('GET',roomPath(id)+'/messages?dir=f&limit=100'+(index.cursor?'&from='+enc(index.cursor):''));
      assert(Array.isArray(page.chunk),'History unavailable');
      for(const e of page.chunk){
        if(e.sender!==r.owner)continue;
        if(e.type==='m.room.redaction') {const target=e.redacts||e.content?.redacts;index.removed.add(target);index.events.delete(target);continue;}
        if(e.unsigned?.redacted_because){index.removed.add(e.event_id);index.events.delete(e.event_id);continue;}
        if(e.type!=='m.room.message'||e.state_key!==undefined)continue;
        const relation=e.content?.['m.relates_to'];
        if(relation?.rel_type==='m.replace'){
          // The source uplink mirrors a native edit as flat replacement content
          // plus m.replace; standard Matrix edits use m.new_content instead.
          if(editContent(e.content))index.edits.set(e.event_id,{target:relation.event_id,event:e});continue;
        }
        if(message(e,r.owner)&&!index.removed.has(e.event_id))index.events.set(e.event_id,e);
      }
      assert(index.events.size<=100000,'Conversation exceeds the supported history window');
      if(!page.end||page.end===index.cursor){index.cursor ||= page.start;complete=true;break;}
      index.cursor=page.end;
    }
    assert(complete,'Conversation history is still loading. Try again to continue.');
    const corrections=timestampCorrections(r.state,r.owner);
    const latestEdits=new Map();
    for(const [editId,entry] of index.edits)if(!index.removed.has(editId)){
      const old=latestEdits.get(entry.target);if(!old||entry.event.origin_server_ts>=old.origin_server_ts)latestEdits.set(entry.target,entry.event);
    }
    const messages=[...index.events.values()].map(original=>{
      let e=original;
      const bundled=e.unsigned?.['m.relations']?.['m.replace'];
      if(bundled&&index.removed.has(bundled.event_id))e={...e,unsigned:{...e.unsigned,'m.relations':{}}};
      const edit=editContent(latestEdits.get(e.event_id)?.content);
      return message(edit?{...e,unsigned:{...e.unsigned,'m.relations':{'m.replace':{sender:r.owner,content:{'m.new_content':edit}}}}}:e,r.owner,corrections);
    }).filter(Boolean).sort((a,b)=>a.ts-b.ts||a.id.localeCompare(b.id));
    index.messages=messages;
    let end=messages.length;
    if(cursor){let anchor;try{anchor=JSON.parse(Buffer.from(cursor,'base64url').toString());}catch{throw new Error('Invalid history cursor');}
      assert(anchor.room===id,'History cursor belongs to another conversation');end=messages.findIndex(m=>m.id===anchor.before);
      assert(end>=0,'History changed. Reopen the latest messages.');}
    const begin=Math.max(0,end-limit),slice=messages.slice(begin,end);
    const source=this.sourceStatus(r.owner),tracking=this.tracking(r);
    return {conversation:this.summary(r),messages:slice,version:hash(messages.map(m=>[m.id,m.version])),cursor:begin?Buffer.from(JSON.stringify({room:id,before:slice[0].id})).toString('base64url'):null,start:index.cursor,complete:begin===0,
      availability:{...source,checked_at:this.clock(),mirror_caught_up:true,native_history_complete:source.fresh&&tracking.fresh&&tracking.complete&&tracking.all_messages_included&&tracking.messages.length===messages.length,
        coverage:tracking.coverage,available_count:messages.length,oldest_at:messages[0]?.ts??null,newest_at:messages.at(-1)?.ts??null}};
  }
  async searchHistory(id,query){
    assert(typeof query==='string'&&query.trim()&&query.length<=200,'Use a short history search query');
    const base=await this.history(id,1),messages=this.histories.get(id).messages;
    const normalize=text=>String(text).normalize('NFKD').replace(/\p{M}/gu,'').toLowerCase();
    const terms=[...new Set(normalize(query).match(/[\p{L}\p{N}]+/gu)||[])];
    assert(terms.length>0&&terms.length<=12,'Use a few distinctive topic words');
    const hits=messages.map((m,i)=>{const text=normalize(m.text);return {i,score:terms.filter(t=>text.includes(t)).length};}).filter(h=>h.score>0).sort((a,b)=>b.score-a.score||b.i-a.i);
    const selected=new Set();for(const {i} of hits.slice(0,12))for(let n=Math.max(0,i-2);n<=Math.min(messages.length-1,i+2);n++)selected.add(n);
    return {...base,messages:[...selected].sort((a,b)=>a-b).map(i=>messages[i]),cursor:null,search:{query,matched_messages:hits.length,returned_matches:Math.min(hits.length,12),truncated:hits.length>12,scanned_messages:messages.length},complete:base.availability.native_history_complete};
  }
  async prepareSend({room_id,body}){
    const r=await this.liveRoom(room_id);assert(typeof body==='string'&&body.trim()&&body.length<=8000&&sanitize(body.slice(0,4000))+sanitize(body.slice(4000))===body&&!body.startsWith('!'),'Use visible message text, up to 8000 characters');
    await this.account(r.owner);const h=await this.history(room_id,15),a=this.accounts.get(r.owner),id=randomUUID();
    return {kind:'send',id,created:this.clock(),room_id,owner:r.owner,conversation:this.summary(r),native:r.native,account:a,consent:r.consent,
      latest:h.messages.at(-1)?.id||null,context_version:h.version,latest_message:h.messages.at(-1)||null,body,content:{target_room:r.native,'com.jkali.send_mode':r.level==='direct'?'direct':'suggest','com.jkali.control_draft_id':id,body,created_by:this.manager,origin_ts:this.clock()}};
  }
  pruneHistory(maxAge){for(const [id,index] of this.histories)if(this.clock()-(index.used_at||0)>maxAge)this.histories.delete(id);}
  async validateSend(d){
    const r=await this.liveRoom(d.room_id);assert(this.clock()-d.created<=600000,'Review expired. Prepare the message again.');await this.account(r.owner);
    const h=await this.history(r.id,1);
    assert(typeof d.context_version==='string'&&h.version===d.context_version&&r.owner===d.owner&&r.native===d.native&&canonicalCRMJSON(r.consent)===canonicalCRMJSON(d.consent)&&canonicalCRMJSON(this.accounts.get(r.owner))===canonicalCRMJSON(d.account)&& (h.messages.at(-1)?.id||null)===d.latest,'Conversation, permissions or latest message changed. Review again.');
  }
  async liveContact(binding){
    const matches=[];
    for(const room of this.contactRooms?.get(binding.owner_mxid)||[]){
      const state=await this.api('GET',roomPath(room)+'/state');
      assert(Array.isArray(state),'Shared contact permissions unavailable');
      scopeAssert(get(state,'m.room.create')?.sender===binding.owner_mxid
        &&owned(state,'com.jkali.contacts',binding.owner_mxid)
        &&!state.some(e=>['m.room.tombstone','m.room.encryption'].includes(e.type)),'Shared contact catalog changed');
      for(const e of state)if(e.type==='com.jkali.contact'&&e.sender===binding.owner_mxid&&!e.content?.deleted
        &&e.content?.source===binding.contact.source&&e.content?.network_id===binding.contact.network_id)matches.push(e.content);
    }
    scopeAssert(matches.length===1&&matches[0].contact_version===binding.contact_version,'Contact sharing or identity changed. Review again.');
  }
  async validateReviewScope(d){
    if(['send','batch','contact'].includes(d.kind))assert(this.settings().sending_mode==='draft_confirm','Control is in read-only mode');
    if(d.kind==='send')return this.validateSend(d);
    if(d.kind==='contact'){await this.liveContact(d.plan.binding);return this.members.validate(d.plan);}
    if(d.kind==='batch'){
      for(const item of d.manifest.items){
        const binding=this.state().bindings[item.binding_id];assert(binding,'Contact binding unavailable');await this.liveContact(binding);
        if(item.route.mode==='direct_existing'){
          const r=[...this.rooms.values()].find(r=>r.owner===item.owner_mxid&&r.native===item.route.room_id);
          assert(r,'Conversation is no longer shared');assert((await this.liveRoom(r.id)).level==='direct','Direct permission changed');
        }
      }
      return this.batches.validate(d.manifest);
    }
    if(d.kind==='list'){
      if(d.operations){
        assert(this.settings().notifications_enabled,'Automatic notifications are off. Enable them first.');
        const room=await this.liveRoom(d.room_id);assert(room.owner===d.owner&&this.allowedOwner(room.owner),'Conversation account changed. Review again.');
        return this.notificationOperations(d);
      }
      if(d.operation.type==='member.remove'){
        const scope=d.member_scope,binding=this.state().bindings[scope?.binding_id];
        const member=Object.values(this.state().memberships).find(m=>m.membership_id===d.operation.entity_id);
        assert(scope&&binding&&this.allowedOwner(binding.owner_mxid)&&hash(binding)===scope.binding_hash
          &&member&&!member.removed&&member.operation_id===scope.operation_id&&member.binding_id===scope.binding_id
          &&this.list(member.list_id).revision===scope.list_revision,'List assignment or account access changed. Review the removal again.');
      }
      if(d.operation.payload.room_id)await this.liveRoom(d.operation.payload.room_id);
      if(d.operation.type==='list.contact_obligation'){
        const entry=this.contactMember(d.operation.entity_id,d.operation.payload.binding_id);
        assert(entry,'Shared list contact is unavailable');await this.liveContact(entry.binding);
      }
      return this.validateOperation(d.operation);
    }
  }
  async executeSend(d){await this.validateSend(d);const result=await this.api('PUT',roomPath(d.account.proposal_room_id)+'/send/com.jkali.proposal/'+enc('control_'+d.id),d.content);assert(typeof result.event_id==='string','Submission outcome is unknown; do not resend');return {proposal_id:result.event_id,proposal_room:d.account.proposal_room_id,mode:d.content['com.jkali.send_mode'],status:'submitted',room_id:d.room_id};}
  async reconcileSend(d){
    if(d.content?.['com.jkali.control_draft_id']!==d.id)return null;
    await this.account(d.owner);
    assert(canonicalCRMJSON(this.accounts.get(d.owner))===canonicalCRMJSON(d.account),'Sending account changed; original submission remains uncertain');
    let cursor=null,found=null,complete=false;
    const visited=new Set(),seen=new Set();
    for(let page=0;page<200;page++){
      const data=await this.api('GET',roomPath(d.account.proposal_room_id)+'/messages?dir=f&limit=100&filter='+enc(JSON.stringify({types:['com.jkali.proposal']}))+(cursor?'&from='+enc(cursor):''));
      assert(Array.isArray(data.chunk),'Proposal history unavailable');
      for(const event of data.chunk){
        if(event.sender!==this.manager||event.type!=='com.jkali.proposal'||event.state_key!==undefined||event.unsigned?.redacted_because||event.content?.['com.jkali.control_draft_id']!==d.id)continue;
        if(seen.has(event.event_id))continue;seen.add(event.event_id);
        assert(!found&&typeof event.event_id==='string'&&canonicalCRMJSON(event.content)===canonicalCRMJSON(d.content),'Conflicting proposal identity; submission requires reconciliation');
        found={proposal_id:event.event_id,proposal_room:d.account.proposal_room_id,mode:d.content['com.jkali.send_mode'],status:'submitted',room_id:d.room_id};
      }
      if(!data.end||data.end===cursor){complete=true;break;}
      assert(!visited.has(data.end),'Proposal history cursor loop');visited.add(data.end);cursor=data.end;
    }
    assert(complete,'Proposal history is incomplete; submission remains uncertain');return found;
  }
  operation(type,id,base,payload,op_id=randomUUID()){
    payload=reminderOperationPayload(type,this.state().lists[id],payload,this.clock());
    return {schema:1,op_id,workspace_id:this.state().workspace_id,type,entity_id:id,base_revision:base,payload};
  }
  async validateOperation(op){const result=await applyVerifiedCRMOperation(this.state(),op);assert(result.outcome.accepted,'List change refused: '+result.outcome.code);}
  async apply(op){const result=await (['list.archive','member.remove'].includes(op.type)?this.members.applyChange(op):this.store.append(op));assert(result.accepted,'List change refused: '+result.code);return result;}
  prepareMemberRemoval({list_id,binding_id}){
    const list=this.list(list_id),binding=this.state().bindings[binding_id];
    const member=Object.values(this.state().memberships).find(m=>m.list_id===list_id&&!m.removed&&m.binding_id===binding_id);
    assert(member&&binding&&this.allowedOwner(binding.owner_mxid),'This account-bound list contact is unavailable');
    return {owner:binding.owner_mxid,operation:this.operation('member.remove',member.membership_id,member.revision,{list_id,person_id:member.person_id}),
      member_scope:{binding_id,binding_hash:hash(binding),operation_id:member.operation_id,list_revision:list.revision},
      description:`Remove ${this.state().persons[member.person_id]?.label||binding.contact.network_id} from ${list.name}\n${binding.owner_mxid} · ${binding.contact.source} · ${binding.contact.network_id}\nCancel pending list Direct setup through the source account. Existing Direct stays enabled. Other lists and sent messages stay unchanged.`};
  }
  async validateOperationPlan(operations){
    assert(Array.isArray(operations)&&operations.length>0&&operations.length<=3,'Invalid notification setup');
    let state=this.state();
    for(const op of operations){
      assert(['list.create','list.monitoring','list.watch'].includes(op.type),'Invalid notification operation');
      const next=await applyVerifiedCRMOperation(state,op);
      assert(next.outcome.accepted,'List change refused: '+next.outcome.code);state=next.state;
    }
  }
  async prepareNotifications({room_id,channels=['browser','telegram']}){
    assert(this.settings().notifications_enabled,'Automatic notifications are off. Enable them first.');
    const room=await this.liveRoom(room_id);assert(this.allowedOwner(room.owner),'This source account is excluded by control access settings');
    const state=this.state(),common=state.lists['browser-followed-conversations'];
    // A disabled/archived shared list must not be re-enabled for other watches.
    const list_id=!common||(!common.archived&&common.monitoring?.enabled)?'browser-followed-conversations':'reply-alerts-'+hash(room_id).slice(0,24);
    const list=state.lists[list_id];assert(!list?.archived,'This reply-alert list is archived. Restore it first.');
    assert(!list||list.monitoring?.enabled||Object.values(list.watches||{}).every(w=>!w.enabled||w.room_id===room_id),'This list has other disabled alerts. Choose a separate list or review enabling it first.');
    const operations=[],name=list?.name||(list_id==='browser-followed-conversations'?'Followed conversations':'Replies · '+room.title);
    let revision=list?.revision||0;
    if(!list)operations.push(this.operation('list.create',list_id,revision++,{name,purpose:'monitoring'}));
    if(!list?.monitoring?.enabled)operations.push(this.operation('list.monitoring',list_id,revision++,{...defaultMonitoring(),enabled:true}));
    const policy={enabled:true,all_messages:false,channels:[...channels].sort(),activity_enabled:true,schedule:null,presentation:null,routing:null};
    operations.push(this.operation('list.watch',list_id,revision,{...list?.watches?.[room_id],room_id,owner_mxid:room.owner,due_at:list?.watches?.[room_id]?.due_at??null,snooze_until:list?.watches?.[room_id]?.snooze_until??null,resolved_event:list?.watches?.[room_id]?.resolved_event??null,...policy}));
    await this.validateOperationPlan(operations);
    let projected=state;
    for(const op of operations.slice(0,-1))projected=(await applyVerifiedCRMOperation(projected,op)).state;
    const notification_scope={
      workspace_id:state.workspace_id,
      list:notificationListScope(projected.lists[list_id]),
      watch:list?.watches?.[room_id]??null,
    };
    return {operations,notification_scope,room_id,owner:room.owner,list_name:name,policy:{...policy,preview:list?.monitoring?.preview??false}};
  }
  async notificationOperations(d){
    // Legacy reviews lack a scoped snapshot and retain exact-revision checks.
    if(!d.notification_scope){await this.validateOperationPlan(d.operations);return d.operations;}
    const watch=d.operations.at(-1),state=this.state(),list=state.lists[watch.entity_id],scope=d.notification_scope;
    assert(watch.type==='list.watch'&&watch.payload.room_id===d.room_id&&watch.payload.owner_mxid===d.owner
      &&scope.workspace_id===state.workspace_id,'Notification review changed. Prepare a fresh review.');
    if(!list||list.revision===d.operations[0].base_revision){await this.validateOperationPlan(d.operations);return d.operations;}
    assert(hash(notificationListScope(list))===hash(scope.list),
      'The list settings changed. Prepare a fresh notification review.');
    const current=list.watches?.[d.room_id]??null;
    // Already effective is a successful no-op, never an uncertain-work replay.
    if(hash(current)===hash(watch.payload))return [];
    assert(hash(current)===hash(scope.watch),
      'This conversation’s alert settings or reminders changed. Prepare a fresh notification review.');
    const operations=[{...watch,base_revision:list.revision}];
    await this.validateOperationPlan(operations);return operations;
  }
  async applyNotificationPlan(d){
    const operations=await this.validateReviewScope(d);
    // Each operation has its own durable ID. An interrupted/partial execution
    // stays unknown in the draft ledger and is never automatically replayed.
    for(const op of operations){await this.liveRoom(d.room_id);await this.apply(op);}
    return {notifications_enabled:true,room_id:d.room_id,...(!operations.length?{already_enabled:true}:{})};
  }
  preparePolicy({list_id,...changes}){const l=this.list(list_id);return this.operation('list.monitoring',list_id,l.revision,{...defaultMonitoring(),...l.monitoring,...changes});}
  prepareWatch({list_id,room_id,inherit_monitoring,...changes}){
    const l=this.list(list_id),r=this.room(room_id),payload={room_id,owner_mxid:r.owner,enabled:true,due_at:null,snooze_until:null,resolved_event:null,...l.watches?.[room_id],...changes};
    if(inherit_monitoring===true)for(const key of ['all_messages','waiting_them_ms','waiting_us_ms','schedule','repeat_ms','activity_enabled','reminders_enabled','presentation','channels','routing'])delete payload[key];
    return this.operation('list.watch',list_id,l.revision,payload);
  }
  async prepareObligation({list_id,room_id,event_id,...changes}){
    const l=this.list(list_id),r=this.room(room_id),update=(await this.updates(list_id)).find(u=>u.id===room_id);
    const current=update?.obligations.find(o=>o.event_id===event_id);
    assert(current,'Message obligation is unavailable. Refresh this conversation.');
    const previous=l.obligations?.[JSON.stringify([room_id,event_id])];
    return this.operation('list.obligation',list_id,l.revision,{room_id,owner_mxid:r.owner,event_id,due_at:current.due_at??null,snooze_until:current.snooze_until??null,resolved:current.waiting==='resolved',...previous,...changes});
  }
  async prepareContactObligation({list_id,binding_id,...changes}){
    const l=this.list(list_id),entry=this.contactMember(list_id,binding_id);
    assert(entry,'Shared list contact is unavailable. Refresh the list.');
    await this.liveContact(entry.binding);
    return this.operation('list.contact_obligation',list_id,l.revision,{binding_id,owner_mxid:entry.binding.owner_mxid,generation:entry.binding.generation,
      membership_operation:entry.member.operation_id,due_at:null,snooze_until:null,resolved:false,...entry.obligation,...changes,
      created_at:!entry.obligation||Object.hasOwn(changes,'due_at')&&changes.due_at!==entry.obligation.due_at||entry.obligation.resolved&&changes.resolved===false?this.clock():entry.obligation.created_at,
      episode_at:!entry.obligation||Object.entries(changes).some(([k,v])=>entry.obligation[k]!==v)?Math.max(this.clock(),(entry.obligation?.episode_at||0)+1):entry.obligation.episode_at});
  }
  async contactUpdates(list_id=null,filters={}){
    assert(filters.since===undefined||Number.isSafeInteger(filters.since)&&filters.since>=0,'Invalid update time');
    const lists=list_id?[this.list(list_id)]:Object.values(this.state().lists).filter(l=>!l.archived),out=[];
    for(const l of lists)for(const member of Object.values(this.state().memberships).filter(m=>m.list_id===l.list_id&&!m.removed)){
      const entry=this.contactMember(l.list_id,member.binding_id);if(!entry)continue;
      const {binding:b,contact,obligation:o}=entry;
      if(filters.owner&&filters.owner!==b.owner_mxid||filters.source&&filters.source!==contact.source)continue;
      const rooms=this.memberRooms(member).filter(r=>r.source===contact.source);
      if(rooms.length&&!o)continue;
      // This means no linked shared conversation, never proof of an empty native
      // inbox. A dated contact task is explicit intent, not a missing-reply claim.
      let evidence=null,fresh=this.sourceStatus(b.owner_mxid).fresh;
      for(const r of rooms){
        const history=await this.history(r.id,1),tracking=this.tracking(r);
        if(!tracking.fresh||!this.sourceStatus(r.owner).fresh||(tracking.messages.at(-1)?.event_id||null)!==(history.messages.at(-1)?.id||null)){fresh=false;continue;}
        const sent=tracking.messages.find(m=>!m.incoming&&m.timestamp>=(o?.created_at??this.clock())&&['sent','delivered','read'].includes(m.delivery));
        if(sent){evidence={room_id:r.id,event_id:sent.event_id,timestamp:sent.timestamp};break;}
      }
      if(filters.since!==undefined&&!(evidence?.timestamp>=filters.since||o?.due_at>=filters.since))continue;
      out.push({kind:'contact',id:b.binding_id,binding_id:b.binding_id,owner:b.owner_mxid,generation:b.generation,membership_operation:member.operation_id,
        person_id:b.person_id,title:contact.display_name||contact.person_display||contact.network_id,source:contact.source,list_id:l.list_id,list_name:l.name,
        waiting:o?.resolved?'resolved':evidence?'contacted':rooms.length?'contact_pending':'uncontacted',
        status_label:o?.resolved?'Resolved':evidence?'Confirmed outreach recorded':rooms.length?'Contact task open':'No shared conversation linked',
        history_known:!!evidence,conversation_ids:rooms.map(r=>r.id),source_evidence:evidence,
        created_at:o?.created_at??null,episode_at:o?.episode_at??null,due_at:o?.due_at??null,snooze_until:o?.snooze_until??null,resolved:o?.resolved===true,fresh,
        contact_version:b.contact_version});
    }return out;
  }
  contactRules(){
    if(!this.settings().notifications_enabled)return [];
    const rules=[];
    for(const l of Object.values(this.state().lists))if(!l.archived&&l.monitoring?.enabled&&l.monitoring.reminders_enabled!==false){
      for(const binding_id of Object.keys(l.contact_obligations||{})){
        const e=this.contactMember(l.list_id,binding_id),o=e?.obligation;if(!o?.due_at||o.resolved)continue;
        rules.push({...l.monitoring,list_id:l.list_id,list_name:l.name,revision:l.revision,binding_id,owner:e.binding.owner_mxid,generation:e.binding.generation,
          created_at:o.created_at,episode_at:o.episode_at,due_at:o.due_at,snooze_until:o.snooze_until,title:e.contact.display_name||e.contact.network_id,source:e.contact.source});
      }
    }return rules;
  }
  async prepareSeen({list_id,room_id}){
    const l=this.list(list_id),u=(await this.updates(list_id)).find(u=>u.id===room_id);
    assert(u?.seen_checkpoint,'No verified message checkpoint is available');
    return this.operation('list.seen',list_id,l.revision,u.seen_checkpoint);
  }
  async personContext(person_id,owner=null){
    const person=this.state().persons[person_id];assert(person,'Person unavailable');
    const bindings=[];
    for(const binding of Object.values(this.state().bindings)){
      if(binding.person_id!==person_id||binding.availability!=='shared'||owner&&binding.owner_mxid!==owner
        ||this.accounts.get(binding.owner_mxid)?.generation!==binding.generation)continue;
      const contacts=this.contacts.filter(c=>c.owner_mxid===binding.owner_mxid&&(binding.contact.profile_id
        ?c.person_id===binding.contact.profile_id:c.source===binding.contact.source&&c.network_id===binding.contact.network_id));
      for(const contact of contacts)bindings.push({binding_id:binding.binding_id,owner:binding.owner_mxid,source:contact.source,network_id:contact.network_id,display_name:contact.display_name||null,profile:contact.person_id||null});
    }
    assert(bindings.length,'No currently shared contact context is available for this person/account');
    const rooms=[...this.rooms.values()].filter(r=>bindings.some(b=>b.owner===r.owner&&b.source===r.source&&b.profile&&b.profile===r.profile));
    const conversations=[];for(const room of rooms.slice(0,10))conversations.push(await this.history(room.id,15));
    return {person_id,label:person.label,bindings,conversations,more_conversations:rooms.length>10,
      scope:'Only current shared contact profiles with exact account/source identity. Missing history or unlinked conversations are unavailable, not evidence of no contact.'};
  }
  tracking(r){
    const p=owned(r.state,'com.jkali.conversation_tracking',r.owner),a=this.accounts.get(r.owner);
    const empty={state:'unknown',coverage:'unavailable',complete:false,messages:[],fresh:false};
    if(!p||p.schema!==1||p.state!=='ready'||!a||p.owner_mxid!==r.owner||p.generation!==a.generation||p.journal_epoch!==a.journal_epoch
      ||p.room_id!==r.native||p.mirror_room_id!==r.id||p.source!==r.source||typeof p.bridge_epoch!=='string'||!p.bridge_epoch
      ||!['observed_history','native_history','native_suffix'].includes(p.coverage)||typeof p.complete!=='boolean'
      ||p.complete&&p.coverage!=='native_history'||!Array.isArray(p.messages)||p.messages.length>250)return empty;
    const messages=[],seen=new Set(),index=this.histories.get(r.id);let order=-1n;
    for(const item of p.messages){
      if(typeof item.event_id!=='string'||!/^\$[^\s]{1,255}$/.test(item.event_id)||seen.has(item.event_id)||typeof item.incoming!=='boolean'
        ||!Number.isSafeInteger(item.timestamp)||item.timestamp<=0||typeof item.order!=='string'||!/^\d{1,32}$/.test(item.order)||BigInt(item.order)<=order
        ||item.answered_by!==null&&(typeof item.answered_by!=='string'||!/^\$[^\s]{1,255}$/.test(item.answered_by)))return empty;
      const original=index?.events.get(item.event_id);
      if(!original||index.removed.has(item.event_id)||(original.content?.['com.jkali.from_me']===true)===item.incoming)return empty;
      seen.add(item.event_id);order=BigInt(item.order);
      if(item.answered_by){const answer=index.events.get(item.answered_by);if(!answer||index.removed.has(item.answered_by)||(answer.content?.['com.jkali.from_me']===true)!==item.incoming)return empty;}
      const receipt=owned(r.state,'com.jkali.message_receipt',r.owner,item.event_id);
      messages.push({...item,delivery:item.incoming?null:receipt?.read===true?'read':receipt?.delivered===true?'delivered':receipt?.sent===true?'sent':receipt?.failed===true?'failed':'unknown'});
    }
    const targets=new Set();
    for(const item of messages)if(item.answered_by){const answer=messages.find(m=>m.event_id===item.answered_by);
      if(!answer||answer.incoming===item.incoming||BigInt(answer.order)<=BigInt(item.order)||targets.has(item.answered_by))return empty;targets.add(item.answered_by);}
    return {state:'ready',coverage:p.coverage,complete:p.complete,messages,bridge_epoch:p.bridge_epoch,
      all_messages_included:p.total_messages===messages.length,
      fresh:Number.isSafeInteger(p.updated_ts)&&p.updated_ts<=this.clock()+30000&&this.clock()-p.updated_ts<420000};
  }
  rules(){const result=[];if(!this.settings().notifications_enabled)return result;for(const l of Object.values(this.state().lists)) if(!l.archived&&l.monitoring?.enabled)for(const r of this.listRooms(l)){
    const watch=l.watches?.[r.id]?.owner_mxid===r.owner?l.watches[r.id]:{};
    if(watch.enabled===false)continue;
    result.push({list_id:l.list_id,list_name:l.name,revision:l.revision,room:r.id,owner:r.owner,title:r.title,source:r.source,...l.monitoring,...watch});
  }return result;}
  async publishMonitoringScopes(){
    this.monitoringScopes ||= new Map();
    const selected=new Set(Object.values(this.state().lists).filter(l=>!l.archived).flatMap(l=>this.listRooms(l).map(r=>r.id)));
    for(const [owner,account] of this.accounts){
      if(account.conversation_tracking!==true)continue;
      const rooms=[...this.rooms.values()].filter(r=>r.owner===owner&&selected.has(r.id)).map(r=>r.native).sort();
      const content={schema:1,owner_mxid:owner,generation:account.generation,journal_epoch:account.journal_epoch,rooms};
      assert(rooms.length<=1000&&Buffer.byteLength(canonicalCRMJSON(content))<48000,'Monitoring scope exceeds the supported account limit');
      const digest=hash(content),old=this.monitoringScopes.get(owner);
      if(old?.hash===digest&&this.clock()-old.sent<60000)continue;
      await this.api('PUT',roomPath(account.proposal_room_id)+'/send/com.jkali.monitoring_scope/'+enc('monitor_'+randomUUID()),content);
      this.monitoringScopes.set(owner,{hash:digest,sent:this.clock()});
    }
  }
  async updates(list_id=null,filters={}){
    assert(filters.since===undefined||Number.isSafeInteger(filters.since)&&filters.since>=0,'Invalid update time');
    const lists=list_id?[this.list(list_id)]:Object.values(this.state().lists).filter(l=>!l.archived),out=[];
    for(const list of lists)for(const r of this.listRooms(list)){
      if(filters.room_id&&filters.room_id!==r.id)continue;
      if(filters.owner&&filters.owner!==r.owner||filters.source&&filters.source!==r.source)continue;
      const h=await this.history(r.id,100),last=h.messages.at(-1),w=list.watches?.[r.id]?.owner_mxid===r.owner?list.watches[r.id]:{},policy={...defaultMonitoring(),...list.monitoring,...w};
      let evidence=this.tracking(r);
      // A newer mirrored message means the source projection has not caught up;
      // an old fresh timestamp alone must not authorize an overdue reminder.
      if((evidence.messages.at(-1)?.event_id||null)!==(last?.id||null))evidence={...evidence,state:'unknown',fresh:false};
      const obligations=evidence.messages.map(m=>{
        const setting=list.obligations?.[JSON.stringify([r.id,m.event_id])],override=setting?.owner_mxid===r.owner?setting:null;
        const delay=m.incoming?policy.waiting_us_ms:policy.waiting_them_ms;
        const resolved=override?.resolved===true||w.resolved_event===m.event_id;
        return {...m,waiting:m.answered_by?'answered':resolved?'resolved':m.incoming?'waiting_for_us':'waiting_for_them',
          due_at:override?override.due_at:(m.event_id===last?.id?w.due_at:null)??(delay?m.timestamp+delay:null),
          snooze_until:override?.snooze_until??w.snooze_until??null,
          ...(override?.episode_at||w.episode_at?{episode_at:Math.max(override?.episode_at||0,w.episode_at||0)}:{})};
      });
      if(evidence.state==='ready'&&evidence.complete&&!evidence.messages.length){
        const setting=list.obligations?.[JSON.stringify([r.id,'uncontacted'])],override=setting?.owner_mxid===r.owner?setting:null;
        obligations.push({event_id:'uncontacted',waiting:override?.resolved?'resolved':'uncontacted',due_at:override?override.due_at:w.due_at??null,snooze_until:override?.snooze_until??w.snooze_until??null,
          ...(override?.episode_at||w.episode_at?{episode_at:Math.max(override?.episode_at||0,w.episode_at||0)}:{})});
      }
      const active=obligations.filter(o=>!['answered','resolved'].includes(o.waiting)),directions=new Set(active.map(o=>o.waiting));
      const waiting=evidence.state!=='ready'?'unknown':directions.size>1?'waiting_both_directions':active.at(-1)?.waiting||(obligations.length?'resolved':'unknown');
      const due=active.map(o=>o.due_at).filter(Number.isSafeInteger).sort((a,b)=>a-b)[0]??null;
      const sourceFresh=this.sourceStatus(r.owner).fresh;
      const fresh=evidence.fresh&&sourceFresh,a=this.accounts.get(r.owner);
      // Seeing a conversation through one list also acknowledges that same
      // account/conversation in overlapping lists. Records remain in the journal.
      const seenOrder=Object.values(this.state().lists).map(l=>l.seen?.[r.id]).filter(s=>s?.owner_mxid===r.owner&&s.bridge_epoch===evidence.bridge_epoch&&s.generation===a?.generation)
        .reduce((order,s)=>BigInt(s.source_order)>order?BigInt(s.source_order):order,-1n);
      const master_unseen={count:evidence.state==='ready'?evidence.messages.filter(m=>m.incoming&&BigInt(m.order)>seenOrder).length:null,complete:evidence.state==='ready'&&evidence.complete&&evidence.all_messages_included};
      const checkpoint=evidence.state==='ready'&&evidence.messages.at(-1);
      if(filters.since!==undefined&&!(last?.ts>=filters.since||obligations.some(o=>o.timestamp>=filters.since)))continue;
      out.push({list_id:list.list_id,list_name:list.name,...this.summary(r),waiting,last,latest_messages:h.messages,context_version:h.version,obligations,
        due_at:due,snooze_until:w.snooze_until||null,fresh,window_complete:evidence.complete,coverage:evidence.coverage,tracking_state:evidence.state,
        source_unread:{count:Number.isSafeInteger(r.unread)&&r.unread>=0?r.unread:null,fresh:sourceFresh},master_unseen,
        seen_checkpoint:checkpoint?{room_id:r.id,owner_mxid:r.owner,event_id:checkpoint.event_id,source_order:checkpoint.order,bridge_epoch:evidence.bridge_epoch,generation:a.generation}:null});
    }return out;
  }
}

// Read-only diagnostics. A recent delivery is not required on an idle account.
// Native health is independent of the account's connection to the master.
export function accountHealth(health, now=Date.now()) {
  const count=v=>Number.isSafeInteger(v)&&v>=0?v:null;
  const time=v=>{if(typeof v!=='number')return null;const ms=v<1e12?v*1000:v;return Number.isSafeInteger(ms)&&ms>0&&ms<=now+30000?ms:null;};
  const reported_at=time(health?.updated_at),ingestion_at=time(health?.last_ingestion),delivery_at=time(health?.last_delivery);
  const queued=count(health?.pending_events),receipts=count(health?.receipts_pending),incomplete=count(health?.delivery_incomplete),history=count(health?.history_pages_pending),history_incomplete=count(health?.history_incomplete);
  const media=count(health?.media_retry),errors=Array.isArray(health?.stage_errors)?health.stage_errors.length:null;
  let status='unknown';
  if(reported_at!==null){
    if(now-reported_at>=420000)status='stale';
    else if(health.connected===false)status='disconnected';
    else if(health.connected===true){
      if(ingestion_at!==null&&now-ingestion_at>=420000)status='stalled';
      else if(errors>0)status='error';
      else if(queued>0||receipts>0||media>0)status='catching_up';
      else if(ingestion_at!==null&&queued===0&&receipts===0&&errors===0)status='ok';
    }
  }
  const native=health?.imessage,native_at=time(native?.updated_at),native_pending=count(native?.inbound_pending),native_refused=count(native?.inbound_refused_components);
  const retryable=count(native?.inbound_retryable),unavailable=count(native?.inbound_source_unavailable);
  const detailed=retryable!==null&&unavailable!==null&&native_pending!==null&&retryable+unavailable===native_pending;
  const failed_chats=count(native?.poll_failed_chats);
  let native_status='unknown';
  if(native_at!==null){
    if(now-native_at>=480000)native_status='stale';
    else if(native.poll_ok===false)native_status='error';
    else if(native.poll_ok===true){
      if(failed_chats>0)native_status='error';
      else if(detailed&&retryable>0)native_status='retrying';
      else native_status=native_pending>0?'incomplete':native_pending===0?'ok':'unknown';
    }
  }
  return {checked_at:now,master:{status,reported_at,ingestion_at,delivery_at,queued,receipts,media,incomplete,history,history_incomplete,error_count:errors},
    imessage:{status:native_status,reported_at:native_at,pending:native_pending,refused:native_refused,
      retryable:detailed?retryable:null,source_unavailable:detailed?unavailable:null,
      attachment_unavailable:detailed&&count(native?.inbound_attachment_unavailable)<=retryable?count(native?.inbound_attachment_unavailable):null,
      deferred:detailed&&count(native?.inbound_deferred)<=native_pending?count(native?.inbound_deferred):null,failed_chats},
    // This is a source-published snapshot, not an active end-to-end canary.
    live_probe:false};
}
