// Actual master entry point and DOM; synthetic, fully intercepted Matrix only.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../..');
(async () => {
  const browser = await chromium.launch({executablePath:process.env.CHROMIUM_PATH || '/usr/bin/chromium',headless:true,args:['--no-sandbox']});
  try {
    const page = await browser.newPage(), errors=[], unknown=[], commands=[];
    page.setDefaultTimeout(10000);
    const owner='@elliot:master', manager='@manager:master', room='!chat:master', proposals='!proposals:master';
    const state=(type,content,state_key='')=>({type,content,state_key,sender:owner});
    const native={schema:1,supported:true,unread_count:3,marked_unread:false,revision:'a'.repeat(64),latest_ts:3000,observed_ts:4000};
    const read=()=>state('com.jkali.read_state',{unread_count:native.unread_count,native:{...native}});
    const messages=[1,2,3].map(n=>({type:'m.room.message',event_id:'$message'+n,sender:owner,origin_server_ts:n*1000,
      content:{msgtype:'m.text',body:'Synthetic message '+n,'com.jkali.origin_ts':n*1000}}));
    const rooms={
      '!space:master':{state:{events:[state('m.room.create',{type:'m.space'}),state('m.room.name',{name:'space:elliot'}),
        ...[room,proposals].map(id=>state('m.space.child',{via:['master']},id))]}},
      [proposals]:{state:{events:[state('m.room.create',{}),state('com.jkali.proposals',{}),state('com.jkali.crm.capability',{
        schema:1,owner_mxid:owner,generation:'generation',journal_epoch:'journal',ordered_policy:true,direct_prepare_commit:true,native_read:true})]},timeline:{events:[]}},
      [room]:{state:{events:[state('m.room.create',{'com.jkali.mirror_of':'!native:local'}),state('m.room.name',{name:'Unread fixture'}),
        state('com.jkali.source',{source:'imessage'}),state('com.jkali.share_level',{level:'share'}),read()]},timeline:{events:messages}},
    };
    let tail=null, sync=0;
    page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*',async route=>{
      const req=route.request(),url=new URL(req.url()),name=decodeURIComponent(url.pathname);
      const reply=data=>route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
      if(url.origin!=='http://127.0.0.1:9876'){unknown.push('foreign origin');return route.abort();}
      if(name==='/_matrix/client/v3/login')return reply({access_token:'synthetic',user_id:manager});
      if(name==='/admin/notifications')return reply({follows:[],history:[],devices:0});
      if(name==='/_matrix/client/v3/sync'){
        const filter=JSON.parse(url.searchParams.get('filter') || '{}');
        if(filter.room?.rooms?.length===1 && filter.room.rooms[0]===room){tail=route;return;}
        return reply({next_batch:'global-'+(++sync),rooms:{join:rooms}});
      }
      if(name===`/_matrix/client/v3/rooms/${room}/messages`)return reply({chunk:messages});
      if(name===`/_matrix/client/v3/rooms/${proposals}/state`)return reply(rooms[proposals].state.events);
      if(name===`/_matrix/client/v3/rooms/${proposals}/messages`)return reply({chunk:[]});
      if(name.startsWith(`/_matrix/client/v3/rooms/${proposals}/send/com.jkali.read_request/`) && req.method()==='PUT'){
        commands.push(req.postDataJSON());return reply({event_id:'$command'+commands.length});
      }
      if(name==='/apps/master/config.js')return route.fulfill({contentType:'text/javascript',body:''});
      if(req.method()==='GET' && /^(\/apps\/master\/|\/shared\/)/.test(name) && /\.(html|js|css|svg|png)$/.test(name) && !name.includes('..')){
        const file=path.join(root,name);if(fs.existsSync(file))return route.fulfill({path:file});
      }
      unknown.push(req.method()+' '+name);return route.abort();
    });
    const until=async predicate=>{for(let i=0;i<100;i++){if(predicate())return;await new Promise(r=>setTimeout(r,50));}throw Error('Expected browser action did not occur: '+errors.join(';'));};
    async function confirm(action,count){
      await until(()=>!!tail);
      const command=commands.at(-1)['com.jkali.read_action'];assert.equal(command.action,action);
      native.unread_count=count;native.marked_unread=count>0;native.revision=(count?'b':'c').repeat(64);
      native.observed_ts++;native.last_command={id:command.command_id,status:'applied'};
      rooms[room].state.events=rooms[room].state.events.filter(e=>e.type!=='com.jkali.read_state').concat(read());
      const route=tail;tail=null;
      // State-only response: sidebar must update without the 20-second refresh.
      await route.fulfill({contentType:'application/json',body:JSON.stringify({next_batch:'tail-'+native.observed_ts,rooms:{join:{[room]:{state:{events:[read()]}}}}})});
    }
    await page.goto('http://127.0.0.1:9876/apps/master/index.html');
    await page.locator('#in-user').fill('manager');await page.locator('#in-pass').fill('synthetic-password');await page.locator('#btn-signin').click();
    const row=page.locator('.convo').filter({has:page.locator('.title').filter({hasText:'Unread fixture'})});
    await row.waitFor();assert.equal(await row.locator('.pill.unread').textContent(),'3');
    await row.click();await page.getByText('Synthetic message 3',{exact:true}).waitFor();await until(()=>commands.length===1);
    assert.equal(commands[0].target_room,'!native:local');assert.equal(commands[0]['com.jkali.read_action'].generation,'generation');
    assert.equal(await row.locator('.pill.unread').textContent(),'3','HTTP acceptance alone must not clear native unread');
    await confirm('read',0);await row.locator('.pill.unread').waitFor({state:'detached',timeout:3000});
    await page.locator('#room-mark-unread').click();await until(()=>commands.length===2);await confirm('unread',1);
    await row.locator('.pill.unread').waitFor({timeout:3000});assert.equal(await row.locator('.pill.unread').textContent(),'1');
    await until(()=>!!tail);assert.equal(commands.length,2,'manual unread must survive the open viewer');
    await page.locator('#room-mark-read').click();await until(()=>commands.length===3);await confirm('read',0);
    await row.locator('.pill.unread').waitFor({state:'detached',timeout:3000});
    assert.deepEqual(errors,[]);assert.deepEqual(unknown,[]);
    console.log('PASS actual master Chromium UI: multi-message open requests native read, confirmed state repaints badge immediately, unread hold and explicit read work, no unexpected writes or JS errors');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
