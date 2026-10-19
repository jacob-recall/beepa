import { el } from '../../shared/ui/el.js';

let workerPolicy;
export function registerNotificationWorker(base) {
  const api = new URL(base);
  const workerUrl = new URL('./notification-sw.js', import.meta.url);
  if (api.hostname !== workerUrl.hostname ||
      !(api.protocol === 'https:' || (api.protocol === 'http:' && ['127.0.0.1','localhost'].includes(api.hostname))))
    throw new Error('Notification API must use the master hostname.');
  workerUrl.searchParams.set('api', api.origin);
  let scriptUrl = workerUrl.href;
  if (globalThis.trustedTypes) {
    const allowed = workerUrl.href;
    workerPolicy ||= trustedTypes.createPolicy('beepa-notification-worker', {createScriptURL(value) {
      if (value !== allowed) throw new TypeError('Unexpected notification worker URL');
      return value;
    }});
    scriptUrl = workerPolicy.createScriptURL(scriptUrl);
  }
  return navigator.serviceWorker.register(scriptUrl, {scope:'./'});
}

// Manager-only preferences/history. All updates live on the master; browser
// storage retains only this browser's subscription identifier, never messages.
export function createNotifications({ base, token, userId, current, openMessage, openContact, getRoom, getVerifiedRooms }) {
  let state = { follows: [], history: [] }, registration = null, closed = false, timer = null, groupItems=null;
  const storageKey = 'beepa-notification-device:' + userId + ':' + base;
  const preferenceKey = storageKey + ':enabled';
  let clientId=crypto.randomUUID();
  try { clientId=localStorage.getItem(storageKey+':client')||clientId;localStorage.setItem(storageKey+':client',clientId); } catch {}
  let device = '';
  try { device = localStorage.getItem(storageKey) || ''; } catch {}
  const dialog = document.getElementById('notifications-dialog');
  const followButton = document.getElementById('room-follow');
  const live = () => !closed && current();
  async function request(data) {
    if (!live()) throw new Error('Session changed');
    const response = await fetch(base + '/admin/notifications', { method: 'POST', headers: {
      'Content-Type': 'application/json', Authorization: 'Bearer ' + token,
    }, body: JSON.stringify(data) });
    const value = await response.json();
    if (!live()) throw new Error('Session changed');
    if (!response.ok) throw new Error(value.error || 'Notification request failed');
    return value;
  }
  let connectionStage = 'permission';
  async function report(stage,error) {
    if(stage!=='error')connectionStage=stage;
    await request({action:'client_status',client:clientId,stage:connectionStage,
      permission:typeof Notification==='undefined'?'unsupported':Notification.permission,
      error_code:error?.name?.replace(/[^A-Za-z0-9_]/g,'').slice(0,64)||''}).catch(()=>{});
  }
  async function connectionError(error){status(error.message);await report('error',error);}
  function button(label, fn, disabled = false) {
    const node = el('button','btn',label); node.type = 'button'; node.disabled = disabled;
    node.addEventListener('click', () => { if (live()) Promise.resolve().then(fn).catch(error => status(error.message)); }); return node;
  }
  function status(text) { if (live()) document.getElementById('notification-status').textContent = text; }
  function renderFollow() {
    if (!followButton) return;
    const room = getRoom(), allowed = getVerifiedRooms().includes(room);
    const followed = state.follows.some(f => f.room === room);
    followButton.disabled = !allowed; followButton.textContent = followed ? 'Following' : 'Follow';
    followButton.setAttribute('aria-pressed', String(followed));
  }
  async function refresh() {
    state = await request({ action: 'status' }); renderFollow();
    if (dialog.open) render();
    document.getElementById('nav-notifications').setAttribute('title', 'Notifications · ' + state.history.filter(h=>!h.seen).length + ' new');
    return state;
  }
  async function openItem(item) {
    if (item.kind === 'test') { await request({action:'seen',id:item.id}); await refresh(); return; }
    if (item.kind === 'contact_reminder') {
      if(!openContact)throw new Error('Refresh Beepa to open this contact task.');
      await openContact(JSON.parse(item.rule_refs));dialog.close();
      await request({action:'seen',id:item.id});await refresh();return;
    }
    if (!getVerifiedRooms().includes(item.room)) throw new Error('This conversation is no longer available. Refresh the master.');
    dialog.close();
    await openMessage(item.room, { eventId: item.event });
    await request({action:'seen',id:item.id}); await refresh();
  }
  async function enable() {
    if (!window.isSecureContext || !('Notification' in window) || !('serviceWorker' in navigator) || !('PushManager' in window))
      throw new Error('Use a browser with Web Push on HTTPS or localhost.');
    // Permission request must stay directly in the user gesture, before reads.
    const permission = Notification.permission === 'granted' ? 'granted' : await Notification.requestPermission();
    if (permission !== 'granted') throw new Error('Allow notifications for Beepa in your browser, then try again.');
    try { localStorage.setItem(preferenceKey,'1'); } catch {}
    status('Connecting this browser…');await report('worker');
    registration = await registerNotificationWorker(base);
    await navigator.serviceWorker.ready;
    await report('key');
    const key = (await request({action:'key'})).public_key;
    const raw = atob(key.replace(/-/g,'+').replace(/_/g,'/') + '='.repeat((4-key.length%4)%4));
    await report('subscription');
    const subscription = await registration.pushManager.getSubscription() || await registration.pushManager.subscribe({
      userVisibleOnly: true, applicationServerKey: Uint8Array.from(raw, c=>c.charCodeAt(0)),
    });
    const value = await request({action:'subscribe',subscription:subscription.toJSON()}); device = value.device;
    try { localStorage.setItem(storageKey,device); } catch {}
    await report('connected');
    await refresh(); status('This browser is connected. Use Send test notification to check Mac pop-ups.');
  }
  async function disable() {
    await request({action:'unsubscribe',device});
    try { localStorage.setItem(preferenceKey,'0'); } catch {}
    registration ||= await navigator.serviceWorker.getRegistration(new URL('./',import.meta.url));
    await (await registration?.pushManager.getSubscription())?.unsubscribe();
    device='';try { localStorage.removeItem(storageKey); } catch {}
    await report('disabled');
    await refresh(); status('Notifications disabled for this browser.');
  }
  function render() {
    document.getElementById('notification-title').textContent=groupItems?'Message digest':'Notifications';
    document.getElementById('notification-controls').hidden=!!groupItems;
    document.getElementById('notification-follows').hidden=!!groupItems;
    document.getElementById('notification-follow-title').hidden=!!groupItems;
    document.getElementById('notification-history-title').textContent=groupItems?'Conversations in this digest':'Latest updates';
    const controls = document.getElementById('notification-controls'); controls.replaceChildren();
    const registered = device && (Array.isArray(state.device_list) ? state.device_list.some(d=>d.id===device && d.enabled) : state.devices>0);
    const permission = typeof Notification === 'undefined' ? 'unsupported' : Notification.permission;
    controls.append(el('p','muted', registered && permission==='granted' ? 'This browser is connected. Alerts can arrive with this tab closed.' : 'Connect this browser to receive Mac pop-ups.'));
    // This handler intentionally calls enable synchronously inside the gesture.
    const connect=el('button','btn',device?'Reconnect browser':'Enable browser notifications');connect.type='button';
    connect.addEventListener('click',()=>{if(live()) enable().catch(connectionError);});controls.append(connect);
    if(registered) controls.append(button('Send test notification',async()=>{await request({action:'test',device,request_id:crypto.randomUUID()});await refresh();status('Test queued. Delivery status appears below.');}),button('Disable this browser',disable));
    controls.append(button('Check for updates',async()=>{await request({action:'sync'});await refresh();status('Notification status updated.');}));
    controls.append(el('p','muted','Alerts show the conversation name, without message text. Mac notification settings and Focus control banners and sounds.'));
    const follows=document.getElementById('notification-follows');follows.replaceChildren();
    for(const follow of state.follows){
      const row=el('div','notification-follow');row.append(el('strong','',follow.title));
      const label=el('label','','Notify about '),select=el('select','input');select.setAttribute('aria-label','Notification mode for '+follow.title);
      for(const [value,text] of [['incoming','Incoming messages'],['all','All messages']]){const option=el('option','',text);option.value=value;select.append(option);}
      select.value=follow.all_messages?'all':'incoming';select.addEventListener('change',()=>{request({action:'follow',room:follow.room,all_messages:select.value==='all'}).then(refresh).catch(e=>status(e.message));});label.append(select);
      row.append(label,button('Unfollow',async()=>{await request({action:'unfollow',room:follow.room});await refresh();}));follows.append(row);
    }
    if(!state.follows.length)follows.append(el('p','muted','Open a conversation and choose Follow.'));
    const history=document.getElementById('notification-history');history.replaceChildren();
    if(groupItems)history.append(el('p','muted','Choose a conversation from this digest.'),button('Show all notifications',()=>{groupItems=null;render();}));
    for(const item of groupItems||state.history){
      const row=el('div','notification-entry');
      row.append(button((item.seen?'':'● ')+item.title,()=>openItem(item)),el('span','muted',new Date(item.created).toLocaleString()));
      const deliveries=item.deliveries||[];
      const label=d=>d.clicked_at?'Opened notification':d.shown_at?'Browser acknowledged display':d.state==='accepted'?'Push service accepted':d.state==='pending'?(d.attempts?'Retrying':'Queued'):d.state==='sending'?'Sending':d.state==='unknown'?'Delivery uncertain':d.state==='failed'?'Delivery failed':d.state==='expired'?'Expired':'Canceled';
      row.append(el('span','muted',deliveries.length?deliveries.map(label).join(' · '):'No connected browser when this arrived'));
      history.append(row);
    }
    if(!state.history.length)history.append(el('p','muted','New activity in followed conversations will appear here.'));
    if(state.error)status(state.error);
  }
  async function open(){groupItems=null;render();if(!dialog.open)dialog.showModal();await refresh();}
  async function toggleFollow(){const room=getRoom();if(!getVerifiedRooms().includes(room))return;
    await request({action:state.follows.some(f=>f.room===room)?'unfollow':'follow',room});await refresh();}
  async function handleLink(){const hash=location.hash;if(hash==='#notifications'){await open();return;}
    if(hash.startsWith('#notification-group=')){
      const ids=hash.slice(20).split(',');
      if(!ids.length||ids.length>10||new Set(ids).size!==ids.length||ids.some(id=>!/^[a-f0-9]{64}$/.test(id))){await open();status('This notification group link is invalid.');return;}
      await refresh();const items=[];for(const id of ids)items.push(state.history.find(h=>h.id===id)||await request({action:'delivery',id}));
      if(!live())return;groupItems=items;status('');render();if(!dialog.open)dialog.showModal();return;
    }
    if(!hash.startsWith('#notification='))return;const id=hash.slice(14);await refresh();const item=state.history.find(h=>h.id===id);
    if(item){if(item.kind==='test')await open();await openItem(item);}else if(/^[a-f0-9]{64}$/.test(id)){const older=await request({action:'delivery',id});if(older.kind==='test')await open();await openItem(older);}else{await open();status('This notification link is invalid.');}}
  return {open,toggleFollow,renderFollow,handleLink,
    async start(){
      timer=setInterval(()=>{refresh().catch(()=>{});},15000);await refresh();
      let optedOut=false;try { optedOut=localStorage.getItem(preferenceKey)==='0'; } catch {}
      // Existing browser consent lets reload recover interrupted registration;
      // never prompt automatically, and respect an explicit device disable.
      if (!optedOut && typeof Notification!=='undefined' && Notification.permission==='granted')
        await enable().catch(connectionError);
    },
    close(){closed=true;clearInterval(timer);if(dialog.open)dialog.close();
      // Logout/expired sessions revoke this browser endpoint even if the API
      // no longer accepts the old bearer. Closing a tab does not call close().
      if ('serviceWorker' in navigator) navigator.serviceWorker.getRegistration(new URL('./',import.meta.url))
        .then(reg=>reg?.pushManager.getSubscription()).then(sub=>sub?.unsubscribe()).catch(()=>{});
      try { localStorage.removeItem(storageKey); } catch {}
    },
  };
}
