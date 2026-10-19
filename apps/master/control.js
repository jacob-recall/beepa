import { el } from '../../shared/ui/el.js';

function alertTiming(host,policy,locked){
  const check=(label,value)=>{const wrap=el('label'),input=el('input');input.type='checkbox';input.checked=value;input.disabled=locked;wrap.append(input,el('span','',label));host.append(wrap);return input;};
  const field=(label,type,value)=>{const wrap=el('label','',label),input=el('input','input');input.type=type;input.value=value;input.disabled=locked;wrap.append(input);host.append(wrap);return input;};
  const activity=check('New-message alerts',policy.activity_enabled!==false),reminders=check('Overdue reminders',policy.reminders_enabled!==false);
  const presentationLabel=el('label','','Message updates'),presentation=el('select','input');presentation.setAttribute('aria-label','Message updates');presentation.disabled=locked;
  for(const [value,label] of [['immediate','As they arrive'],['grouped','Group by conversation'],['digest','Digest across the list']]){const option=el('option','',label);option.value=value;presentation.append(option);}presentation.value=policy.presentation?.mode||'immediate';presentationLabel.append(presentation);host.append(presentationLabel);
  const collection=field('Collection window (minutes)','number',String((policy.presentation?.interval_ms||120000)/60000));collection.min='1';collection.step='1';
  const updateCollection=()=>{collection.parentElement.hidden=presentation.value==='immediate';collection.disabled=locked||presentation.value==='immediate';collection.max=presentation.value==='grouped'?'60':'1440';};presentation.addEventListener('change',updateCollection);updateCollection();
  const repeat=field('Repeat overdue reminders (hours)','number',policy.repeat_ms?String(policy.repeat_ms/3600000):'');repeat.min='0.017';repeat.max='8760';repeat.step='any';repeat.placeholder='Once per deadline';
  const wrap=el('label','','Delivery hours'),mode=el('select','input');mode.disabled=locked;mode.setAttribute('aria-label','Delivery hours');
  for(const [value,label] of [['none','Any time'],['quiet','Pause during these hours'],['working','Only during these hours']]){const o=el('option','',label);o.value=value;mode.append(o);}mode.value=policy.schedule?.mode||'none';wrap.append(mode);host.append(wrap);
  const hours=el('fieldset','control-options control-hours');host.append(hours);
  const zoneLabel=el('label','','Timezone'),zone=el('input','input');zone.value=policy.schedule?.timezone||Intl.DateTimeFormat().resolvedOptions().timeZone;zone.placeholder='America/Los_Angeles';zoneLabel.append(zone);hours.append(zoneLabel);
  const days=el('div','control-weekdays'),choices=[];for(const [i,label] of ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].entries()){
    const w=el('label'),c=el('input');c.type='checkbox';c.checked=(policy.schedule?.days||[1,2,3,4,5,6,7]).includes(i+1);w.append(c,el('span','',label));days.append(w);choices.push(c);
  }hours.append(days);
  const clock=n=>String(Math.floor(n/60)).padStart(2,'0')+':'+String(n%60).padStart(2,'0');
  const time=(label,value)=>{const w=el('label','',label),input=el('input','input');input.value=value;input.placeholder='HH:MM';input.pattern='([01][0-9]|2[0-3]):[0-5][0-9]|24:00';w.append(input);hours.append(w);return input;};
  const start=time('Start (24-hour time)',clock(policy.schedule?.start_minute??1320)),end=time('End (24:00 is midnight)',clock(policy.schedule?.end_minute??480));
  const toggle=()=>{hours.hidden=mode.value==='none';hours.disabled=locked||mode.value==='none';};mode.addEventListener('change',toggle);toggle();
  hours.append(el('p','muted','Days refer to when the window starts. An overnight window continues into the next day. Daylight-saving changes follow this timezone.'));
  return ()=>{
    const minutes=input=>{if(!/^(?:[01]\d|2[0-3]):[0-5]\d$|^24:00$/.test(input.value))throw Error('Use HH:MM for delivery hours');const [h,m]=input.value.split(':').map(Number);return h*60+m;};
    const schedule=mode.value==='none'?null:{timezone:zone.value.trim(),mode:mode.value,days:choices.flatMap((c,i)=>c.checked?[i+1]:[]),start_minute:minutes(start),end_minute:minutes(end)};
    const interval=Math.round(Number(collection.value)*60000),limit=presentation.value==='grouped'?60:1440;
    if(presentation.value!=='immediate'&&(!collection.value||!Number.isSafeInteger(interval)||interval<60000||interval>limit*60000))throw Error('Choose a collection window between 1 and '+limit+' minutes.');
    if(schedule){if(!schedule.days.length)throw Error('Choose at least one day for delivery hours.');if(schedule.start_minute>=1440||schedule.start_minute===schedule.end_minute)throw Error('Choose different start and end times; the start must be before 24:00.');try{new Intl.DateTimeFormat('en-US',{timeZone:schedule.timezone});}catch{throw Error('Choose a valid IANA timezone, such as America/Los_Angeles.');}}
    return {activity_enabled:activity.checked,reminders_enabled:reminders.checked,presentation:presentation.value==='immediate'?null:{mode:presentation.value,interval_ms:interval},repeat_ms:repeat.value===''?null:Math.round(Number(repeat.value)*3600000),schedule};
  };
}

