import assert from 'node:assert/strict';
import {acceptanceFixture} from '../fixtures/control_acceptance.mjs';
const h=acceptanceFixture();try{
 h.core.historyTimezone='America/Los_Angeles';h.domain.accountNames={'@david:fixture':'David','@elliot:fixture':'Elliot'};
 const room='!ada-david:fixture',r=h.domain.rooms.get(room),stamp=Date.parse('2026-10-17T19:53:00Z');
 const raw='Conversation state — @david account, imessage, Ada (!ada-david:fixture)\n- $david-19 — Waiting for a reply.\nLatest activity: 2026-10-17 19:53 UTC.\n$100 total.';
 const clean=h.core.displayText(raw);
 assert(!clean.includes('@david'));assert(!clean.includes('!ada'));assert(!clean.includes('$david'));assert(clean.includes('David'));assert(clean.includes('iMessage'));assert(clean.includes('Oct 17, 12:53 PM'));assert(clean.includes('$100 total.'));
 const shown=h.core.modelView({owner:r.owner,source:r.source,ts:stamp,due_at:stamp});assert.equal(shown.account_display_name,'David');assert.equal(shown.app_display_name,'iMessage');assert.equal(shown.local_time,'Oct 17, 12:53 PM');assert.equal(shown.ts,stamp);
 h.core.model=async messages=>{assert(messages[0].content.includes('never show account handles'));assert(!messages[0].content.includes('cite exact returned message IDs'));return {content:raw};};
 await h.ask('What is the conversation state?','summary');assert.equal(h.sent.at(-1).text,clean);
 const rules=[{...r,list_name:'Follow-ups',preview:true}],due=Date.parse('2026-10-19T12:00:00Z');
 for(const text of [h.core.activityAlert(rules,{outgoing:false}),h.core.contactAlert(rules,{due}),h.core.reminderText(rules,due,'waiting_for_them')]){assert(text.includes('David’s account'));assert(!/[@!$]|2026-10-19T|imessage/.test(text));}
 const draft=await h.core.tool('prepare_obligation',{list_id:'followups',room_id:room,event_id:'$david-19',due_at:due});
 const reminder=h.core.review(draft);assert(reminder.includes('Deadline: Oct 19, 5:00 AM'));assert(reminder.includes('david exact message 19'));assert(!reminder.includes('$david-19'));assert(!reminder.includes(draft.id));assert(!reminder.includes('"room_id"'));
 const policy=await h.core.tool('prepare_list_change',{operation:'monitoring',list_id:'followups',enabled:true,channels:['telegram','browser'],activity_enabled:true,reminders_enabled:true});const review=h.core.review(policy);assert(review.includes('Notify via: Telegram and Browser'));assert(!review.includes('"enabled"'));
 const body='Exact literal @david:fixture $identifier **stars** 2026-10-17T19:53:00Z';
 const send=await h.core.tool('prepare_send',{room_id:room,body});h.core.enqueue('send-review',h.core.review(send),{draft:send.id,room,review:true});await h.core.flush();
 assert(h.sent.at(-1).text.includes(body));assert(!h.sent.at(-1).text.includes('Draft '+send.id));assert.equal(h.core.ref(1,h.sent.length).draft,send.id);
 const source=await h.domain.history(room);source.messages=[{id:'$literal',ts:stamp,from_me:true,text:body}];h.core.enqueueHistory('literal-history',source);await h.core.flush();assert(h.sent.at(-1).text.endsWith(body));
 await h.ask('/drafts','draft-list');const card=h.sent.find(p=>p.reply_markup?.inline_keyboard?.flat().some(b=>b.callback_data==='status:'+send.id));assert(card);assert(!card.text.includes(send.id));
 const message=h.sent.indexOf(card)+1;assert.equal(h.core.ref(1,message).draft,send.id);
 h.core.ingest([{update_id:1,callback_query:{id:'status-tap',from:{id:1},message:{message_id:message,chat:{id:1,type:'private'}},data:'status:'+send.id}}]);
 // Fixture transport normally only accepts sendMessage; accept the callback acknowledgment.
 const transport=h.core.telegram;h.core.telegram=async(method,payload)=>method==='answerCallbackQuery'?true:transport(method,payload);
 await h.core.process();await h.core.flush();assert(h.sent.at(-1).text.includes('Waiting for your confirmation'));assert(!h.sent.at(-1).text.includes(send.id));assert.equal(h.writes.length,0);
 console.log('Presentation: summary ID filter/local time, model labels, alerts, reminders, readable policies, literal bodies, hidden exact draft routing and status buttons passed.');
}finally{h.close();}
