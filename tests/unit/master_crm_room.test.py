#!/usr/bin/env python3
"""Independent CRM bootstrap security/retry tests; injected transport only."""
import copy
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'master'))
import crm
import enroll

MANAGER = '@boss:fixture.example'
ROOM = '!crm:fixture.example'
BASE = 'http://fixture.invalid'
OP = 'com.jkali.crm.operation'
DEFAULT_STATE = object()


def state():
    # Authored independently from room_payload/power_levels to catch contract drift.
    content = {
        'm.room.create': {'type': 'com.jkali.crm', 'room_version': '11', 'm.federate': False},
        'com.jkali.crm.schema': {'version': 1, 'manager_mxid': MANAGER},
        'm.room.power_levels': {'users': {MANAGER: 100}, 'users_default': 0,
            'events_default': 101, 'state_default': 100, 'invite': 101,
            'kick': 101, 'ban': 101, 'redact': 100,
            'events': {OP: 100, 'm.room.message': 101, 'm.room.encrypted': 101}},
        'm.room.join_rules': {'join_rule': 'invite'},
        'm.room.history_visibility': {'history_visibility': 'joined'},
        'm.room.guest_access': {'guest_access': 'forbidden'},
    }
    events = [dict(type=t, state_key='', sender=MANAGER, content=c) for t, c in content.items()]
    events.append(dict(type='m.room.member', state_key=MANAGER, sender=MANAGER,
                       content={'membership': 'join'}))
    return events


class Transport:
    def __init__(self, exists=True, events=DEFAULT_STATE, create_error=None, create_commits=True):
        self.exists = exists
        self.events = state() if events is DEFAULT_STATE else events
        self.create_error = create_error
        self.create_commits = create_commits
        self.calls = []
        self.alias_error = None

    def __call__(self, method, url, headers=None, data=None):
        self.calls.append((method, url, headers, data))
        assert headers == {'Authorization': 'Bearer fixture-token'}
        if '/directory/room/' in url:
            if self.alias_error:
                return self.alias_error
            return (200, json.dumps({'room_id': ROOM}).encode()) if self.exists else (
                404, b'{"errcode":"M_NOT_FOUND"}')
        if method == 'POST' and url.endswith('/createRoom'):
            self.exists = self.create_commits
            if self.create_error:
                raise self.create_error
            return 200, b'{"room_id":"!untrusted-response:elsewhere"}'
        if method == 'GET' and url.endswith('/state'):
            return 200, json.dumps(self.events).encode()
        raise AssertionError('unexpected request: ' + method + ' ' + url)

    def ensure(self):
        return crm.ensure_room(request=self, cs_base=BASE, token='fixture-token', manager_mxid=MANAGER)

    @property
    def mutations(self):
        return [c for c in self.calls if c[0] != 'GET']


