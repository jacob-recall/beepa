// Derived dispatch evidence, never a second store of editable list settings.
// The caller supplies fresh canonical rules and source/obligation validation.
import {createHash} from 'node:crypto';
import {monitoringRouting} from '../shared/model/crm.js';

const channels=['browser','telegram'];
const terminalFailure=new Set(['failed','expired','unavailable']);
const states=new Set(['pending','in_flight','unknown','accepted','failed','expired','unavailable','canceled']);
const plain=v=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const assert=(ok,message)=>{if(!ok)throw new Error(message);};
const digest=value=>createHash('sha256').update(value).digest('hex');

export const notificationRouting=monitoringRouting;
export function notificationMatches(source,rule){
  return source.kind==='contact_reminder'?rule.binding_id===source.binding&&rule.owner===source.owner&&rule.generation===source.generation&&rule.created_at===source.created&&rule.episode_at===source.episode:rule.room===source.room;
}

export function notificationIdentity(source){
  if(plain(source)&&source.kind==='contact_reminder'){
    const fields=['kind','binding','owner','generation','created','episode','due','slot'];
    assert(Object.keys(source).length===fields.length&&fields.every(k=>Object.hasOwn(source,k)),'Unexpected contact reminder fields');
    assert(typeof source.binding==='string'&&/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(source.binding)&&!['__proto__','constructor','prototype'].includes(source.binding),'Invalid contact binding');
    assert(typeof source.owner==='string'&&source.owner.length<=256&&/^@[^\s:]+:[^\s]+$/.test(source.owner)&&typeof source.generation==='string'&&source.generation.length>0&&source.generation.length<=128&&!/[\u0000-\u001f]/.test(source.generation),'Invalid contact owner');
    assert(['created','episode','due','slot'].every(k=>Number.isSafeInteger(source[k])&&source[k]>0)&&source.slot>=source.due,'Invalid contact deadline');
    return digest('contact-deadline:'+JSON.stringify([source.owner,source.binding,source.generation,source.created,source.episode,source.due,source.slot]));
  }
  assert(plain(source)&&['message','reminder'].includes(source.kind)&&typeof source.room==='string'&&/^![^\s:]+:[^\s]+$/.test(source.room)&&source.room.length<=256,'Invalid notification source');
  assert(typeof source.event==='string'&&(/^\$[^\s]{1,254}$/.test(source.event)||source.kind==='reminder'&&source.event==='uncontacted'),'Invalid notification event');
  const fields=source.kind==='message'?['kind','room','event','outgoing']:['kind','room','event','due','slot',...(Object.hasOwn(source,'episode')?['episode']:[])];
  assert(Object.keys(source).length===fields.length&&fields.every(k=>Object.hasOwn(source,k)),'Unexpected notification source fields');
  if(source.kind==='message'){
    assert(typeof source.outgoing==='boolean','Invalid notification direction');
    return digest(source.room+'\0'+source.event);
  }
  assert(Number.isSafeInteger(source.due)&&source.due>0&&Number.isSafeInteger(source.slot)&&source.slot>=source.due,'Invalid reminder identity');
  if(Object.hasOwn(source,'episode')){
    assert(Number.isSafeInteger(source.episode)&&source.episode>0,'Invalid reminder episode');
    return digest('deadline-episode:'+JSON.stringify([source.room,source.event,source.due,source.episode,source.slot]));
  }
  return digest('deadline:'+JSON.stringify([source.room,source.event,source.due,...(source.slot===source.due?[]:[source.slot])]));
}

