"""Source-owned exact receipts and ordered response links; stores no message text.

A native reader proves a complete latest window. A retained window can extend
only across an exact overlapping native ID. Missing parts, gaps, conflicting
identity/order and ties retain prior links as reconciling, never invent a reply.
"""
import json
import re

from crm_batch import canonical, digest, require, valid, _EVENT


def project_window(previous, snapshot, outgoing_ids):
    require(isinstance(snapshot, dict), 'native-window-incomplete')
    kind = snapshot.get('evidence_kind', 'native_complete')
    require(kind in ('native_complete', 'observed_native'), 'native-evidence-kind')
    observed = kind == 'observed_native'
    require((snapshot.get('observed_window_complete') is True
             and snapshot.get('window_complete') is False and snapshot.get('complete') is False
             if observed else snapshot.get('window_complete') is True)
            and type(snapshot.get('has_more')) is bool, snapshot.get('reason') or 'native-window-incomplete')
    require(not previous or previous.get('evidence_kind', 'native_complete') == kind, 'native-evidence-changed')
    values = snapshot.get('events')
    require(isinstance(values, list) and 0 < len(values) <= 10000, 'native-window-empty')
    fresh = {}
    for value in values:
        require(isinstance(value, dict) and isinstance(value.get('native_id'), str)
                and 0 < len(value['native_id']) <= 512
                and isinstance(value.get('order'), str) and re.fullmatch(r'[0-9]{1,32}', value['order'])
                and type(value.get('timestamp')) is int and value['timestamp'] > 0
                and type(value.get('incoming')) is bool
                and (value.get('event_id') is None or valid(value['event_id'], _EVENT)), 'invalid-native-order')
        event = {key: value.get(key) for key in ('native_id', 'order', 'timestamp', 'incoming', 'event_id')}
        require(event['native_id'] not in fresh or fresh[event['native_id']] == event, 'conflicting-native-id')
        fresh[event['native_id']] = event
    prior = (previous or {}).get('events', {})
    for identity in fresh.keys() & prior.keys():
        before, now = prior[identity], fresh[identity]
        require(all(before[key] == now[key] for key in ('order', 'timestamp', 'incoming')), 'native-order-changed')
        require(before.get('event_id') in (None, now.get('event_id')) or now.get('event_id') is None, 'native-event-remapped')
        if now.get('event_id') is None:
            now['event_id'] = before.get('event_id')
    events = {**prior, **fresh}
    require(len(events) <= 10000, 'native-ledger-bound')
    parts = snapshot.get('outgoing_parts')
    require(isinstance(parts, dict), 'native-parts-unavailable')
    lower = min(int(event['order']) for event in fresh.values())
    prior_complete = bool((previous or {}).get('observed_complete' if observed else 'complete'))
    overlap = bool(prior.keys() & fresh.keys())
    if prior_complete:
        require(overlap, 'native-window-gap')
    if observed:
        # A configured backfill stop is not native EOF. Only the reader's
        # explicit bounded observed-history proof authorizes a first baseline.
        require(prior_complete or snapshot.get('observed_complete') is True,
                snapshot.get('reason') or 'provider-observed-baseline-incomplete')
        # This is a latest-window query. A missing retained tail above its
        # newest fresh row is also deletion/conflicting evidence, not an older
        # page boundary that can safely remain authoritative.
        require(all(identity in fresh or int(event['order']) < lower
                    for identity, event in prior.items()), 'provider-observed-history-changed')
    outgoing, claimed = {}, set()
    for target in outgoing_ids:
        ids = parts.get(target)
        require(isinstance(ids, list) and 0 < len(ids) <= 100 and len(set(ids)) == len(ids)
                and all(isinstance(identity, str) and identity in events for identity in ids), 'native-outgoing-parts-missing')
        require(not claimed.intersection(ids), 'native-part-multiple-outreach')
        claimed.update(ids)
        group = [events[identity] for identity in ids]
        require(all(not event['incoming'] for event in group), 'native-outgoing-direction')
        start, end = min(int(e['order']) for e in group), max(int(e['order']) for e in group)
        if not prior_complete and snapshot['has_more']:
            require(lower < start, 'native-baseline-incomplete')
        outgoing[target] = {'start': start, 'order': end, 'timestamp': max(e['timestamp'] for e in group)}
    first = min(value['start'] for value in outgoing.values())
    ordered = sorted((event for event in events.values() if int(event['order']) >= first), key=lambda e: int(e['order']))
    orders, incoming_ids = {}, set()
    last_time = 0
    for event in ordered:
        order = int(event['order'])
        if event['incoming'] and event.get('event_id'):
            require(event['event_id'] not in incoming_ids, 'native-event-multiple-identities')
            incoming_ids.add(event['event_id'])
        require(order not in orders or (event['native_id'] in claimed and orders[order] in claimed), 'native-order-tie')
        orders[order] = event['native_id']
        require(event['timestamp'] >= last_time, 'native-time-order-inversion')
        last_time = event['timestamp']
    require(len({value['order'] for value in outgoing.values()}) == len(outgoing), 'outreach-order-tie')
    answers, used = {}, set()
    for event in ordered:
        if not event['incoming']:
            continue
        cursor = int(event['order'])
        require(not any(value['start'] <= cursor <= value['order'] for value in outgoing.values()), 'incoming-between-outgoing-parts')
        eligible = [(value['order'], target) for target, value in outgoing.items()
                    if target not in used and value['order'] < cursor and value['timestamp'] <= event['timestamp']]
        if not eligible:
            continue
        require(valid(event.get('event_id'), _EVENT), 'native-incoming-not-mapped')
        target = max(eligible)[1]
        used.add(target)
        answers[target] = {'incoming_event_id': event['event_id'], 'native_id': event['native_id'], 'source_order': event['order'], 'source_timestamp': event['timestamp']}
    return {'complete': not observed, 'observed_complete': observed,
            'evidence_kind': kind, 'events': events, 'answers': answers}