class CRMRoomTests(unittest.TestCase):
    def test_existing_room_is_read_only_and_identity_pinned(self):
        io = Transport()
        self.assertEqual(io.ensure(), {'room_id': ROOM, 'manager_mxid': MANAGER, 'schema_version': 1})
        self.assertEqual(io.mutations, [])
        self.assertIn('%23beepa-crm-v1%3Afixture.example', io.calls[0][1])

    def test_create_is_alias_reconciled_then_verified_and_repeat_does_not_create(self):
        io = Transport(exists=False)
        self.assertEqual(io.ensure()['room_id'], ROOM)
        payload = json.loads(io.mutations[0][3])
        self.assertEqual(payload['room_alias_name'], 'beepa-crm-v1')
        self.assertEqual(payload['room_version'], '11')
        self.assertEqual(payload['visibility'], 'private')
        self.assertEqual(payload['creation_content'], {'type': 'com.jkali.crm', 'm.federate': False})
        self.assertNotIn('invite', payload)
        expected_levels = next(e['content'] for e in state() if e['type'] == 'm.room.power_levels')
        self.assertEqual(payload['power_level_content_override'], expected_levels)
        io.ensure()
        self.assertEqual(len(io.mutations), 1)

    def test_ambiguous_create_recovers_without_second_post(self):
        io = Transport(exists=False, create_error=TimeoutError('secret transport details'))
        self.assertEqual(io.ensure()['room_id'], ROOM)
        self.assertEqual(len(io.mutations), 1)
        io.ensure()
        self.assertEqual(len(io.mutations), 1)

    def test_unconfirmed_create_stops_without_blind_retry(self):
        io = Transport(exists=False, create_error=OSError('lost'), create_commits=False)
        with self.assertRaisesRegex(crm.CRMRoomError, 'unconfirmed'):
            io.ensure()
        self.assertEqual(len(io.mutations), 1)

    def test_alias_errors_never_trigger_create(self):
        for response in [(403, b'{}'), (500, b'{}'), (404, b'{}'), (404, b'bad'),
                         (200, b'[]'), (200, b'{"room_id":"!bad/path:fixture"}'),
                         (200, b'{"room_id":null}')]:
            with self.subTest(response=response):
                io = Transport(); io.alias_error = response
                with self.assertRaises(crm.CRMRoomError):
                    io.ensure()
                self.assertEqual(io.mutations, [])

    def test_missing_duplicate_foreign_and_malformed_state_fail_closed(self):
        bad = [None, {}, [None], [{'type': 3, 'state_key': ''}]]
        for index in range(len(state())):
            events = state(); events.pop(index); bad.append(events)
        bad.append(state() + [copy.deepcopy(state()[0])])
        for membership in ['join', 'invite', 'leave', 'ban']:
            bad.append(state() + [dict(type='m.room.member', state_key='@foreign:fixture.example',
                sender=MANAGER, content={'membership': membership})])
        for index in range(len(state()) - 1):
            events = state(); events[index]['sender'] = '@foreign:fixture.example'; bad.append(events)
        for typ in ['m.room.encryption', 'm.room.tombstone']:
            bad.append(state() + [dict(type=typ, state_key='', sender=MANAGER, content={})])
        for events in bad:
            with self.subTest(events=events):
                io = Transport(events=events)
                with self.assertRaises(crm.CRMRoomError):
                    io.ensure()
                self.assertEqual(io.mutations, [], 'never repair a foreign or compromised room')

    def test_security_shape_tampering_is_rejected(self):
        mutations = [
            ('m.room.create', 'room_version', '12'), ('m.room.create', 'm.federate', True),
            ('m.room.create', 'type', 'other'), ('com.jkali.crm.schema', 'version', True),
            ('com.jkali.crm.schema', 'manager_mxid', '@foreign:fixture.example'),
            ('m.room.join_rules', 'join_rule', 'public'),
            ('m.room.history_visibility', 'history_visibility', 'world_readable'),
            ('m.room.guest_access', 'guest_access', 'can_join'),
        ]
        for field in ['invite', 'kick', 'ban', 'events_default', 'state_default', 'redact']:
            mutations.append(('m.room.power_levels', field, 0))
        mutations += [('m.room.power_levels', 'users', {MANAGER: 100, '@foreign:fixture.example': 100}),
                      ('m.room.power_levels', 'events', {OP: 100, 'm.room.message': 0}),
                      ('m.room.power_levels', 'users_default', 100)]
        for typ, key, value in mutations:
            with self.subTest(typ=typ, key=key, value=value):
                events = state(); next(e for e in events if e['type'] == typ)['content'][key] = value
                with self.assertRaises(crm.CRMRoomError):
                    Transport(events=events).ensure()

    def test_invalid_manager_performs_no_io(self):
        for manager in ['', None, '@boss:fixture.example/evil', '@boss:fixture.example\n']:
            io = Transport()
            with self.assertRaises(crm.CRMRoomError):
                crm.ensure_room(request=io, cs_base=BASE, token='fixture-token', manager_mxid=manager)
            self.assertEqual(io.calls, [])


class CRMHandlerTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        def provision(**kwargs):
            self.calls.append(kwargs)
            return {'room_id': ROOM, 'manager_mxid': MANAGER, 'schema_version': 1}
        self.patches = [patch.object(enroll, '_manager_mxid', return_value=MANAGER),
            patch.object(enroll, '_cs_base', return_value=BASE),
            patch.object(enroll, '_whoami', side_effect=lambda base, token: {
                'manager': MANAGER, 'foreign': '@foreign:fixture.example'}.get(token)),
            patch.object(enroll, 'ensure_crm_room', side_effect=provision)]
        for item in self.patches:
            item.start(); self.addCleanup(item.stop)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), enroll._make_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start(); self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def request(self, token=None, body=b'{}'):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        headers = {'Content-Type': 'application/json'}
        if token is not None:
            headers['Authorization'] = 'Bearer ' + token
        try:
            conn.request('POST', '/admin/crm-room', body, headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_authentication_precedes_parse_and_provision(self):
        for token, expected in [(None, 401), ('invalid', 401), ('foreign', 403)]:
            self.assertEqual(self.request(token, b'not json')[0], expected)
        self.assertEqual(self.calls, [])

    def test_no_caller_control_over_room_or_identity(self):
        for body in [b'[]', b'null', b'bad', b'{"room_id":"!foreign:fixture"}',
                     b'{"manager_mxid":"@foreign:fixture"}', b'{"event_type":"m.room.message"}']:
            self.assertEqual(self.request('manager', body)[0], 400)
        self.assertEqual(self.calls, [])
        status, body = self.request('manager')
        self.assertEqual(status, 200)
        self.assertEqual(body['room_id'], ROOM)
        self.assertEqual(self.calls[0]['manager_mxid'], MANAGER)
        self.assertEqual(self.calls[0]['token'], 'manager')

    def test_auth_and_transport_failures_do_not_expose_secrets(self):
        with patch.object(enroll, '_whoami', side_effect=OSError('TOKEN_SECRET')):
            status, body = self.request('manager')
            self.assertEqual(status, 502); self.assertNotIn('TOKEN_SECRET', json.dumps(body))
        self.assertEqual(self.calls, [])
        with patch.object(enroll, 'ensure_crm_room', side_effect=OSError('TOKEN_SECRET')):
            status, body = self.request('manager')
            self.assertEqual(status, 502); self.assertNotIn('TOKEN_SECRET', json.dumps(body))
        with patch.object(enroll, 'ensure_crm_room', side_effect=crm.CRMRoomError('Room rejected')):
            self.assertEqual(self.request('manager')[0], 409)


if __name__ == '__main__':
    unittest.main()