// Same manager session as the console. Never persists a token or bot credential.
export function createControl({base, token, current, openRoom}) {
  const commandSession=crypto.randomUUID();let pendingSubmission=null;
  async function request(data) {
    if (!current()) throw new Error('Session changed');
    const response = await fetch(base + '/admin/control', {method:'POST', headers:{
      'Content-Type':'application/json', Authorization:'Bearer '+token}, body:JSON.stringify(data)});
    const value = await response.json();
    if (!current()) throw new Error('Session changed');
    if (!response.ok) throw new Error(value.error || 'Beepa control unavailable');
    return value;
  }
  const button = (label, fn, status, disabled=false) => {
    const b=el('button','btn',label);b.type='button';b.disabled=disabled;
    b.addEventListener('click',async()=>{if(!current())return;b.disabled=true;try{await fn();}catch(e){if(current())status.textContent=e.message;}finally{b.disabled=disabled;}});return b;
  };
  function commandQueue(host,initial,dialog,status){
    const section=el('section','control-command-queue'),form=el('form','control-options'),input=el('textarea','input'),jobsHost=el('div','control-command-jobs');
    input.rows=2;input.maxLength=8000;input.placeholder='Show my lists, or what needs a reply?';input.setAttribute('aria-label','Ask Beepa');input.value=pendingSubmission?.text||'';
    const submit=el('button','btn','Send request');submit.type='submit';
    form.append(input,submit);section.append(el('h3','','Ask Beepa'),el('p','muted','Replies arrive in Telegram. Sending a message still requires your exact review and confirmation.'),form,el('h3','','Recent requests'),jobsHost);host.append(section);
    let jobs=initial||[],timer=null;
    const active=()=>current()&&dialog.open&&section.isConnected;
    const label=job=>{
      if(job.state!=='processed')return ({queued:'Queued',processing:'Working',failed:'Failed',unknown:'Processing interrupted — check draft status',canceled:'Canceled',expired:'Expired'})[job.state]||job.state;
      const states=new Set((job.delivery||[]).filter(x=>x.count).map(x=>x.state));
      return states.has('unknown')?'Reply delivery uncertain':states.has('failed')?'Reply delivery failed':states.has('canceled')||states.has('expired')?'Reply no longer current':states.has('pending')||states.has('sending')?'Reply waiting for delivery':states.has('accepted')?'Reply accepted by Telegram':'Processed';
    };
    const schedule=()=>{
      clearTimeout(timer);
      if(active()&&jobs.some(j=>['queued','processing'].includes(j.state)||(j.delivery||[]).some(x=>['pending','sending'].includes(x.state))))timer=setTimeout(refresh,1000);
    };
    const render=()=>{
      if(!active())return;jobsHost.replaceChildren();
      if(!jobs.length)jobsHost.append(el('p','muted','No requests yet.'));
      for(const job of jobs){
        const row=el('div','control-command-job'),text=el('span','',job.request_id.slice(0,8)+' · '+label(job));text.title=job.request_id;row.append(text);
        if(job.state==='queued')row.append(button('Cancel request',async()=>{const result=await request({action:'command_cancel',request_id:job.request_id});jobs=jobs.map(j=>j.request_id===result.request_id?result:j);render();schedule();},status));
        if(job.error)row.append(el('p','muted',job.error));jobsHost.append(row);
      }
    };
    async function refresh(){
      if(!active())return;
      try{const value=await request({action:'status'});if(!active())return;jobs=value.commands||[];render();schedule();}
      catch(error){if(active()){status.textContent='Request status unavailable. Your saved request will not be resubmitted.';timer=setTimeout(refresh,3000);}}
    }
    form.addEventListener('submit',async event=>{
      event.preventDefault();if(!active()||submit.disabled||!input.value.trim())return;
      if(!pendingSubmission||pendingSubmission.text!==input.value)pendingSubmission={request_id:crypto.randomUUID(),text:input.value,session_id:commandSession};
      submit.disabled=true;input.disabled=true;
      try{
        const job=await request({action:'command',...pendingSubmission});pendingSubmission=null;
        if(!active())return;input.value='';jobs=[job,...jobs.filter(j=>j.request_id!==job.request_id)].slice(0,10);status.textContent='Request saved. You can close this window.';render();schedule();
      }catch(error){if(active())status.textContent=error.message+' Retry the same text to check the same request.';}
      finally{submit.disabled=false;input.disabled=false;}
    });
    dialog.addEventListener('close',()=>clearTimeout(timer),{once:true});render();schedule();
  }
  async function open() {
    const dialog=el('dialog','control-dialog'), status=el('p','','Connecting…'), body=el('div');
    dialog.append(el('h2','','Telegram'),status,body,button('Close',()=>dialog.close(),status));
    document.body.append(dialog);dialog.addEventListener('close',()=>dialog.remove());dialog.showModal();
    try {
      const state=await request({action:'status'});if(!current()||!dialog.open)return;
      status.textContent=state.transport_ready===false?(state.paired?'Your Telegram account is paired. ':'')+'Telegram is reconnecting; browser controls remain available.':state.paired?'Your Telegram account is connected.':'Connect your Telegram account to Beepa.';
      body.append(el('p','muted','Alerts and reminders follow your central list settings. Every outgoing message gets an exact review before you confirm it.'));
      if(state.health){
        const labels={startup:'Master lists',telegram_setup:'Telegram connection',telegram_poll:'Receiving Telegram messages',telegram_delivery:'Sending Telegram replies',monitoring:'Conversation monitoring',model:'Language model',commands:'Command processing'};
        for(const [name,item] of Object.entries(state.health))body.append(el('p',item.error||item.stale?'error':'muted',(labels[name]||name)+': '+(item.error||(item.stale?'Status is stale':'Connected'))));
      }else if(state.poll_error)body.append(el('p','error',state.poll_error));
      if(state.paired)body.append(button('Send test alert',async()=>{await request({action:'test',request_id:crypto.randomUUID()});status.textContent='Test requested. Check Telegram.';},status,state.transport_ready===false),
        button('Disconnect Telegram',async()=>{await request({action:'disconnect'});status.textContent='Disconnected.';body.replaceChildren();},status));
      else body.append(button('Connect Telegram',async()=>{const value=await request({action:'pair'});const url=new URL(value.url);
        if(url.origin!=='https://t.me')throw new Error('Invalid pairing link');
        const link=el('a','btn','Open Beepa in Telegram');link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';body.replaceChildren(link);status.textContent='Open this private link and press Start. It expires in 15 minutes.';
      },status));
      if(state.paired)commandQueue(body,state.commands,dialog,status);
      const preferences=await request({action:'settings'});if(!current()||!dialog.open)return;
      const section=el('details','control-options');section.append(el('summary','','Access & retention'));
      const value=preferences.settings,inputs={};
      const checkbox=(label,checked)=>{const wrap=el('label'),input=el('input');input.type='checkbox';input.checked=checked;wrap.append(input,el('span','',label));section.append(wrap);return input;};
      inputs.model=checkbox('Allow hosted model processing of relevant shared context',value.allow_model_context);
      inputs.notifications=checkbox('Enable automatic notifications',value.notifications_enabled);
      section.append(el('p','muted','Model: '+preferences.model));
      const mode=el('select','input');mode.setAttribute('aria-label','Sending access');
      for(const [id,label] of [['draft_confirm','Draft and confirm messages'],['read_only','Read only']]){const option=el('option','',label);option.value=id;mode.append(option);}mode.value=value.sending_mode;section.append(mode);
      const all=checkbox('Allow all managed accounts',value.allowed_accounts===null),accounts=[];
      for(const owner of preferences.accounts){const input=checkbox(owner,value.allowed_accounts===null||value.allowed_accounts.includes(owner));input.disabled=all.checked;accounts.push({owner,input});}
      all.addEventListener('change',()=>{for(const {input} of accounts)input.disabled=all.checked;});
      const retention=el('input','input');retention.type='number';retention.min='1';retention.max='365';retention.value=String(value.retention_days);retention.setAttribute('aria-label','Keep cached control content for days');
      section.append(el('label','','Keep cached control content (days)'),retention,el('p','muted','Source conversations stay unchanged. Completed command and notification content expires; minimal deduplication records and unresolved submissions are retained.'));
      section.append(button('Save access settings',async()=>{
        await request({action:'settings_update',revision:preferences.revision,settings:{allow_model_context:inputs.model.checked,notifications_enabled:inputs.notifications.checked,
          sending_mode:mode.value,allowed_accounts:all.checked?null:accounts.filter(a=>a.input.checked).map(a=>a.owner),retention_days:Number(retention.value)}});
        dialog.close();await open();
      },status));body.append(section);
    }catch(e){status.textContent=e.message;}
  }
  function destinations(host,policy,locked,prefix=''){
    const check=(label,value)=>{const wrap=el('label'),input=el('input');input.type='checkbox';input.checked=value;input.disabled=locked;wrap.append(input,el('span','',prefix+label));host.append(wrap);return input;};
    const browser=check('Mac browser',policy.channels.includes('browser')),telegram=check('Telegram',policy.channels.includes('telegram'));
    const label=el('label','',prefix+'Delivery preference'),select=el('select','input');
    select.setAttribute('aria-label',prefix+'Delivery preference');
    for(const [value,text] of [['all','Notify on every selected channel'],['browser','Prefer Mac browser'],['telegram','Prefer Telegram']]){const o=el('option','',text);o.value=value;select.append(o);}
    select.value=policy.routing?.primary||'all';select.disabled=true;label.append(select);host.append(label);
    const fallback=check('Use the other channel if delivery fails',policy.routing?.fallback===true),hint=el('p','muted','Channel preferences are connecting.');host.append(hint);
    let ready=false;
    const update=()=>{
      for(const option of select.options)option.disabled=option.value==='browser'&&!browser.checked||option.value==='telegram'&&!telegram.checked;
      if(select.selectedOptions[0]?.disabled)select.value='all';
      select.disabled=locked||!ready;
      browser.disabled=telegram.disabled=locked||!!policy.routing&&!ready;
      fallback.disabled=locked||!ready||select.value==='all'||!browser.checked||!telegram.checked;
      if(select.value==='all'||!browser.checked||!telegram.checked)fallback.checked=false;
    };
    for(const input of [browser,telegram,select])input.addEventListener('change',update);
    request({action:'status'}).then(s=>{if(!current()||!host.isConnected)return;ready=s.routing_ready===true;hint.textContent=ready?'Fallback is used only after confirmed failure or unavailability. An accepted or uncertain delivery is not sent again.':'Channel preferences are connecting.';update();}).catch(()=>{});
    update();
    return ()=>({channels:[...(browser.checked?['browser']:[]),...(telegram.checked?['telegram']:[])],routing:ready?(select.value==='all'?null:{primary:select.value,fallback:fallback.checked}):policy.routing??null});
  }
  function renderMonitoring(host,list,{perform,operation,locked,refresh}) {
    const details=el('details','crm-monitoring'),summary=el('summary','','Alerts & waiting');details.append(summary);
    const status=el('p','muted');status.setAttribute('role','status');
    const policy={enabled:false,all_messages:false,channels:['browser','telegram'],waiting_them_ms:null,waiting_us_ms:null,preview:false,...list.monitoring};
    const form=el('form','control-options');
    const check=(label,value)=>{const wrap=el('label'),input=el('input');input.type='checkbox';input.checked=value;input.disabled=locked;wrap.append(input,el('span','',label));form.append(wrap);return input;};
    const enabled=check('Notify me about this list',policy.enabled),all=check('Include outgoing messages',policy.all_messages);
    const readDestinations=destinations(form,policy,locked),preview=check('Include message text in Telegram alerts',policy.preview);
    const delay=(label,value)=>{const wrap=el('label','',label),input=el('input','input');input.type='number';input.min='0.017';input.max='8760';input.step='any';input.placeholder='No reminder';input.value=value===null?'':String(value/3600000);input.disabled=locked;wrap.append(input);form.append(wrap);return input;};
    const them=delay('Remind me after waiting for them (hours)',policy.waiting_them_ms),us=delay('Remind me after an unanswered incoming message (hours)',policy.waiting_us_ms);
    const timing=alertTiming(form,policy,locked);
    form.append(el('p','muted','Waiting status appears immediately. Blank deadlines mean no reminders. Delivery hours defer alerts; deadlines still use elapsed time.'));
    const save=button('Save alerts',()=>{},status,locked);save.type='submit';form.append(save);
    form.addEventListener('submit',e=>{e.preventDefault();if(!current()||locked)return;const milliseconds=i=>i.value===''?null:Math.round(Number(i.value)*3600000);
      try{perform(operation('list.monitoring',list.list_id,list.revision,{...policy,enabled:enabled.checked,all_messages:all.checked,...readDestinations(),preview:preview.checked,waiting_them_ms:milliseconds(them),waiting_us_ms:milliseconds(us),...timing()}));}catch(error){status.textContent=error.message;}});
    details.append(form);host.append(details,status);
    const results=el('div','control-conversations');host.append(results);
    const contacts=el('div','control-contact-tasks');host.append(contacts);
    contacts.append(button('Uncontacted & contact tasks',async()=>{
      const value=await request({action:'contact_updates',list_id:list.list_id});if(!host.isConnected)return;
      contacts.replaceChildren(el('p','muted','Contacts without a linked shared conversation, plus contact tasks you set. Native message history may be incomplete.'));
      for(const r of value.updates){
        const row=el('section','control-conversation');row.dataset.bindingId=r.binding_id;
        row.append(el('strong','',r.title+' · '+r.owner+' · '+r.source),el('span','muted',r.status_label+(r.fresh?'':' · sync needs checking')));
        for(const room of r.conversation_ids||[])row.append(button('Open conversation',()=>openRoom(room),status));
        if(r.due_at)row.append(el('p','muted','Contact by '+new Date(r.due_at).toLocaleString()));
        if(r.snooze_until)row.append(el('p','muted','Snoozed until '+new Date(r.snooze_until).toLocaleString()));
        const date=el('input','input');date.type='datetime-local';date.disabled=locked;date.setAttribute('aria-label','Contact date for '+r.title+' · '+r.owner);
        const selected=()=>{const ts=new Date(date.value).getTime();if(!Number.isSafeInteger(ts))throw new Error('Choose a date and time');return ts;};
        const change=async changes=>{await request({action:'contact_obligation',list_id:list.list_id,binding_id:r.binding_id,revision:list.revision,changes});await refresh();};
        row.append(date,button('Set contact-by date',()=>change({due_at:selected(),resolved:false}),status,locked),
          button('Clear contact-by date',()=>change({due_at:null}),status,locked),button('Snooze contact until date',()=>change({snooze_until:selected()}),status,locked),
          button('Clear contact snooze',()=>change({snooze_until:null}),status,locked),button(r.resolved?'Reopen contact task':'Resolve contact task',()=>change({resolved:!r.resolved}),status,locked));
        contacts.append(row);
      }
      if(!value.updates.length)contacts.append(el('p','muted','No uncontacted members or contact tasks in this list.'));
      if(!policy.enabled||policy.reminders_enabled===false)contacts.append(el('p','muted','Enable list notifications and overdue reminders under Alerts & waiting to receive contact-by alerts.'));
    },status,locked));
    const watch=(room,changes)=>{const payload={room_id:room.id,owner_mxid:room.owner,enabled:true,due_at:null,snooze_until:null,resolved_event:null,...list.watches?.[room.id],...changes};
      if(payload.inherit_monitoring){for(const k of ['inherit_monitoring','all_messages','waiting_them_ms','waiting_us_ms','activity_enabled','reminders_enabled','repeat_ms','schedule','presentation','channels','routing'])delete payload[k];}
      return perform(operation('list.watch',list.list_id,list.revision,payload));};
    results.append(button('Show waiting conversations',async()=>{
      const value=await request({action:'updates',list_id:list.list_id});if(!host.isConnected)return;
      results.replaceChildren();
      for(const r of value.updates){const row=el('div','control-conversation');row.append(button(r.title+' · '+r.owner+' · '+r.source,()=>openRoom(r.id),status),el('span','muted',r.waiting.replaceAll('_',' ')+(r.fresh?'':' · sync needs checking')));
        row.append(el('span','muted',r.coverage==='observed_history'?'Based on available bridge history':r.coverage==='native_suffix'?'Earlier history is incomplete':r.coverage==='unavailable'?'Native ordering evidence unavailable':''));
        if(r.source_unread?.count!==null&&r.source_unread?.count!==undefined)row.append(el('span','muted','User unread: '+r.source_unread.count));
        if(r.master_unseen?.count!==null&&r.master_unseen?.count!==undefined)row.append(el('span','muted','Unseen by you: '+r.master_unseen.count+(r.master_unseen.complete?'':' in available history')));
        if(r.seen_checkpoint)row.append(button('Mark updates seen',()=>perform(operation('list.seen',list.list_id,list.revision,r.seen_checkpoint)),status,locked));
        for(const item of r.obligations||[]){
          const entry=el('details','control-obligation'),message=r.latest_messages?.find(m=>m.id===item.event_id);
          entry.append(el('summary','',item.waiting.replaceAll('_',' ')+' · '+(item.timestamp?new Date(item.timestamp).toLocaleString():'No messages')+(item.delivery?' · '+item.delivery:'')));
          entry.append(el('p','',message?.text||item.event_id));
          if(item.answered_by)entry.append(el('p','muted','Response: '+item.answered_by));
          const change=patch=>perform(operation('list.obligation',list.list_id,list.revision,{room_id:r.id,owner_mxid:r.owner,event_id:item.event_id,due_at:item.due_at??null,snooze_until:item.snooze_until??null,resolved:item.waiting==='resolved',...patch}));
          if(!item.answered_by){
            const due=el('input','input');due.type='datetime-local';due.disabled=locked;due.setAttribute('aria-label','Date for message '+item.event_id);
            const selected=()=>{const ts=new Date(due.value).getTime();if(!Number.isSafeInteger(ts))throw new Error('Choose a date and time');return ts;};
            if(item.due_at)entry.append(el('p','muted','Deadline '+new Date(item.due_at).toLocaleString()));
            if(item.snooze_until)entry.append(el('p','muted','Snoozed until '+new Date(item.snooze_until).toLocaleString()));
            entry.append(due,button('Set deadline',()=>change({due_at:selected(),resolved:false}),status,locked),button('Clear deadline',()=>change({due_at:null}),status,locked),
              button('Snooze until date',()=>change({snooze_until:selected()}),status,locked),button('Clear snooze',()=>change({snooze_until:null}),status,locked),
              button(item.waiting==='resolved'?'Reopen':'Resolve',()=>change({resolved:item.waiting!=='resolved'}),status,locked));
          }
          row.append(entry);
        }
        const custom=el('details');custom.append(el('summary','','Conversation alert settings'));
        const effective={...policy,...list.watches?.[r.id]},customFields=el('div','control-options'),readTiming=alertTiming(customFields,effective,locked);custom.append(customFields);
        const readConversationDestinations=destinations(customFields,effective,locked,'Conversation ');
        const outgoingLabel=el('label'),outgoing=el('input');outgoing.type='checkbox';outgoing.checked=effective.all_messages;outgoing.disabled=locked;outgoingLabel.append(outgoing,el('span','','Include outgoing messages in this conversation'));customFields.append(outgoingLabel);
        const deadlines={};for(const [key,label] of [['waiting_them_ms','Waiting for them (hours)'],['waiting_us_ms','Waiting for us (hours)']]){
          const w=el('label','',label),input=el('input','input');input.type='number';input.min='0.017';input.max='8760';input.step='any';input.disabled=locked;input.placeholder='No reminder';input.value=effective[key]?String(effective[key]/3600000):'';w.append(input);customFields.append(w);deadlines[key]=input;
        }
        custom.append(button('Save conversation alerts',()=>watch(r,{...readTiming(),...readConversationDestinations(),all_messages:outgoing.checked,...Object.fromEntries(Object.entries(deadlines).map(([k,i])=>[k,i.value===''?null:Math.round(Number(i.value)*3600000)]))}),status,locked),button('Use list alert settings',()=>watch(r,{inherit_monitoring:true}),status,locked));
        row.append(custom,button('Remove from monitoring',()=>watch(r,{enabled:false}),status,locked));results.append(row);
      }
      if(!value.updates.length)results.append(el('p','muted','No shared conversations in this list yet.'));
    },status,locked));
    const search=el('input','input');search.placeholder='Search conversations across accounts';search.setAttribute('aria-label','Add a conversation to monitoring');search.disabled=locked;
    const matches=el('div');let sequence=0;
    search.addEventListener('input',async()=>{const n=++sequence;matches.replaceChildren();if(search.value.trim().length<2)return;
      try{const rows=await request({action:'tool',name:'search_conversations',arguments:{query:search.value}});if(n!==sequence||!host.isConnected)return;
        for(const room of rows)matches.append(button('Add '+room.title+' · '+room.owner+' · '+room.source,()=>watch(room,{enabled:true}),status,locked));
        if(!rows.length)matches.append(el('p','muted','No shared conversations match.'));
      }catch(e){if(n===sequence&&current())status.textContent=e.message;}});
    details.append(el('p','muted','Add shared conversations to monitor. Their sending permissions stay as configured.'),search,matches);
  }
  return {open,renderMonitoring,request};
}
