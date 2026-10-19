"""Independent hostile-envelope, permission ordering and poisoned-room probes."""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
import urllib.parse
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'agents' / 'uplink'))
import uplink
from crm_control import CRMControl, CONTROL_MARKER, DIRECT_REQUEST, DIRECT_RESULT, POLICY_REQUEST

OWNER, MANAGER, LOCAL = '@owner:master', '@manager:master', '@owner:local'
ROOM, MASTER_ROOM, LOCAL_ROOM = '!person:local', '!proposals:master', '!policy:local'
NOW = 1800000000000


class ControlAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = sqlite3.connect(str(Path(self.tmp.name) / 'state.db')); self.addCleanup(self.db.close)
        self.writes, self.master_writes, self.remote, self.pages = [], [], {}, []
        self.active, self.shared = True, True
        self.local_cap = None
        self.extra_account = {}
        self.checkpoint_state = {}
        self.proofs = [{'room_id': ROOM, 'source': 'imessage', 'network_id': 'target@example.com', 'proof_generation': 'native-1'}]
        self.cfg = SimpleNamespace(local_user=LOCAL, master_hs='http://master.fixture', master_user=OWNER,
            manager_mxid=MANAGER, master_authority_id='authority-1', master_space='!space:master', db_path=str(Path(self.tmp.name) / 'state.db'))
        self.uplink = SimpleNamespace(db=self.db, cfg=self.cfg, local=self.local, master=self.master,
            meta_get=lambda key: MASTER_ROOM if key == 'master_proposals_room' else None,
            active_link_for_dispatch=lambda: self.active, _direct_suspended=False)
        self.now = NOW
        self.control = CRMControl(self.uplink, self.resolve, clock=lambda: self.now)
        self.control.refresh(True)
        self.control._meta('local_room', LOCAL_ROOM)
        self.local_state = self.room_state(LOCAL, local=True)
        self.master_state = self.room_state(OWNER, local=False)
        self.control.local_once()

    def room_state(self, creator, local):
        def event(kind, content, key=''):
            return {'type': kind, 'state_key': key, 'sender': creator, 'content': content}
        marker = event(CONTROL_MARKER, {'schema': 1, 'journal_epoch': self.control.epoch}) if local else event('com.jkali.proposals', {})
        levels = {'users': {creator: 100}, 'users_default': 0, 'events_default': 100, 'state_default': 100,
                  'invite': 100, 'kick': 100, 'ban': 100, 'redact': 100, 'events': {}}
        members = [event('m.room.member', {'membership': 'join'}, creator)]
        if not local:
            levels['users'][MANAGER] = 50; levels['events']['com.jkali.proposal'] = 50
            members.append(event('m.room.member', {'membership': 'join'}, MANAGER))
        return [event('m.room.create', {'room_version': '11', 'm.federate': False}), marker,
                event('m.room.power_levels', levels), event('m.room.join_rules', {'join_rule': 'invite'}),
                event('m.room.history_visibility', {'history_visibility': 'joined'}),
                event('m.room.guest_access', {'guest_access': 'forbidden'}), *members]

    def local(self, method, path, content=None, **kwargs):
        if path.endswith('/account_data/com.jkali.crm_control'):
            if method == 'PUT': self.local_cap = copy.deepcopy(content); return {}
            if self.local_cap is None: raise urllib.error.HTTPError(path, 404, 'absent', {}, None)
            return copy.deepcopy(self.local_cap)
        if '/state/com.jkali.policy.checkpoint/' in path:
            key = urllib.parse.unquote(path.rsplit('/', 1)[1])
            if method == 'PUT':
                self.checkpoint_state[key] = {'type': 'com.jkali.policy.checkpoint', 'state_key': key, 'sender': LOCAL, 'content': copy.deepcopy(content)}
                return {}
            if key not in self.checkpoint_state: raise urllib.error.HTTPError(path, 404, 'absent', {}, None)
            return copy.deepcopy(self.checkpoint_state[key]['content'])
        if method == 'GET' and path.endswith('/state'):
            return copy.deepcopy(self.local_state + list(self.checkpoint_state.values()))
        if method == 'GET' and path.endswith('/messages'):
            return {'chunk': [], 'end': kwargs.get('query', {}).get('from', 'local-end')}
        if '/account_data/com.jkali.share_override' in path:
            if method == 'GET': return copy.deepcopy(self.remote)
            self.writes.append(copy.deepcopy(content)); self.remote = copy.deepcopy(content); return {}
        if '/account_data/' in path:
            if method == 'PUT': self.extra_account[path] = copy.deepcopy(content); return {}
            if path not in self.extra_account: raise urllib.error.HTTPError(path, 404, 'absent', {}, None)
            return copy.deepcopy(self.extra_account[path])
        if method == 'PUT': return {}
        raise AssertionError((method, path))

    def master(self, method, path, content=None, **kwargs):
        if method == 'GET' and path.endswith('/state'):
            return copy.deepcopy(self.master_state)
        if method == 'GET' and path.endswith('/messages'):
            return {'chunk': [] if kwargs.get('query', {}).get('from') == 'next' else copy.deepcopy(self.pages), 'end': 'next'}
        if method == 'GET' and path.endswith('/state/m.room.power_levels/'):
            return copy.deepcopy(next(e['content'] for e in self.master_state if e['type'] == 'm.room.power_levels'))
        if method == 'PUT': self.master_writes.append((path, copy.deepcopy(content))); return {}
        raise AssertionError((method, path))

    def resolve(self, binding, **kwargs):
        self.assertEqual(kwargs, {'owner_mxid': OWNER, 'generation': self.control.generation})
        return {'status': 'ready', 'rooms': copy.deepcopy(self.proofs)} if self.shared else {'status': 'revoked'}

    def direct(self, phase='prepare', **changes):
        content = {'schema': 1, 'command_id': 'grant', 'phase': phase, 'owner_mxid': OWNER,
            'generation': self.control.generation, 'journal_epoch': self.control.epoch,
            'binding_id': 'binding', 'contact': {'source': 'imessage', 'network_id': 'target@example.com'},
            'contact_version': 'contact-v1', 'membership_operation_id': 'member-op'}
        content.update(changes)
        return {'type': DIRECT_REQUEST, 'sender': MANAGER, 'event_id': '$' + phase,
            'room_id': MASTER_ROOM, 'origin_server_ts': NOW, 'content': content}

    def prepare(self):
        return self.control.ingest_direct(self.direct(), MASTER_ROOM)

    def commit(self, prepared):
        return self.control.ingest_direct(self.direct('commit', ticket_id=prepared['ticket_id']), MASTER_ROOM)

    def test_prepare_alone_never_writes_and_commit_dispatches_only_resolved_room(self):
        ticket = self.prepare(); self.assertEqual(self.writes, [])
        self.commit(ticket); self.assertEqual(self.writes, [])
        self.control._drain(); self.assertEqual(len(self.writes), 1); self.assertEqual(self.remote['state'], 'direct')

    def test_manual_private_after_prepare_wins_over_delayed_commit(self):
        ticket = self.prepare()
        revision = self.control.journal.inspect(ROOM)['revision']
        event = {'type': POLICY_REQUEST, 'sender': LOCAL, 'event_id': '$manual', 'room_id': LOCAL_ROOM,
            'content': {'schema': 1, 'command_id': 'manual-private', 'journal_epoch': self.control.epoch,
                'room_id': ROOM, 'level': 'private', 'expected_revision': revision}}
        self.control.ingest_manual(event, LOCAL_ROOM); self.control._drain()
        self.assertEqual(self.remote['state'], 'private')
        self.commit(ticket); self.control._drain(); self.assertEqual(self.remote['state'], 'private'); self.assertEqual(len(self.writes), 1)

    def test_contact_revocation_after_commit_blocks_deferred_grant(self):
        ticket = self.prepare(); self.commit(ticket); self.shared = False; self.control._drain()
        self.assertEqual(self.writes, [])
        self.assertNotEqual(json.loads(self.control._command('grant')['result_json'])['state'], 'applied')

    def test_scope_change_after_prepare_refuses_commit(self):
        ticket = self.prepare(); self.proofs[0]['room_id'] = '!replacement:local'
        with self.assertRaisesRegex(ValueError, 'scope-changed'): self.commit(ticket)
        self.assertEqual(self.writes, [])

    def test_replayed_completed_commit_does_not_reapply_direct(self):
        ticket = self.prepare(); self.commit(ticket); self.control._drain()
        self.control.journal.submit_manual(operation_id='manual-op', actor=LOCAL, room_id=ROOM,
            expected_revision=self.control.journal.inspect(ROOM)['revision'], level='private')
        self.control.journal.process_next(); count = len(self.writes)
        self.commit(ticket); self.control._drain(); self.assertEqual(len(self.writes), count); self.assertEqual(self.remote['state'], 'private')

    def test_payload_substitution_and_foreign_sender_never_change_policy(self):
        self.prepare()
        substituted = self.direct(); substituted['content']['contact']['network_id'] = 'different@example.com'
        with self.assertRaisesRegex(ValueError, 'collision'): self.control.ingest_direct(substituted, MASTER_ROOM)
        foreign = self.direct(); foreign['sender'] = OWNER
        with self.assertRaisesRegex(ValueError, 'authentication'): self.control.ingest_direct(foreign, MASTER_ROOM)
        self.assertEqual(self.writes, [])

    def test_rebind_invalidates_prepared_ticket(self):
        ticket = self.prepare(); before = self.control.generation; self.cfg.master_authority_id = 'authority-2'; self.control.refresh(True)
        self.assertNotEqual(self.control.generation, before)
        with self.assertRaises(ValueError): self.commit(ticket)
        self.assertEqual(self.writes, [])

    def test_invalid_manual_command_does_not_poison_later_privacy_commands(self):
        bad = {'type': POLICY_REQUEST, 'sender': LOCAL, 'event_id': '$bad-manual', 'room_id': LOCAL_ROOM,
            'content': {'schema': 1, 'command_id': 'bad-manual', 'journal_epoch': self.control.epoch,
                'room_id': ROOM, 'level': 'not-a-level', 'expected_revision': 0}}
        try:
            self.control.ingest_manual(bad, LOCAL_ROOM)
        except ValueError:
            pass
        valid = copy.deepcopy(bad); valid['event_id'] = '$good-manual'
        valid['content'].update(command_id='good-manual', level='private')
        self.control.ingest_manual(valid, LOCAL_ROOM)
        self.control._drain()
        self.assertEqual(self.remote.get('state'), 'private')
        self.assertEqual(len(self.writes), 1)

    def test_database_loss_changes_epoch_and_generation_and_rejects_old_command(self):
        old = self.direct()
        fresh_db = sqlite3.connect(str(Path(self.tmp.name) / 'replacement.db')); self.addCleanup(fresh_db.close)
        replacement_uplink = SimpleNamespace(**vars(self.uplink)); replacement_uplink.db = fresh_db
        replacement = CRMControl(replacement_uplink, self.resolve, clock=lambda: self.now); replacement.refresh(True)
        self.assertNotEqual(replacement.epoch, self.control.epoch)
        self.assertNotEqual(replacement.generation, self.control.generation)
        with self.assertRaisesRegex(ValueError, 'stale-direct-authority'):
            replacement.ingest_direct(old, MASTER_ROOM)
        self.assertEqual(self.writes, [])

    def test_rebind_cancels_accepted_but_unattempted_grant(self):
        ticket = self.prepare(); self.commit(ticket)
        self.cfg.master_authority_id = 'authority-2'; self.control.refresh(True); self.control._drain()
        self.assertEqual(self.writes, [])

    def test_crash_after_crm_journal_insert_recovers_exact_child_without_regrant(self):
        ticket = self.prepare()
        original = self.control.journal.submit_crm
        def crash_after_journal(**kwargs):
            original(**kwargs)
            raise SystemExit('crash before control child mapping')
        self.control.journal.submit_crm = crash_after_journal
        with self.assertRaises(SystemExit): self.commit(ticket)
        self.assertEqual(self.writes, [])
        replacement = CRMControl(self.uplink, self.resolve, clock=lambda: self.now); replacement.refresh(True)
        replacement.local_once(); self.assertEqual(len(self.writes), 1); self.assertEqual(self.remote['state'], 'direct')
        replacement._drain(); self.assertEqual(len(self.writes), 1)

    def test_crash_after_manual_journal_insert_recovers_without_duplicate_write(self):
        event = {'type': POLICY_REQUEST, 'sender': LOCAL, 'event_id': '$manual', 'room_id': LOCAL_ROOM,
            'content': {'schema': 1, 'command_id': 'manual-private', 'journal_epoch': self.control.epoch,
                'room_id': ROOM, 'level': 'private', 'expected_revision': 0}}
        original = self.control.journal.submit_manual
        def crash_after_journal(**kwargs):
            original(**kwargs)
            raise SystemExit('crash before control child mapping')
        self.control.journal.submit_manual = crash_after_journal
        with self.assertRaises(SystemExit): self.control.ingest_manual(event, LOCAL_ROOM)
        self.assertEqual(self.writes, [])
        replacement = CRMControl(self.uplink, self.resolve, clock=lambda: self.now); replacement.refresh(True)
        replacement._drain(); replacement._drain()
        self.assertEqual(len(self.writes), 1); self.assertEqual(self.remote['state'], 'private')

    def test_manual_privacy_works_while_master_is_disconnected(self):
        self.active = False; self.control.refresh(False)
        event = {'type': POLICY_REQUEST, 'sender': LOCAL, 'event_id': '$manual', 'room_id': LOCAL_ROOM,
            'content': {'schema': 1, 'command_id': 'offline-private', 'journal_epoch': self.control.epoch,
                'room_id': ROOM, 'level': 'private', 'expected_revision': 0}}
        self.control.ingest_manual(event, LOCAL_ROOM); self.control._drain()
        self.assertEqual(self.remote['state'], 'private')
        self.assertEqual(self.master_writes, [])

    def test_local_public_history_room_is_not_advertised_as_private_control(self):
        next(e for e in self.local_state if e['type'] == 'm.room.history_visibility')['content'] = {'history_visibility': 'world_readable'}
        with self.assertRaises(ValueError): self.control.ensure_local()

    def test_local_encrypted_control_room_is_rejected(self):
        self.local_state.append({'type': 'm.room.encryption', 'state_key': '', 'sender': LOCAL, 'content': {'algorithm': 'm.megolm.v1.aes-sha2'}})
        with self.assertRaises(ValueError): self.control.ensure_local()

    def test_master_mirror_shaped_room_cannot_receive_control_capability(self):
        next(e for e in self.master_state if e['type'] == 'm.room.create')['content']['com.jkali.mirror_of'] = ROOM
        with self.assertRaises(ValueError): self.control.publish_master()
        self.assertEqual(self.master_writes, [])

    def test_master_public_history_room_cannot_advertise_control_capability(self):
        next(e for e in self.master_state if e['type'] == 'm.room.history_visibility')['content'] = {'history_visibility': 'world_readable'}
        with self.assertRaises(ValueError): self.control.publish_master()
        self.assertEqual(self.master_writes, [])

    def test_master_third_party_membership_cannot_receive_control_requests(self):
        self.master_state.append({'type': 'm.room.member', 'state_key': '@third:master', 'sender': OWNER, 'content': {'membership': 'join'}})
        with self.assertRaises(ValueError): self.prepare()
        self.assertEqual(self.writes, [])

    def test_expired_commit_has_explicit_failure_ack_not_stale_prepared_result(self):
        ticket = self.prepare(); self.now = ticket['expires_at'] + 1
        expired = self.direct('commit', ticket_id=ticket['ticket_id']); expired['origin_server_ts'] = self.now
        self.pages = [expired]; self.control._poll(self.master, MASTER_ROOM, 'crm'); self.control.flush_results('crm')
        results = [body for path, body in self.master_writes if '/state/' + DIRECT_RESULT + '/' in path]
        self.assertTrue(results)
        self.assertTrue(any(body.get('state') == 'rejected' or body.get('request_error') for body in results),
            'expired commit must not silently leave its caller waiting on the prepared result')
        self.assertEqual(self.writes, [])

    def test_failed_manual_observation_blocks_sharing_dispatch_and_queued_grant(self):
        ticket = self.prepare(); self.commit(ticket)
        original = self.uplink.local
        def unavailable(method, path, content=None, **kwargs):
            if method == 'GET' and path.endswith('/messages'):
                raise OSError('local stream unavailable')
            return original(method, path, content, **kwargs)
        self.uplink.local = unavailable
        with self.assertRaises(OSError): self.control.local_once()
        self.assertTrue(self.control.blocks_dispatch(ROOM))
        self.assertTrue(self.control.blocks_sharing(ROOM))
        self.control._drain()
        self.assertEqual(self.writes, [], 'failed observation must not allow a deferred Direct grant')
        self.uplink.local = original; self.control.local_once()
        self.assertEqual(len(self.writes), 1, 'a completed later observation can recover the queued grant')

    def test_manual_page_two_is_observed_before_a_prepared_grant_can_commit(self):
        ticket = self.prepare()
        private = {'type': POLICY_REQUEST, 'sender': LOCAL, 'event_id': '$page-two', 'room_id': LOCAL_ROOM,
            'content': {'schema': 1, 'command_id': 'page-two-private', 'journal_epoch': self.control.epoch,
                'room_id': ROOM, 'level': 'private', 'expected_revision': 0}}
        original = self.uplink.local
        tokens = []
        def paginated(method, path, content=None, **kwargs):
            if method == 'GET' and path.endswith('/messages'):
                token = kwargs.get('query', {}).get('from'); tokens.append(token)
                if token == 'local-end':
                    return {'chunk': [{'type': 'ignored'}] * 50, 'end': 'page-two'}
                if token == 'page-two': return {'chunk': [private], 'end': 'page-three'}
                return {'chunk': [], 'end': 'page-three'}
            return original(method, path, content, **kwargs)
        self.uplink.local = paginated
        self.control.local_once()
        self.assertIn('page-two', tokens)
        self.assertEqual(self.remote.get('state'), 'private')
        self.commit(ticket); self.control._drain()
        self.assertEqual(self.remote.get('state'), 'private')
        self.assertEqual(len(self.writes), 1)

    def test_manual_cursor_cycle_keeps_observation_unhealthy(self):
        original = self.uplink.local
        def cycling(method, path, content=None, **kwargs):
            if method == 'GET' and path.endswith('/messages'):
                token = kwargs.get('query', {}).get('from')
                return {'chunk': [{'type': 'ignored'}], 'end': 'cycle-b' if token != 'cycle-b' else 'local-end'}
            return original(method, path, content, **kwargs)
        self.uplink.local = cycling
        with self.assertRaises((ValueError, OSError)): self.control.local_once()
        self.assertTrue(self.control.blocks_dispatch(ROOM))
        self.assertTrue(self.control.blocks_sharing(ROOM))

    def test_database_loss_does_not_replace_prior_control_capability_or_create_fresh_writer(self):
        previous = {'schema': 1, 'journal_epoch': self.control.epoch, 'generation': self.control.generation,
            'policy_room_id': LOCAL_ROOM, 'manual': True, 'crm': True}
        mutations = []
        original = self.uplink.local
        def persisted_matrix(method, path, content=None, **kwargs):
            if method == 'GET' and path.endswith('/account_data/com.jkali.crm_control'):
                return copy.deepcopy(previous)
            if method in ('PUT', 'POST'): mutations.append((method, path, copy.deepcopy(content)))
            if method == 'POST' and path.endswith('/createRoom'): return {'room_id': '!replacement-control:local'}
            return original(method, path, content, **kwargs)
        fresh_db = sqlite3.connect(str(Path(self.tmp.name) / 'lost-journal.db')); self.addCleanup(fresh_db.close)
        replacement_uplink = SimpleNamespace(**vars(self.uplink)); replacement_uplink.db = fresh_db
        replacement_uplink.local = persisted_matrix
        replacement = CRMControl(replacement_uplink, self.resolve, clock=lambda: self.now); replacement.refresh(True)
        with self.assertRaises((ValueError, OSError)): replacement.local_once()
        self.assertEqual(mutations, [], 'old journal capability proves prior possibly in-flight writes; do not replace its writer after DB loss')
        self.assertTrue(replacement.blocks_dispatch(ROOM))
        self.assertTrue(replacement.blocks_sharing(ROOM))

    def test_actual_uplink_abort_archive_and_direct_if_control_construction_failed(self):
        adapter = object.__new__(uplink.Uplink)
        adapter._crm_control_healthy = False
        adapter.local = lambda *args, **kwargs: self.fail('unhealthy control must not read or write dispatch evidence')
        self.assertEqual(adapter.read_room_level(ROOM), 'private')
        with self.assertRaisesRegex(OSError, 'observation-incomplete'): adapter.archive_level(ROOM)
        with self.assertRaisesRegex(OSError, 'observation-incomplete'): adapter.crm_master_once()

    def test_actual_crm_profiles_callback_preserves_malformed_vs_absent(self):
        adapter = object.__new__(uplink.Uplink); adapter.cfg = self.cfg
        for malformed in ({}, {'profiles': None}, [], {'profiles': 'corrupt'}):
            adapter.local = lambda *args, value=malformed, **kwargs: copy.deepcopy(value)
            self.assertIsNone(adapter.read_crm_profiles())
        def missing(*args, **kwargs): raise urllib.error.HTTPError('fixture', 404, 'absent', {}, None)
        adapter.local = missing
        self.assertEqual(adapter.read_crm_profiles(), [])
        def unavailable(*args, **kwargs): raise OSError('unavailable')
        adapter.local = unavailable
        with self.assertRaises(OSError): adapter.read_crm_profiles()

    def test_observed_private_supersedes_unattempted_queued_crm_grant_without_cas_loss(self):
        ticket = self.prepare(); self.commit(ticket)
        event = {'type': POLICY_REQUEST, 'sender': LOCAL, 'event_id': '$privacy-after-queue', 'room_id': LOCAL_ROOM,
            'content': {'schema': 1, 'command_id': 'privacy-after-queue', 'journal_epoch': self.control.epoch,
                'room_id': ROOM, 'level': 'private', 'expected_revision': 0}}
        self.control.ingest_manual(event, LOCAL_ROOM)
        self.assertTrue(self.control.blocks_sharing(ROOM))
        self.control._drain(); self.control._drain()
        self.assertEqual([body.get('state') for body in self.writes], ['private'])
        self.assertEqual(self.remote.get('state'), 'private')
        self.assertNotEqual(json.loads(self.control._command('grant')['result_json'])['state'], 'applied')

    def test_same_epoch_restore_cannot_write_private_over_forgotten_inflight_direct(self):
        snapshot = sqlite3.connect(str(Path(self.tmp.name) / 'pre-grant-snapshot.db')); self.addCleanup(snapshot.close)
        self.db.backup(snapshot)
        delayed = []
        original = self.uplink.local
        def delayed_direct(method, path, content=None, **kwargs):
            if method == 'PUT' and path.endswith('/account_data/com.jkali.share_override'):
                delayed.append(copy.deepcopy(content))
                raise TimeoutError('request remains in-flight on server')
            return original(method, path, content, **kwargs)
        self.uplink.local = delayed_direct
        ticket = self.prepare(); self.commit(ticket); self.control._drain()
        self.assertEqual(len(delayed), 1)
        self.assertEqual(self.remote, {})
        replacement_uplink = SimpleNamespace(**vars(self.uplink)); replacement_uplink.db = snapshot; replacement_uplink.local = original
        replacement = CRMControl(replacement_uplink, self.resolve, clock=lambda: self.now); replacement.refresh(True)
        self.assertEqual(replacement.epoch, self.control.epoch, 'this counterexample restores an old DB rather than replacing its UUID')
        with self.assertRaises((ValueError, OSError)): replacement.local_once()
        self.assertTrue(replacement.blocks_dispatch(ROOM))
        self.assertTrue(replacement.blocks_sharing(ROOM))
        event = {'type': POLICY_REQUEST, 'sender': LOCAL, 'event_id': '$restore-private', 'room_id': LOCAL_ROOM,
            'content': {'schema': 1, 'command_id': 'restore-private', 'journal_epoch': replacement.epoch,
                'room_id': ROOM, 'level': 'private', 'expected_revision': 0}}
        try:
            replacement.ingest_manual(event, LOCAL_ROOM); replacement._drain()
        except (ValueError, OSError):
            pass
        self.assertEqual(self.writes, [], 'lost in-flight barrier must prevent a contradictory fresh override PUT')
        self.remote = delayed[0]  # The old request can complete after the failed restored attempt.
        self.assertTrue(replacement.blocks_sharing(ROOM))

    def test_unknown_checkpoint_put_blocks_other_rooms_and_never_writes_native_override(self):
        delayed_checkpoints = []
        original = self.uplink.local
        def delayed_checkpoint(method, path, content=None, **kwargs):
            if method == 'PUT' and '/state/com.jkali.policy.checkpoint/' in path:
                delayed_checkpoints.append((path, copy.deepcopy(content)))
                raise TimeoutError('checkpoint request is still running; GET retains predecessor')
            return original(method, path, content, **kwargs)
        self.uplink.local = delayed_checkpoint
        ticket = self.prepare(); self.commit(ticket); self.control._drain()
        self.assertEqual(len(delayed_checkpoints), 1, 'a durable remote checkpoint must precede any override PUT')
        self.assertEqual(self.writes, [])
        second_room = '!other:local'
        self.control.journal.inspect(second_room)
        self.control.journal.submit_manual(operation_id='other-room-private', actor=LOCAL, room_id=second_room,
            expected_revision=0, level='private')
        self.control._drain(); self.control._drain()
        self.assertEqual(len(delayed_checkpoints), 1, 'unknown checkpoint is a global barrier, not a same-room retry')
        self.assertEqual(self.writes, [], 'an unseen late checkpoint must not be allowed to supersede a newer native-write fence')
        replacement = CRMControl(self.uplink, self.resolve, clock=lambda: self.now); replacement.refresh(True)
        try: replacement.local_once()
        except (ValueError, OSError): pass
        self.assertEqual(len(delayed_checkpoints), 1, 'restarting the process must retain the unknown checkpoint barrier')
        self.assertEqual(self.writes, [])

    def test_checkpoint_2xx_without_exact_readback_cannot_authorize_override(self):
        checkpoints = []
        original = self.uplink.local
        def stale_readback(method, path, content=None, **kwargs):
            if method == 'PUT' and '/state/com.jkali.policy.checkpoint/' in path:
                checkpoints.append((path, copy.deepcopy(content)))
                return {}  # The following GET still returns the old stored checkpoint.
            return original(method, path, content, **kwargs)
        self.uplink.local = stale_readback
        ticket = self.prepare(); self.commit(ticket); self.control._drain()
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(self.writes, [], 'positive PUT alone does not verify an independent recovery fence')
        self.control._drain()
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(self.writes, [])

    def test_crash_during_checkpoint_put_keeps_persisted_global_no_override_barrier(self):
        original = self.uplink.local
        attempted = []
        def crash_checkpoint(method, path, content=None, **kwargs):
            if method == 'PUT' and '/state/com.jkali.policy.checkpoint/' in path:
                attempted.append((path, copy.deepcopy(content)))
                raise SystemExit('crash during checkpoint HTTP request')
            return original(method, path, content, **kwargs)
        self.uplink.local = crash_checkpoint
        ticket = self.prepare(); self.commit(ticket)
        with self.assertRaises(SystemExit): self.control._drain()
        self.assertEqual(len(attempted), 1)
        replacement = CRMControl(self.uplink, self.resolve, clock=lambda: self.now); replacement.refresh(True)
        try: replacement.local_once()
        except (ValueError, OSError): pass
        self.assertEqual(len(attempted), 1)
        self.assertEqual(self.writes, [])
        self.assertTrue(replacement.blocks_dispatch(ROOM))

    def test_crash_after_checkpoint_and_applied_override_recovers_without_resending(self):
        original = self.uplink.local
        def crash_after_override(method, path, content=None, **kwargs):
            result = original(method, path, content, **kwargs)
            if method == 'PUT' and path.endswith('/account_data/com.jkali.share_override'):
                raise SystemExit('crash after server applied native override')
            return result
        self.uplink.local = crash_after_override
        ticket = self.prepare(); self.commit(ticket)
        with self.assertRaises(SystemExit): self.control._drain()
        self.assertEqual(len(self.writes), 1)
        self.assertTrue(self.checkpoint_state)
        self.uplink.local = original
        replacement = CRMControl(self.uplink, self.resolve, clock=lambda: self.now); replacement.refresh(True)
        replacement.local_once(); replacement.local_once()
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(json.loads(replacement._command('grant')['result_json'])['state'], 'applied')

    def test_exact_late_checkpoint_recovery_uses_new_immutable_attempt_before_native_write(self):
        original = self.uplink.local
        attempts = []
        def delay_first(method, path, content=None, **kwargs):
            if method == 'PUT' and '/state/com.jkali.policy.checkpoint/' in path:
                attempts.append((path, copy.deepcopy(content)))
                if len(attempts) == 1: raise TimeoutError('first checkpoint is delayed')
            return original(method, path, content, **kwargs)
        self.uplink.local = delay_first
        ticket = self.prepare(); self.commit(ticket); self.control._drain()
        self.assertEqual(self.writes, [])
        path, payload = attempts[0]
        original('PUT', path, payload)  # Exact late server completion proves the old fence has finished.
        self.control.local_once()
        self.assertEqual(len(attempts), 2)
        self.assertNotEqual(attempts[0][0], attempts[1][0])
        self.assertEqual(len(self.checkpoint_state), 2)
        self.assertEqual([body.get('state') for body in self.writes], ['direct'])
        self.control.local_once()
        self.assertEqual(len(self.writes), 1)

    def test_late_fence_after_snapshot_restore_cannot_overwrite_newer_fence_and_forces_recovery(self):
        snapshot = sqlite3.connect(str(Path(self.tmp.name) / 'before-checkpoint.db')); self.addCleanup(snapshot.close)
        self.db.backup(snapshot)
        original = self.uplink.local
        delayed = []
        def delay_checkpoint(method, path, content=None, **kwargs):
            if method == 'PUT' and '/state/com.jkali.policy.checkpoint/' in path:
                delayed.append((path, copy.deepcopy(content)))
                raise TimeoutError('old checkpoint not yet visible')
            return original(method, path, content, **kwargs)
        self.uplink.local = delay_checkpoint
        ticket = self.prepare(); self.commit(ticket); self.control._drain()
        self.assertEqual(len(delayed), 1); self.assertEqual(self.writes, [])
        replacement_uplink = SimpleNamespace(**vars(self.uplink)); replacement_uplink.db = snapshot; replacement_uplink.local = original
        replacement = CRMControl(replacement_uplink, self.resolve, clock=lambda: self.now); replacement.refresh(True)
        replacement.local_once()  # Before the old fence appears, no old native request has ever started.
        event = {'type': POLICY_REQUEST, 'sender': LOCAL, 'event_id': '$new-private', 'room_id': LOCAL_ROOM,
            'content': {'schema': 1, 'command_id': 'new-private', 'journal_epoch': replacement.epoch,
                'room_id': ROOM, 'level': 'private', 'expected_revision': 0}}
        replacement.ingest_manual(event, LOCAL_ROOM); replacement._drain()
        new_keys = set(self.checkpoint_state)
        self.assertEqual(len(new_keys), 1)
        self.assertEqual([body.get('state') for body in self.writes], ['private'])
        original('PUT', delayed[0][0], delayed[0][1])
        self.assertEqual(len(self.checkpoint_state), 2, 'the late fence adds its unique key rather than overwriting the newer recovery evidence')
        self.assertTrue(new_keys.issubset(self.checkpoint_state))
        with self.assertRaises((ValueError, OSError)): replacement.local_once()
        self.assertTrue(replacement.blocks_dispatch(ROOM))
        self.assertTrue(replacement.blocks_sharing(ROOM))
        self.assertEqual([body.get('state') for body in self.writes], ['private'])

    def test_positive_native_rate_limit_retry_gets_distinct_attempt_fence(self):
        original = self.uplink.local
        native_attempts = []
        def rate_limit_once(method, path, content=None, **kwargs):
            if method == 'PUT' and path.endswith('/account_data/com.jkali.share_override'):
                native_attempts.append(copy.deepcopy(content))
                if len(native_attempts) == 1:
                    raise urllib.error.HTTPError(path, 429, 'positive no-write rate limit', {}, None)
            return original(method, path, content, **kwargs)
        self.uplink.local = rate_limit_once
        ticket = self.prepare(); self.commit(ticket); self.control._drain()
        self.assertEqual(len(self.checkpoint_state), 1); self.assertEqual(self.writes, [])
        self.control._drain()
        self.assertEqual(len(native_attempts), 2)
        self.assertEqual(len(self.checkpoint_state), 2, 'each actual native retry needs fresh immutable restore evidence')
        self.assertEqual([body.get('state') for body in self.writes], ['direct'])

    def test_cancel_queued_replay_cannot_resurrect_direct_or_substitute_membership(self):
        ticket = self.prepare(); self.commit(ticket)
        result = self.control.ingest_direct(self.direct('cancel'), MASTER_ROOM)
        self.assertTrue(result['safe_to_change_membership']); self.assertEqual(result['state'], 'canceled')
        self.commit(ticket); self.control.ingest_direct(self.direct('cancel'), MASTER_ROOM); self.control._drain()
        self.assertEqual(self.writes, [])
        with self.assertRaisesRegex(ValueError, 'collision'):
            self.control.ingest_direct(self.direct('cancel', membership_operation_id='replacement-membership'), MASTER_ROOM)
        with self.assertRaisesRegex(ValueError, 'collision'):
            self.control.ingest_direct(self.direct('cancel', binding_id='replacement-binding'), MASTER_ROOM)
        self.assertEqual(self.writes, [])

    def test_cancel_unknown_grant_does_not_claim_safe_removal_or_release_manual_private(self):
        original = self.uplink.local; delayed = []
        def unknown(method, path, content=None, **kwargs):
            if method == 'PUT' and path.endswith('/account_data/com.jkali.share_override'):
                delayed.append(copy.deepcopy(content)); raise TimeoutError('native request still running')
            return original(method, path, content, **kwargs)
        self.uplink.local = unknown
        ticket = self.prepare(); self.commit(ticket); self.control._drain()
        result = self.control.ingest_direct(self.direct('cancel'), MASTER_ROOM)
        self.assertFalse(result['safe_to_change_membership']); self.assertEqual(result['state'], 'pending')
        self.control.journal.submit_manual(operation_id='waiting-private', actor=LOCAL, room_id=ROOM,
            expected_revision=1, level='private')
        for _ in range(2): self.control._drain()
        self.assertEqual(len(delayed), 1); self.assertEqual(self.writes, [])
        self.assertTrue(self.control.blocks_sharing(ROOM))
        result = self.commit(ticket)
        self.assertFalse(result['safe_to_change_membership'])

    def test_cancel_parent_crash_before_child_transition_recovers_without_direct(self):
        ticket = self.prepare(); self.commit(ticket)
        def crash(**kwargs): raise SystemExit('after durable canceling parent before journal child')
        self.control.journal.cancel_crm = crash
        with self.assertRaises(SystemExit): self.control.ingest_direct(self.direct('cancel'), MASTER_ROOM)
        self.assertEqual(self.control._command('grant')['state'], 'canceling')
        replacement = CRMControl(self.uplink, self.resolve, clock=lambda: self.now); replacement.refresh(True)
        replacement.local_once()
        result = json.loads(replacement._command('grant')['result_json'])
        self.assertTrue(result['safe_to_change_membership']); self.assertEqual(result['state'], 'canceled')
        self.assertEqual(self.writes, [])

    def test_cancel_orphan_child_after_commit_crash_never_creates_missing_siblings(self):
        self.proofs.append({**self.proofs[0], 'room_id': '!second:local'})
        ticket = self.prepare()
        original = self.control.journal.submit_crm
        def crash_after_child(**kwargs):
            original(**kwargs); raise SystemExit('journal child exists but parent mapping and sibling do not')
        self.control.journal.submit_crm = crash_after_child
        with self.assertRaises(SystemExit): self.commit(ticket)
        self.control.journal.submit_crm = original
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM crm_control_children').fetchone()[0], 0)
        result = self.control.ingest_direct(self.direct('cancel'), MASTER_ROOM)
        self.assertTrue(result['safe_to_change_membership']); self.assertEqual(result['state'], 'canceled')
        self.commit(ticket); self.control._drain()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM crm_policy_operations').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT state FROM crm_policy_operations').fetchone()[0], 'canceled')
        self.assertEqual(self.writes, [])

    def test_cancel_between_parent_check_and_child_insert_cannot_resurrect_completed_parent(self):
        ticket = self.prepare(); original = self.control.journal.submit_crm
        cancellations = []
        def cancel_before_insert(**kwargs):
            cancellations.append(self.control.ingest_direct(self.direct('cancel'), MASTER_ROOM))
            return original(**kwargs)
        self.control.journal.submit_crm = cancel_before_insert
        self.commit(ticket)
        self.assertTrue(cancellations[0]['safe_to_change_membership'])
        self.assertEqual(self.control._command('grant')['state'], 'complete')
        self.control._drain()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.db.execute('SELECT state FROM crm_policy_operations').fetchone()[0], 'rejected')

    def test_cancel_unknown_then_rebind_never_erases_attempted_ordering_barrier(self):
        original = self.uplink.local; delayed = []
        def unknown(method, path, content=None, **kwargs):
            if method == 'PUT' and path.endswith('/account_data/com.jkali.share_override'):
                delayed.append(copy.deepcopy(content)); raise TimeoutError('still running')
            return original(method, path, content, **kwargs)
        self.uplink.local = unknown
        ticket = self.prepare(); self.commit(ticket); self.control._drain()
        old_cancel = self.direct('cancel')
        self.assertFalse(self.control.ingest_direct(old_cancel, MASTER_ROOM)['safe_to_change_membership'])
        self.cfg.master_authority_id = 'replacement-authority'; self.control.refresh(True)
        with self.assertRaisesRegex(ValueError, 'stale-direct-authority'):
            self.control.ingest_direct(old_cancel, MASTER_ROOM)
        self.control._drain()
        self.assertEqual(len(delayed), 1)
        self.assertTrue(self.control.blocks_dispatch(ROOM)); self.assertTrue(self.control.blocks_sharing(ROOM))
        self.assertFalse(json.loads(self.control._command('grant')['result_json'])['safe_to_change_membership'])

    def test_cancel_applied_keeps_direct_and_replay_does_not_undo_subsequent_private(self):
        ticket = self.prepare(); self.commit(ticket); self.control._drain()
        result = self.control.ingest_direct(self.direct('cancel'), MASTER_ROOM)
        self.assertTrue(result['safe_to_change_membership']); self.assertEqual(result['state'], 'applied')
        self.assertEqual(self.remote.get('state'), 'direct')
        self.control.journal.submit_manual(operation_id='later-private', actor=LOCAL, room_id=ROOM,
            expected_revision=self.control.journal.inspect(ROOM)['revision'], level='private')
        self.control._drain()
        self.control.ingest_direct(self.direct('cancel'), MASTER_ROOM); self.commit(ticket); self.control._drain()
        self.assertEqual([body.get('state') for body in self.writes], ['direct', 'private'])


if __name__ == '__main__':
    unittest.main()
