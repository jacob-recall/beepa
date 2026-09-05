#!/usr/bin/env python3
"""Disposable nginx checks private bootstrap ownership and atomic replacement."""
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
from beepa_update import Updater


def compile_services(checkout, state, manifest, uid, gid):
    identity = 'HOST_UID=%d\nHOST_GID=%d\n' % (uid, gid)
    atomic_write(state / '.env', 'POSTGRES_PASSWORD=fixture\n' + identity)
    atomic_write(state / 'master/.env', 'MASTER_POSTGRES_PASSWORD=fixture\n' + identity)
    views = None
    for role, compose in Updater(checkout).compositions(manifest, ROOT):
        compiled = json.loads(subprocess.run(compose + ['config', '--format', 'json'], check=True, capture_output=True, text=True).stdout)
        assert compiled['name'] == (manifest['master_compose_project'] if role == 'master' else manifest['compose_project'])
        if role != 'local-ui':
            mounts = compiled['services']['synapse']['volumes']
            expected = state / 'master/synapse' if role == 'master' else state / 'synapse'
            assert any(v.get('source') == str(expected) and v.get('target') == '/data' for v in mounts)
        if role in ('teammate', 'local-ui'):
            views = compiled['services']['views']
    assert views is not None
    return views


def exercise(views, state, linux_owned=False):
    name = 'beepa-test-views-' + uuid.uuid4().hex[:12]
    command = ['docker', 'run', '--detach', '--name', name, '--label', 'beepa.test=views-runtime',
               '--publish', '127.0.0.1::80', '--read-only']
    if views.get('user'):
        command += ['--user', views['user']]
    for mount in views['tmpfs']:
        command += ['--tmpfs', mount]
    for option in views.get('security_opt', []):
        command += ['--security-opt', option]
    for volume in views['volumes']:
        if linux_owned and volume['target'] == '/usr/share/nginx/runtime/apps':
            continue
        command += ['--mount', 'type=bind,src=' + volume['source'] + ',dst=' + volume['target'] + ',readonly']
    if linux_owned:
        # Native Linux tmpfs ownership bypasses Docker Desktop's host-file mapping.
        command += ['--tmpfs', '/usr/share/nginx/runtime:uid=1001,gid=1001,mode=0700']
    command.append(views['image'])
    started = False
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
                if linux_owned:
                    target = '/usr/share/nginx/runtime/' + relative
                    subprocess.run(['docker', 'exec', '--user', '1001:1001', name, 'sh', '-c',
                        'umask 077; mkdir -p "$(dirname "$1")"; printf "%s" "$2" > "$1.tmp"; mv -f "$1.tmp" "$1"',
                        'fixture', target, json.dumps(expected)], check=True)
                    attributes = subprocess.run(['docker', 'exec', '--user', '1001:1001', name,
                        'stat', '-c', '%a %u %g', target], check=True, capture_output=True, text=True).stdout.strip()
                    assert attributes == '600 1001 1001', attributes
                else:
                    atomic_write(state / relative, json.dumps(expected))
                    assert (state / relative).stat().st_mode & 0o777 == 0o600
                    assert (state / relative).stat().st_uid == os.getuid()
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
                        raise AssertionError('Private atomic bootstrap replacement was not visible to the production nginx worker')
                    time.sleep(.1)
        print('PASS: %s ownership; mode600 bootstrap missing → created → atomically replaced; all three URLs' %
              ('native Linux UID1001' if linux_owned else 'host UID%d' % os.getuid()))
    except Exception:
        if started:
            subprocess.run(['docker', 'logs', name], check=False)
        raise
    finally:
        if started:
            subprocess.run(['docker', 'rm', '--force', name], check=True, stdout=subprocess.DEVNULL)


def main():
    with tempfile.TemporaryDirectory(prefix='beepa-test-views-') as tmp:
        base = Path(tmp).resolve()
        checkout, state = base / 'checkout', base / 'runtime'
        checkout.mkdir()
        ensure_manifest(checkout, env={'LOCAL_LOCALPART': 'fixture', 'BEEPA_STATE_ROOT': str(state), 'BEEPA_LOG_ROOT': str(base / 'logs')})
        manifest = initialize_state(checkout)
        manifest.update(roles=['master', 'teammate'], compose_project='beepa-test-views', master_compose_project='beepa-test-views-master')
        save_manifest(checkout, manifest)
        views = compile_services(checkout, state, manifest, os.getuid(), os.getgid())
        exercise(views, state)
        manifest['roles'] = ['master']
        linux_views = compile_services(checkout, state, manifest, 1001, 1001)
        exercise(linux_views, state, linux_owned=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
