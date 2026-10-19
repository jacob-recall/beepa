import assert from 'node:assert/strict';
import { ControlCore } from '../../master/control-core.mjs';

const incoming=(id,extra={})=>({update_id:id,message:{message_id:id,chat:{id:7,type:'private'},from:{id:7},text:'/help',...extra}});
function fixture(){
  const calls=[],commands=[];
  const core=new ControlCore({path:':memory:',domain:{},telegram:async(method,data)=>{calls.push({method,data});return true;},model:async()=>({content:'Unused'})});
  core.set('operator',{user:7,chat:7});
  core.command=async text=>{commands.push(text);};
  return {core,calls,commands};
}
{
  const {core,calls,commands}=fixture();let finish;
  core.command=async text=>{assert.equal(calls[0].method,'setMessageReaction');commands.push(text);await new Promise(resolve=>{finish=resolve;});};
  core.ingest([incoming(1)]);const processing=core.process();
  for(let i=0;i<5;i++)await Promise.resolve();
  assert.equal(core.db.prepare('SELECT state FROM inbox WHERE id=1').get().state,'processing');
  assert.deepEqual(calls,[{method:'setMessageReaction',data:{chat_id:7,message_id:1,reaction:[{type:'emoji',emoji:'👀'}],is_big:false}}]);
  finish();await processing;core.ingest([incoming(1)]);await core.process();
  assert.equal(calls.length,1);assert.equal(commands.length,1);core.db.close();
}
{
  const {core,calls,commands}=fixture();
  core.ingest([incoming(1,{from:{id:8}}),incoming(2,{chat:{id:8,type:'private'}}),incoming(3,{chat:{id:7,type:'group'}}),incoming(4,{from:{id:7,is_bot:true}}),incoming(5,{forward_origin:{type:'user'}}),incoming(6,{text:undefined,photo:[]})]);
  await core.process();assert.equal(calls.length,0);assert.equal(commands.length,0);
  core.ingest([{update_id:7,callback_query:{id:'button',from:{id:7},data:'noop',message:{message_id:90,chat:{id:7,type:'private'}}}}]);
  await core.process();assert.deepEqual(calls.map(c=>c.method),['answerCallbackQuery']);core.db.close();
}
{
  const {core,commands}=fixture();core.telegram=async()=>{throw Error('Reaction rejected or timed out');};
  core.ingest([incoming(1)]);await core.process();assert.equal(commands.length,1);
  assert.equal(core.db.prepare('SELECT state FROM inbox WHERE id=1').get().state,'done');core.db.close();
}
{
  const {core,commands}=fixture();core.telegram=async()=>{core.disconnect();};
  core.ingest([incoming(1)]);await core.process();assert.equal(commands.length,0,'disconnect during ack invalidates command');core.db.close();
}
console.log('Telegram reactions: before command work, exact operator message, durable dedup, authorization, nonfatal errors, callback exclusion and binding change passed.');
