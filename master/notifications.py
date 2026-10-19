"""Manager-owned browser push. Matrix reads only; no conversation writes.

State is local SQLite (0700 directory, 0600 files). Enrollment authenticates
all API callers. The background reader uses the provisioned manager token,
never source-account credentials. Optional pywebpush runs in a restricted Docker helper.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import shutil
import sys
import threading
import time
from urllib.parse import quote, urlsplit
from datetime import datetime
from zoneinfo import ZoneInfo

class VerificationPending(Exception):
    """Retry before any external delivery; this is not a revoked policy."""


ROOM = re.compile(r'^![^\s:]{1,255}:[^\s]{1,255}$')
EVENT = re.compile(r'^\$[^\s]{1,255}$')


def contact_identity(source):
    """Same typed identity as notification-routing.mjs; never a fabricated room."""
    if (not isinstance(source, dict) or set(source) != {'kind', 'binding', 'owner', 'generation', 'created', 'episode', 'due', 'slot'}
            or source['kind'] != 'contact_reminder'
            or not isinstance(source['binding'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', source['binding'])
            or source['binding'] in ('__proto__', 'constructor', 'prototype')
            or not isinstance(source['owner'], str) or len(source['owner']) > 256 or not re.fullmatch(r'@[^\s:]+:[^\s]+', source['owner'])
            or not isinstance(source['generation'], str) or not 1 <= len(source['generation']) <= 128 or re.search(r'[\x00-\x1f]', source['generation'])
            or any(type(source[k]) is not int or not 0 < source[k] <= 9007199254740991 for k in ('created', 'episode', 'due', 'slot'))
            or source['slot'] < source['due']):
        raise ValueError('Invalid contact reminder identity')
    identity = [source[k] for k in ('owner', 'binding', 'generation', 'created', 'episode', 'due', 'slot')]
    return hashlib.sha256(('contact-deadline:'+json.dumps(identity, ensure_ascii=False, separators=(',', ':'))).encode()).hexdigest()


def reminder_identity(room, event, due, slot, episode=None):
    if any(type(v) is not int or not 0 < v <= 9007199254740991 for v in (due, slot)) or slot < due:
        raise ValueError('Invalid reminder identity')
    if episode is not None:
        if type(episode) is not int or not 0 < episode <= 9007199254740991:
            raise ValueError('Invalid reminder episode')
        prefix, values = 'deadline-episode:', [room, event, due, episode, slot]
    else:
        prefix, values = 'deadline:', [room, event, due] + ([slot] if slot != due else [])
    # Keep legacy Python IDs exactly as previously written, including escaping.
    return hashlib.sha256((prefix+json.dumps(values, ensure_ascii=episode is None, separators=(',', ':'))).encode()).hexdigest()


def monitoring_allows(schedule, at):
    """Parity with shared/model/crm.js; canonical list data is the authority."""
    if schedule is None:
        return True
    try:
        if (not isinstance(schedule, dict) or set(schedule) != {'timezone', 'mode', 'days', 'start_minute', 'end_minute'}
                or not isinstance(schedule['timezone'], str) or len(schedule['timezone']) > 100
                or not re.fullmatch(r'(?:UTC|GMT|[A-Za-z_]+(?:/[A-Za-z0-9_+-]+)+)', schedule['timezone'])
                or schedule['mode'] not in ('quiet', 'working')
                or not isinstance(schedule['days'], list) or not 1 <= len(schedule['days']) <= 7
                or any(type(day) is not int or not 1 <= day <= 7 for day in schedule['days'])
                or len(set(schedule['days'])) != len(schedule['days'])
                or type(schedule['start_minute']) is not int or not 0 <= schedule['start_minute'] < 1440
                or type(schedule['end_minute']) is not int or not 0 <= schedule['end_minute'] <= 1440
                or schedule['start_minute'] == schedule['end_minute']):
            return False
        local = datetime.fromtimestamp(at/1000, ZoneInfo(schedule['timezone']))
        day, minute = local.isoweekday(), local.hour*60+local.minute
        start, end, days = schedule['start_minute'], schedule['end_minute'], schedule['days']
        inside = (day in days and start <= minute < end) if start < end else (
            day in days and minute >= start or (7 if day == 1 else day-1) in days and minute < end)
        return inside if schedule['mode'] == 'working' else not inside if schedule['mode'] == 'quiet' else False
    except (ValueError, KeyError, TypeError, OverflowError):
        return False


def reminder_slot(due, repeat, at):
    if type(due) is not int or due <= 0 or at < due:
        return None
    if repeat is None:
        return due
    if type(repeat) is not int or not 60000 <= repeat <= 31536000000:
        return None
    return due + int((at-due)//repeat)*repeat


def delivery_plan(rules, created, at, room):
    choices = []
    for rule in rules:
        if not monitoring_allows(rule.get('schedule'), at):
            continue
        p = rule.get('presentation')
        if p is None:
            p = {'mode': 'immediate', 'interval_ms': None}
        if not isinstance(p, dict) or set(p) != {'mode', 'interval_ms'}:
            continue
        mode, interval = p['mode'], p['interval_ms']
        if mode == 'immediate':
            if interval is not None:
                continue
            ready = created
        elif mode in ('grouped', 'digest') and type(interval) is int and 60000 <= interval <= (3600000 if mode == 'grouped' else 86400000):
            ready = (created//interval+1)*interval
        else:
            continue
        collection = max(ready, int(at//interval)*interval) if mode != 'immediate' else created
        key = ('grouped:'+room+':'+str(collection)) if mode == 'grouped' else ('digest:'+rule['list_id']+':'+str(collection)) if mode == 'digest' else None
        choices.append({'mode': mode, 'ready_at': ready, 'key': key, 'list_id': rule['list_id']})
    return min(choices, key=lambda p: (p['ready_at'], p['key'] or '')) if choices else None


def valid_subscription(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid browser subscription')
    endpoint = value.get('endpoint', '')
    if not isinstance(endpoint, str) or len(endpoint) > 4096:
        raise ValueError('Invalid push endpoint')
    url = urlsplit(endpoint)
    host = url.hostname or ''
    if (url.scheme != 'https' or url.port not in (None, 443) or url.username or url.password or url.fragment
            or not (host in ('fcm.googleapis.com', 'updates.push.services.mozilla.com', 'web.push.apple.com')
                    or host.endswith('.push.apple.com'))):
        raise ValueError('Unsupported browser push service')
    keys = value.get('keys', {})
    if not isinstance(keys, dict):
        raise ValueError('Invalid subscription keys')
    for name, length in [('auth', 16), ('p256dh', 65)]:
        key = keys.get(name, '')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,100}={0,2}', key):
            raise ValueError('Invalid subscription keys')
        raw = base64.urlsafe_b64decode(key + '=' * (-len(key) % 4))
        if len(raw) != length or (name == 'p256dh' and raw[0] != 4):
            raise ValueError('Invalid subscription keys')
    return {'endpoint': endpoint, 'keys': {k: keys[k] for k in ('auth', 'p256dh')}}


def notification_event(event, owner, since, all_messages=False):
    if not isinstance(event, dict) or not isinstance(event.get('content'), dict) or not isinstance(event.get('event_id'), str):
        return None
    content = event['content']
    relation = content.get('m.relates_to')
    if (event.get('type') != 'm.room.message' or event.get('sender') != owner
            or not EVENT.fullmatch(event.get('event_id', '')) or 'state_key' in event
            or isinstance(relation, dict) and relation.get('rel_type') == 'm.replace'
            or not isinstance(event.get('origin_server_ts'), (int, float))
            or event['origin_server_ts'] < since
            or content.get('com.jkali.from_me') is True and not all_messages
            or content.get('msgtype') not in ('m.text', 'm.notice', 'm.image', 'm.video', 'm.audio', 'm.file', 'm.location')):
        return None
    return event['event_id']


class Notifications:
    def __init__(self, directory, matrix, roster, push=None, clock=time.time, canonical=None):
        self.canonical = canonical
        self.directory = Path(directory)
        self.matrix, self.roster, self.clock = matrix, roster, clock
        self.push = push or self._push
        self.lock = threading.RLock()
        self.poll_lock = threading.Lock()
        self.stop = threading.Event()
        self.error = None
        self.last_poll = None
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.db = sqlite3.connect(str(self.directory / 'notifications.db'), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        os.chmod(self.directory / 'notifications.db', 0o600)
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS follows(room TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
            cursor TEXT NOT NULL, started INTEGER NOT NULL, all_messages INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY, subscription TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
          CREATE TABLE IF NOT EXISTS history(id TEXT PRIMARY KEY, room TEXT NOT NULL, event TEXT NOT NULL,
            title TEXT NOT NULL, created INTEGER NOT NULL, seen INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS deliveries(id TEXT NOT NULL, device TEXT NOT NULL, state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0, PRIMARY KEY(id,device));
        ''')
        # Additive migration: preserve subscriptions, cursors and notification IDs.
        for table, column, definition in [
                ('history', 'kind', "TEXT NOT NULL DEFAULT 'message'"),
                ('history', 'rule_refs', 'TEXT'),
                ('deliveries', 'receipt_token', 'TEXT'),
                ('deliveries', 'receipt_issued_at', 'INTEGER'),
                ('deliveries', 'shown_at', 'INTEGER'),
                ('deliveries', 'clicked_at', 'INTEGER')]:
            if column not in {r[1] for r in self.db.execute('PRAGMA table_info(' + table + ')')}:
                self.db.execute('ALTER TABLE ' + table + ' ADD COLUMN ' + column + ' ' + definition)
        self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS receipt_lookup ON deliveries(receipt_token)')
        self.db.execute('CREATE TABLE IF NOT EXISTS clients(id TEXT PRIMARY KEY,stage TEXT,permission TEXT,error_code TEXT,updated INTEGER)')
        self.db.execute('CREATE TABLE IF NOT EXISTS control_metadata(key TEXT PRIMARY KEY,value TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS receipt_groups(token TEXT PRIMARY KEY,device TEXT NOT NULL,members TEXT NOT NULL,created INTEGER NOT NULL)')
        self.db.execute("UPDATE deliveries SET state='unknown' WHERE state='sending'")
        if not self.db.execute("SELECT 1 FROM control_metadata WHERE key='delivery_claim_version'").fetchone():
            # Legacy retries did not distinguish rejection from a lost response.
            self.db.execute("UPDATE deliveries SET state='unknown' WHERE state='pending' AND attempts>0")
            self.db.execute("INSERT INTO control_metadata VALUES('delivery_claim_version','1')")
        self.db.commit()

    def _helper(self, data):
        docker = shutil.which('docker') or '/Applications/Docker.app/Contents/Resources/bin/docker'
        payload = dict(data, key='/state/vapid.pem')
        result = subprocess.run([docker, 'run', '--rm', '-i', '--user', str(os.getuid()) + ':' + str(os.getgid()),
                                 '--cap-drop=ALL', '--security-opt=no-new-privileges', '--read-only',
                                 '--mount', 'type=bind,src=' + str(self.directory.resolve()) + ',dst=/state',
                                 'beepa-notification-push:local'], input=json.dumps(payload),
                                text=True, capture_output=True, timeout=35)
        if result.returncode:
            raise RuntimeError('Browser push service is unavailable')
        return json.loads(result.stdout)

    def public_key(self):
        with self.lock:
            return self._helper({'action': 'key', 'key': str(self.directory / 'vapid.pem')})['public_key']

    def _push(self, subscription, payload):
        return self._helper({'action': 'send', 'key': str(self.directory / 'vapid.pem'),
                             'subscription': subscription, 'payload': payload})['status']

    def verified(self, room):
        if not isinstance(room, str) or not ROOM.fullmatch(room):
            raise ValueError('Choose a shared conversation')
        state = self.matrix('/rooms/' + quote(room, safe='') + '/state')
        if not isinstance(state, list):
            raise ValueError('Conversation unavailable')
        def find(events, kind, key=''):
            return next((e for e in events if e.get('type') == kind and e.get('state_key') == key), {})
        create = find(state, 'm.room.create')
        owner = create.get('sender')
        space_id = self.roster().get(owner)
        if not space_id or not ROOM.fullmatch(create.get('content', {}).get('com.jkali.mirror_of', '')):
            raise ValueError('Conversation is not from a managed account')
        space = self.matrix('/rooms/' + quote(space_id, safe='') + '/state')
        if (find(space, 'm.room.create').get('sender') != owner
                or not find(space, 'm.space.child', room).get('content')
                or find(space, 'm.space.child', room).get('sender') != owner):
            raise ValueError('Conversation is no longer shared')
        level = find(state, 'com.jkali.share_level')
        if level.get('sender') != owner or level.get('content', {}).get('level') not in ('share', 'direct'):
            raise ValueError('Conversation is no longer shared')
        name = find(state, 'm.room.name')
        title = name.get('content', {}).get('name', 'Conversation') if name.get('sender') == owner else 'Conversation'
        return owner, str(title)[:120]

    def snapshot(self, notification_id=None):
        with self.lock:
            query = 'SELECT * FROM history'
            args = ()
            if notification_id is not None:
                query += ' WHERE id=?'
                args = (notification_id,)
            history = [dict(r) for r in self.db.execute(query + ' ORDER BY created DESC,rowid DESC LIMIT 100', args)]
            for item in history:
                item['deliveries'] = [dict(r) for r in self.db.execute(
                    'SELECT device,state,attempts,shown_at,clicked_at FROM deliveries WHERE id=?', (item['id'],))]
            return {'follows': [dict(r) for r in self.db.execute('SELECT room,owner,title,all_messages FROM follows')],
                    'history': history,
                    'devices': self.db.execute('SELECT count(*) FROM devices WHERE enabled=1').fetchone()[0],
                    'device_list': [dict(r) for r in self.db.execute('SELECT id,enabled FROM devices')],
                    'clients': [dict(r) for r in self.db.execute('SELECT * FROM clients ORDER BY updated DESC LIMIT 20')],
                    'last_poll': self.last_poll, 'error': self.error}

    def enqueue(self, nid, room, event, title, kind='message', device=None, rule_refs=None):
        # Caller owns lock and transaction. An idempotent retry never re-sends.
        inserted = self.db.execute('INSERT OR IGNORE INTO history(id,room,event,title,created,seen,kind,rule_refs) VALUES(?,?,?,?,?,0,?,?)',
                                   (nid, room, event, title, int(self.clock()*1000), kind, json.dumps(rule_refs) if rule_refs is not None else None)).rowcount
        if inserted:
            devices = self.db.execute('SELECT id FROM devices WHERE enabled=1' + (' AND id=?' if device else ''),
                                      (device,) if device else ()).fetchall()
            for row in devices:
                self.db.execute("INSERT INTO deliveries(id,device,state,receipt_token) VALUES(?,?,'pending',?)",
                                (nid, row[0], secrets.token_urlsafe(32)))
        return bool(inserted)

    def receipt(self, data):
        # A capability acknowledges one delivery or a frozen group on one device.
        # It cannot add members or authorize source reads/writes.
        token, stage = data.get('token'), data.get('stage')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token) or stage not in ('shown', 'clicked'):
            raise ValueError('Invalid notification receipt')
        with self.lock, self.db:
            group = self.db.execute('SELECT g.* FROM receipt_groups g JOIN devices s ON s.id=g.device WHERE g.token=? AND s.enabled=1 AND g.created>=?', (token, int((self.clock()-86400)*1000))).fetchone()
            if group:
                column = 'shown_at' if stage == 'shown' else 'clicked_at'
                for member in json.loads(group['members']):
                    self.db.execute("UPDATE deliveries SET state='accepted', " + column + '=COALESCE(' + column + ",?) WHERE id=? AND device=? AND state IN ('pending','sending','unknown','accepted')", (int(self.clock()*1000), member, group['device']))
                return {'recorded': True}
            row = self.db.execute("""SELECT d.id,d.device FROM deliveries d JOIN history h ON h.id=d.id
                JOIN devices s ON s.id=d.device WHERE d.receipt_token=? AND s.enabled=1
                AND d.state IN ('pending','sending','unknown','accepted') AND COALESCE(d.receipt_issued_at,h.created)>=?""", (token, int((self.clock()-86400)*1000))).fetchone()
            if not row:
                raise ValueError('Invalid or expired notification receipt')
            column = 'shown_at' if stage == 'shown' else 'clicked_at'
            self.db.execute("UPDATE deliveries SET state='accepted', " + column + '=COALESCE(' + column + ',?) WHERE id=? AND device=?',
                            (int(self.clock()*1000), row['id'], row['device']))
        return {'recorded': True}

    def control_outcomes(self, ids):
        """Delivery evidence only; never settings, subscriptions or source text."""
        if (not isinstance(ids, list) or len(ids) > 100
                or any(not isinstance(nid, str) or not re.fullmatch(r'[a-f0-9]{64}', nid) for nid in ids) or len(set(ids)) != len(ids)):
            raise ValueError('Invalid notification IDs')
        with self.lock:
            outcomes = []
            for nid in ids:
                if not self.db.execute('SELECT 1 FROM history WHERE id=?', (nid,)).fetchone():
                    outcomes.append({'id': nid, 'state': 'missing', 'deliveries': []})
                    continue
                rows = [dict(r) for r in self.db.execute('''SELECT d.device,d.state,d.attempts,d.shown_at,d.clicked_at,s.enabled
                    FROM deliveries d JOIN devices s ON s.id=d.device WHERE d.id=?''', (nid,))]
                states = {r['state'] for r in rows}
                state = ('accepted' if 'accepted' in states else 'unknown' if 'unknown' in states else
                         'in_flight' if 'sending' in states else
                         'pending' if any(r['state'] == 'pending' and r['enabled'] for r in rows) else
                         'failed' if 'failed' in states else 'expired' if 'expired' in states else
                         'canceled' if 'canceled' in states else 'unavailable')
                outcomes.append({'id': nid, 'state': state, 'deliveries': [{k: v for k, v in row.items() if k != 'enabled'} for row in rows]})
            return {'devices': self.db.execute('SELECT count(*) FROM devices WHERE enabled=1').fetchone()[0], 'outcomes': outcomes}

    def control_enqueue(self, entries):
        """Idempotent adapter for the control router; delivery still checks lists.

        Do not call back to the controller here: it may be waiting for this
        acknowledgment while holding its domain execution lock.
        """
        if not self.canonical:
            raise ValueError('Canonical list control is required')
        if not isinstance(entries, list) or not 1 <= len(entries) <= 50:
            raise ValueError('Invalid notification batch')
        prepared = []
        for entry in entries:
            contact = isinstance(entry, dict) and entry.get('kind') == 'contact_reminder'
            if not isinstance(entry, dict) or set(entry) != ({'kind', 'contact', 'refs'} if contact else {'room', 'event', 'kind', 'refs'}):
                raise ValueError('Invalid notification entry')
            room, event, kind, refs = entry.get('room', ''), entry.get('event', ''), entry['kind'], entry['refs']
            if not contact and (kind not in ('message', 'reminder') or not isinstance(event, str) or not (re.fullmatch(r'\$[^\s]{1,254}', event) or kind == 'reminder' and event == 'uncontacted')):
                raise ValueError('Invalid notification event')
            fields = {'outgoing', 'rules'} if kind == 'message' else {'due', 'slot', 'rules'}
            if kind == 'reminder' and isinstance(refs, dict) and 'episode' in refs:
                fields.add('episode')
            if not isinstance(refs, dict) or set(refs) != fields:
                raise ValueError('Invalid notification policy references')
            if kind == 'message' and type(refs['outgoing']) is not bool:
                raise ValueError('Invalid notification direction')
            if kind != 'message' and any(type(refs[k]) is not int or not 0 < refs[k] <= 9007199254740991 for k in ('due', 'slot')):
                raise ValueError('Invalid reminder identity')
            if 'episode' in refs and (type(refs['episode']) is not int or not 0 < refs['episode'] <= 9007199254740991):
                raise ValueError('Invalid reminder episode')
            rules = refs['rules']
            if not isinstance(rules, list) or not 1 <= len(rules) <= 50:
                raise ValueError('Invalid policy references')
            for rule in rules:
                if (not isinstance(rule, dict) or set(rule) != ({'id', 'revision'} if kind == 'message' else {'id', 'revision', 'slot'})
                        or not isinstance(rule['id'], str) or not 1 <= len(rule['id']) <= 256
                        or type(rule['revision']) is not int or not 0 <= rule['revision'] <= 9007199254740991
                        or kind != 'message' and (type(rule['slot']) is not int or not refs['due'] <= rule['slot'] <= refs['slot'])):
                    raise ValueError('Invalid policy reference')
            if len({r['id'] for r in rules}) != len(rules):
                raise ValueError('Duplicate policy reference')
            if contact:
                nid = contact_identity(entry['contact'])
                if any(refs[k] != entry['contact'][k] for k in ('due', 'slot')):
                    raise ValueError('Contact reminder deadline changed')
                prepared.append((nid, '', '', 'Contact-by reminder', kind, dict(refs, contact=entry['contact'])))
                continue
            owner, title = self.verified(room)
            if event != 'uncontacted':
                source = self.matrix('/rooms/' + quote(room, safe='') + '/event/' + quote(event, safe=''))
                if notification_event(source, owner, 0, True) != event or kind == 'message' and (source['content'].get('com.jkali.from_me') is True) != refs['outgoing']:
                    raise ValueError('Notification source event changed')
            nid = hashlib.sha256((room+'\0'+event).encode()).hexdigest() if kind == 'message' else reminder_identity(room, event, refs['due'], refs['slot'], refs.get('episode'))
            prepared.append((nid, room, event, title+' · '+owner, kind, refs))
        if len({item[0] for item in prepared}) != len(prepared):
            raise ValueError('Duplicate notification entry')
        with self.lock, self.db:
            for nid, room, event, title, kind, refs in prepared:
                self.enqueue(nid, room, event, title, kind=kind, rule_refs=refs)
        return self.control_outcomes([item[0] for item in prepared])

    def dispatcher_handoff(self):
        """Freeze legacy discovery and return its durable positions atomically.

        Never acquire poll_lock or call the controller: a polling thread may be
        waiting for that controller while it requests this handoff. poll_room
        checks the same marker under self.lock before any enqueue/cursor write.
        """
        if not self.canonical:
            raise ValueError('Canonical list control is required')
        with self.lock, self.db:
            row = self.db.execute("SELECT value FROM control_metadata WHERE key='dispatcher_handoff'").fetchone()
            if row:
                return json.loads(row[0])
            handoff = {'version': 1, 'owner': 'control', 'id': secrets.token_hex(16),
                       'follows': [dict(r) for r in self.db.execute('SELECT room,owner,cursor,started FROM follows ORDER BY room')]}
            self.db.execute("INSERT INTO control_metadata VALUES('dispatcher_handoff',?)", (json.dumps(handoff),))
            return handoff

    def dispatcher_owned(self):
        return bool(self.db.execute("SELECT 1 FROM control_metadata WHERE key='dispatcher_handoff'").fetchone())

    def handle(self, data):
        action = data.get('action')
        if action == 'control_outcomes':
            return self.control_outcomes(data.get('ids', []))
        if action == 'control_enqueue':
            return self.control_enqueue(data.get('entries'))
        if action == 'control_dispatcher':
            return self.dispatcher_handoff()
        if self.canonical and action in ('follow', 'unfollow'):
            self.verified(data.get('room'))
            result = self.canonical(data)
            self.sync_canonical()
            return result
        if action in ('status', 'delivery'):
            if action == 'delivery':
                nid = data.get('id')
                if not isinstance(nid, str) or not re.fullmatch(r'[a-f0-9]{64}', nid):
                    raise ValueError('Invalid notification ID')
                result = self.snapshot(nid)['history']
                if not result:
                    raise ValueError('Notification not found')
                return result[0]
            return self.snapshot()
        if action == 'client_status':
            client, stage, permission, error = (data.get(k, '') for k in ('client','stage','permission','error_code'))
            if (not isinstance(client,str) or not re.fullmatch(r'[a-f0-9-]{36}',client)
                    or stage not in ('permission','worker','key','subscription','connected','disabled','error')
                    or permission not in ('default','granted','denied','unsupported')
                    or not isinstance(error,str) or not re.fullmatch(r'[A-Za-z0-9_]{0,64}',error)):
                raise ValueError('Invalid browser diagnostic')
            with self.lock, self.db:
                self.db.execute('INSERT OR REPLACE INTO clients VALUES(?,?,?,?,?)', (client,stage,permission,error,int(self.clock()*1000)))
            return {'recorded':True}
        if action == 'sync':
            self.poll_once()
            return self.snapshot()
        if action == 'key':
            return {'public_key': self.public_key()}
        if action == 'follow':
            room = data.get('room'); owner, title = self.verified(room)
            mode = data.get('all_messages', False)
            if not isinstance(mode, bool):
                raise ValueError('Invalid notification mode')
            page = self.matrix('/rooms/' + quote(room, safe='') + '/messages?dir=b&limit=1')
            cursor = page.get('start')
            if not isinstance(cursor, str) or not cursor:
                raise ValueError('Conversation has no current sync position')
            with self.lock, self.db:
                self.db.execute('INSERT INTO follows VALUES(?,?,?,?,?,?) ON CONFLICT(room) DO UPDATE SET all_messages=excluded.all_messages',
                                (room, owner, title, cursor, int(self.clock()*1000), int(mode)))
            return {'following': True}
        if action == 'unfollow':
            with self.lock, self.db:
                self.db.execute('DELETE FROM follows WHERE room=?', (data.get('room'),))
                self.db.execute("UPDATE deliveries SET state='canceled' WHERE state='pending' AND id IN (SELECT id FROM history WHERE room=?)", (data.get('room'),))
            return {'following': False}
        if action == 'subscribe':
            sub = valid_subscription(data.get('subscription')); device = hashlib.sha256(sub['endpoint'].encode()).hexdigest()
            with self.lock, self.db:
                self.db.execute('INSERT OR REPLACE INTO devices VALUES(?,?,1)', (device, json.dumps(sub)))
            return {'device': device}
        if action == 'unsubscribe':
            with self.lock, self.db:
                self.db.execute('UPDATE devices SET enabled=0 WHERE id=?', (data.get('device'),))
                self.db.execute("UPDATE deliveries SET state='canceled' WHERE device=? AND state='pending'", (data.get('device'),))
            return {'disabled': True}
        if action == 'test':
            request_id = data.get('request_id') or secrets.token_hex(16)
            if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', request_id):
                raise ValueError('Invalid test request ID')
            device = data.get('device')
            if device is not None and (not isinstance(device, str) or not re.fullmatch(r'[a-f0-9]{64}', device)):
                raise ValueError('Invalid device ID')
            nid = hashlib.sha256(('test\0' + request_id + '\0' + (device or '*')).encode()).hexdigest()
            with self.lock, self.db:
                if not self.db.execute('SELECT 1 FROM devices WHERE enabled=1' + (' AND id=?' if device else ''),
                                       (device,) if device else ()).fetchone():
                    raise ValueError('No connected browser. Enable browser notifications once, then retry.')
                self.enqueue(nid, '', '', 'Beepa notification test', 'test', device)
            return {'notification_id': nid, 'queued': True}
        if action == 'seen':
            with self.lock, self.db:
                self.db.execute('UPDATE history SET seen=1 WHERE id=?', (data.get('id'),))
            return {'seen': True}
        raise ValueError('Unknown notification action')

    def sync_canonical(self):
        if not self.canonical:
            return
        with self.lock:
            migrated = self.db.execute("SELECT 1 FROM control_metadata WHERE key='lists_migrated'").fetchone()
            prior = [dict(r) for r in self.db.execute('SELECT * FROM follows')]
        if not migrated:
            self.canonical({'action': 'migrate_follows', 'follows': prior})
            with self.lock, self.db:
                self.db.execute("INSERT OR REPLACE INTO control_metadata VALUES('lists_migrated','1')")
        rules = self.canonical({'action': 'rules'})['rules']
        self.canonical_rules = rules
        wanted = {}
        for rule in rules:
            if 'browser' not in rule['channels']:
                continue
            room = rule['room']
            wanted[room] = {'owner': rule['owner'], 'title': rule['title'],
                            'all_messages': bool(rule['all_messages']) or wanted.get(room, {}).get('all_messages', False)}
        for room, rule in wanted.items():
            owner, title = self.verified(room)
            if owner != rule['owner']:
                raise ValueError('List conversation owner changed')
            with self.lock:
                exists = self.db.execute('SELECT all_messages FROM follows WHERE room=?', (room,)).fetchone()
            if not exists:
                page = self.matrix('/rooms/' + quote(room, safe='') + '/messages?dir=b&limit=1')
                cursor = page.get('start')
                if not isinstance(cursor, str) or not cursor:
                    raise ValueError('Conversation has no current sync position')
                with self.lock, self.db:
                    self.db.execute('INSERT OR IGNORE INTO follows VALUES(?,?,?,?,?,?)',
                                    (room, owner, title, cursor, int(self.clock()*1000), int(rule['all_messages'])))
            with self.lock, self.db:
                if exists and bool(exists['all_messages']) != rule['all_messages']:
                    self.db.execute("UPDATE deliveries SET state='canceled' WHERE state='pending' AND id IN (SELECT id FROM history WHERE room=?)", (room,))
                self.db.execute('UPDATE follows SET all_messages=?,title=? WHERE room=?', (int(rule['all_messages']), title, room))
        with self.lock, self.db:
            for prior_row in prior:
                room = prior_row['room']
                if room not in wanted:
                    self.db.execute('DELETE FROM follows WHERE room=?', (room,))
                    self.db.execute("UPDATE deliveries SET state='canceled' WHERE state='pending' AND id IN (SELECT id FROM history WHERE room=?)", (room,))

    def poll_once(self):
        # API-triggered sync and background polling share one consumer/outbox.
        with self.poll_lock:
            self.sync_canonical()
            with self.lock:
                follows = [] if self.dispatcher_owned() else [dict(r) for r in self.db.execute('SELECT * FROM follows')]
            failed = False
            self.caught_up = set()
            for follow in follows:
                try:
                    self.poll_room(follow)
                except ValueError:
                    # Revocation clears only the derived delivery cursor. It
                    # cannot rewrite the user's canonical list preference.
                    if self.canonical:
                        with self.lock, self.db:
                            self.db.execute('DELETE FROM follows WHERE room=?', (follow['room'],))
                            self.db.execute("UPDATE deliveries SET state='canceled' WHERE state='pending' AND id IN (SELECT id FROM history WHERE room=?)", (follow['room'],))
                    else:
                        self.handle({'action':'unfollow', 'room':follow['room']})
                except Exception:
                    failed = True  # One unavailable account must not stall others.
            if self.canonical:
                self.sync_deadlines()
            self.deliver()
            self.last_poll = int(self.clock()*1000)
            self.error = 'Some followed conversations are unavailable; retrying.' if failed else None

    def poll_room(self, follow):
        room = follow['room']
        owner, title = self.verified(room)
        if owner != follow['owner']:
            raise ValueError('Conversation owner changed')
        page = self.matrix('/rooms/' + quote(room, safe='') + '/messages?dir=f&limit=100&from=' + quote(follow['cursor'], safe=''))
        if len(page.get('chunk', [])) < 100:
            getattr(self, 'caught_up', set()).add(room)
        with self.lock, self.db:
            if self.dispatcher_owned():
                return
            current = self.db.execute('SELECT * FROM follows WHERE room=?', (room,)).fetchone()
            if not current or any(current[key] != follow[key] for key in ('cursor', 'started', 'all_messages')):
                return
            for event in page.get('chunk', []):
                eid = notification_event(event, owner, follow['started'], bool(follow['all_messages']))
                if eid:
                    refs = None
                    if self.canonical:
                        outgoing = event['content'].get('com.jkali.from_me') is True
                        policies = [r for r in self.canonical_rules if r['room'] == room and 'browser' in r['channels']
                                    and r.get('activity_enabled') is not False and (not outgoing or r['all_messages'])]
                        if not policies:
                            continue
                        refs = {'outgoing': outgoing, 'rules': [{'id': r['list_id'], 'revision': r.get('revision')} for r in policies]}
                    self.enqueue(hashlib.sha256((room+'\0'+eid).encode()).hexdigest(), room, eid, title, rule_refs=refs)
            if isinstance(page.get('end'), str):
                self.db.execute('UPDATE follows SET cursor=? WHERE room=?', (page['end'], room))

    def sync_deadlines(self):
        updates = self.canonical({'action': 'updates'})['updates']
        self.reminder_updates = updates
        rules = {(r['list_id'], r['room']): r for r in self.canonical_rules if 'browser' in r['channels'] and r.get('reminders_enabled') is not False}
        candidates = {}
        with self.lock, self.db:
            owned = self.dispatcher_owned()
            for item in updates:
                if ((item['list_id'], item['id']) not in rules or not owned and item['id'] not in self.caught_up
                        or not item.get('fresh')):
                    continue
                for obligation in item.get('obligations', []):
                    due, event = obligation.get('due_at'), obligation.get('event_id')
                    if (not due or not event or due > self.clock()*1000
                            or (obligation.get('snooze_until') or 0) > self.clock()*1000
                            or obligation.get('waiting') not in ('waiting_for_us', 'waiting_for_them', 'uncontacted')):
                        continue
                    rule = rules[(item['list_id'], item['id'])]
                    slot = reminder_slot(due, rule.get('repeat_ms'), self.clock()*1000)
                    if slot is None:
                        continue
                    episode = obligation.get('episode_at')
                    key = (item['id'], event, due, episode)
                    candidate = candidates.setdefault(key, {'item': item, 'event': event, 'open_rules': [], 'refs': {'due': due, 'slot': slot, **({'episode': episode} if episode is not None else {}), 'rules': []}})
                    rule_ref = {'id': rule['list_id'], 'revision': rule.get('revision'), 'slot': slot}
                    candidate['refs']['rules'].append(rule_ref)
                    if monitoring_allows(rule.get('schedule'), self.clock()*1000):
                        candidate['open_rules'].append(rule_ref)
            for (room, event, due, episode), candidate in candidates.items():
                candidate['refs']['rules'] = candidate['open_rules'] or candidate['refs']['rules']
                slot = max(r['slot'] for r in candidate['refs']['rules'])
                candidate['refs']['slot'] = slot
                nid = reminder_identity(room, event, due, slot, episode)
                item = candidate['item']
                if not owned:
                    self.enqueue(nid, item['id'], candidate['event'], item['title'] + ' · ' + item['owner'], kind='reminder', rule_refs=candidate['refs'])
            for row in self.db.execute("SELECT * FROM history WHERE kind='reminder'").fetchall():
                try:
                    allowed = self.delivery_policies(dict(row))
                except VerificationPending:
                    continue
                if not allowed:
                    self.db.execute("UPDATE deliveries SET state='canceled' WHERE id=? AND state='pending'", (row['id'],))

    def delivery_policies(self, job):
        if not self.canonical or job['kind'] == 'test':
            return None
        ref = json.loads(job['rule_refs']) if job['rule_refs'] else None
        if job['kind'] == 'contact_reminder':
            source = ref['contact']
            if contact_identity(source) != job['id']:
                raise ValueError('Contact reminder identity changed')
            # This callback is only in delivery, never the synchronous enqueue
            # acknowledgment. The controller checks live contact access, native
            # completion and the current snooze/deadline episode.
            validation = self.canonical({'action': 'contact_notification', 'source': source})
            def matching(r):
                return 'browser' in r['channels'] and any(old['id'] == r['list_id'] and old['revision'] == r['revision'] and old['slot'] == r['delivery_slot'] for old in ref['rules'])
            rules = [r for r in validation['rules'] if matching(r)]
            if not rules and any(matching(r) for r in validation.get('deferred_rules', [])):
                raise VerificationPending()
            return rules
        policies = [r for r in getattr(self, 'canonical_rules', []) if r['room'] == job['room'] and 'browser' in r['channels']
                    and (not ref or any(old['id'] == r['list_id'] and old.get('revision') == r.get('revision') for old in ref['rules']))
                    and (r.get('reminders_enabled') is not False if job['kind'] == 'reminder' else
                         r.get('activity_enabled') is not False and (not ref or not ref.get('outgoing') or r['all_messages']))]
        if job['kind'] == 'reminder' and ref:
            policies = [r for r in policies if any(old['id'] == r['list_id'] and reminder_slot(ref['due'], r.get('repeat_ms'), self.clock()*1000) == old.get('slot', ref['slot']) for old in ref['rules'])]
        if job['kind'] == 'reminder' and hasattr(self, 'reminder_updates'):
            ready, deferred = [], False
            for rule in policies:
                item = next((u for u in self.reminder_updates if u['list_id'] == rule['list_id'] and u['id'] == job['room']), None)
                if not item:
                    continue
                if not item.get('fresh') or not self.dispatcher_owned() and item['id'] not in self.caught_up:
                    deferred = True
                    continue
                for obligation in item.get('obligations', []):
                    due = obligation.get('due_at')
                    slot = reminder_slot(due, rule.get('repeat_ms'), self.clock()*1000) if due else None
                    if (obligation.get('event_id') == job['event'] and slot is not None
                            and obligation.get('waiting') in ('waiting_for_us', 'waiting_for_them', 'uncontacted')
                            and not (obligation.get('snooze_until') or 0) > self.clock()*1000
                            and (not ref and reminder_identity(job['room'], job['event'], due, slot, obligation.get('episode_at')) == job['id']
                                 or ref and due == ref['due'] and obligation.get('episode_at') == ref.get('episode'))):
                        ready.append(rule)
                        break
            if not ready and deferred:
                raise VerificationPending()
            return ready
        return policies

    @staticmethod
    def push_outcome(status, attempts):
        if type(status) is not int or not 200 <= status <= 599:
            raise ValueError('Invalid push provider response')
        return ('accepted' if status < 300 else 'expired' if status in (404, 410) else
                'pending' if (status == 429 or status >= 500) and attempts < 4 else 'failed')

    @staticmethod
    def delivery_lifetime(policies):
        if any(r.get('schedule') for r in policies or []):
            return 8*86400000
        return max([3600000] + [(r.get('presentation') or {}).get('interval_ms', 0) + 3600000
                               for r in policies or [] if (r.get('presentation') or {}).get('interval_ms')])

    def deliver_group(self, first, jobs, plan):
        selected, rooms = [], {}
        for job in jobs:
            if len(selected) >= 50:
                break
            if job['device'] != first['device'] or job['kind'] != 'message':
                continue
            with self.lock:
                if not self.db.execute("SELECT 1 FROM deliveries WHERE id=? AND device=? AND state='pending' AND next_try<=?", (job['id'], job['device'], self.clock())).fetchone():
                    continue
            policies = self.delivery_policies(job) or []
            current = delivery_plan(policies, job['created'], self.clock()*1000, job['room'])
            if (not current or current['ready_at'] > self.clock()*1000 or current['key'] != plan['key']
                    or job['room'] not in rooms and len(rooms) >= 10
                    or self.clock()*1000-job['created'] > self.delivery_lifetime(policies)):
                continue
            try:
                owner, title = self.verified(job['room'])
            except ValueError:
                with self.lock, self.db:
                    self.db.execute("UPDATE deliveries SET state='canceled' WHERE id=? AND device=? AND state='pending'", (job['id'], job['device']))
                continue
            rooms[job['room']] = {'id': job['id'], 'title': title, 'owner': owner}
            selected.append(job)
        if not selected:
            return
        for job in selected:
            self.verified(job['room'])
            current = delivery_plan(self.delivery_policies(job) or [], job['created'], self.clock()*1000, job['room'])
            if not current or current['ready_at'] > self.clock()*1000 or current['key'] != plan['key']:
                return
        token = secrets.token_urlsafe(32)
        with self.lock, self.db:
            if not self.db.execute('SELECT 1 FROM devices WHERE id=? AND enabled=1', (first['device'],)).fetchone():
                return
            if any(not self.db.execute("SELECT 1 FROM deliveries WHERE id=? AND device=? AND state='pending'", (job['id'], job['device'])).fetchone() for job in selected):
                return
            self.db.execute('DELETE FROM receipt_groups WHERE created<?', (int((self.clock()-172800)*1000),))
            self.db.execute('INSERT INTO receipt_groups VALUES(?,?,?,?)', (token, first['device'], json.dumps([job['id'] for job in selected]), int(self.clock()*1000)))
            for job in selected:
                self.db.execute("UPDATE deliveries SET state='sending',attempts=attempts+1 WHERE id=? AND device=?", (job['id'], job['device']))
        ids = [r['id'] for r in rooms.values()]
        payload = {'title': 'Beepa · '+str(len(selected))+' messages in '+str(len(rooms))+' conversation'+('s' if len(rooms) != 1 else ''),
                   'body': '; '.join((r['title']+' · '+r['owner'])[:100] for r in rooms.values())[:300],
                   'tag': 'beepa-group-'+hashlib.sha256(json.dumps([job['id'] for job in selected]).encode()).hexdigest()[:24],
                   'url': 'index.html#notification-group='+','.join(ids), 'id': selected[0]['id'], 'receipt_token': token}
        try:
            status = self.push(json.loads(first['subscription']), payload)
            state = self.push_outcome(status, max(job['attempts'] for job in selected))
            if status in (404, 410):
                with self.lock, self.db:
                    self.db.execute('UPDATE devices SET enabled=0 WHERE id=?', (first['device'],))
        except Exception:
            state = 'unknown'  # A lost network response must not replay the group.
        with self.lock, self.db:
            for job in selected:
                self.db.execute("UPDATE deliveries SET state=CASE WHEN shown_at IS NOT NULL OR clicked_at IS NOT NULL THEN 'accepted' ELSE ? END,next_try=? WHERE id=? AND device=? AND state IN ('sending','accepted')",
                                (state, self.clock()+60, job['id'], job['device']))

    def deliver(self):
        with self.lock:
            jobs = [dict(r) for r in self.db.execute("""SELECT d.*,h.room,h.event,h.title,h.created,h.kind,h.rule_refs,s.subscription FROM deliveries d
              JOIN history h ON h.id=d.id JOIN devices s ON s.id=d.device
              WHERE d.state='pending' AND s.enabled=1 AND d.next_try<=? ORDER BY d.next_try,h.created LIMIT 500""", (self.clock(),))]
        if self.canonical and any(job['kind'] != 'test' for job in jobs):
            # Re-read the canonical policy at delivery, including direct retry
            # calls. A failed read leaves jobs pending; cached rules cannot grant
            # permission after a list/account change.
            self.canonical_rules = self.canonical({'action': 'rules'})['rules']
            if any(job['kind'] == 'reminder' for job in jobs):
                self.sync_deadlines()
        for job in jobs:
            with self.lock:
                if not self.db.execute("SELECT 1 FROM deliveries WHERE id=? AND device=? AND state='pending' AND next_try<=?", (job['id'], job['device'], self.clock())).fetchone():
                    continue
            attempted = False
            try:
                policies = self.delivery_policies(job)
            except VerificationPending:
                continue
            if self.canonical and job['kind'] != 'test':
                if not policies:
                    with self.lock, self.db:
                        self.db.execute("UPDATE deliveries SET state='canceled' WHERE id=? AND device=? AND state='pending'", (job['id'], job['device']))
                    continue
                open_policies = [r for r in policies if monitoring_allows(r.get('schedule'), self.clock()*1000)]
                if not open_policies:
                    with self.lock, self.db:
                        self.db.execute('UPDATE deliveries SET next_try=? WHERE id=? AND device=?', (self.clock()+60, job['id'], job['device']))
                    continue
                policies = open_policies
            # A weekly window can defer a weekend alert. Keep the established
            # one-hour expiry for ordinary retries without an explicit schedule.
            lifetime = self.delivery_lifetime(policies)
            # A first reminder may wait for source verification longer than the
            # provider retry lifetime. Canonical validation still gates delivery.
            unattempted_reminder = job['kind'] in ('reminder', 'contact_reminder') and job['attempts'] == 0
            if self.clock()*1000-job['created'] > lifetime and not unattempted_reminder:
                state = 'expired'
            else:
                try:
                    if job['kind'] == 'message' and policies:
                        plan = delivery_plan(policies, job['created'], self.clock()*1000, job['room'])
                        if not plan or plan['ready_at'] > self.clock()*1000:
                            with self.lock, self.db:
                                self.db.execute('UPDATE deliveries SET next_try=? WHERE id=? AND device=?', (plan['ready_at']/1000 if plan else self.clock()+60, job['id'], job['device']))
                            continue
                        if plan['mode'] != 'immediate':
                            self.deliver_group(job, jobs, plan)
                            continue
                        policies = [r for r in policies if (delivery_plan([r], job['created'], self.clock()*1000, job['room']) or {}).get('ready_at', float('inf')) <= self.clock()*1000]
                    if job['kind'] == 'contact_reminder':
                        policies = [r for r in self.delivery_policies(job) if monitoring_allows(r.get('schedule'), self.clock()*1000)]
                        if not policies:
                            with self.lock, self.db:
                                self.db.execute("UPDATE deliveries SET state='canceled' WHERE id=? AND device=? AND state='pending'", (job['id'], job['device']))
                            continue
                        job['title'] = policies[0]['title'] + ' · ' + policies[0]['owner']
                    elif job['kind'] != 'test':
                        self.verified(job['room'])
                    with self.lock:
                        active = self.db.execute("SELECT 1 FROM deliveries d JOIN devices s ON s.id=d.device WHERE d.id=? AND d.device=? AND d.state='pending' AND s.enabled=1", (job['id'], job['device'])).fetchone()
                        if not active or (job['kind'] not in ('test', 'contact_reminder') and not self.db.execute('SELECT 1 FROM follows WHERE room=?', (job['room'],)).fetchone()):
                            continue
                        # Older pending jobs acquire a capability during upgrade.
                        if not job['receipt_token']:
                            job['receipt_token'] = secrets.token_urlsafe(32)
                            with self.db:
                                self.db.execute('UPDATE deliveries SET receipt_token=? WHERE id=? AND device=?', (job['receipt_token'], job['id'], job['device']))
                        with self.db:
                            claimed = self.db.execute("UPDATE deliveries SET state='sending',attempts=attempts+1,receipt_issued_at=COALESCE(receipt_issued_at,?) WHERE id=? AND device=? AND state='pending'", (int(self.clock()*1000), job['id'], job['device'])).rowcount
                            if not claimed:
                                continue
                            attempted = True
                    payload = {'title': 'Beepa · ' + job['title'],
                               'body': 'This is a test notification.' if job['kind']=='test' else 'Your contact-by deadline has arrived.' if job['kind']=='contact_reminder' else 'An unanswered conversation reached your deadline.' if job['kind']=='reminder' else 'New activity in a conversation you follow.',
                               'tag': 'beepa-' + (job['id'] if job['kind'] in ('test', 'contact_reminder') else hashlib.sha256(job['room'].encode()).hexdigest())[:24],
                               'url': 'index.html#notification=' + job['id'], 'id': job['id'], 'receipt_token': job['receipt_token']}
                    if policies:
                        names = list(dict.fromkeys(r.get('list_name') or r['list_id'] for r in policies))
                        payload['body'] += ' Lists: ' + ', '.join(names)
                    status = self.push(json.loads(job['subscription']), payload)
                    state = self.push_outcome(status, job['attempts'])
                    if status in (404,410):
                        with self.lock, self.db:
                            self.db.execute('UPDATE devices SET enabled=0 WHERE id=?', (job['device'],))
                except ValueError:
                    state = 'unknown' if attempted else 'canceled'
                except Exception:
                    state = 'unknown' if attempted else 'pending'
            with self.lock, self.db:
                self.db.execute("UPDATE deliveries SET state=CASE WHEN shown_at IS NOT NULL OR clicked_at IS NOT NULL THEN 'accepted' ELSE ? END,next_try=? WHERE id=? AND device=? AND state IN ('pending','sending','accepted')",
                                (state, self.clock()+min(300, 5*2**min(job['attempts'],6)), job['id'], job['device']))

    def run(self):
        while not self.stop.is_set():
            try:
                self.poll_once()
            except Exception:
                self.error = 'Notification sync is retrying. Check account connectivity.'
            self.stop.wait(3)

    def start(self):
        threading.Thread(target=self.run, name='beepa-notifications', daemon=True).start()


def helper(data):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    keyfile = Path(data['key'])
    if data['action'] == 'key':
        if not keyfile.exists():
            key = ec.generate_private_key(ec.SECP256R1())
            raw = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
            fd = os.open(str(keyfile), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd,'wb') as stream: stream.write(raw)
        key = serialization.load_pem_private_key(keyfile.read_bytes(), password=None)
        public = key.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
        return {'public_key':base64.urlsafe_b64encode(public).decode().rstrip('=')}
    from pywebpush import webpush, WebPushException
    import requests
    class NoRedirectSession(requests.Session):
        def post(self, *args, **kwargs):
            kwargs['allow_redirects'] = False
            return super().post(*args, **kwargs)
    try:
        response = webpush(subscription_info=valid_subscription(data['subscription']), data=json.dumps(data['payload']),
                           vapid_private_key=str(keyfile), vapid_claims={'sub':'mailto:notifications@beepa.local'},
                           ttl=300, timeout=15, requests_session=NoRedirectSession())
        return {'status':response.status_code}
    except WebPushException as error:
        if error.response is None:
            raise RuntimeError('Push delivery outcome is unknown') from None
        return {'status': error.response.status_code}


if __name__ == '__main__' and sys.argv[1:] == ['--helper']:
    try:
        print(json.dumps(helper(json.load(sys.stdin))))
    except Exception:
        sys.exit(1)
