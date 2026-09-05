"""Durable archive work; IDs and cursors only, never another message-body store.

This mixin deliberately does not implement consent or Direct sending. Those
decisions stay in Uplink and its existing shared consent resolver.
"""
import hashlib
import json
import logging
import time
import urllib.error
import urllib.parse
import uuid
import urllib.request

log = logging.getLogger('uplink')
q = lambda value: urllib.parse.quote(value, safe='')


def install_schema(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS mirror_lifecycle (
        local_room_id TEXT PRIMARY KEY, generation TEXT NOT NULL,
        status TEXT NOT NULL, revoke_step INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS delivery_map (
        master_room_id TEXT NOT NULL, local_event_id TEXT NOT NULL,
        master_event_id TEXT, PRIMARY KEY(master_room_id,local_event_id));
      CREATE TABLE IF NOT EXISTS legacy_mirrors (master_room_id TEXT PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS pending_events (
        master_room_id TEXT NOT NULL, local_room_id TEXT NOT NULL,
        local_event_id TEXT NOT NULL, origin_ts INTEGER NOT NULL DEFAULT 0,
        priority INTEGER NOT NULL DEFAULT 0, ordinal INTEGER NOT NULL DEFAULT 0,
        error TEXT, PRIMARY KEY(master_room_id,local_event_id));
      CREATE INDEX IF NOT EXISTS pending_events_order ON pending_events(priority,origin_ts,ordinal);
      CREATE TABLE IF NOT EXISTS history_jobs (
        job_id TEXT PRIMARY KEY, local_room_id TEXT NOT NULL,
        master_room_id TEXT NOT NULL, direction TEXT NOT NULL,
        boundary TEXT NOT NULL, cursor TEXT, anchor TEXT,
        status TEXT NOT NULL DEFAULT 'discover', count INTEGER NOT NULL DEFAULT 0,
        error TEXT, UNIQUE(master_room_id,boundary,direction));
      CREATE TABLE IF NOT EXISTS history_refs (
        job_id TEXT NOT NULL, event_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
        origin_ts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(job_id,event_id));
      CREATE TABLE IF NOT EXISTS proposal_pending (
        master_room_id TEXT NOT NULL, event_id TEXT NOT NULL,
        origin_ts INTEGER NOT NULL DEFAULT 0, cold_start INTEGER NOT NULL,
        PRIMARY KEY(master_room_id,event_id));
      CREATE TABLE IF NOT EXISTS media_retry (
        master_room_id TEXT NOT NULL, local_room_id TEXT NOT NULL,
        local_event_id TEXT NOT NULL, next_attempt REAL NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
        PRIMARY KEY(master_room_id,local_event_id));
      CREATE TABLE IF NOT EXISTS delivery_attempts (
        master_room_id TEXT NOT NULL, local_event_id TEXT NOT NULL,
        credential_hash TEXT NOT NULL, status TEXT NOT NULL,
        PRIMARY KEY(master_room_id,local_event_id));
      CREATE TABLE IF NOT EXISTS retired_mirrors (
        destination TEXT NOT NULL, master_room_id TEXT NOT NULL,
        connection TEXT NOT NULL, revoke_step INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(destination,master_room_id));
    ''')
    # Additive migration keeps already-persisted cleanup steps and failures
    # resumable. Wall-clock deadlines survive daemon restart.
    for table in ('mirror_lifecycle', 'retired_mirrors'):
        columns = {row[1] for row in db.execute('PRAGMA table_info(' + table + ')')}
        for column, definition in (('next_attempt', 'REAL NOT NULL DEFAULT 0'),
                                   ('attempts', 'INTEGER NOT NULL DEFAULT 0'),
                                   ('error', 'TEXT')):
            if column not in columns:
                db.execute('ALTER TABLE ' + table + ' ADD COLUMN ' + column + ' ' + definition)
    # Only the first migration may adopt the old global map. New generations
    # never consult it, so a Private -> Share transition replays retained history.
    if not db.execute("SELECT 1 FROM meta WHERE k='durable_schema_adopted'").fetchone():
        db.execute('INSERT OR IGNORE INTO legacy_mirrors SELECT master_room_id FROM mirror_rooms')
        db.execute("INSERT INTO meta VALUES ('durable_schema_adopted','1')")
    db.commit()


class DurableSync:
    @staticmethod
    def validate_wire_version(data):
        if data.get('wire_version', 1) != 1:
            raise ValueError('Master protocol requires a newer Beepa release')

    def control_fingerprint(self, control):
        # Includes all user control fields; credentials are hashed, never
        # logged or exposed through diagnostics. Absent legacy control binds
        # to immutable startup environment, not mutable recovered cfg fields.
        value = {'legacy_env': self.cfg.env_master} if control is None else {'master_link': control}
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def read_master_control(self):
        try:
            return self.local('GET', '/_matrix/client/v3/user/' + q(self.cfg.local_user)
                              + '/account_data/com.jkali.master_link')
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            return None

    def archive_level(self, room):
        """A failed read pauses forwarding; it is not an explicit revocation."""
        import consent
        path = ('/_matrix/client/v3/user/' + q(self.cfg.local_user) + '/rooms/' + q(room)
                + '/account_data/' + consent.SHARE_OVERRIDE_TYPE)
        try:
            return consent.effective_level(self.local('GET', path))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return 'private'
            raise

    def active_link_for_dispatch(self):
        if self.meta_get('link_disabled') == '1':
            return False
        link = self.read_master_control()
        if link is None:
            return self.meta_get('link_disabled') != '1'
        if not isinstance(link, dict) or not link.get('master_token') or link.get('enabled') is False or link.get('disabled'):
            self.meta_set('link_disabled', '1')
            for room in self.existing_mirror_ids():
                self.mark_revoking(room)
            return False
        # A changed connection must first pass the normal binding/reconcile
        # path; never dispatch an old room under a replacement credential.
        fingerprint = self.control_fingerprint(link)
        if fingerprint == getattr(self, '_control_fingerprint', None):
            return True  # cfg may carry recovered credentials for this control
        return (not hasattr(self, '_control_fingerprint') and link.get('master_token') == self.cfg.master_token
                and (link.get('master_hs_url') or '').rstrip('/') == self.cfg.master_hs
                and link.get('master_user') == self.cfg.master_user)

    def recovery_request(self, path, payload=None, bearer=None):
        base = getattr(self.cfg, 'master_enroll_url', '')
        if not base:
            raise ValueError('No explicit enrollment/recovery endpoint configured')
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')):
            raise ValueError('Recovery endpoint must use HTTPS or loopback')
        request = urllib.request.Request(base.rstrip('/') + path,
                    data=None if payload is None else json.dumps(payload).encode(),
                    method='GET' if payload is None else 'POST')
        if payload is not None:
            request.add_header('Content-Type', 'application/json')
        if bearer:
            request.add_header('Authorization', 'Bearer ' + bearer)
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read())

    def refresh_recovery(self):
        """Adopt recovery only while this scoped Matrix credential is valid."""
        if not getattr(self.cfg, 'master_enroll_url', '') or self.meta_get('link_disabled') == '1':
            return
        manifest = self.recovery_request('/enroll/manifest')
        self.validate_wire_version(manifest)
        authority = manifest.get('master_authority_id')
        epoch = manifest.get('master_data_epoch')
        if not isinstance(authority, str) or not authority or not isinstance(epoch, str) or not epoch:
            raise ValueError('Invalid master manifest')
        known = self.meta_get('recovery_authority')
        if known and known != authority:
            # Continuity cannot be established by reusing a hostname/localpart.
            self.meta_set('recovery_status', 'authority_changed_reenroll_required')
            if self.meta_get('recovery_rejected_authority') != authority:
                ts = int(time.time() * 1000)
                self.meta_set(self.SUSPENDED_META, str(ts))
                self._write_suspension(self.direct_send_identity(), ts)
                self.meta_set('recovery_rejected_authority', authority)
            self._direct_suspended = True
            raise ValueError('Recovery authority changed')
        install_id = self.meta_get('recovery_install_id')
        if not install_id:
            install_id = getattr(self.cfg, 'install_id', '') or str(uuid.uuid4())
            self.meta_set('recovery_install_id', install_id)
        secret = self.meta_get('recovery_token')
        if not secret:
            import secrets
            secret = secrets.token_urlsafe(32)
            self.meta_set('recovery_token', secret)  # durable before ambiguous HTTP
        if not self.meta_get('recovery_issued'):
            result = self.recovery_request('/enroll/recovery/issue',
                {'install_id': install_id, 'recovery_token': secret}, self.cfg.master_token)
            self.validate_wire_version(result)
            if result.get('master_authority_id') != authority:
                raise ValueError('Recovery issuance authority mismatch')
            self.meta_set('recovery_authority', authority)
            self.meta_set('recovery_issued', '1')
        self.destination_binding(authority, epoch)
        self.cfg.master_authority_id = authority
        self.cfg.master_data_epoch = epoch
        self.meta_set('recovery_status', 'ready')
        # Store a runtime overlay bound to the user control fingerprint. The
        # worker NEVER writes the user-owned master_link account-data event.
        self.persist_master_runtime()

    def persist_master_runtime(self):
        link = {'enabled': True, 'master_hs_url': self.cfg.master_hs,
                'master_user': self.cfg.master_user, 'master_token': self.cfg.master_token,
                'manager_mxid': self.cfg.manager_mxid, 'master_space': self.cfg.master_space,
                'master_enroll_url': getattr(self.cfg, 'master_enroll_url', ''),
                'master_authority_id': getattr(self.cfg, 'master_authority_id', ''),
                'master_data_epoch': getattr(self.cfg, 'master_data_epoch', '')}
        current = self.read_master_control()
        if current is not None and (not current.get('master_token') or current.get('enabled') is False or current.get('disabled')):
            self.meta_set('link_disabled', '1')
            return False
        fingerprint = self.control_fingerprint(current)
        expected = getattr(self, '_control_fingerprint', fingerprint)
        if fingerprint != expected:
            return False
        self.meta_set('master_runtime', json.dumps({'control_fingerprint': expected, 'link': link}))
        return True

    def recover_master(self):
        if self.meta_get('link_disabled') == '1' or not self.meta_get('recovery_issued'):
            return False
        current = self.read_master_control()
        if current is not None and (not current.get('master_token') or current.get('enabled') is False or current.get('disabled')):
            self.meta_set('link_disabled', '1')
            return False
        if self.control_fingerprint(current) != getattr(self, '_control_fingerprint', self.control_fingerprint(current)):
            return False
        authority = self.meta_get('recovery_authority')
        result = self.recovery_request('/enroll/recovery', {
            'install_id': self.meta_get('recovery_install_id'),
            'recovery_token': self.meta_get('recovery_token'),
            'master_authority_id': authority})
        self.validate_wire_version(result)
        if (result.get('master_authority_id') != authority
                or result.get('master_user') != self.cfg.master_user
                or result.get('manager_mxid') != self.cfg.manager_mxid
                or (result.get('master_hs_url') or '').rstrip('/') != self.cfg.master_hs):
            raise ValueError('Recovered pairing identity differs; explicit enrollment required')
        if not result.get('master_token') or not result.get('master_space') or not result.get('master_data_epoch'):
            raise ValueError('Incomplete recovery response')
        self.cfg.master_token = result['master_token']
        self.cfg.master_space = result['master_space']
        self.cfg.master_authority_id = authority
        self.cfg.master_data_epoch = result['master_data_epoch']
        if not self.persist_master_runtime():
            return False
        self.destination_binding(authority, result['master_data_epoch'])
        self.meta_set('recovery_status', 'recovered')
        return True

    def mirror_status(self, room):
        row = self.db.execute('SELECT status FROM mirror_lifecycle WHERE local_room_id=?', (room,)).fetchone()
        return row[0] if row else 'live'  # explicitly adopted legacy room

    def mark_revoking(self, room):
        self.db.execute("INSERT INTO mirror_lifecycle(local_room_id,generation,status) VALUES (?,?,'revoking') "
                        "ON CONFLICT(local_room_id) DO UPDATE SET status='revoking'", (room, uuid.uuid4().hex))
        self.db.execute('DELETE FROM pending_events WHERE local_room_id=?', (room,))
        self.db.execute("UPDATE history_jobs SET status='revoked' WHERE local_room_id=? AND direction='up'", (room,))
        self.db.execute('DELETE FROM media_retry WHERE local_room_id=?', (room,))
        self.db.commit()

    def destination_binding(self, authority='', epoch=''):
        key = json.dumps([self.cfg.master_hs, self.cfg.master_user,
                          self.cfg.manager_mxid, authority or '', epoch or ''])
        stored = self.meta_get('archive_destination')
        if stored is not None:
            previous = json.loads(stored)
            # N-1 clients had no epoch metadata. First authenticated adoption
            # of it must not manufacture replacement rooms during an update.
            if previous[:3] == json.loads(key)[:3] and not previous[3] and not previous[4]:
                self.meta_set('archive_destination', key)
                return
        if stored is not None and stored != key:
            previous = json.loads(stored)
            old_connection = self.meta_get('cleanup_connection')
            if old_connection:
                old_rooms = {row[0] for row in self.db.execute('SELECT master_room_id FROM mirror_rooms').fetchall()}
                old_rooms.update(value for value in (self.meta_get('master_contacts_room'), self.meta_get('master_proposals_room')) if value)
                for master_room in old_rooms:
                    self.db.execute('INSERT OR IGNORE INTO retired_mirrors(destination,master_room_id,connection) VALUES (?,?,?)',
                                    (stored, master_room, old_connection))
            if previous[:3] != json.loads(key)[:3] or previous[3] and authority and previous[3] != authority:
                self.db.execute("DELETE FROM meta WHERE k IN ('recovery_token','recovery_authority',"
                                "'recovery_issued','recovery_status','recovery_rejected_authority')")
            if previous[3] and authority and previous[3] != authority:
                ts = int(time.time() * 1000)
                self.meta_set(self.SUSPENDED_META, str(ts))
                self._write_suspension(self.direct_send_identity(), ts)
                self._direct_suspended = True
            # These tables describe copies on the destination, NOT send safety.
            for table in ('mirror_rooms', 'mirror_lifecycle', 'delivery_map', 'legacy_mirrors',
                          'pending_events', 'history_jobs', 'history_refs', 'contact_mirror',
                          'proposal_pending', 'media_retry'):
                self.db.execute('DELETE FROM ' + table)
            self.db.execute("DELETE FROM meta WHERE k IN ('master_contacts_room','master_proposals_room',"
                            "'proposal_sync_since','sync_since') OR k LIKE 'mname:%' OR k LIKE 'last_event:%'")
            self.db.commit()
            self._last_reconcile = float("-inf")
            log.warning('archive destination changed; rebuilding current shares; Direct ledgers retained')
        self.meta_set('archive_destination', key)

    def retry_retired_mirrors(self):
        """Finish old-organization cleanup using only its old scoped credential."""
        row = self.db.execute('SELECT destination,master_room_id,connection,revoke_step,attempts '
                              'FROM retired_mirrors WHERE next_attempt<=? '
                              'ORDER BY next_attempt,rowid LIMIT 1', (time.time(),)).fetchone()
        if not row:
            return
        self.run_cleanup('retired_mirrors', 'destination=? AND master_room_id=?', row[:2], row[4],
                         lambda: self._retry_retired_mirror(row[:4]))

    def _retry_retired_mirror(self, row):
        destination, room, raw, step = row
        connection = json.loads(raw)
        active_destination = self.meta_get('archive_destination')
        if active_destination and json.loads(active_destination)[:4] == json.loads(destination)[:4]:
            # Same authority after restore may have a refreshed scoped token.
            connection['master_token'] = self.cfg.master_token
        operations = [
            ('PUT', '/_matrix/client/v3/rooms/' + q(connection['master_space']) + '/state/m.space.child/' + q(room), {}),
            ('POST', '/_matrix/client/v3/rooms/' + q(room) + '/kick', {'user_id': connection['manager_mxid'], 'reason': 'unshared'}),
            ('POST', '/_matrix/client/v3/rooms/' + q(room) + '/leave', {})]
        for index in range(step, 3):
            method, path, body = operations[index]
            # Do not invoke active-pairing recovery on an old destination.
            try:
                self.retired_request(connection, method, path, body)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    try:
                        absent = json.loads(exc.read()).get('errcode') == 'M_NOT_FOUND'
                    except (ValueError, AttributeError):
                        absent = False
                    if absent:
                        # Positive not-found response, unlike an ambiguous
                        # authorization 403 which must stay visibly pending.
                        self.db.execute('UPDATE retired_mirrors SET revoke_step=? WHERE destination=? AND master_room_id=?',
                                        (1 if index == 0 else 3, destination, room))
                        self.db.commit()
                        if index == 0:
                            continue  # absent space does not prove absent room
                        break
                if index == 1 and exc.code in (403, 404):
                    membership = self.retired_request(connection, 'GET', '/_matrix/client/v3/rooms/' + q(room)
                        + '/state/m.room.member/' + q(connection['manager_mxid']), None)
                    if membership.get('membership') not in ('leave', 'ban'):
                        raise
                elif index == 2 and exc.code in (403, 404):
                    joined = self.retired_request(connection, 'GET', '/_matrix/client/v3/joined_rooms', None)
                    if room in joined.get('joined_rooms', []):
                        raise
                else:
                    raise
            self.db.execute('UPDATE retired_mirrors SET revoke_step=? WHERE destination=? AND master_room_id=?', (index + 1, destination, room))
            self.db.commit()
        self.db.execute('DELETE FROM retired_mirrors WHERE destination=? AND master_room_id=?', (destination, room))
        self.db.commit()

    def record_delivery(self, master_room, event_id, master_event_id):
        self.db.execute('INSERT OR REPLACE INTO delivery_map VALUES (?,?,?)',
                        (master_room, event_id, master_event_id))
        self.db.commit()

    def claim_delivery(self, master_room, event_id):
        """A changed Matrix token cannot silently weaken txn deduplication."""
        token_hash = hashlib.sha256(self.cfg.master_token.encode()).hexdigest()
        prior = self.db.execute('SELECT credential_hash,status FROM delivery_attempts WHERE master_room_id=? AND local_event_id=?',
                                (master_room, event_id)).fetchone()
        if prior and prior[0] != token_hash and prior[1] == 'attempted':
            # The earlier response may have been lost. Never assume the new
            # token shares the old token's Matrix transaction namespace.
            self.db.execute("UPDATE pending_events SET error='ambiguous_credentials' WHERE master_room_id=? AND local_event_id=?",
                            (master_room, event_id))
            self.db.commit()
            raise ValueError('Archive delivery needs reconciliation after credential rotation')
        self.db.execute("INSERT OR REPLACE INTO delivery_attempts VALUES (?,?,?,'attempted')",
                        (master_room, event_id, token_hash))
        self.db.commit()

    def finish_delivery(self, master_room, event_id, master_event_id):
        if not isinstance(master_event_id, str) or not master_event_id:
            raise ValueError('Matrix delivery response did not confirm an event ID')
        self.record_delivery(master_room, event_id, master_event_id)
        self.db.execute("UPDATE delivery_attempts SET status='confirmed' WHERE master_room_id=? AND local_event_id=?",
                        (master_room, event_id))
        self.db.commit()

    def delivery_for(self, master_room, event_id):
        row = self.db.execute('SELECT master_event_id FROM delivery_map WHERE master_room_id=? AND local_event_id=?',
                              (master_room, event_id)).fetchone()
        if row:
            return row[0]
        if self.db.execute('SELECT 1 FROM legacy_mirrors WHERE master_room_id=?', (master_room,)).fetchone():
            row = self.db.execute('SELECT master_event_id FROM event_map WHERE local_event_id=?', (event_id,)).fetchone()
            return row[0] if row else None
        return None

    def enqueue_events(self, room, master_room, events, priority=0):
        for ordinal, event in enumerate(events):
            if not isinstance(event, dict) or event.get('type') not in ('m.room.message', 'm.room.redaction'):
                continue
            event_id = event.get('event_id')
            if not isinstance(event_id, str) or not event_id:
                continue
            if self.delivery_for(master_room, event_id):
                continue
            self.db.execute('INSERT OR IGNORE INTO pending_events '
                            '(master_room_id,local_room_id,local_event_id,origin_ts,priority,ordinal) VALUES (?,?,?,?,?,?)',
                            (master_room, room, event_id, event.get('origin_server_ts') or 0, priority, ordinal))
        # Caller can now checkpoint ingestion even when master is asleep.
        self.db.commit()

    def schedule_history(self, room, master_room, cursor=None, anchor=None, direction='up', boundary='full'):
        self.db.execute('INSERT OR IGNORE INTO history_jobs '
                        '(job_id,local_room_id,master_room_id,direction,boundary,cursor,anchor) VALUES (?,?,?,?,?,?,?)',
                        (uuid.uuid4().hex, room, master_room, direction, boundary, cursor, anchor))
        self.db.commit()

    def history_slice(self):
        """One backwards page, committed atomically with its opaque cursor.

        Staging IDs on disk before delivery gives chronological order without
        holding an entire room in memory. Live events use a separate queue.
        """
        job = self.db.execute("SELECT job_id,local_room_id,master_room_id,direction,cursor,anchor,count "
                              "FROM history_jobs WHERE status='discover' ORDER BY count,rowid LIMIT 1").fetchone()
        if not job:
            return
        jid, room, master_room, direction, cursor, anchor, count = job
        if direction == 'up' and (self.mirror_status(room) == 'revoking' or self.archive_level(room) not in ('share', 'direct')):
            self.mark_revoking(room)
            return
        transport, source_room = (self.local, room) if direction == 'up' else (self.master, master_room)
        query = {'dir': 'b', 'limit': str(getattr(self.cfg, 'history_page_size', 200))}
        if cursor:
            query['from'] = cursor
        try:
            result = transport('GET', '/_matrix/client/v3/rooms/' + q(source_room) + '/messages', query=query)
        except urllib.error.HTTPError as exc:
            if exc.code == 400:
                # An invalidated token is not a verified end of history. Keep
                # its explicit incomplete outcome for diagnostics/operator retry.
                self.db.execute("UPDATE history_jobs SET status='incomplete',error='invalid_cursor' WHERE job_id=?", (jid,))
                self.db.commit()
                return
            raise
        events = result.get('chunk') or []
        end = result.get('end')
        done = not end  # a short/empty page WITH a new token is not terminal
        expected = ('m.room.message', 'm.room.redaction') if direction == 'up' else ('com.jkali.proposal',)
        for event in events:
            if not isinstance(event, dict):
                continue
            eid = event.get('event_id')
            if eid and eid == anchor:
                done = True
                break
            if event.get('type') not in expected or not isinstance(eid, str):
                continue
            count += 1
            self.db.execute('INSERT OR IGNORE INTO history_refs VALUES (?,?,?,?)',
                            (jid, eid, -count, event.get('origin_server_ts') or 0))
            cap = getattr(self.cfg, 'backfill', 0) if direction == 'up' else 0
            if cap and count >= cap:
                done = True
                break
        if end and end == cursor and not done:
            self.db.execute("UPDATE history_jobs SET status='incomplete',error='repeated_cursor',count=? WHERE job_id=?", (count, jid))
        elif done:
            if direction == 'up':
                self.db.execute('INSERT OR IGNORE INTO pending_events '
                                '(master_room_id,local_room_id,local_event_id,origin_ts,priority,ordinal) '
                                'SELECT ?,?,event_id,origin_ts,10,ordinal FROM history_refs WHERE job_id=?', (master_room, room, jid))
            else:
                # Gap/history proposals ALWAYS use the cold-start gate. They
                # cannot turn into external sends even when timestamp is fresh.
                self.db.execute('INSERT OR IGNORE INTO proposal_pending '
                                'SELECT ?,event_id,origin_ts,1 FROM history_refs WHERE job_id=?', (master_room, jid))
            self.db.execute("UPDATE history_jobs SET status='queued',cursor=?,count=? WHERE job_id=?", (end, count, jid))
            self.db.execute('DELETE FROM history_refs WHERE job_id=?', (jid,))
        else:
            self.db.execute('UPDATE history_jobs SET cursor=?,count=? WHERE job_id=?', (end, count, jid))
        self.db.commit()

    def deliver_pending(self):
        """Bounded delivery independent of ingestion and contact-store health."""
        started = time.monotonic()
        rows = self.db.execute('SELECT local_room_id,master_room_id,local_event_id FROM pending_events '
                               'WHERE error IS NULL ORDER BY priority,origin_ts,ordinal LIMIT ?',
                               (getattr(self.cfg, 'history_page_size', 200),)).fetchall()
        for room, master_room, event_id in rows:
            if time.monotonic() - started >= 5:
                break
            if not self.active_link_for_dispatch():
                return
            active = self.mirror_for(room)
            if not active or active[0] != master_room:
                self.db.execute('DELETE FROM pending_events WHERE master_room_id=? AND local_event_id=?', (master_room, event_id))
                self.db.commit()
                continue
            if self.mirror_status(room) == 'revoking' or self.archive_level(room) not in ('share', 'direct'):
                self.mark_revoking(room)
                continue
            try:
                event = self.local('GET', '/_matrix/client/v3/rooms/' + q(room) + '/event/' + q(event_id))
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 404):
                    self.db.execute("UPDATE pending_events SET error='source_unavailable' WHERE master_room_id=? AND local_event_id=?", (master_room, event_id))
                    self.db.commit()
                    continue
                raise
            content = event.get('content') or {}
            relation = content.get('m.relates_to') or {}
            target = (event.get('redacts') or content.get('redacts') if event.get('type') == 'm.room.redaction'
                      else relation.get('event_id') if relation.get('rel_type') == 'm.replace' else None)
            if target and not self.delivery_for(master_room, target):
                known = self.db.execute('SELECT 1 FROM pending_events WHERE master_room_id=? AND local_event_id=?', (master_room, target)).fetchone()
                discovering = self.db.execute("SELECT 1 FROM history_jobs WHERE master_room_id=? AND status='discover'", (master_room,)).fetchone()
                self.db.execute('UPDATE pending_events SET priority=20,error=? WHERE master_room_id=? AND local_event_id=?',
                                (None if known or discovering else 'missing_relation_target', master_room, event_id))
                self.db.commit()
                continue
            self.forward_events(room, master_room, [event])
            if not self.delivery_for(master_room, event_id):
                continue
            self.db.execute('DELETE FROM pending_events WHERE master_room_id=? AND local_event_id=?', (master_room, event_id))
            self.db.commit()
            self.meta_set('last_delivery_success', str(int(time.time())))
        self.db.execute("UPDATE mirror_lifecycle SET status='live' WHERE status='history' "
                        "AND NOT EXISTS (SELECT 1 FROM pending_events p WHERE p.local_room_id=mirror_lifecycle.local_room_id) "
                        "AND NOT EXISTS (SELECT 1 FROM history_jobs h WHERE h.local_room_id=mirror_lifecycle.local_room_id AND h.status='discover')")
        self.db.commit()

    def retry_media_slice(self):
        row = self.db.execute('SELECT master_room_id,local_room_id,local_event_id,attempts FROM media_retry '
                              'WHERE next_attempt<=? ORDER BY next_attempt LIMIT 1', (time.time(),)).fetchone()
        if not row or not self.active_link_for_dispatch():
            return
        master_room, room, eid, attempts = row
        if self.mirror_status(room) == 'revoking' or self.archive_level(room) not in ('share', 'direct'):
            self.mark_revoking(room)
            return
        event = self.local('GET', '/_matrix/client/v3/rooms/' + q(room) + '/event/' + q(eid))
        content = dict(event.get('content') or {})
        uri = self._reupload_media(content, room)
        if not uri:
            if getattr(self, '_media_retryable', False):
                self.db.execute('UPDATE media_retry SET next_attempt=?,attempts=? WHERE master_room_id=? AND local_event_id=?',
                                (time.time() + min(3600, 2 ** min(attempts + 1, 12)), attempts + 1, master_room, eid))
            else:
                self.db.execute('DELETE FROM media_retry WHERE master_room_id=? AND local_event_id=?', (master_room, eid))
            self.db.commit()
            return
        target = self.delivery_for(master_room, eid)
        if not target or not self.active_link_for_dispatch() or self.archive_level(room) not in ('share', 'direct'):
            return
        content['url'] = uri
        content.pop('file', None)
        content['com.jkali.media_placeholder'] = False
        source = (self.mirror_for(room) or (None, 'unknown'))[1]
        sender = event.get('sender') or ''
        content['com.jkali.from_me'] = (sender == self.cfg.local_user or sender in self.self_mxids
            or source == 'imessage' and sender == getattr(self.cfg, 'imessage_bot', None)
            and content.get('com.jkali.from_me') is True)
        content['com.jkali.origin_ts'] = event.get('origin_server_ts')
        content['com.jkali.source'] = source
        content['com.jkali.origin_sender'] = self._display_name(room, sender)
        # Edit the existing placeholder; do not duplicate the archive message.
        replacement = dict(content)
        replacement['m.new_content'] = content
        replacement['m.relates_to'] = {'rel_type': 'm.replace', 'event_id': target}
        self.master('PUT', '/_matrix/client/v3/rooms/' + q(master_room)
                    + '/send/m.room.message/media_retry_' + q(eid), replacement)
        self.db.execute('DELETE FROM media_retry WHERE master_room_id=? AND local_event_id=?', (master_room, eid))
        self.db.commit()

    def deliver_proposal_pending(self):
        room = self.meta_get('local_proposals_room')
        if not room:
            return
        rows = self.db.execute('SELECT master_room_id,event_id,cold_start FROM proposal_pending '
                               'ORDER BY origin_ts LIMIT 100').fetchall()
        for master_room, eid, cold in rows:
            if self.meta_get('link_disabled') == '1':
                return
            event = self.master('GET', '/_matrix/client/v3/rooms/' + q(master_room) + '/event/' + q(eid))
            self.forward_proposals(master_room, room, [event], cold_start=bool(cold),
                                   suspended=getattr(self, '_direct_suspended', True))
            self.db.execute('DELETE FROM proposal_pending WHERE master_room_id=? AND event_id=?', (master_room, eid))
            self.db.commit()

    def sync_health(self):
        counts = {}
        for table in ('pending_events', 'proposal_pending', 'media_retry'):
            counts[table] = self.db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
        counts['history_pages_pending'] = self.db.execute("SELECT count(*) FROM history_jobs WHERE status='discover'").fetchone()[0]
        counts['history_incomplete'] = self.db.execute("SELECT count(*) FROM history_jobs WHERE status='incomplete'").fetchone()[0]
        counts['revocations_pending'] = self.db.execute("SELECT count(*) FROM mirror_lifecycle WHERE status='revoking'").fetchone()[0]
        counts['retired_revocations_pending'] = self.db.execute('SELECT count(*) FROM retired_mirrors').fetchone()[0]
        counts['delivery_incomplete'] = self.db.execute('SELECT count(*) FROM pending_events WHERE error IS NOT NULL').fetchone()[0]
        counts['last_ingestion'] = int(self.meta_get('last_ingestion_success') or 0) or None
        counts['last_delivery'] = int(self.meta_get('last_delivery_success') or 0) or None
        oldest = self.db.execute('SELECT min(origin_ts) FROM pending_events').fetchone()[0]
        counts['oldest_pending_ts'] = oldest / 1000 if oldest else None
        counts['connected'] = bool(getattr(self, '_conn_state', self.cfg.master_token)) and self.meta_get('link_disabled') != '1'
        counts['errors'] = {row[0].split(':', 1)[1]: row[1] for row in self.db.execute(
            "SELECT k,v FROM meta WHERE k LIKE 'stage_error:%'").fetchall()}
        for table, stage in (('mirror_lifecycle', 'revocation'), ('retired_mirrors', 'retired_revocation')):
            error = self.db.execute('SELECT error FROM ' + table + ' WHERE error IS NOT NULL LIMIT 1').fetchone()
            if error:
                counts['errors'][stage] = error[0]
        return counts

    def publish_health(self):
        health = self.sync_health()
        health['updated_at'] = int(time.time())
        self.meta_set('sync_health', json.dumps(health))
        self.local('PUT', '/_matrix/client/v3/user/' + q(self.cfg.local_user)
                   + '/account_data/com.beepa.sync_health', health)
