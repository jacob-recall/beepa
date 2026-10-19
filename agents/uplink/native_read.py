"""Own iMessage unread projection and narrow manager read/unread requests."""
import json
import re
import time
import urllib.parse

STATE = 'com.jkali.imessage_read_state'
REQUEST = 'com.jkali.imessage.read_request'
MASTER_REQUEST = 'com.jkali.read_request'
KEY = 'com.jkali.read_action'


def validated(c):
    if not isinstance(c, dict) or c.get('schema') != 1 or c.get('supported') is not True:
        return None
    if (type(c.get('unread_count')) is not int or not 0 <= c['unread_count'] <= 9007199254740991
            or type(c.get('marked_unread')) is not bool
            or not isinstance(c.get('revision'), str) or not re.fullmatch(r'[a-f0-9]{64}', c['revision'])
            or type(c.get('latest_ts')) is not int or not 0 <= c['latest_ts'] <= 8640000000000000
            or type(c.get('observed_ts')) is not int or c['observed_ts'] <= 0):
        return None
    out = {key: c[key] for key in ('schema', 'supported', 'unread_count', 'marked_unread', 'revision', 'latest_ts', 'observed_ts')}
    last = c.get('last_command')
    if (isinstance(last, dict) and isinstance(last.get('id'), str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', last['id'])
            and last.get('status') in ('applied', 'changed', 'expired', 'unconfirmed', 'attempted')):
        out['last_command'] = {'id': last['id'], 'status': last['status']}
    return out


def observe(u, room, master, section, state):
    """Persist native authority across incremental syncs with Matrix counters."""
    source = u.db.execute('SELECT source FROM mirror_rooms WHERE local_room_id=? AND master_room_id=?', (room, master)).fetchone()
    if source != ('imessage',):
        return state
    key = 'native_read:' + master
    old = u.meta_get(key)
    current = validated(json.loads(old)) if old else None
    events = ((section or {}).get('state') or {}).get('events', []) + ((section or {}).get('timeline') or {}).get('events', [])
    for e in events:
        if isinstance(e, dict) and e.get('type') == STATE and e.get('state_key') == '' and e.get('sender') == u.cfg.imessage_bot:
            candidate = validated(e.get('content'))
            if candidate and (not current or candidate['observed_ts'] >= current['observed_ts']):
                current = candidate
    if current:
        encoded = json.dumps(current, sort_keys=True)
        if encoded != old:
            u.meta_set(key, encoded)
        state['_native_changed'] = encoded != u.meta_get('native_read_delivered:' + master)
        state['unread_count'] = current['unread_count']
    return state


def forward(u, proposals_room, ev, suspended=True):
    """Only metadata enters an existing local portal, never m.room.message."""
    cfg = u.cfg
    c = ev.get('content') or {}
    action = c.get(KEY)
    control = getattr(u, '_crm_control', None)
    if (ev.get('type') != MASTER_REQUEST
            or suspended or not control or ev.get('sender') != cfg.manager_mxid or cfg.manager_mxid == cfg.master_user
            or proposals_room != u.meta_get('master_proposals_room') or ev.get('room_id', proposals_room) != proposals_room
            or 'state_key' in ev or not u.active_link_for_dispatch()):
        return
    if not isinstance(action, dict) or set(action) != {'schema', 'command_id', 'action', 'expected_revision', 'expires_at', 'generation', 'mirror_room'}:
        return
    room, eid = c.get('target_room'), ev.get('event_id')
    if (not isinstance(room, str) or not isinstance(eid, str) or not eid.startswith('$')
            or action.get('schema') != 1 or action.get('generation') != control.generation
            or action.get('action') not in ('read', 'unread')
            or not isinstance(action.get('command_id'), str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', action['command_id'])
            or not isinstance(action.get('expected_revision'), str) or not re.fullmatch(r'[a-f0-9]{64}', action['expected_revision'])):
        return
    now = int(time.time() * 1000)
    ts, expires = ev.get('origin_server_ts'), action.get('expires_at')
    if type(ts) is not int or not now - 120000 <= ts <= now + 10000 or type(expires) is not int or not now < expires <= now + 120000:
        return
    row = u.db.execute('SELECT master_room_id,source FROM mirror_rooms WHERE local_room_id=?', (room,)).fetchone()
    if row != (action.get('mirror_room'), 'imessage') or u.mirror_status(room) == 'revoking' or u.archive_level(room) not in ('share', 'direct'):
        return
    key = 'native_read_forwarded:' + eid
    if u.meta_get(key):
        return
    # Re-read native capability as the local owner; bot author is checked by
    # the remembered state projection. The daemon independently validates the
    # portal and revision immediately before invoking the native command.
    observed = u.meta_get('native_read:' + row[0])
    if not observed or not validated(json.loads(observed)):
        return
    payload = {k: action[k] for k in ('schema', 'command_id', 'action', 'expected_revision', 'expires_at')}
    u.local('PUT', '/_matrix/client/v3/rooms/' + urllib.parse.quote(room, safe='') + '/send/' + REQUEST +
            '/native_read_' + urllib.parse.quote(eid, safe=''), payload, timeout=15)
    u.meta_set(key, 'forwarded')
