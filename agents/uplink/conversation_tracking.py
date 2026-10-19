"""ID-only, source-ordered obligations for every shared conversation.

This is derived evidence, not another watch list or a sender. CRM lists remain
the sole authority for deadlines and alerts. Unknown order/identity is explicit.
"""
import json
import re
import time
from urllib.parse import quote

from crm_batch import canonical, require, valid, _EVENT

STATE_TYPE = 'com.jkali.conversation_tracking'


def project_conversation(previous, snapshot):
    kind = snapshot.get('evidence_kind')
    observed = kind == 'observed_native'
    require(kind in ('native_complete', 'observed_native'), 'evidence-kind')
    require(snapshot.get('monitoring_ready') is True and type(snapshot.get('has_more')) is bool,
            snapshot.get('reason') or 'native-window-incomplete')
    require((snapshot.get('observed_window_complete') is True and snapshot.get('complete') is False
             and snapshot.get('window_complete') is False if observed
             else snapshot.get('window_complete') is True), 'native-window-incomplete')
    require(not previous or previous['evidence_kind'] == kind, 'evidence-kind-changed')
    values = snapshot.get('events')
    require(isinstance(values, list) and len(values) <= 10000, 'native-window-invalid')
    fresh = {}
    for value in values:
        require(isinstance(value, dict) and isinstance(value.get('native_id'), str)
                and 0 < len(value['native_id']) <= 512
                and isinstance(value.get('order'), str) and re.fullmatch(r'[0-9]{1,32}', value['order'])
                and type(value.get('timestamp')) is int and value['timestamp'] > 0
                and type(value.get('incoming')) is bool
                and (value.get('event_id') is None or valid(value['event_id'], _EVENT)), 'invalid-native-order')
        event = {key: value.get(key) for key in ('native_id', 'order', 'timestamp', 'incoming', 'event_id')}
        require(event['native_id'] not in fresh, 'duplicate-native-id')
        fresh[event['native_id']] = event
    prior = (previous or {}).get('events', {})
    # A missing retained tail cannot be called an answer or native deletion.
    lower = min((int(e['order']) for e in fresh.values()), default=0)
    require(not prior or bool(prior.keys() & fresh.keys()), 'native-window-gap')
    require(all(identity in fresh or (snapshot['has_more'] and int(event['order']) < lower)
                for identity, event in prior.items()), 'native-history-changed')
    for identity in prior.keys() & fresh.keys():
        before, now = prior[identity], fresh[identity]
        require(all(before[k] == now[k] for k in ('order', 'timestamp', 'incoming')), 'native-order-changed')
        require(before['event_id'] is None or now['event_id'] in (None, before['event_id']), 'native-event-remapped')
        now['event_id'] = now['event_id'] or before['event_id']
    events = {**prior, **fresh}
    require(len(events) <= 10000, 'native-ledger-bound')
    parts = dict((previous or {}).get('outgoing_parts', {}))
    require(isinstance(snapshot.get('outgoing_parts'), dict), 'native-parts-unavailable')
    for target, identities in snapshot['outgoing_parts'].items():
        require(valid(target, _EVENT) and isinstance(identities, list) and 0 < len(identities) <= 100
                and all(isinstance(i, str) and i for i in identities)
                and len(set(identities)) == len(identities), 'native-parts-invalid')
        require(target not in parts or set(parts[target]) == set(identities), 'native-parts-changed')
        parts[target] = identities
    aliases = {}
    for target, identities in parts.items():
        require(all(i in events and not events[i]['incoming'] for i in identities), 'native-parts-incomplete')
        for identity in identities:
            require(identity not in aliases, 'native-part-multiple-messages')
            aliases[identity] = target
    ordered = sorted(events.values(), key=lambda e: int(e['order']))
    require(len(ordered) == len({e['order'] for e in ordered}), 'native-order-tie')
    require(all(a['timestamp'] <= b['timestamp'] for a, b in zip(ordered, ordered[1:])), 'native-time-order-inversion')
    groups, sequence = {}, []
    for event in ordered:
        target = aliases.get(event['native_id'], event['event_id'])
        require(valid(target, _EVENT), 'native-message-not-mapped')
        if target in groups:
            require(sequence[-1] == target and not event['incoming']
                    and event['native_id'] in parts.get(target, []), 'native-message-noncontiguous')
            groups[target]['timestamp'] = event['timestamp']
            groups[target]['order'] = event['order']
        else:
            groups[target] = {'event_id': target, 'incoming': event['incoming'],
                              'timestamp': event['timestamp'], 'order': event['order'], 'answered_by': None}
            sequence.append(target)
    # The first partial native page may start inside a multipart message.
    # Retain it as an ordering anchor but do not claim it as an obligation.
    boundary = (previous or {}).get('boundary')
    if not previous and snapshot['has_more'] and sequence:
        boundary = sequence[0]
    if not snapshot['has_more']:
        boundary = None
    pending = {True: [], False: []}
    messages = []
    for target in sequence:
        if target == boundary:
            continue
        item = groups[target]
        opposite = pending[not item['incoming']]
        if opposite:
            groups[opposite.pop()]['answered_by'] = target
        pending[item['incoming']].append(target)
        messages.append(item)
    complete = not observed and (not snapshot['has_more'] or bool((previous or {}).get('complete')))
    return {'evidence_kind': kind, 'events': events, 'outgoing_parts': parts,
            'boundary': boundary, 'complete': complete,
            'coverage': 'observed_history' if observed else 'native_history' if complete else 'native_suffix',
            'messages': messages}


