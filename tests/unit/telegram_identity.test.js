import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {ControlCore} from '../../master/control-core.mjs';

const bot=id=>({id,is_bot:true,username:'fixture_'+id});
const incoming=(id,text)=>({update_id:id,message:{message_id:id,chat:{id:7,type:'private'},from:{id:7},text}});
const dir=mkdtempSync(join(tmpdir(),'beepa-identity-')),path=join(dir,'state.db');
const sent=[];
const make=()=>new ControlCore({path,domain:{rooms:new Map()},telegram:async(_,p)=>{sent.push(p);return {message_id:sent.length};},model:async()=>{}});
let core=make();
try{
  // Legacy pairing is not silently assigned to an unproven bot identity.
  core.set('operator',{user:7,chat:7});core.enqueue('legacy','Old private text');
  core.bindTransport(bot(100));assert.equal(core.status().paired,false);
  assert.equal(core.db.prepare('SELECT state FROM transport_outcomes WHERE bot=?').get('legacy').state,'canceled');
  const pair=async(id)=>{const code=new URL(core.pairLink(bot(id).username).url).searchParams.get('start');core.ingest([incoming(id,'/start '+code)]);await core.process();await core.flush();assert(core.status().paired);};
  await pair(100);const count=sent.length;
  core.enqueue('held','Pending reply');core.set('api:native',{state:'processed'});
  core.db.prepare('INSERT INTO drafts VALUES(?,?,?,?,?)').run('submitted','{"id":"submitted","kind":"send"}','submitted','{"proposal_id":"$native"}',Date.now());
  core.db.prepare('INSERT INTO drafts VALUES(?,?,?,?,?)').run('uncertain','{"id":"uncertain","kind":"send"}','unknown',null,Date.now());
  core.db.prepare('INSERT INTO drafts VALUES(?,?,?,?,?)').run('review','{"id":"review","kind":"send"}','pending',null,Date.now());
  core.db.exec("INSERT INTO observed VALUES('source-event'); INSERT INTO cursors VALUES('room','cursor',1)");
  core.db.prepare('INSERT OR REPLACE INTO refs VALUES(?,?,?)').run('7',500,'{"draft":"review"}');
  const oldCode=new URL(core.pairLink('fixture_100').url).searchParams.get('start');
  const offset=core.value('offset');
  // A rotated token for the SAME provider identity preserves pairing and queues.
  core.db.close();core=make();assert.equal(core.bindTransport({...bot(100),username:'renamed_bot'}).changed,false);
  assert(core.status().paired);assert.equal(core.value('offset'),offset);assert.equal(core.status().pending,1);
  // A DIFFERENT bot atomically retires local transport state, never native state.
  core.bindTransport(bot(200));assert.equal(core.status().paired,false);assert.equal(core.value('pair'),null);assert.equal(core.value('offset'),0);
  assert.equal(core.ref(7,500),null);assert.equal(core.status().pending,0);await core.flush();assert.equal(sent.length,count);
  assert.equal(core.db.prepare('SELECT state FROM drafts WHERE id=?').get('review').state,'canceled');
  for(const [id,state] of [['submitted','submitted'],['uncertain','unknown']])assert.equal(core.db.prepare('SELECT state FROM drafts WHERE id=?').get(id).state,state);
  assert.equal(core.value('api:native').state,'processed');assert.equal(core.db.prepare('SELECT count(*) AS n FROM observed').get().n,1);assert.equal(core.db.prepare('SELECT cursor FROM cursors').get().cursor,'cursor');
  core.ingest([incoming(100,'/start '+oldCode)]);await core.process();assert.equal(core.status().paired,false);
  // Same numeric update/message IDs in another bot's scope cannot bind old refs.
  await pair(200);assert.equal(core.ref(7,500),null);
  core.bindTransport(bot(100));assert.equal(core.value('offset'),offset);assert.equal(core.status().paired,false);
  await pair(101);const before=core.db.prepare('SELECT count(*) AS n FROM inbox').get().n;
  core.ingest([incoming(100,'/help'),{update_id:'bad'}]);assert.equal(core.db.prepare('SELECT count(*) AS n FROM inbox').get().n,before);
  core.enqueue('held','Do not resurrect canceled old delivery');assert.equal(core.status().pending,0);
  assert.equal(core.db.prepare('SELECT state FROM transport_outcomes WHERE bot=? AND id=?').get('100','held:0').state,'canceled');
  for(const invalid of [{id:0,is_bot:true,username:'a'},{id:123,is_bot:false,username:'a'},{id:123,is_bot:true,username:'bad/name'}])assert.throws(()=>core.bindTransport(invalid),/identity/);
  assert.equal(core.value('transport_identity').id,'100');
  // An interrupted migration is all-or-nothing, including the archives.
  core.enqueue('rollback-check','Keep until a successful rotation');
  core.db.exec("CREATE TRIGGER reject_rotation BEFORE DELETE ON outbox BEGIN SELECT RAISE(ABORT,'fixture failure'); END");
  assert.throws(()=>core.bindTransport(bot(300)),/fixture failure/);
  assert.equal(core.value('transport_identity').id,'100');assert(core.status().paired);assert.equal(core.status().pending,1);
  assert.equal(core.db.prepare('SELECT count(*) AS n FROM transport_outcomes WHERE bot=? AND id=?').get('100','rollback-check:0').n,0);
  core.db.exec('DROP TRIGGER reject_rotation');
  core.db.close();core=make();assert.equal(core.bindTransport(bot(100)).changed,false);
  assert.equal(core.status().pending,1);assert(core.status().retired_delivery_outcomes.some(x=>x.bot==='100'&&x.state==='canceled'));
}finally{core.db.close();rmSync(dir,{recursive:true,force:true});}
console.log('Telegram identity: same-bot rotation, changed-bot isolation, old-code/ref rejection, restart and native ledger preservation passed.');
