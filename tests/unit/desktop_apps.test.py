#!/usr/bin/env python3
"""Launcher lifecycle regressions; no real accounts, browser, Docker or TCC."""
import importlib.util
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("install_apps", ROOT / "desktop/install_apps.py")
apps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apps)


def fake_compile(argv, check):
    if argv[0] == "/usr/bin/codesign":
        return
    bundle = Path(argv[2])
    (bundle / "Contents/MacOS").mkdir(parents=True)
    (bundle / "Contents/Resources/Scripts").mkdir(parents=True)
    (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({}))
    (bundle / "Contents/MacOS/applet").write_bytes(b"fake native applet")
    (bundle / "Contents/MacOS/applet").chmod(0o755)
    (bundle / "Contents/Resources/Scripts/main.scpt").write_text(Path(argv[3]).read_text())


class Launchers(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="beepa apps ' & ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.compiler = patch.object(apps.subprocess, "run", side_effect=fake_compile).start()
        self.addCleanup(patch.stopall)

    def info(self, bundle):
        return plistlib.loads((bundle / "Contents/Info.plist").read_bytes())

    def test_separate_apps_open_separate_roles(self):
        user = apps.install_app("user", self.root)
        master = apps.install_app("master", self.root)
        self.assertEqual(user.name, "Beepa.app")
        self.assertEqual(master.name, "Beepa Master.app")
        self.assertNotEqual(self.info(user)["CFBundleIdentifier"], self.info(master)["CFBundleIdentifier"])
        self.assertTrue(self.info(user)["BeepaURL"].endswith("/apps/user/index.html"))
        self.assertTrue(self.info(master)["BeepaURL"].endswith("/apps/master/index.html"))
        self.assertEqual(self.info(user)["CFBundleIconFile"], "Beepa.icns")
        self.assertNotEqual(self.info(user)["BeepaIconHash"], self.info(master)["BeepaIconHash"])
        for role, bundle in (("user", user), ("master", master)):
            self.assertEqual((bundle / "Contents/Resources/Beepa.icns").read_bytes(),
                             (apps.ASSETS / (role + ".icns")).read_bytes())

    def test_reinstall_preserves_custom_url_and_bundle(self):
        url = "https://master.example.ts.net/apps/master/index.html"
        bundle = apps.install_app("master", self.root, url)
        inode = bundle.stat().st_ino
        apps.install_app("master", self.root)
        self.assertEqual(bundle.stat().st_ino, inode)
        self.assertEqual(self.compiler.call_count, 4)  # compile, sign, verify, reverify
        self.assertEqual(self.info(bundle)["BeepaURL"], url)

    def test_explicit_address_change_preserves_identity(self):
        bundle = apps.install_app("master", self.root)
        identity = self.info(bundle)["CFBundleIdentifier"]
        apps.install_app("master", self.root, "https://master.example/new")
        self.assertEqual(self.info(bundle)["CFBundleIdentifier"], identity)
        self.assertEqual(self.info(bundle)["BeepaURL"], "https://master.example/new")

    def test_reinstall_repairs_nonexecutable_launcher(self):
        bundle = apps.install_app("user", self.root)
        executable = bundle / "Contents/MacOS/applet"
        executable.chmod(0o644)
        apps.install_app("user", self.root)
        self.assertTrue(executable.stat().st_mode & 0o111)

    def test_reinstall_repairs_invalid_signature(self):
        bundle = apps.install_app("user", self.root)
        def damaged(argv, check):
            if argv[0] == "/usr/bin/codesign" and argv[-1] == str(bundle):
                raise subprocess.CalledProcessError(1, argv)
            fake_compile(argv, check)
        self.compiler.side_effect = damaged
        apps.install_app("user", self.root)
        self.assertEqual(self.compiler.call_count, 7)

    def test_compile_failure_preserves_working_install(self):
        bundle = apps.install_app("user", self.root)
        original = self.info(bundle)
        self.compiler.side_effect = subprocess.CalledProcessError(1, "osacompile")
        with self.assertRaises(subprocess.CalledProcessError):
            apps.install_app("user", self.root, "https://hub.example/")
        self.assertEqual(self.info(bundle), original)

    def test_refuses_foreign_application(self):
        foreign = self.root / "Beepa.app"
        foreign.mkdir()
        marker = foreign / "keep.txt"
        marker.write_text("unrelated app")
        with self.assertRaises(ValueError):
            apps.install_app("user", self.root)
        self.assertEqual(marker.read_text(), "unrelated app")

    def test_signature_failure_preserves_working_install(self):
        bundle = apps.install_app("user", self.root)
        original = self.info(bundle)

        def fail_sign(argv, check):
            if argv[0] == "/usr/bin/codesign":
                raise subprocess.CalledProcessError(1, argv)
            fake_compile(argv, check)

        self.compiler.side_effect = fail_sign
        with self.assertRaises(subprocess.CalledProcessError):
            apps.install_app("user", self.root, "https://hub.example/")
        self.assertEqual(self.info(bundle), original)

    def test_refuses_symlink(self):
        other = self.root / "other"
        other.mkdir()
        (self.root / "Beepa.app").symlink_to(other)
        with self.assertRaises(ValueError):
            apps.install_app("user", self.root)
        self.assertTrue(other.is_dir())

    def test_rejects_non_web_urls_and_credentials(self):
        for url in ("file:///tmp/x", "javascript:alert(1)", "https://u:p@host/x",
                    "http:///x", "http://host:99999", "https://host/\nunsafe"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                apps.install_app("user", self.root, url)
        self.compiler.assert_not_called()

    def test_url_is_literal_not_applescript_or_shell(self):
        url = 'https://example.test/a?x="quoted"&y=\'single\'&z=$(touch%20/tmp/no)'
        source = apps.launcher_source("Beepa", url)
        self.assertIn('set appURL to "https://example.test/a?x=\\"quoted\\"', source)
        self.assertIn('"/usr/bin/open " & quoted form of appURL', source)
        self.assertIn('--max-time 4', source)
        self.assertIn('--globoff', source)
        self.assertNotIn(str(ROOT), source)

    def test_install_leaves_unrelated_state_untouched(self):
        fixtures = ("session.local.json", "imessage-cli", "uplink.db")
        before = {}
        for name in fixtures:
            path = self.root / name
            path.write_bytes(b"fixture only")
            before[name] = (path.stat().st_ino, path.read_bytes())
        apps.install_app("user", self.root / "Applications")
        apps.install_app("master", self.root / "Applications")
        for name in fixtures:
            path = self.root / name
            self.assertEqual((path.stat().st_ino, path.read_bytes()), before[name])


if __name__ == "__main__":
    unittest.main()
