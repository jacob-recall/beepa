#!/usr/bin/env python3
"""API-only hardware probe: one real self-message and its native incoming copy.
Requires explicit authorization; no computer use, other recipients or permission
changes. Restores the self-chat's previous follow setting. Never resends a timeout.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.parse import quote
import uuid
from probe_propagation import load_setup, leg_matrix, http, poll, LOCAL_HS, MASTER_HS
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'master'))
from notify import call, local_token, DEFAULT_BASE


def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--i-am-sending-real-imessages',action='store_true',required=True)
 parser.add_argument('--report',type=Path,required=True)
 parser.add_argument('--require-browser',action='store_true')
 args=parser.parse_args();setup=load_setup();token=local_token(DEFAULT_BASE)
 api=lambda data:call(DEFAULT_BASE,token,data)
 initial=api({'action':'status'})
 if args.require_browser and not initial['devices']:raise RuntimeError('Connect a browser before requiring live push verification')
 previous=next((f for f in initial['follows'] if f['room']==setup['mirror']),None)
 canary='Beepa notification API self-test '+uuid.uuid4().hex[:12]
 report={'started':time.time(),'self_handle':setup['self_handle'],'mirror':setup['mirror'],'devices_at_start':initial['devices'],
         'canary':canary,'notification':None,'propagation_ok':False,'master_notifications_ok':False,'browser_acknowledged':False}
 def incoming(base,credential,room,local_ts=None):
  def read():
   data=http(base,credential,'GET','/_matrix/client/v3/rooms/'+quote(room,safe='')+'/messages?dir=b&limit=100')
   for event in data.get('chunk',[]):
    c=event.get('content',{})
    if (event.get('type')!='m.room.message' or c.get('body')!=canary or c.get('com.jkali.from_me') is True
        or event.get('origin_server_ts',0)<report['started']*1000):continue
    if base==LOCAL_HS and event.get('sender') in (setup['local_user'],setup['bot_id']):continue
    if local_ts is not None and c.get('com.jkali.hops',{}).get('local_ts')!=local_ts:continue
    return event
  return read
 try:
  # Default incoming-only mode: our outgoing event must not generate the alert.
  api({'action':'follow','room':setup['mirror'],'all_messages':False})
  sent=leg_matrix(setup,canary,report)
  received,delay=poll(incoming(LOCAL_HS,setup['local_token'],setup['room']),90)
  report['incoming']={'local_found':bool(received),'local_s':delay,'local_event_id':received.get('event_id') if received else None}
  if received:
   mirrored,delay=poll(incoming(MASTER_HS,setup['master_token'],setup['mirror'],received['origin_server_ts']),90)
   report['incoming'].update(master_found=bool(mirrored),master_s=delay,master_event_id=mirrored.get('event_id') if mirrored else None)
   report['propagation_ok']=bool(sent and mirrored)
   if mirrored:
    api({'action':'sync'});nid=hashlib.sha256((setup['mirror']+'\0'+mirrored['event_id']).encode()).hexdigest()
    deadline=time.monotonic()+45
    while True:
     item=api({'action':'delivery','id':nid})
     if not initial['devices'] or (item['deliveries'] and all(d['shown_at'] or d['clicked_at'] for d in item['deliveries'])) or time.monotonic()>=deadline:break
     time.sleep(1)
    report['notification']=item;report['master_notifications_ok']=True
    report['browser_acknowledged']=bool(item['deliveries']) and all(d['shown_at'] or d['clicked_at'] for d in item['deliveries'])
 except Exception as error:
  report['error']=type(error).__name__ # No credential-bearing exceptions in reports.
 finally:
  try:
   if previous:api({'action':'follow','room':setup['mirror'],'all_messages':bool(previous['all_messages'])})
   else:api({'action':'unfollow','room':setup['mirror']})
  finally:args.report.write_text(json.dumps(report,indent=2)+'\n')
 print(json.dumps({key:report.get(key) for key in ('propagation_ok','master_notifications_ok','browser_acknowledged','error')},indent=2))
 return 0 if report['propagation_ok'] and report['master_notifications_ok'] and (not args.require_browser or report['browser_acknowledged']) else 1

if __name__=='__main__':sys.exit(main())
