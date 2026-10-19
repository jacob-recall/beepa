// Actual user modules, intercepted synthetic Matrix only; never real messages.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../..');
(async () => {
  const browser = await chromium.launch({ executablePath:'/usr/bin/chromium', headless:true, args:['--no-sandbox'] });
  try {
    const page = await browser.newPage({viewport:{width:1100,height:850}}), errors=[], unexpected=[], sent=[];
    page.on('pageerror', e=>errors.push(e.message));
    const owner='@self:localhost', room='!chat:localhost', proposals='!props:localhost', decisions=[];
    const events=[1,2,3].map(n=>({type:'com.jkali.proposal',event_id:'$p'+n,room_id:proposals,sender:owner,origin_server_ts:Date.now()-10000+n,
      content:{target_room:room,body:'Suggestion '+n,'com.jkali.send_mode':'suggest'}}));
    events.push({type:'com.jkali.proposal',event_id:'$direct',room_id:proposals,sender:owner,origin_server_ts:Date.now(),content:{target_room:room,body:'Direct send failed','com.jkali.send_mode':'direct','com.jkali.direct_refused':true}});
    const html=`<!doctype html><html><head><link rel="stylesheet" href="/shared/style/beepa.css"><link rel="stylesheet" href="/apps/user/style.css"></head><body><main style="max-width:800px;margin:40px auto"><h2>Conversation with Ada</h2><div id="convo-messages"></div><textarea id="convo-input"></textarea><div id="convo-status"></div><div id="convo-compose"></div></main></body></html>`;
    await page.route('**/*', async route=>{
      const req=route.request(),url=new URL(req.url()), p=decodeURIComponent(url.pathname);
      const reply=data=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
      if(url.origin!=='http://127.0.0.1:9876'){unexpected.push(req.method()+' '+url.origin);return route.abort();}
      if(p==='/_matrix/client/versions')return reply({versions:['v1.11']});
      if(p==='/fixture')return route.fulfill({status:200,contentType:'text/html',body:html});
      if(p==='/_matrix/client/v3/sync')return reply({rooms:{join:{[proposals]:{state:{events:[{type:'com.jkali.proposals',state_key:'',content:{}}]}}}}});
      if(p===`/_matrix/client/v3/rooms/${proposals}/state/com.jkali.proposals/`)return reply({});
      if(p===`/_matrix/client/v3/rooms/${proposals}/messages`)return reply({chunk:events});
      if(p===`/_matrix/client/v3/rooms/${proposals}/state`)return reply(decisions);
      if(p.startsWith(`/_matrix/client/v3/rooms/${proposals}/event/`))return reply(events.find(e=>p.endsWith('/'+e.event_id)));
      if(req.method()==='PUT'&&p.startsWith(`/_matrix/client/v3/rooms/${proposals}/state/com.jkali.proposal_decision/`)){
        const c=req.postDataJSON(),id=c.proposal_event_id,i=decisions.findIndex(e=>e.type==='com.jkali.proposal_decision'&&e.state_key===id);
        const e={type:'com.jkali.proposal_decision',state_key:id,sender:owner,content:c}; if(i<0)decisions.push(e);else decisions[i]=e;
        const eventId='$decision'+(decisions.length+sent.length);
        if(!decisions.some(e=>e.type==='com.jkali.proposal_claim'&&e.state_key===id))decisions.push({type:'com.jkali.proposal_claim',state_key:id,sender:owner,content:{...c,decision_event_id:eventId}});
        return reply({event_id:eventId});
      }
      if(req.method()==='PUT'&&p.startsWith(`/_matrix/client/v3/rooms/${room}/send/m.room.message/`)){
        sent.push(req.postDataJSON());return reply({event_id:'$sent'+sent.length});
      }
      if(p.startsWith('/_matrix')){unexpected.push(req.method()+' '+p);return route.abort();}
      const file=path.resolve(root,'.'+p);
      if(!file.startsWith(root+'/')||!fs.existsSync(file))return route.abort();
      return route.fulfill({path:file,contentType:p.endsWith('.js')?'text/javascript':p.endsWith('.css')?'text/css':'application/octet-stream'});
    });
    async function boot(){
      await page.goto('http://127.0.0.1:9876/fixture');
      await page.evaluate(async()=>{
        const {S,feedModel}=await import('/shared/state.js');const {configureMatrixBase}=await import('/shared/matrix/client.js');
        configureMatrixBase({csBase:location.origin,serverName:'localhost'});
        S.token='synthetic';S.userId='@self:localhost';S.openRoomId='!chat:localhost';S.joinedSet=new Set([S.openRoomId]);
        feedModel.set(S.openRoomId,{id:S.openRoomId,name:'Ada',sourceId:'imessage',lastTs:Date.now(),drafts:[]});
        const {initProposalsUI}=await import('/apps/user/proposals.js');initProposalsUI();
      });
    }
    await boot();
    await page.getByText('Queued suggestions · 3',{exact:true}).waitFor();
    assert.equal(await page.locator('.proposal-queue').getAttribute('open'),null,'collapsed dropdown');
    await page.locator('.proposal-queue > summary').click();
    assert.equal(await page.locator('.proposal-queue .ghost').count(),3);
    await page.getByRole('button',{name:'Accept & send',exact:true}).first().click();
    await page.getByText('Queued suggestions · 2',{exact:true}).waitFor();
    await page.getByRole('button',{name:'Decline',exact:true}).first().click();
    await page.getByText('Queued suggestions · 1',{exact:true}).waitFor();
    await page.locator('.proposal-history > summary').click();
    await page.getByText('Accepted · sent',{exact:true}).waitFor();
    await page.getByText('Declined',{exact:true}).waitFor();
    await page.getByText('Not sent',{exact:true}).waitFor();
    assert.equal(sent.length,1);assert.equal(sent[0]['com.jkali.from_proposal'],'$p1');
    await boot();
    await page.getByText('Queued suggestions · 1',{exact:true}).waitFor();
    await page.locator('.proposal-history > summary').click();
    await page.getByText('Accepted · sent',{exact:true}).waitFor();await page.getByText('Declined',{exact:true}).waitFor();
    assert.equal(sent.length,1,'reload never sends again');
    assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);
    await page.screenshot({path:process.env.PROPOSAL_SCREENSHOT||'/tmp/beepa-suggestion-queue.png',fullPage:true});
    console.log('PASS actual user queue dropdown: three independent suggestions, accept, decline, persistent history, Direct refusal excluded, reload, no unexpected requests');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