class CRMTracking:
    def __init__(self, batch):
        self.batch, self.control, self.uplink, self.db = batch, batch.control, batch.uplink, batch.db
        with self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS crm_tracking_scopes (scope TEXT PRIMARY KEY, bridge_epoch TEXT, projection_json TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS crm_tracking_items (child_id TEXT,item_id TEXT,revision INTEGER NOT NULL,tracking_json TEXT NOT NULL,PRIMARY KEY(child_id,item_id))')
            self.db.execute('CREATE TABLE IF NOT EXISTS crm_tracking_corrections (scope TEXT,revision INTEGER,old_links TEXT,new_links TEXT,reason TEXT,PRIMARY KEY(scope,revision))')

    def result(self, child, item):
        row = self.db.execute('SELECT tracking_json FROM crm_tracking_items WHERE child_id=? AND item_id=?', (child, item)).fetchone()
        return json.loads(row[0]) if row else None

    def _delivery(self, row):
        room, target = row['sent_room_id'], row['sent_event_id']
        receipt = self.db.execute('SELECT read,delivered,sent,failed FROM message_receipts WHERE local_room_id=? AND target=?', (room, target)).fetchone()
        state = 'sent'
        if receipt:
            read, delivered, sent, failed = receipt
            state = 'read' if read else 'delivered' if delivered else 'sent' if sent else 'failed' if failed else 'sending'
        mirror = self.uplink.mirror_for(room)
        mapped = self.uplink.delivery_for(mirror[0], target) if mirror else None
        return {'state': state, 'sent_event_id': target, 'mirror_event_id': mapped, 'mirror_room_id': mirror[0] if mirror else None}

    def update(self):
        if not self.control.local_ready:
            return
        scopes = {}
        for event, in self.db.execute("SELECT proposal_event_id FROM crm_batch_items WHERE state='sent' AND sent_event_id IS NOT NULL AND sent_room_id IS NOT NULL").fetchall():
            row = self.batch.item_for_event(event)
            parent = json.loads(self.batch.batch(row['child_id'])['request_json'])
            item = json.loads(row['item_json'])
            if parent['owner_mxid'] != self.uplink.cfg.master_user or parent['generation'] != self.control.generation or parent['journal_epoch'] != self.control.epoch:
                continue
            key = canonical([parent['owner_mxid'], parent['generation'], row['sent_room_id'], item['route']['source']])
            scopes.setdefault(key, []).append(row)
        for scope, rows in scopes.items():
            self._scope(scope, rows)

    def _scope(self, scope, rows):
        room = rows[0]['sent_room_id']
        source = json.loads(rows[0]['item_json'])['route']['source']
        old = self.db.execute('SELECT bridge_epoch,projection_json FROM crm_tracking_scopes WHERE scope=?', (scope,)).fetchone()
        previous = json.loads(old[1]) if old else None
        projection, epoch, reason = previous, old[0] if old else None, 'native-order-unavailable'
        complete, visible = False, False
        try:
            visible = self.uplink.archive_level(room) in ('share', 'direct')
            require(visible, 'conversation-not-shared')
            reader = getattr(self.uplink, '_crm_native_history' if source == 'imessage' else '_crm_provider_history', None)
            require(reader is not None, 'native-order-unavailable')
            targets = [row['sent_event_id'] for row in rows]
            snapshot = (reader.snapshot(room, targets, bridge_epoch=epoch) if source == 'imessage'
                        else reader.snapshot(source, room, targets, bridge_epoch=epoch))
            require(snapshot.get('evidence_kind', 'native_complete') ==
                    ('native_complete' if source == 'imessage' else 'observed_native'), 'native-evidence-kind')
            require(snapshot.get('room_id') == room and snapshot.get('source') == source
                    and isinstance(snapshot.get('bridge_epoch'), str) and bool(snapshot['bridge_epoch']), snapshot.get('reason') or 'native-snapshot-scope')
            require(epoch in (None, snapshot['bridge_epoch']), 'native-bridge-changed')
            epoch = snapshot['bridge_epoch']
            projection = project_window(previous, snapshot, [row['sent_event_id'] for row in rows])
            complete, reason = True, ''
        except (ValueError, OSError) as error:
            reason = str(error)
        prior_answers = (previous or {}).get('answers', {})
        answers = (projection or {}).get('answers', {}) if visible else {}
        revision = 1 + max([self.result(row['child_id'], row['item_id']).get('revision', 0) if self.result(row['child_id'], row['item_id']) else 0 for row in rows])
        updates = []
        for row in rows:
            target = row['sent_event_id']
            answer = answers.get(target)
            response = {'state': 'replied' if complete and answer else 'waiting' if complete else 'reconciling',
                        'reason': 'next-incoming-message' if complete and answer else reason,
                        'incoming_event_id': answer['incoming_event_id'] if answer else None,
                        'mirror_event_id': None, 'mirror_room_id': None, 'source_order': answer['source_order'] if answer else None}
            response['evidence_kind'] = (projection or {}).get('evidence_kind', 'native_complete' if source == 'imessage' else 'observed_native')
            response['coverage'] = ('observed_history' if response['evidence_kind'] == 'observed_native'
                                    else 'complete_window')
            mirror = self.uplink.mirror_for(room) if visible else None
            if answer and mirror:
                response['mirror_event_id'] = self.uplink.delivery_for(mirror[0], answer['incoming_event_id'])
                response['mirror_room_id'] = mirror[0]
            prior = self.result(row['child_id'], row['item_id'])
            delivery = self._delivery(row) if visible else {
                'state': prior['delivery']['state'] if prior else 'sent',
                'sent_event_id': target, 'mirror_event_id': None, 'mirror_room_id': None}
            tracking = {'revision': revision, 'delivery': delivery, 'response': response}
            if prior and {k:v for k,v in prior.items() if k != 'revision'} == {k:v for k,v in tracking.items() if k != 'revision'}:
                continue
            updates.append((row['child_id'], row['item_id'], revision, canonical(tracking)))
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            current = self.db.execute('SELECT bridge_epoch,projection_json FROM crm_tracking_scopes WHERE scope=?', (scope,)).fetchone()
            if current != old:
                return  # Another worker advanced this scope during native I/O.
            if complete:
                self.db.execute('INSERT OR REPLACE INTO crm_tracking_scopes VALUES (?,?,?)', (scope, epoch, canonical(projection)))
            if complete and prior_answers != answers:
                self.db.execute('INSERT OR REPLACE INTO crm_tracking_corrections VALUES (?,?,?,?,?)', (scope, revision, canonical(prior_answers), canonical(answers),
                    'observed-native-history' if projection.get('evidence_kind') == 'observed_native' else 'complete-native-window'))
            self.db.executemany('INSERT OR REPLACE INTO crm_tracking_items VALUES (?,?,?,?)', updates)
