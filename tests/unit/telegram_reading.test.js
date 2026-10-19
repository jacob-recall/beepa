import assert from 'node:assert/strict';
import {acceptanceFixture} from '../fixtures/control_acceptance.mjs';
const h=acceptanceFixture();
const call=(name,args)=>({tool_calls:[{id:Math.random().toString(),type:'function',function:{name,arguments:JSON.stringify(args)}}]});
try{
 const owner='@david:fixture',room='!ada-david:fixture';
 h.domain.accountNames[owner]='Jacob';Object.assign(h.domain.room(room),{title:'Daniel McCormmick (Candidate)',source:'gmessages'});
 const search=async(query,extra={})=>h.core.tool('search_conversations',{query,owner:'Jacob',source:'Google Messages',...extra});
 assert.equal((await search('Daniel McCormick'))[0].id,room);
 assert.equal((await search('Daniel McCormick'))[0].name_match,'approximate');
 assert.equal((await search('daniel mccormmick'))[0].name_match,'exact_or_tokens');
 assert.equal((await search('Daniel McCormmick',{owner:'Elliot'})).length,0);
 assert.equal((await search('Daniel McCormmick',{source:'iMessage'})).length,0);
 const events=h.events.get(room);events.unshift({...events[0],event_id:'$interview',origin_server_ts:h.now-86400000,content:{msgtype:'m.text',body:'My TML interview is Thursday at 10 am.','com.jkali.from_me':false}});
 const found=await h.core.tool('search_history',{room_id:room,query:'TML interview'});
 assert(found.messages.some(m=>m.id==='$interview'));assert.equal(found.search.matched_messages,1);assert.equal(found.search.scanned_messages,21);
 assert(!(await h.core.tool('history',{room_id:room,display:false})).messages.some(m=>m.id==='$interview'),'answer is older than latest 15');
 assert.equal((await h.core.tool('search_history',{room_id:room,query:'unmentioned-topic'})).messages.length,0);
 let step=0;h.core.model=async(messages,tools)=>{
  assert(!tools.some(t=>t.function.name==='finish_request'));
  return step++===0?call('search_history',{room_id:room,query:'TML interview'}):{content:'He said Thursday at 10 am.'};
 };
 await h.ask('Read my texts: when is his TML interview?','read');assert.match(h.sent.at(-1).text,/Thursday at 10 am/);
 assert.equal(h.core.db.prepare('SELECT count(*) AS n FROM drafts').get().n,0);assert.equal(h.writes.length,0);
 step=0;h.core.model=async(messages,tools)=>{
  step++;if(!tools.length){assert.match(messages.at(-1).content,/final reading round/);return {content:'He said Thursday at 10 am.'};}
  return call('history',{room_id:room,display:false});
 };
 await h.ask('When was the interview?','bounded');assert.equal(step,8);assert.match(h.sent.at(-1).text,/Thursday/);assert.equal(h.writes.length,0);
}finally{h.close();}
console.log('Reading: corrected names, account/source isolation, older topic evidence, no action checklist and bounded final answer passed.');
