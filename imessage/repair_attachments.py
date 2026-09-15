#!/usr/bin/env python3
"""Audit/retry refused native attachment components in existing shared rooms.

Default is read-only. --apply requires the local iMessage daemon stopped,
backs up both SQLite databases and config, and journals each stable component
transaction. It adds only this user's Messages/Attachments and StickerCache
folders to the existing file allowlist. Unavailable sources stay refused.
No native messages, sharing changes, room creation or send-ledger resets occur.
"""
import sys,os,json,sqlite3,time,collections,mimetypes,subprocess,argparse
from pathlib import Path
CODE_ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(CODE_ROOT))
from imessage import daemon as d
from imessage import repair_timestamps as r
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(os.environ.get('BEEPA_INSTALL_ROOT',CODE_ROOT)))
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    manifest=r.read_manifest(args.root) or {}
    state=Path(manifest.get('state_root') or args.root).resolve()
    apply=args.apply
    config=json.loads((state/'imessage/daemon.json').read_text())
    assert manifest.get('owner_uid',os.getuid())==os.getuid()
    env=r.read_env(state/'agents/uplink/local.env.local');env.update(r.read_env(state/'agents/uplink/uplink.env.local'));cfg=r.Config(env)
    assert cfg.local_user==config['user_id']
    local=lambda method,path,body=None:r._mx(cfg.local_hs,cfg.local_token,method,path,body,timeout=15)
    db=sqlite3.connect(f'file:{state}/imessage/state.db?mode=ro',uri=True)
    up=sqlite3.connect(f'file:{state}/agents/uplink/state.db?mode=ro',uri=True)
    d.CLI=config['cli_path']
    native_roots=[str((Path.home()/'Library/Messages'/name).resolve()) for name in ('Attachments','StickerCache')]
    configured_roots=[os.path.realpath(p) for p in config['attachment_allow_prefixes']]
    additions=[p for p in native_roots if p not in configured_roots]
    allowed=configured_roots+native_roots
    rows=db.execute("select m.chat_id,c.room_id,c.msg_id,c.component,c.status from inbound_component c join map m on m.room_id=c.room_id where c.status IN ('refused_invalid_path','refused_path')").fetchall()
    counts=collections.Counter();entries=[];candidates=[]
    def shared(room):
        mirror=up.execute("select m.master_room_id from mirror_rooms m join mirror_lifecycle l on l.local_room_id=m.local_room_id where m.local_room_id=? and m.source='imessage' and l.status='live'",(room,)).fetchone()
        level=r.consent.effective_level(r.get_optional(local,'/_matrix/client/v3/user/'+r.q(cfg.local_user)+'/rooms/'+r.q(room)+'/account_data/'+r.consent.SHARE_OVERRIDE_TYPE))
        return bool(mirror and level in ('share','direct'))
    for chat in dict.fromkeys(x[0] for x in rows):
        group=[x for x in rows if x[0]==chat];wanted={x[2] for x in group};found={};before=None;seen=set()
        for _ in range(1000):
            page=d.cli_json('messages',chat,*(('--before',before) if before else ()),timeout=45)
            items=page.get('items',[])
            for m in items:
                if str(m.get('id')) in wanted:found[str(m['id'])]=m
            if wanted.issubset(found) or not page.get('hasMore'):break
            before=str(items[0].get('cursor') or '') if items else ''
            if not before.isdigit() or before in seen:counts['incomplete_history']+=1;break
            seen.add(before)
        for row in group:
            chat,room,mid,key,status=row
            entry={'chat':chat,'room':room,'native_id':mid,'component':key,'previous_status':status}
            m=found.get(mid);att=None
            if m:
                att=next((a for i,a in enumerate(m.get('attachments') or []) if 'attachment:'+str(a.get('id') or i)==key),None)
            if not shared(room):outcome='private_or_not_live'
            elif not m or not att:outcome='source_unavailable'
            elif not r.valid_ts(m.get('timestamp')):outcome='invalid_timestamp'
            else:
                path=d.decode_asset_url(att.get('srcURL') or '')
                real=os.path.realpath(path) if path else ''
                if not path:outcome='native_attachment_loading' if att.get('loading') else 'source_url_unavailable'
                elif not any(real.startswith(p+os.sep) for p in allowed):outcome='path_not_allowed'
                elif not os.path.isfile(real):outcome='file_unavailable'
                else:
                    outcome='would_recover';candidates.append((entry,m,att,real))
                    entry['timestamp']=m['timestamp']
            entry['outcome']=outcome;counts[outcome]+=1;entries.append(entry)
    folder=state/'.beepa-repair-verification'
    folder.mkdir(exist_ok=True,mode=0o700)
    if apply:
        for prefix in ('org.beepa.', 'com.jkali.'):
            label=f'gui/{os.getuid()}/{prefix}imessage-daemon'
            if subprocess.run(['launchctl','print',label],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:
                raise RuntimeError('Stop the iMessage daemon before applying this exact-component repair')
        folder=folder/('attachments-'+str(time.time_ns()));folder.mkdir(mode=0o700)
        for name,source in [('imessage',db),('uplink',up)]:
            p=folder/(name+'.sqlite')
            with sqlite3.connect(p) as target:source.backup(target)
            p.chmod(0o600)
        r.atomic_write(folder/'daemon.before.json',json.dumps(config,indent=2)+'\n')
        journal={'entries':entries,'counts':dict(counts)}
        def save():r.atomic_write(folder/'journal.json',json.dumps(journal,indent=2)+'\n')
        save()
        config['attachment_allow_prefixes']=list(dict.fromkeys(config['attachment_allow_prefixes']+native_roots))
        r.atomic_write(state/'imessage/daemon.json',json.dumps(config,indent=2)+'\n')
        # Configure only inbound transport/receipts. Do not call initialize(), which
        # performs outbound restart recovery, or any native mutation function.
        d.CFG=config;d.HS=config['hs_url'];d.AS_TOKEN=config['as_token'];d.DOMAIN=config['domain'];d.BOT_ID=config['bot_id']
        d.DB=sqlite3.connect(state/'imessage/state.db');d.ATTACH_PREFIXES=allowed
        for entry,m,att,path in candidates:
            room,mid,key=entry['room'],entry['native_id'],entry['component']
            try:
                if not shared(room):entry['outcome']='sharing_changed';save();continue
                current=d.component_get(room,mid,key)
                if not current or current[1] not in ('refused_invalid_path','refused_path'):entry['outcome']='mapping_changed';save();continue
                events=d.mx('GET','/_matrix/client/v3/rooms/'+r.q(room)+'/state',user=d.BOT_ID)
                assert any(e.get('type')=='m.room.create' and e.get('sender')==d.BOT_ID for e in events)
                sender=d.BOT_ID if m.get('isSender') is True else d.ensure_ghost(str(m.get('senderID') or entry['chat'].rsplit(';',1)[-1]),m.get('senderName') or '')
                if sender!=d.BOT_ID:d.ghost_join(sender,room)
                entry['outcome']='dispatching_local_component';save()
                mime=mimetypes.guess_type(path)[0] or 'application/octet-stream'
                content={'msgtype':'m.image' if mime.startswith('image/') else 'm.file','body':d.clean_name(os.path.basename(path)),'url':d.upload_media(path,mime),'info':{'mimetype':mime,'size':os.path.getsize(path)},**d.native_metadata(m)}
                if m.get('isSender') is True:content['com.jkali.from_me']=True
                txn=d.component_txn(room,mid,key)
                receipt=d.mx('PUT','/_matrix/client/v3/rooms/'+r.q(room)+'/send/m.room.message/'+txn,content,user=sender)
                eid=receipt['event_id'];assert eid
                ev=d.mx('GET','/_matrix/client/v3/rooms/'+r.q(room)+'/event/'+r.q(eid),user=d.BOT_ID)
                assert ev['content'][r.ORIGIN_TS]==m['timestamp']
                d.component_put(room,mid,key,eid)
                if not d.event_map_get(entry['chat'],mid):d.event_map_put(entry['chat'],mid,eid,sender,d.sha(d.clean_text(m.get('text') or '')))
                entry['event_id']=eid;entry['outcome']='recovered'
            except Exception as exc:entry['outcome']='error';entry['error']=type(exc).__name__
            save()
        counts=collections.Counter(x['outcome'] for x in entries);journal['counts']=dict(counts);save()
    else:
        r.atomic_write(folder/'attachments-audit.json',json.dumps({'entries':entries,'counts':dict(counts)},indent=2)+'\n')
    print(json.dumps({'apply':apply,'counts':dict(counts),'native_media_allowlist_additions':additions,'evidence':str(folder)},indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Attachment repair stopped: '+type(exc).__name__,file=sys.stderr)
        sys.exit(1)
