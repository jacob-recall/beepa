#!/usr/bin/env python3
"""Exercise the unchanged downloader in a temporary checkout with fake tools."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'imessage').mkdir()
        self.script = self.root / 'imessage/build-cli.sh'
        shutil.copyfile(ROOT / 'imessage/build-cli.sh', self.script)
        self.tools = self.root / 'fake-tools'
        self.tools.mkdir()
        self.env = dict(os.environ, PATH=str(self.tools) + os.pathsep + os.environ['PATH'])
        self.env.pop('SKIP_IMESSAGE', None)
        self.env.pop('BEEPA_INSTALL_ROOT', None)
        self.tool('uname', 'echo Darwin')
        # These intercept every download/signature call, even on a real Mac.
        self.tool('curl', 'exit 22')
        self.tool('codesign', 'exit 90')

    def tool(self, name, source):
        path = self.tools / name
        path.write_text('#!/bin/sh\n' + source + '\n')
        path.chmod(0o755)

    def run_build(self):
        return subprocess.run(['/bin/bash', str(self.script)], env=self.env, capture_output=True, text=True, timeout=10)

    def test_existing_executable_is_left_byte_and_inode_identical(self):
        native = self.root / 'imessage/bin/imessage-cli'
        native.parent.mkdir()
        native.write_bytes(b'never execute this fixture')
        native.chmod(0o755)
        before = native.stat()
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('already present', result.stderr)
        self.assertEqual(native.read_bytes(), b'never execute this fixture')
        self.assertEqual(native.stat().st_ino, before.st_ino)
        self.assertEqual(native.stat().st_mtime_ns, before.st_mtime_ns)

    def test_external_state_preserves_authorized_binary_without_relocation(self):
        state = self.root / 'external state'
        native = state / 'imessage/bin/imessage-cli'
        native.parent.mkdir(parents=True)
        native.write_bytes(b'authorized fixture')
        native.chmod(0o755)
        inode = native.stat().st_ino
        self.env['BEEPA_INSTALL_ROOT'] = str(state)
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('already present', result.stderr)
        self.assertEqual(native.stat().st_ino, inode)
        self.assertEqual(native.read_bytes(), b'authorized fixture')
        self.assertFalse((self.root / 'imessage/bin').exists())

    def test_offline_download_is_nonfatal_and_installs_nothing(self):
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('download failed', result.stderr)
        self.assertFalse((self.root / 'imessage/bin/imessage-cli').exists())

    def test_explicit_skip_and_non_mac_skip_need_no_downloader(self):
        self.env['SKIP_IMESSAGE'] = '1'
        self.assertIn('skipping', self.run_build().stderr)
        self.env.pop('SKIP_IMESSAGE')
        self.tool('uname', 'echo Linux')
        self.assertIn('Mac-only', self.run_build().stderr)

    def fake_archive(self):
        self.tool('curl', 'exit 0')
        self.tool('shasum', 'echo 7629c828593faef7e324cd86a94df2e8fdbe7ae48c7b6f8d22167589627a77e6')
        self.tool('tar', 'printf "fixture" > "$4/imessage-cli"\nchmod 755 "$4/imessage-cli"')

    def test_checksum_mismatch_refuses_install(self):
        self.tool('curl', 'exit 0')
        self.tool('shasum', 'echo invalid-checksum')
        result = self.run_build()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('MISMATCH', result.stderr)
        self.assertFalse((self.root / 'imessage/bin/imessage-cli').exists())

    def test_invalid_signature_or_team_refuses_install(self):
        self.fake_archive()
        result = self.run_build()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('signature verification FAILED', result.stderr)
        self.tool('codesign', 'if [ "$1" = "--verify" ]; then exit 0; fi\necho TeamIdentifier=WRONG >&2')
        result = self.run_build()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unexpected code-signing team', result.stderr)
        self.assertFalse((self.root / 'imessage/bin/imessage-cli').exists())

    def test_validated_fixture_download_then_second_run_preserves_it(self):
        self.fake_archive()
        self.tool('codesign', 'if [ "$1" = "--verify" ]; then exit 0; fi\necho TeamIdentifier=PZYM8XX95Q >&2')
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stderr)
        native = self.root / 'imessage/bin/imessage-cli'
        before = native.stat()
        self.assertEqual(native.read_bytes(), b'fixture')
        self.assertEqual(self.run_build().returncode, 0)
        self.assertEqual(native.stat().st_ino, before.st_ino)


if __name__ == '__main__':
    unittest.main()
