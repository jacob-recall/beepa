#!/usr/bin/env python3
"""Tailscale mapping script with fake CLI/health; never changes real Serve."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ServeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tools = self.root / 'tools'
        self.tools.mkdir()
        shutil.copyfile(ROOT / 'master/tailscale-serve.sh', self.root / 'tailscale-serve.sh')
        (self.root / 'tokens.local').write_text("MASTER_MANAGER_TOKEN='fixture'\n")
        self.env = dict(os.environ, PATH=str(self.tools) + os.pathsep + os.environ['PATH'], TEST_SERVE_LOG=str(self.root / 'calls'))
        self.tool('tailscale', '''if [ "$1" = "status" ]; then
  if [ "$2" = "--json" ]; then printf '{"Self":{"DNSName":"master.fixture.ts.net."}}'; fi
  exit 0
fi
printf '%s\\n' "$*" >> "$TEST_SERVE_LOG"
exit "${TEST_SERVE_RC:-0}"''')
        self.tool('curl', 'exit "${TEST_HEALTH_RC:-0}"')

    def tool(self, name, code):
        path = self.tools / name
        path.write_text('#!/bin/sh\n' + code + '\n')
        path.chmod(0o755)

    def run_script(self):
        return subprocess.run(['/bin/bash', str(self.root / 'tailscale-serve.sh')],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def test_matrix_address_preserved_and_console_gateway_selected(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.root / 'calls').read_text()
        self.assertIn('--https=443 http://127.0.0.1:8017', calls)
        self.assertIn('--https=8443 http://127.0.0.1:8019', calls)
        self.assertNotIn('funnel', calls)
        tokens = (self.root / 'tokens.local').read_text()
        self.assertIn("MASTER_PUBLIC_URL='https://master.fixture.ts.net'", tokens)
        self.assertIn("ENROLL_PUBLIC_URL='https://master.fixture.ts.net:8443'", tokens)
        self.assertIn("MASTER_MANAGER_TOKEN='fixture'", tokens)

    def test_external_state_tokens_are_updated(self):
        state = self.root/'external-state'
        (state/'master').mkdir(parents=True)
        (state/'master/tokens.local').write_text("MASTER_MANAGER_TOKEN='external-fixture'\n")
        self.env['BEEPA_INSTALL_ROOT'] = str(state)
        self.assertEqual(self.run_script().returncode, 0)
        self.assertIn('MASTER_PUBLIC_URL=', (state/'master/tokens.local').read_text())
        self.assertEqual((self.root/'tokens.local').read_text(), "MASTER_MANAGER_TOKEN='fixture'\n")

    def test_unhealthy_gateway_does_not_change_mapping_or_advertise(self):
        self.env['TEST_HEALTH_RC'] = '22'
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'calls').exists())
        self.assertEqual((self.root / 'tokens.local').read_text(), "MASTER_MANAGER_TOKEN='fixture'\n")

    def test_failed_serve_does_not_advertise_success(self):
        self.env['TEST_SERVE_RC'] = '1'
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / 'tokens.local').read_text(), "MASTER_MANAGER_TOKEN='fixture'\n")

    def test_configured_gateway_port_is_applied(self):
        self.env['MASTER_GATEWAY_PORT'] = '8123'
        self.assertEqual(self.run_script().returncode, 0)
        self.assertIn('http://127.0.0.1:8123', (self.root / 'calls').read_text())


if __name__ == '__main__':
    unittest.main()