class ConversationTracking:
    def __init__(self, uplink, clock=time.time):
        self.uplink, self.db, self.clock = uplink, uplink.db, clock
        self.next_scan = 0
        self.cursor = ''
        with self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS conversation_tracking (scope TEXT PRIMARY KEY, epoch TEXT, projection TEXT NOT NULL)')

    def update(self):
        u, now = self.uplink, self.clock()
        control = getattr(u, '_crm_control', None)
        if now < self.next_scan or not control or not control.local_ready or not control.connected:
            return
        self.next_scan = now + 5
        # Expiring read-work hints are derived from the canonical lists. They
        # contain no deadlines/policies and never authorize sharing or sending.
        proposal_room = u.meta_get('master_proposals_room')
        if not proposal_room:
            return
        control._verify_master_room(proposal_room)
        page = u.master('GET', '/_matrix/client/v3/rooms/' + quote(proposal_room, safe='') + '/messages',
                        query={'dir': 'b', 'limit': 20, 'filter': canonical({'types': ['com.jkali.monitoring_scope']})}, timeout=15)
        require(isinstance(page.get('chunk'), list), 'monitoring-scope-unavailable')
        newest = next((e for e in page['chunk'] if e.get('sender') == u.cfg.manager_mxid
                       and e.get('type') == 'com.jkali.monitoring_scope' and 'state_key' not in e), None)
        if not newest or newest.get('unsigned', {}).get('redacted_because'):
            return
        content, stamp = newest.get('content', {}), newest.get('origin_server_ts')
        if (type(stamp) is not int or not now * 1000 - 300000 <= stamp <= now * 1000 + 30000
                or content.get('schema') != 1 or content.get('owner_mxid') != u.cfg.master_user
                or content.get('generation') != control.generation or content.get('journal_epoch') != control.epoch):
            return
        selected = content.get('rooms')
        require(isinstance(selected, list) and len(selected) <= 1000
                and all(isinstance(r, str) and re.fullmatch(r'![^\s:]{1,255}:[^\s]{1,255}', r) for r in selected), 'monitoring-scope-invalid')
        rooms = sorted(set(selected) & set(u.existing_mirror_ids()))
        if not rooms:
            return
        room = next((r for r in rooms if r > self.cursor), rooms[0])
        self.cursor = room
        self.update_room(room)

    def update_room(self, room):
        u, control = self.uplink, self.uplink._crm_control
        mirror = u.mirror_for(room)
        if not mirror or u.mirror_status(room) != 'live' or not u.active_link_for_dispatch():
            return
        if not control.local_ready or not control.connected or u.archive_level(room) not in ('share', 'direct'):
            return
        scope = [u.cfg.master_user, control.generation, control.epoch, room, mirror[0], mirror[1]]
        key = canonical(scope)
        old = self.db.execute('SELECT epoch,projection FROM conversation_tracking WHERE scope=?', (key,)).fetchone()
        epoch, previous = (old[0], json.loads(old[1])) if old else (None, None)
        projection = None
        payload = {'schema': 1, 'owner_mxid': scope[0], 'generation': scope[1], 'journal_epoch': scope[2],
                   'room_id': room, 'mirror_room_id': mirror[0], 'source': mirror[1],
                   'updated_ts': int(self.clock() * 1000), 'state': 'reconciling', 'messages': []}
        try:
            reader = getattr(u, '_crm_native_history' if mirror[1] == 'imessage' else '_crm_provider_history', None)
            require(reader is not None, 'native-order-unavailable')
            snap = (reader.conversation_snapshot(room, bridge_epoch=epoch) if mirror[1] == 'imessage'
                    else reader.conversation_snapshot(mirror[1], room, bridge_epoch=epoch))
            require(snap.get('room_id') == room and snap.get('source') == mirror[1]
                    and isinstance(snap.get('bridge_epoch'), str) and bool(snap['bridge_epoch']), 'native-snapshot-scope')
            require(epoch in (None, snap['bridge_epoch']), 'native-bridge-changed')
            projection = project_conversation(previous, snap)
            epoch = snap['bridge_epoch']
            # Bound the state event, retaining every unanswered obligation.
            messages = projection['messages']
            keep = {m['event_id'] for m in messages[-100:]} | {m['event_id'] for m in messages if not m['answered_by']}
            # Include referenced answers, which can themselves reference a later
            # message. The closure keeps every published link independently checkable.
            for m in messages:
                if m['event_id'] in keep and m['answered_by']:
                    keep.add(m['answered_by'])
            require(len(keep) <= 250, 'obligation-window-bound')
            mapped = {}
            for item in messages:
                if item['event_id'] in keep:
                    target = u.delivery_for(mirror[0], item['event_id'])
                    require(valid(target, _EVENT), 'mirror-message-not-mapped')
                    mapped[item['event_id']] = target
            require(len(set(mapped.values())) == len(mapped), 'mirror-message-alias')
            payload.update(state='ready', bridge_epoch=epoch, coverage=projection['coverage'],
                           complete=projection['complete'], total_messages=len(messages), messages=[{
                               'event_id': mapped[m['event_id']], 'incoming': m['incoming'],
                               'timestamp': m['timestamp'], 'order': m['order'],
                               'answered_by': mapped[m['answered_by']] if m['answered_by'] else None
                           } for m in messages if m['event_id'] in keep])
            require(len(canonical(payload).encode('utf-8')) < 48000, 'obligation-state-bound')
        except (ValueError, OSError):
            # Do not publish arbitrary exception text (it could contain paths or
            # bridge credentials). Old links stay in the local reconciliation ledger.
            projection = None
            payload.update(state='reconciling', messages=[], reason='native-or-mirror-evidence-incomplete')
        # Native/bridge I/O must not turn a revoked conversation into a write.
        if (u.mirror_for(room) != mirror or u.mirror_status(room) != 'live'
                or not u.active_link_for_dispatch() or not control.local_ready or not control.connected
                or [u.cfg.master_user, control.generation, control.epoch] != scope[:3]
                or u.archive_level(room) not in ('share', 'direct')):
            return
        if projection is not None:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                if self.db.execute('SELECT epoch,projection FROM conversation_tracking WHERE scope=?', (key,)).fetchone() != old:
                    return
                self.db.execute('INSERT OR REPLACE INTO conversation_tracking VALUES (?,?,?)', (key, epoch, canonical(projection)))
        # Keep native overlap locally even when the master is offline. Publishing
        # the latest derived state is idempotent and grants no send capability.
        u.master('PUT', '/_matrix/client/v3/rooms/' + quote(mirror[0], safe='')
                 + '/state/' + STATE_TYPE + '/', payload, timeout=15)
