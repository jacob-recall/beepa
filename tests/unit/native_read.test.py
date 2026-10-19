"""Read-state authority, durable projection and manager request boundaries."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'agents/uplink'))
import uplink
import native_read

class NativeReadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.u = object.__new__(uplink.Uplink)
        self.u.db = uplink.Uplink._open_db(os.path.join(self.tmp.name, 'state.db'))
        self.addCleanup(lambda: self.u.db.close())
        self.u.cfg = SimpleNamespace(imessage_bot='@imessagebot:local', manager_mxid='@manager:master', master_user='@peer:master')
        self.u._crm_control = SimpleNamespace(generation='generation')
        self.u.db.execute("INSERT INTO mirror_rooms(local_room_id,master_room_id,source) VALUES ('!local:local','!mirror:master','imessage')")
        self.u.db.commit()
        self.u.meta_set('master_proposals_room', '!proposals:master')
        self.u.active_link_for_dispatch = lambda: True
        self.u.archive_level = lambda room: 'share'
        self.u.master = Mock(return_value={}); self.u.local = Mock(return_value={'event_id': '$local-command'})
        self.c = dict(schema=1, supported=True, unread_count=0, marked_unread=False, revision='a'*64, latest_ts=1, observed_ts=1000)
        self.section = {'state': {'events': [dict(type=native_read.STATE, sender=self.u.cfg.imessage_bot, state_key='', content=self.c)]}}

    def observe(self, section=None, counter=900):
        return native_read.observe(self.u, '!local:local', '!mirror:master', self.section if section is None else section, {'unread_count':counter})

    def test_authoritative_zero_survives_stale_server_counts_and_restart(self):
        state = self.observe(); self.assertEqual(state['unread_count'], 0)
        self.u._queue_read_state('!local:local', '!mirror:master', state); self.u.flush_read_state()
        payload = self.u.master.call_args.args[2]
        self.assertEqual(payload['native']['unread_count'], 0)
        self.assertEqual(self.observe({}, 9999)['unread_count'], 0)
        self.u.db.close(); self.u.db = uplink.Uplink._open_db(os.path.join(self.tmp.name, 'state.db'))
        self.assertEqual(self.observe({}, 9999)['unread_count'], 0)

    def test_spoofed_and_invalid_native_state_does_not_clear(self):
        section = copy.deepcopy(self.section); section['state']['events'][0]['sender'] = '@attacker:local'
        self.assertEqual(self.observe(section)['unread_count'], 900)
        section = copy.deepcopy(self.section); section['state']['events'][0]['content']['unread_count'] = True
        self.assertEqual(self.observe(section)['unread_count'], 900)

    def test_same_count_changed_revision_is_retried_after_publish_failure(self):
        state = self.observe(); self.u._queue_read_state('!local:local', '!mirror:master', state)
        self.u.master.side_effect = OSError('offline')
        with self.assertRaises(OSError): self.u.flush_read_state()
        self.u.master.side_effect = None; self.u.flush_read_state()
        self.c.update(revision='b'*64, observed_ts=2000, last_command={'id':'read-1', 'status':'applied'})
        state = self.observe(); self.assertTrue(state['_native_changed'])
        self.u._queue_read_state('!local:local', '!mirror:master', state); self.u.flush_read_state()
        self.assertEqual(self.u.master.call_args.args[2]['native']['last_command']['status'], 'applied')

    def event(self):
        return dict(type=native_read.MASTER_REQUEST, event_id='$intent', sender='@manager:master', origin_server_ts=1000000,
            content={'target_room':'!local:local', native_read.KEY:dict(schema=1, command_id='read-1', action='unread', expected_revision='a'*64,
                expires_at=1120000, generation='generation', mirror_room='!mirror:master')})

    def test_request_forwards_only_fixed_metadata_and_is_deduplicated(self):
        self.observe()
        with patch.object(native_read.time, 'time', return_value=1000):
            native_read.forward(self.u, '!proposals:master', self.event(), suspended=False)
            native_read.forward(self.u, '!proposals:master', self.event(), suspended=False)
        self.assertEqual(self.u.local.call_count, 1)
        self.assertIn('/send/com.jkali.imessage.read_request/', self.u.local.call_args.args[1])
        self.assertNotIn('body', self.u.local.call_args.args[2])

    def test_wrong_actor_epoch_destination_expiry_private_and_suspension_refused(self):
        self.observe()
        cases = []
        for key,value in [('sender','@peer:master'),('room_id','!other:master'),('state_key',''),('origin_server_ts',1)]:
            e=self.event();e[key]=value;cases.append(e)
        for key,value in [('generation','old'),('mirror_room','!old:master'),('expires_at',1),('action','send')]:
            e=self.event();e['content'][native_read.KEY][key]=value;cases.append(e)
        with patch.object(native_read.time, 'time', return_value=1000):
            for ev in cases: native_read.forward(self.u, '!proposals:master', ev, suspended=False)
            native_read.forward(self.u, '!proposals:master', self.event(), suspended=True)
            self.u.archive_level=lambda room:'private'
            native_read.forward(self.u, '!proposals:master', self.event(), suspended=False)
        self.assertEqual(self.u.local.call_count, 0)

if __name__ == '__main__': unittest.main()
