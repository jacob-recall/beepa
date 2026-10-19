import {hash} from './control-domain.mjs';
import {notificationIdentity} from './notification-routing.mjs';

// Match the pre-router Telegram keys so switching discovery owners cannot replay
// an event already attempted by the old worker. Browser uses its existing hash.
export function telegramNotificationKey(source){
  notificationIdentity(source);
  if(source.kind==='contact_reminder')return 'contact-due:'+notificationIdentity(source);
  if(source.kind==='reminder'&&source.episode)return 'due-episode:'+hash([source.room,source.event,source.due,source.episode,source.slot]);
  return source.kind==='message'?'alert:'+hash([source.room,source.event]):
    'due:'+hash([source.room,source.event,source.due,...(source.slot===source.due?[]:[source.slot])]);
}
const validStates=new Set(['missing','pending','in_flight','unknown','accepted','failed','expired','unavailable','canceled']);
function browserOutcome(result,id){
  if(!result||!Array.isArray(result.outcomes)||result.outcomes.length!==1||result.outcomes[0].id!==id||!validStates.has(result.outcomes[0].state))throw Error('Browser delivery evidence unavailable');
  return result.outcomes[0].state;
}

export function notificationAdapters({core,browser,sourceFor}){
  const telegramOutcome=id=>{
    const source=sourceFor(id);if(!source)throw Error('Notification source missing');
    const prefix=telegramNotificationKey(source)+':';
    const rows=core.db.prepare(`SELECT state FROM outbox WHERE substr(id,1,?)=?
      UNION ALL SELECT state FROM transport_outcomes WHERE substr(id,1,?)=?`).all(prefix.length,prefix,prefix.length,prefix);
    const states=new Set(rows.map(r=>r.state));
    // Any accepted part rules out fallback, including after bot rotation. An
    // uncertain old bot attempt must likewise survive a fresh transport binding.
    return states.has('accepted')?'accepted':states.has('unknown')?'unknown':states.has('sending')?'in_flight':states.has('pending')?'pending':states.has('failed')?'failed':states.has('expired')?'expired':states.has('canceled')?'canceled':'missing';
  };
  return {
    browser:{
      async enqueue({id,source,refs}){
        if(notificationIdentity(source)!==id)throw Error('Notification identity changed');
        const entry=source.kind==='contact_reminder'?{kind:source.kind,contact:source,refs}:{room:source.room,event:source.event,kind:source.kind,refs};
        return browserOutcome(await browser({action:'control_enqueue',entries:[entry]}),id);
      },
      async outcome(id){return browserOutcome(await browser({action:'control_outcomes',ids:[id]}),id);},
    },
    telegram:{
      async enqueue(job){
        if(notificationIdentity(job.source)!==job.id)throw Error('Notification identity changed');
        const old=telegramOutcome(job.id);if(old!=='missing')return old;
        if(!core.value('operator'))return 'unavailable';
        const result=await core.enqueueRoutedAlert({...job,key:telegramNotificationKey(job.source)});
        return result==='canceled'?'canceled':result==='unavailable'?'unavailable':telegramOutcome(job.id);
      },
      async outcome(id){return telegramOutcome(id);},
    },
  };
}
