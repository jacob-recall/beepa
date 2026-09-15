#!/usr/bin/env python3
import os
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import helper_config as h

class HelperPorts(unittest.TestCase):
    def test_default_and_instance_origins_do_not_overlap(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(h.app_origins(), ('http://127.0.0.1:8011', 'http://localhost:8011'))
            self.assertEqual(h.helper_port('gmessages'), 8020)
        with patch.dict(os.environ, {'BEEPA_APP_PORT':'8111','BEEPA_GMESSAGES_PORT':'8120','BEEPA_SESSION_PORT':'8121'}, clear=True):
            self.assertEqual(h.app_origins(), ('http://127.0.0.1:8111', 'http://localhost:8111'))
            self.assertEqual(h.helper_port('gmessages'), 8120)
            self.assertEqual(h.helper_port('session'), 8121)
            self.assertNotIn('http://127.0.0.1:18011', h.app_origins())
    def test_invalid_ports_fail_closed(self):
        for port in ('0','65536','not-a-port'):
            with patch.dict(os.environ, {'BEEPA_SESSION_PORT':port}):
                with self.assertRaises(ValueError):h.helper_port('session')

if __name__ == '__main__':unittest.main()
