"""Exact target receipt evidence. No message bodies, tokens or receipt-time inference."""
import urllib.parse

TYPE = 'com.jkali.message_receipt'
STATUS_TYPE = 'com.beeper.message_send_status'


def parse_receipts(room, self_ids, bots, source_bot):
    out = {}
    def add(event_id, **value):
        if not isinstance(event_id, str) or not event_id.startswith('$') or len(event_id) > 256:
            return
        row = out.setdefault(event_id, {})
        for key, flag in value.items():
            row[key] = row.get(key, False) or flag
    if not isinstance(room, dict):
        return out
    for ev in (room.get('ephemeral') or {}).get('events', []):
        if not isinstance(ev, dict) or ev.get('type') != 'm.receipt' or not isinstance(ev.get('content'), dict):
            continue
        for target, per_event in ev['content'].items():
            reads = per_event.get('m.read') if isinstance(per_event, dict) else None
            if not isinstance(reads, dict):
                continue
            for user, info in reads.items():
                if user in bots or not isinstance(info, dict):
                    continue
                ts = info.get('ts')
                if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not 0 <= ts < float('inf'):
                    continue
                if info.get('thread_id') not in (None, 'main'):
                    continue
                add(target, **({'teammate_read': True} if user in self_ids else {'read': True, 'delivered': True}))
    for ev in (room.get('timeline') or {}).get('events', []):
        if not isinstance(ev, dict) or ev.get('type') != STATUS_TYPE or not source_bot or ev.get('sender') != source_bot:
            continue
        c = ev.get('content') or {}
        if not isinstance(c, dict):
            continue
        rel = c.get('m.relates_to')
        if not isinstance(rel, dict) or rel.get('rel_type') != 'm.reference':
            continue
        users = c.get('delivered_to_users')
        delivered = c.get('status') == 'SUCCESS' and isinstance(users, list) and any(
            isinstance(user, str) and user.startswith('@') and user not in self_ids and user not in bots for user in users)
        delivered = delivered or (source_bot.startswith('@imessagebot:') and c.get('status') == 'SUCCESS' and c.get('com.jkali.delivered') is True)
        add(rel.get('event_id'), delivered=delivered, sent=c.get('status') == 'SUCCESS', failed=c.get('status') == 'FAIL_PERMANENT')
    return out


def queue(uplink, room, master, section):
    from source_catalog import SOURCES
    row = uplink.mirror_for(room)
    source = row[1] if row else None
    bot = next((s.get('botMxid') for s in SOURCES if s['id'] == source), None)
    selfs = set(uplink.self_mxids) | {uplink.cfg.local_user}
    bots = {s['botMxid'] for s in SOURCES if s.get('botMxid')}
    evidence = parse_receipts(section, selfs, bots, bot)
    for target, value in evidence.items():
        flags = tuple(int(bool(value.get(key))) for key in ('read', 'delivered', 'teammate_read', 'sent', 'failed'))
        uplink.db.execute('''INSERT INTO message_receipts
            (local_room_id,master_room_id,target,read,delivered,teammate_read,sent,failed,dirty) VALUES (?,?,?,?,?,?,?,?,1)
            ON CONFLICT(local_room_id,target) DO UPDATE SET
            master_room_id=excluded.master_room_id,
            read=CASE WHEN master_room_id=excluded.master_room_id THEN max(read,excluded.read) ELSE excluded.read END, delivered=CASE WHEN master_room_id=excluded.master_room_id THEN max(delivered,excluded.delivered) ELSE excluded.delivered END,
            teammate_read=CASE WHEN master_room_id=excluded.master_room_id THEN max(teammate_read,excluded.teammate_read) ELSE excluded.teammate_read END, sent=CASE WHEN master_room_id=excluded.master_room_id THEN max(sent,excluded.sent) ELSE excluded.sent END,
            failed=CASE WHEN master_room_id=excluded.master_room_id THEN max(failed,excluded.failed) ELSE excluded.failed END,
            dirty=CASE WHEN master_room_id!=excluded.master_room_id OR excluded.read>read
              OR excluded.delivered>delivered OR excluded.teammate_read>teammate_read
              OR excluded.sent>sent OR excluded.failed>failed THEN 1 ELSE dirty END''',
            (room, master, target, *flags))
    uplink.db.commit()


def flush(uplink):
    rows = uplink.db.execute('SELECT local_room_id,master_room_id,target,read,delivered,teammate_read,sent,failed '
                             'FROM message_receipts WHERE dirty=1 ORDER BY rowid LIMIT 32').fetchall()
    for room, master, target, read, delivered, teammate_read, sent, failed in rows:
        row = uplink.mirror_for(room)
        if not row or row[0] != master or uplink.mirror_status(room) == 'revoking':
            uplink.db.execute('DELETE FROM message_receipts WHERE local_room_id=?', (room,))
            uplink.db.commit()
            continue
        if not uplink.active_link_for_dispatch():
            return
        if uplink.archive_level(room) not in ('share', 'direct'):
            uplink.db.execute('DELETE FROM message_receipts WHERE local_room_id=?', (room,))
            uplink.db.commit()
            continue
        mapped = uplink.delivery_for(master, target)
        if not mapped:
            # Receipt may precede archived history. Keep it durable until mapped;
            # rotate it so an absent/unsupported target cannot starve other rooms.
            uplink.db.execute('UPDATE message_receipts SET rowid=(SELECT max(rowid)+1 FROM message_receipts) '
                              'WHERE local_room_id=? AND target=?', (room, target))
            uplink.db.commit()
            continue
        payload = dict(read=bool(read), delivered=bool(delivered), teammate_read=bool(teammate_read),
                       sent=bool(sent), failed=bool(failed and not sent and not delivered and not read))
        uplink.master('PUT', '/_matrix/client/v3/rooms/' + urllib.parse.quote(master, safe='') + '/state/' + TYPE + '/' +
                      urllib.parse.quote(mapped, safe=''), payload, timeout=15)
        uplink.db.execute('UPDATE message_receipts SET dirty=0 WHERE local_room_id=? AND target=? AND master_room_id=?',
                          (room, target, master))
        uplink.db.commit()
