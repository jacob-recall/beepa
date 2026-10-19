import assert from 'node:assert/strict';
import {accountHealth} from '../../master/control-domain.mjs';
import {acceptanceFixture} from '../fixtures/control_acceptance.mjs';

const now=Date.parse('2026-10-18T12:00:00Z');
const healthy={connected:true,updated_at:now/1000,last_ingestion:now/1000,pending_events:0,receipts_pending:0,stage_errors:[],last_delivery:(now-86400000)/1000,
 imessage:{updated_at:now/1000,poll_ok:true,inbound_pending:0}};
const status=(changes={})=>accountHealth({...healthy,...changes},now);
assert.equal(status().master.status,'ok','idle last delivery is healthy');
assert.equal(status().imessage.status,'ok');
for(const [changes,expected] of [
 [{updated_at:(now-420000)/1000},'stale'],[{connected:false},'disconnected'],
 [{last_ingestion:(now-420000)/1000},'stalled'],[{pending_events:3},'catching_up'],
 [{receipts_pending:2},'catching_up'],[{media_retry:1},'catching_up'],[{stage_errors:['tail']},'error'],
 [{updated_at:now+31000},'unknown'],[{updated_at:'123'},'unknown'],[{pending_events:-1},'unknown'],
 [{last_ingestion:null},'unknown'],[{connected:'true'},'unknown']])assert.equal(status(changes).master.status,expected);
assert.equal(accountHealth(undefined,now).master.status,'unknown');
assert.equal(status({imessage:undefined}).imessage.status,'unknown');
assert.equal(status({imessage:{updated_at:now-480000,poll_ok:true,inbound_pending:0}}).imessage.status,'stale');
assert.equal(status({imessage:{updated_at:now,poll_ok:false}}).imessage.status,'error');
assert.equal(status({imessage:{updated_at:now,poll_ok:true,inbound_pending:1}}).imessage.status,'catching_up');
assert.equal(status({history_incomplete:100,delivery_incomplete:7}).master.status,'ok','old incomplete history does not pretend current ingestion stalled');

const h=acceptanceFixture();try{
 h.domain.roster=async()=>({'@david:fixture':'!d:fixture','@elliot:fixture':'!e:fixture','@offline:fixture':'!o:fixture'});
 h.domain.accountNames={'@david:fixture':'David','@elliot:fixture':'Elliot','@offline:fixture':'Offline'};
 h.domain.health.set('@david:fixture',healthy);
 await h.ask('/ping David','ping-one');assert.equal(h.sent.length,1);
 assert.match(h.sent[0].text,/David\nMaster link: Connected/);assert.match(h.sent[0].text,/iMessage: Reading Messages/);assert.match(h.sent[0].text,/1d ago/);
 assert(!/[@!$]|Off ago|\d{4}-\d\d-\d\dT/.test(h.sent[0].text));
 await h.ask('/ping@fixture_bot all','ping-all');assert.match(h.sent.at(-1).text,/Offline\nMaster link: Status unavailable/);
 assert.equal((await h.core.tool('account_health',{account:'Elliot'})).accounts.length,1);
 await assert.rejects(h.core.ping('missing'),/Account not found/);
 h.domain.accountNames['@offline:fixture']='Elliot';await assert.rejects(h.core.ping('Elliot'),/More than one account/);
 h.apply(h.domain.operation('control.settings','control',0,{...h.domain.settings(),allowed_accounts:['@david:fixture'],allow_model_context:false}));
 assert.equal((await h.core.ping()).accounts.length,1);
 await assert.rejects(h.core.ping('Elliot'),/Account not found/);
 await h.ask('/ping','no-model');assert.match(h.sent.at(-1).text,/David/);assert(!h.sent.at(-1).text.includes('Elliot'));
 assert.equal(h.writes.length,0,'pings cannot send a native message');
}finally{h.close();}
console.log('Ping: idle/stale/stalled/backlog/native health, malformed evidence, names, ambiguity, scope and no model/native sends passed.');
