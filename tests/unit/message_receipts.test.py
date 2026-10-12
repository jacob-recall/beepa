import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../agents/uplink'))
import uplink
import message_receipts as receipts


class ReceiptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'state.db')
        self.u = object.__new__(uplink.Uplink)
        self.u.db = uplink.Uplink._open_db(self.path)
        self.addCleanup(lambda: self.u.db.close())
        self.u.cfg = SimpleNamespace(local_user='@me:localhost')
        self.u.self_mxids = set()
        self.u.db.execute("INSERT INTO mirror_rooms(local_room_id,master_room_id,source) VALUES ('!l:localhost','!m:master','whatsapp')")
        self.u.db.commit()
        self.u.active_link_for_dispatch = lambda: True
        self.u.archive_level = lambda room: 'share'
        self.u.master = Mock(return_value={})

    def receipt(self, target='$a', user='@peer:localhost', ts=9999999999999):
        return {'ephemeral': {'events': [{'type': 'm.receipt', 'content': {target: {'m.read': {user: {'ts': ts}}}}}]}}

    def queue(self, room=None):
        receipts.queue(self.u, '!l:localhost', '!m:master', room or self.receipt())

    def test_exact_target_mapping_outage_restart(self):
        self.queue()
        receipts.flush(self.u)
        self.u.master.assert_not_called()  # unknown target must not infer a different event
        self.u.record_delivery('!m:master', '$b', '$masterB')
        receipts.flush(self.u)
        self.u.master.assert_not_called()
        self.u.record_delivery('!m:master', '$a', '$masterA')
        self.u.master.side_effect = uplink.MasterUnreachable('offline')
        with self.assertRaises(uplink.MasterUnreachable): receipts.flush(self.u)
        self.u.db.close()
        self.u.db = uplink.Uplink._open_db(self.path)
        self.u.master.side_effect = None
        receipts.flush(self.u)
        args = self.u.master.call_args.args
        self.assertTrue(args[1].endswith('/com.jkali.message_receipt/%24masterA'))
        self.assertTrue(args[2]['read'])
        self.assertEqual(self.u.db.execute('SELECT dirty FROM message_receipts').fetchone()[0], 0)
        count = self.u.master.call_count
        self.queue(); receipts.flush(self.u)
        self.assertEqual(self.u.master.call_count, count)

    def test_status_trust_and_no_false_delivery(self):
        def status(sender, **extra):
            return {'timeline': {'events': [{'type': receipts.STATUS_TYPE, 'sender': sender, 'content': {
                'status': 'SUCCESS', 'm.relates_to': {'rel_type': 'm.reference', 'event_id': '$a'}, **extra}}]}}
        parse = lambda room: receipts.parse_receipts(room, {'@me:localhost'}, uplink.SOURCE_BOT_MXIDS, '@whatsappbot:localhost')
        self.assertEqual(parse(status('@evil:localhost', delivered_to_users=['@peer:localhost'])), {})
        self.assertFalse(parse(status('@whatsappbot:localhost'))['$a']['delivered'])
        self.assertFalse(parse(status('@whatsappbot:localhost', delivered_to_users=['@me:localhost']))['$a']['delivered'])
        self.assertTrue(parse(status('@whatsappbot:localhost', delivered_to_users=['@peer:localhost']))['$a']['delivered'])
        self.assertEqual(parse(self.receipt(user='@whatsappbot:localhost')), {})
        self.assertEqual(parse(self.receipt(ts=float('nan'))), {})

    def test_partial_updates_consent_generation(self):
        self.queue()
        self.queue(self.receipt('$b', '@me:localhost'))
        self.assertEqual(self.u.db.execute('SELECT count(*) FROM message_receipts').fetchone()[0], 2)
        self.u.active_link_for_dispatch = lambda: False
        receipts.flush(self.u); self.u.master.assert_not_called()
        self.u.active_link_for_dispatch = lambda: True
        self.u.archive_level = lambda room: 'private'
        receipts.flush(self.u)
        self.assertEqual(self.u.db.execute('SELECT count(*) FROM message_receipts').fetchone()[0], 0)
        self.queue(); self.u.mark_revoking('!l:localhost')
        self.assertEqual(self.u.db.execute('SELECT count(*) FROM message_receipts').fetchone()[0], 0)
        self.u.db.execute('DELETE FROM mirror_lifecycle')
        self.u.db.execute("UPDATE mirror_rooms SET master_room_id='!new:master'")
        self.queue(); receipts.flush(self.u)
        self.u.master.assert_not_called()
        self.assertEqual(self.u.db.execute('SELECT count(*) FROM message_receipts').fetchone()[0], 0)


if __name__ == '__main__': unittest.main()
