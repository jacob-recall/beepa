import assert from 'node:assert/strict';
import {acceptanceFixture} from '../fixtures/control_acceptance.mjs';
const h=acceptanceFixture();try{
 const rows=await h.core.tool('updates',{list_id:'followups'});assert.equal(rows.length,2);
 const david=rows.find(r=>r.owner==='@david:fixture'),elliot=rows.find(r=>r.owner==='@elliot:fixture');
 assert.equal(david.title,elliot.title);assert.equal(david.source,'imessage');assert.equal(elliot.source,'whatsapp');
 assert.equal(david.waiting,'waiting_for_us');assert.equal(elliot.waiting,'waiting_for_them');
 assert.equal(david.source_unread.count,3);assert.equal(elliot.source_unread.count,0);
 assert.equal(david.master_unseen.count,20);assert.equal(elliot.master_unseen.count,0);
 assert(rows.every(r=>r.fresh&&r.due_at===null));
 for(const row of rows){assert(row.obligations.every(o=>o.event_id.startsWith('$'+row.owner.split(':')[0].slice(1)+'-')));}
 assert.deepEqual((await h.core.tool('updates',{source:'whatsapp'})).map(r=>r.id),[elliot.id]);
 assert.deepEqual((await h.core.tool('updates',{owner:david.owner})).map(r=>r.id),[david.id]);
 assert.deepEqual((await h.core.tool('updates',{list_id:'followups',room_id:david.id})).map(r=>r.id),[david.id]);
 assert.equal((await h.core.tool('updates',{list_id:'followups',room_id:'!unshared:fixture'})).length,0);
 const seen=await h.domain.prepareSeen({list_id:'followups',room_id:david.id});h.apply(seen);
 const updated=await h.domain.updates();assert.equal(updated[0].master_unseen.count,0);assert.equal(updated[0].source_unread.count,3);assert.equal(updated[1].waiting,elliot.waiting);
 for(const r of rows){
  const history=await h.core.tool('history',{room_id:r.id});assert.equal(history.messages.length,15);assert(history.messages.every(m=>m.id.startsWith('$'+r.owner.split(':')[0].slice(1)+'-')));
  const draft=await h.core.tool('prepare_send',{room_id:r.id,body:'Exact '+r.owner});assert.equal(draft.owner,r.owner);assert.equal(draft.conversation.source,r.source);
  assert.equal(draft.content['com.jkali.send_mode'],r===david?'direct':'suggest');
 }
 assert.equal(h.writes.length,0,'read and prepare operations never submit');
 // Internal history reads must continue into a reviewed message obligation.
 const calls=[{name:'history',args:{room_id:david.id,display:false}},{name:'prepare_obligation',args:{list_id:'followups',room_id:david.id,event_id:'$david-19',due_at:'2026-10-19T12:00:00Z'}}];
 h.core.model=async messages=>{const call=calls.shift();assert(call);return {tool_calls:[{id:'internal-'+calls.length,type:'function',function:{name:call.name,arguments:JSON.stringify(call.args)}}]};};
 await h.ask('Remind me tomorrow to answer the latest message on David’s account.','internal-context');
 const latest=JSON.parse(h.core.db.prepare('SELECT payload FROM drafts ORDER BY rowid DESC LIMIT 1').get().payload);
 assert.equal(latest.operation.type,'list.obligation');assert.equal(latest.operation.payload.event_id,'$david-19');assert.equal(latest.operation.payload.due_at,h.now+86400000);
 assert.equal(calls.length,0);assert(h.sent.at(-1).text.includes('Confirm this exact change'));assert(!h.sent.some(p=>p.text.includes('Shared messages · verbatim text')));
 assert(h.sent.at(-1).text.includes('Deadline: Oct 19, 12:00 PM'));
 for(const value of ['2026-02-30T12:00:00Z','2026-10-19','2026-10-19T12:00:00','tomorrow','2026-10-19T12:00:00+00:00']){
  await assert.rejects(h.core.tool('prepare_obligation',{list_id:'followups',room_id:david.id,event_id:'$david-19',due_at:value}),/Deadline|deadline/);
 }
 const cleared=await h.core.tool('prepare_obligation',{list_id:'followups',room_id:david.id,event_id:'$david-19',due_at:null});assert.equal(cleared.operation.payload.due_at,null);
 const numeric=await h.core.tool('prepare_obligation',{list_id:'followups',room_id:david.id,event_id:'$david-19',due_at:h.now+86400000});assert.equal(numeric.operation.payload.due_at,h.now+86400000);
 assert.equal(h.writes.length,0);
 console.log('Cross-account acceptance: same person, central list, independent channels/history/waiting/unread/seen and reviewed Direct/Share routes passed.');
}finally{h.close();}
