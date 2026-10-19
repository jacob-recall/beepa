// Opt-in hosted-model qualification. Only fictional fixture data leaves the process.
// No production domain, Telegram adapter, native sender or canonical database is opened.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {isAbsolute} from 'node:path';
import {acceptanceFixture} from '../fixtures/control_acceptance.mjs';
if(process.argv[2]!=='--live-model'||!isAbsolute(process.argv[3]||''))throw Error('Requires --live-model /absolute/private/config.json');
const {model,openrouter_key:key}=JSON.parse(readFileSync(process.argv[3],'utf8'));
assert(typeof key==='string'&&key.length>20);assert(typeof model==='string'&&model.length<128);
let calls=0;const observed=[],completedCases=[];
const provider=async(messages,tools)=>{
 assert(++calls<=30,'Qualification request budget exhausted');
 const began=Date.now();console.log(JSON.stringify({provider_request:calls,started:true}));
 const response=await fetch('https://openrouter.ai/api/v1/chat/completions',{method:'POST',redirect:'error',signal:AbortSignal.timeout(60000),
  headers:{Authorization:'Bearer '+key,'Content-Type':'application/json'},body:JSON.stringify({model,messages,tools,tool_choice:'auto',max_tokens:1800,provider:{require_parameters:true,allow_fallbacks:false}})});
 assert(response.ok,'Provider request failed: HTTP '+response.status);
 const data=await response.json(),message=data.choices?.[0]?.message;assert(message,'Provider returned no message');
 console.log(JSON.stringify({provider_request:calls,elapsed_ms:Date.now()-began,finish_reason:data.choices?.[0]?.finish_reason,tokens:data.usage?.total_tokens,reasoning_tokens:data.usage?.completion_tokens_details?.reasoning_tokens}));
 observed.push(...(message.tool_calls||[]).map(c=>({name:c.function?.name,args:JSON.parse(c.function?.arguments||'{}')})));
 return message;
};
const latestDraft=h=>{const row=h.core.db.prepare('SELECT payload FROM drafts ORDER BY rowid DESC LIMIT 1').get();return row?JSON.parse(row.payload):null;};
async function run(name,check){if(process.argv[4]&&!name.includes(process.argv[4]))return;const h=acceptanceFixture(provider),before=calls;observed.length=0;try{await check(h);assert.equal(h.writes.length,0);completedCases.push(name);console.log(JSON.stringify({case:name,passed:true,provider_calls:calls-before,tools:observed.map(c=>c.name)}));}catch(error){console.log(JSON.stringify({case:name,passed:false,tools:observed,synthetic_replies:h.sent.map(p=>p.text)}));throw error;}finally{h.close();}}
await run('ambiguous account and short followup',async h=>{
 await h.ask("Show Ada's messages.",'ambiguity');assert.equal(latestDraft(h),null);assert(observed.some(c=>c.name==='search_conversations'));assert(!observed.some(c=>c.name==='history'),'Ambiguous account must be clarified');
 await h.ask("Elliot's account. Show the last 15.",'choice');assert(observed.some(c=>c.name==='history'&&c.args.room_id==='!ada-elliot:fixture'));
 assert(h.sent.at(-1).text.includes('Elliot’s account'));assert(h.sent.at(-1).text.includes('elliot exact message 19'));assert(!h.sent.at(-1).text.includes('david exact message'));
});
await run('exact Direct review without sending',async h=>{
 await h.ask('From David’s account, draft exactly “Tuesday at 2 works.” to Ada.','draft');const d=latestDraft(h);assert(d);assert.equal(d.kind,'send');assert.equal(d.owner,'@david:fixture');assert.equal(d.body,'Tuesday at 2 works.');assert.equal(d.content['com.jkali.send_mode'],'direct');
});
await run('combined send and reply notifications',async h=>{
 await h.ask('From David’s account, text Ada exactly “Tuesday at 2 works.” and notify me in Telegram and my browser when Ada replies.','combined');
 const drafts=h.core.db.prepare('SELECT payload,state FROM drafts ORDER BY rowid').all().map(r=>({...JSON.parse(r.payload),state:r.state}));
 assert.equal(drafts.length,2);assert(drafts.every(d=>d.state==='pending'));
 const send=drafts.find(d=>d.kind==='send'),notifications=drafts.find(d=>d.operations);
 assert(send&&notifications);assert.equal(send.owner,'@david:fixture');assert.equal(send.body,'Tuesday at 2 works.');assert.equal(notifications.room_id,send.room_id);
 assert(notifications.operations.some(o=>o.type==='list.watch'&&o.payload.enabled&&o.payload.all_messages===false));
 assert.equal(h.sent.filter(p=>p.reply_markup).length,2);assert(!h.state().lists['browser-followed-conversations']);
 assert(observed.some(c=>c.name==='finish_request'&&c.args.remaining.length===0));
});
await run('outreach list creation',async h=>{
 await h.ask('Create an outreach list called Recruiters.','outreach');const d=latestDraft(h);assert(d);assert.equal(d.operation.type,'list.create');assert.equal(d.operation.payload.purpose,'outreach');assert.equal(d.operation.payload.name,'Recruiters');assert(!Object.values(h.state().lists).some(l=>l.name==='Recruiters'));
});
await run('explicit latest-message deadline',async h=>{
 await h.ask('In Follow-ups, remind me tomorrow at 12:00 UTC to answer the latest message from Ada on David’s account.','deadline');const d=latestDraft(h);assert(d);assert.equal(d.operation.type,'list.obligation');assert.equal(d.operation.payload.room_id,'!ada-david:fixture');assert.equal(d.operation.payload.event_id,'$david-19');assert.equal(d.operation.payload.due_at,h.now+86400000);
});
await run('independent activity and reminder settings',async h=>{
 await h.ask('Turn off new-message alerts for Follow-ups, but keep its overdue reminders enabled.','alerts');const d=latestDraft(h);assert(d);assert.equal(d.operation.type,'list.monitoring');assert.equal(d.operation.payload.activity_enabled,false);assert.notEqual(d.operation.payload.reminders_enabled,false);assert.equal(h.domain.list('followups').monitoring.activity_enabled,undefined);
});
await run('cross-account waiting query',async h=>{
 await h.ask('Across Follow-ups, who is waiting on me and who am I waiting on? Include the sending account and app.','waiting');assert(observed.some(c=>c.name==='updates'));assert.equal(latestDraft(h),null);const text=h.sent.map(p=>p.text).join('\n').toLowerCase();assert(text.includes('david'));assert(text.includes('elliot'));assert(text.includes('imessage'));assert(text.includes('whatsapp'));assert(!/@[a-z0-9_-]+:fixture|![a-z0-9_-]+:fixture|\$[a-z0-9_-]+/.test(text),'User-facing answer must not expose protocol IDs');assert(!text.includes('ai-generated answer'));
});
assert(completedCases.length>0,'No qualification cases matched');if(!process.argv[4])assert.equal(completedCases.length,7);
console.log(JSON.stringify({model,passed:true,cases:completedCases,filter:process.argv[4]||null,provider_calls:calls,fixture:'synthetic only',real_messages_sent:0}));