export class NotificationRouting {
  constructor({db,clock=Date.now}){
    this.db=db;this.clock=clock;this.running=false;
    db.exec(`
      CREATE TABLE IF NOT EXISTS notification_routes(id TEXT PRIMARY KEY,source TEXT NOT NULL,created INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS notification_route_rules(id TEXT NOT NULL,list_id TEXT NOT NULL,revision INTEGER NOT NULL,policy TEXT NOT NULL,slot INTEGER,chosen TEXT,state TEXT NOT NULL DEFAULT 'active',PRIMARY KEY(id,list_id));
      CREATE TABLE IF NOT EXISTS notification_route_attempts(id TEXT NOT NULL,channel TEXT NOT NULL,state TEXT NOT NULL,refs TEXT NOT NULL,updated INTEGER NOT NULL,PRIMARY KEY(id,channel));
      CREATE TABLE IF NOT EXISTS notification_route_progress(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
    `);
    // A process can stop after the adapter accepts and before its reply is stored.
    // Reconcile the same ID; never enqueue again or infer failure from absence.
    db.prepare("UPDATE notification_route_attempts SET state='unknown' WHERE state='enqueueing'").run();
  }
  observe(source,rules){
    const id=notificationIdentity(source),serialized=JSON.stringify(Object.fromEntries(Object.entries(source).sort(([a],[b])=>a.localeCompare(b))));
    assert(Array.isArray(rules)&&rules.length>0&&rules.length<=50,'Invalid notification rules');
    const seen=new Set(),prepared=rules.map(r=>{
      assert(plain(r)&&typeof r.list_id==='string'&&r.list_id.length>0&&r.list_id.length<=256&&!seen.has(r.list_id)&&Number.isSafeInteger(r.revision)&&r.revision>=0&&notificationMatches(source,r),'Invalid notification rule');
      seen.add(r.list_id);
      if(source.kind!=='message')assert(Number.isSafeInteger(r.delivery_slot)&&r.delivery_slot>=source.due&&r.delivery_slot<=source.slot,'Invalid reminder rule slot');
      return {rule:r,policy:notificationRouting(r)};
    });
    const old=this.db.prepare('SELECT source FROM notification_routes WHERE id=?').get(id);
    if(old){assert(JSON.stringify(JSON.parse(old.source))===serialized,'Notification identity changed');return id;}
    this.db.exec('BEGIN');
    try{
      this.db.prepare('INSERT INTO notification_routes VALUES(?,?,?)').run(id,serialized,this.clock());
      for(const {rule:r,policy:p} of prepared)this.db.prepare('INSERT INTO notification_route_rules(id,list_id,revision,policy,slot,chosen) VALUES(?,?,?,?,?,?)').run(id,r.list_id,r.revision,JSON.stringify(p),source.kind!=='message'?r.delivery_slot:null,p.primary);
      this.db.exec('COMMIT');
    }catch(error){this.db.exec('ROLLBACK');throw error;}
    return id;
  }
  status(id){
    const event=this.db.prepare('SELECT * FROM notification_routes WHERE id=?').get(id);if(!event)return null;
    return {id,source:JSON.parse(event.source),created:event.created,
      rules:this.db.prepare('SELECT list_id,revision,chosen,state FROM notification_route_rules WHERE id=? ORDER BY list_id').all(id).map(r=>({...r})),
      attempts:this.db.prepare('SELECT channel,state,updated FROM notification_route_attempts WHERE id=? ORDER BY channel').all(id).map(r=>({...r}))};
  }
  reconcile(id,channel,state){
    assert(channels.includes(channel)&& (states.has(state)||state==='missing'),'Invalid notification outcome');
    const old=this.db.prepare('SELECT state FROM notification_route_attempts WHERE id=? AND channel=?').get(id,channel);
    assert(old,'Notification attempt missing');
    if(state==='missing'||old.state==='accepted')return;
    // Explicit terminal evidence cannot regress into work to be retried. A late
    // accepted receipt may still upgrade any state without another delivery.
    if((terminalFailure.has(old.state)||old.state==='canceled')&&state!=='accepted')return;
    this.db.prepare('UPDATE notification_route_attempts SET state=?,updated=? WHERE id=? AND channel=?').run(state,this.clock(),id,channel);
  }
  plan(id,{rules,ready}){
    const event=this.db.prepare('SELECT source FROM notification_routes WHERE id=?').get(id);assert(event,'Notification event missing');
    assert(Array.isArray(rules)&&Array.isArray(ready),'Fresh notification policy validation required');
    const source=JSON.parse(event.source),demands=new Map(),attempts=new Map(this.status(id).attempts.map(a=>[a.channel,a]));
    for(const old of this.db.prepare("SELECT * FROM notification_route_rules WHERE id=? AND state='active' ORDER BY list_id").all(id)){
      const current=rules.find(r=>r.list_id===old.list_id&&r.revision===old.revision&&notificationMatches(source,r));
      if(!current||JSON.stringify(notificationRouting(current))!==old.policy||source.kind!=='message'&&current.delivery_slot!==old.slot){
        this.db.prepare("UPDATE notification_route_rules SET state='canceled' WHERE id=? AND list_id=?").run(id,old.list_id);continue;
      }
      if(!ready.includes(old.list_id))continue; // Schedule/digest deferral is not revocation.
      const policy=JSON.parse(old.policy);let wanted=policy.channels;
      if(policy.primary){
        let chosen=old.chosen;
        if(chosen===policy.primary&&policy.fallback&&terminalFailure.has(attempts.get(chosen)?.state)){
          chosen=policy.channels.find(c=>c!==policy.primary);
          this.db.prepare('UPDATE notification_route_rules SET chosen=? WHERE id=? AND list_id=?').run(chosen,id,old.list_id);
        }
        wanted=[chosen];
      }
      if(wanted.every(c=>['accepted','failed','expired','unavailable','canceled'].includes(attempts.get(c)?.state))){
        this.db.prepare("UPDATE notification_route_rules SET state='settled' WHERE id=? AND list_id=?").run(id,old.list_id);continue;
      }
      for(const channel of wanted){
        const refs=demands.get(channel)||[];
        refs.push({id:old.list_id,revision:old.revision,...(source.kind!=='message'?{slot:old.slot}:{})});demands.set(channel,refs);
      }
    }
    // Existing attempts, including definitive failures, are never resubmitted.
    return [...demands].filter(([channel])=>!attempts.has(channel)).map(([channel,rules])=>({id,channel,source,refs:source.kind==='message'?{outgoing:source.outgoing,rules}:{due:source.due,slot:source.slot,...(source.kind==='reminder'&&source.episode?{episode:source.episode}:{}),rules}}));
  }
  claim(job){
    return !!this.db.prepare("INSERT OR IGNORE INTO notification_route_attempts VALUES(?,?,'enqueueing',?,?)").run(job.id,job.channel,JSON.stringify(job.refs),this.clock()).changes;
  }
  async drain({validate,adapters,limit=100}){
    assert(Number.isInteger(limit)&&limit>0&&limit<=1000,'Invalid routing limit');
    if(this.running)return;this.running=true;
    try{
      // Reconcile accepted/unknown work by its exact stable ID. Missing evidence
      // or an unavailable adapter does not mean a notification failed.
      const after=this.db.prepare("SELECT value FROM notification_route_progress WHERE key='cursor'").get()?.value||0;
      let events=this.db.prepare(`SELECT e.rowid AS sequence,e.* FROM notification_routes e WHERE e.rowid>? AND
        EXISTS(SELECT 1 FROM notification_route_rules r WHERE r.id=e.id AND r.state='active')
        ORDER BY e.rowid LIMIT ?`).all(after,limit);
      if(!events.length&&after){
        this.db.prepare("INSERT OR REPLACE INTO notification_route_progress VALUES('cursor',0)").run();
        events=this.db.prepare(`SELECT e.rowid AS sequence,e.* FROM notification_routes e WHERE EXISTS(SELECT 1 FROM notification_route_rules r WHERE r.id=e.id AND r.state='active') ORDER BY e.rowid LIMIT ?`).all(limit);
      }
      for(const event of events){
        for(const a of this.status(event.id).attempts){
          if(a.state==='accepted'||terminalFailure.has(a.state)||a.state==='canceled')continue;
          try{const outcome=await adapters[a.channel].outcome(event.id);this.reconcile(event.id,a.channel,outcome);}catch{/* Keep exact previous evidence. */}
        }
        // Both adapters may be needed when a known rejection enables fallback.
        // Validation is repeated after every external await, before each claim.
        for(let step=0;step<2;step++){
          let validation;try{validation=await validate(JSON.parse(event.source),event.id);}catch{break;}
          const jobs=this.plan(event.id,validation);if(!jobs.length)break;
          for(const candidate of jobs){
            let fresh;try{fresh=await validate(candidate.source,event.id);}catch{continue;}
            const job=this.plan(event.id,fresh).find(j=>j.channel===candidate.channel);if(!job||!this.claim(job))continue;
            try{
              const state=await adapters[job.channel].enqueue(job);
              if(!states.has(state))throw Error('Invalid adapter acknowledgment');
              this.reconcile(job.id,job.channel,state);
            }catch{this.reconcile(job.id,job.channel,'unknown');}
          }
        }
        this.db.prepare("INSERT OR REPLACE INTO notification_route_progress VALUES('cursor',?)").run(event.sequence);
      }
    }finally{this.running=false;}
  }
}
