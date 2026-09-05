#!/usr/bin/env python3
"""Disposable nginx verifies external bootstrap creation and atomic replacement."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from install_config import ensure_manifest, initialize_state, atomic_write, save_manifest
from beepa_update import views_overlay, Updater


def main():
    name = 'beepa-test-views-' + uuid.uuid4().hex[:12]
    started = False
    with tempfile.TemporaryDirectory(prefix=name) as tmp:
        base = Path(tmp).resolve()
        checkout, state = base / 'checkout', base / 'runtime'
        checkout.mkdir()
        ensure_manifest(checkout, env={'LOCAL_LOCALPART': 'fixture', 'BEEPA_STATE_ROOT': str(state), 'BEEPA_LOG_ROOT': str(base / 'logs')})
        manifest = initialize_state(checkout)
        manifest.update(roles=['master', 'teammate'], compose_project=name, master_compose_project=name + '-master')
        save_manifest(checkout, manifest)
        atomic_write(state / '.env', 'POSTGRES_PASSWORD=fixture\nHOST_UID=501\nHOST_GID=20\n')
        atomic_write(state / 'master/.env', 'MASTER_POSTGRES_PASSWORD=fixture\nHOST_UID=501\nHOST_GID=20\n')
        for role, compose in Updater(checkout).compositions(manifest, ROOT):
            compiled = json.loads(subprocess.run(compose + ['config', '--format', 'json'], check=True, capture_output=True, text=True).stdout)
            assert compiled['name'] == (name + '-master' if role == 'master' else name)
            mounts = compiled['services']['synapse']['volumes']
            expected = state / 'master/synapse' if role == 'master' else state / 'synapse'
            assert any(v.get('source') == str(expected) and v.get('target') == '/data' for v in mounts)
        # Exercise production overlay mounts, with production code from this tree.
        volumes = views_overlay(state, ROOT)['services']['views']['volumes']
        image = re.search(r'image:\s+(\S+)', (ROOT / 'docker-compose.apps.yml').read_text())[1]
        command = ['docker', 'run', '--detach', '--name', name, '--label', 'beepa.test=views-runtime',
                   '--publish', '127.0.0.1::80', '--read-only', '--tmpfs', '/var/cache/nginx', '--tmpfs', '/var/run']
        for volume in volumes:
            command += ['--mount', 'type=bind,src=' + volume['source'] + ',dst=' + volume['target'] + ',readonly']
        command.append(image)
        try:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
            started = True
            address = subprocess.run(['docker', 'port', name, '80/tcp'], check=True, capture_output=True, text=True).stdout.strip()
            assert re.fullmatch(r'127\.0\.0\.1:\d+', address), address
            origin = 'http://' + address
            deadline = time.monotonic() + 20
            while True:
                try:
                    urllib.request.urlopen(origin + '/apps/user/index.html', timeout=1).close()
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.2)
            for relative in ('apps/user/session.local.json', 'apps/master/session.local.json', 'apps/user/connect.local.json'):
                url = origin + '/' + relative
                try:
                    urllib.request.urlopen(url, timeout=2)
                    raise AssertionError('Missing bootstrap unexpectedly served')
                except urllib.error.HTTPError as exc:
                    assert exc.code == 404, exc.code
                for revision in (1, 2):
                    expected = {'fixture': revision}
                    atomic_write(state / relative, json.dumps(expected))
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            with urllib.request.urlopen(url, timeout=2) as response:
                                actual = json.load(response)
                                assert response.headers['Cache-Control'] == 'no-store'
                            if actual == expected:
                                break
                        except urllib.error.HTTPError as exc:
                            if exc.code not in (403, 404):
                                raise
                        if time.monotonic() >= deadline:
                            raise AssertionError('Atomic bootstrap replacement was not visible through the runtime directory mount')
                        time.sleep(.1)
            print('PASS: external runtime bootstrap missing → created → atomically replaced; all three URLs, same nginx container')
        except Exception:
            if started:
                subprocess.run(['docker', 'logs', name], check=False)
                subprocess.run(['docker', 'exec', name, 'ls', '-lan', '/usr/share/nginx/runtime/apps/user'], check=False)
            raise
        finally:
            if started:
                subprocess.run(['docker', 'rm', '--force', name], check=True, stdout=subprocess.DEVNULL)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
