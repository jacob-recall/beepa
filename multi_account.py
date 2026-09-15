#!/usr/bin/env python3
"""Prepare and operate an isolated, fresh macOS iMessage hub.

No live I/O on import. Preparation does not start Docker, read Messages, or send.
Legacy full-network installations keep their existing configuration.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys

from install_config import (atomic_write, ensure_manifest, initialize_state,
                            read_manifest, save_manifest, ensure_runtime, install_agent)


def require_owner(manifest):
    if manifest.get('profile') == 'imessage' and manifest.get('owner_uid') != os.getuid():
        raise ValueError('Run this installation from its owning macOS login, not sudo or another user.')


def prepare(root, slot, handle=None):
    root = Path(root).resolve()
    if not 1 <= slot <= 50:
        raise ValueError('slot must be between 1 and 50; assign a different slot to each Mac user')
    if handle is not None and (not handle.strip() or any(ord(c) < 32 for c in handle) or len(handle) > 254):
        raise ValueError('Provide the Messages phone number or email address, without control characters.')
    old = read_manifest(root)
    if old:
        require_owner(old)
        if old.get('profile') != 'imessage' or old.get('slot') != slot:
            raise ValueError('Existing installation cannot change profile or slot; use a fresh checkout.')
        data = old
    else:
        # Reject adopting any provisioned full stack as an iMessage-only install.
        if any((root / p).exists() for p in ('.env', 'synapse/homeserver.yaml', 'imessage/daemon.json')):
            raise ValueError('Use a fresh checkout; existing hub migration is not supported.')
        data = ensure_manifest(root)
        data.update(profile='imessage', slot=slot, owner_uid=os.getuid(),
                    compose_project='beepa-imessage-%d-%d' % (os.getuid(), slot),
                    ports={'synapse': 8008 + slot * 100, 'app': 8011 + slot * 100,
                           'imessage': 29350 + slot})
        data['local_cs_base'] = 'http://127.0.0.1:%d' % data['ports']['synapse']
    if handle is not None:
        if data.get('self_handle') and data['self_handle'] != handle:
            raise ValueError('Changing a configured sender requires an explicit account migration.')
        data['self_handle'] = handle
    save_manifest(root, data)
    data = initialize_state(root)
    state = Path(data['state_root'])
    state.chmod(0o700)
    envfile = state / '.env'
    if not envfile.exists():
        import secrets
        atomic_write(envfile, '\n'.join([
            'POSTGRES_PASSWORD=' + secrets.token_hex(32),
            'HOST_UID=' + str(os.getuid()), 'HOST_GID=' + str(os.getgid()),
            'LOCAL_LOCALPART=' + data['local_localpart'],
            'LOCAL_DISPLAYNAME=' + data['display_name'],
            'BEEPA_SYNAPSE_PORT=' + str(data['ports']['synapse']),
            'BEEPA_APP_PORT=' + str(data['ports']['app']), '']))
    return data


def enable_networks(root):
    """Add the five network bridges without changing this hub's identity or stores."""
    root = Path(root).resolve()
    data = read_manifest(root)
    if not data or data.get('profile') != 'imessage':
        raise ValueError('An existing isolated installation is required')
    require_owner(data)
    state = Path(data['state_root'])
    backup = state / '.beepa-config/before-all-networks'
    if not backup.exists():
        backup.mkdir(parents=True, mode=0o700)
        for relative in ('.beepa-install.json', '.env', 'synapse/homeserver.yaml', 'imessage/daemon.json'):
            source = state / relative
            if source.is_file():
                target = backup / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    data['ports'].setdefault('gmessages', 8020 + data['slot'] * 100)
    data['ports'].setdefault('session', 8021 + data['slot'] * 100)
    data['ports'].setdefault('element', 8009 + data['slot'] * 100)
    data['all_networks'] = True
    save_manifest(root, data)
    from install_config import read_env
    envfile = state / '.env'
    values = read_env(envfile)
    additions = []
    for key, name in (('BEEPA_SYNAPSE_PORT', 'synapse'), ('BEEPA_APP_PORT', 'app'), ('BEEPA_ELEMENT_PORT', 'element')):
        expected = str(data['ports'][name])
        if key in values and values[key] != expected:
            raise ValueError('Conflicting saved port: ' + key)
        if key not in values:
            additions.append(key + '=' + expected)
    if additions:
        atomic_write(envfile, envfile.read_text().rstrip() + '\n' + '\n'.join(additions) + '\n')
    element = state / 'element/config.json'
    if not element.exists():
        atomic_write(element, json.dumps({'default_server_config': {'m.homeserver': {
            'base_url': data['local_cs_base'], 'server_name': data['local_server_name']}}}))
    print('All network bridges enabled for the retained hub. Run install to start them.')


def endpoints(data):
    data = data or {}
    base = data.get('local_cs_base', 'http://127.0.0.1:8008')
    if data.get('profile') != 'imessage':
        base = os.environ.get('LOCAL_HS_URL', base)
    return {'LOCAL_HS_URL': base,
            'BEEPA_IMESSAGE_PORT': str(data.get('ports', {}).get('imessage', 29350))}


def filter_render(data, stage):
    if not data or data.get('profile') != 'imessage' or data.get('all_networks'):
        return
    path = Path(stage) / 'synapse/homeserver.yaml'
    text = path.read_text()
    text = re.sub(r'^  - /data/(?!imessage-registration\.yaml$)[^\n]+\.yaml\n', '', text, flags=re.M)
    atomic_write(path, text)


def instance_overlay(data, release, overlay):
    """Render only the instance entry point and CSP; immutable JS stays shared.

    Artifact directories are content-addressed, so preparing an update cannot
    replace the files mounted by a currently running older release.
    """
    require_owner(data)
    release = Path(release).resolve()
    state = Path(data['state_root']).resolve()
    index = (release / 'apps/user/index.html').read_text()
    nginx = (release / 'views/nginx.conf').read_text()
    base = data['local_cs_base']
    connect_bases = [base]
    if data.get('all_networks'):
        connect_bases.append('http://127.0.0.1:%d' % data['ports']['gmessages'])
        connect_bases.extend('http://127.0.0.1:%d' % (data['ports']['session'] + i) for i in range(5))
    policy = "default-src 'self'; connect-src 'self' " + ' '.join(connect_bases) + "; img-src 'self' blob:; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; script-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; require-trusted-types-for 'script'; trusted-types 'none'"
    index, n = re.subn(r'(<meta http-equiv="Content-Security-Policy" content=")[^"]+("\s*>)',
                       lambda m: m[1] + policy + m[2], index)
    if n != 1:
        raise ValueError('Expected exactly one user CSP in the release')
    index, n = re.subn(r'src="main\.js[^" ]*"', 'src="instance.js"', index)
    if n != 1:
        raise ValueError('Expected exactly one user application entry point')
    # Keep master policy intact. Only this user app gets instance routing.
    marker = '    # --- manager console'
    if marker not in nginx:
        raise ValueError('Missing separate user/master nginx policy boundary')
    user, master = nginx.split(marker, 1)
    user, n = re.subn(r'add_header Content-Security-Policy "default-src \'self\';[^"\n]+"',
                      lambda m: 'add_header Content-Security-Policy "' + policy + '"', user)
    if n != 1:
        raise ValueError('Expected exactly one teammate nginx CSP')
    nginx = user + marker + master
    config = {'installId': data['install_id'], 'csBase': base, 'profile': 'full' if data.get('all_networks') else 'imessage',
              'helpers': {name: 'http://127.0.0.1:%d' % data['ports'][name]
                          for name in ('gmessages', 'session') if name in data['ports']},
              'loginAlias': data.get('login_alias', ''),
              'loginUser': '@%s:%s' % (data['local_localpart'], data['local_server_name'])}
    entry = ("import { configureMatrixBase } from '../../shared/matrix/client.js';\n"
             "import { configureInstallation } from '../../shared/installation.js';\n"
             'const config = ' + json.dumps(config) + ';\n'
             'configureInstallation(config);\n'
             'configureMatrixBase({csBase: config.csBase});\n'
             "await import('./main.js');\n")
    key = hashlib.sha256((index + nginx + entry).encode()).hexdigest()[:24]
    dest = state / '.beepa-config/instances' / key
    for name, content in [('index.html', index), ('nginx.conf', nginx), ('instance.js', entry)]:
        atomic_write(dest / name, content)
    services = {k: v for k, v in overlay['services'].items()
                if data.get('all_networks') or k in ('views', 'postgres', 'synapse')}
    volumes = services['views']['volumes']
    volumes[:] = [v for v in volumes if v['target'] != '/etc/nginx/conf.d/default.conf']
    for name, target in [('index.html', '/usr/share/nginx/html/apps/user/index.html'),
                         ('instance.js', '/usr/share/nginx/html/apps/user/instance.js'),
                         ('nginx.conf', '/etc/nginx/conf.d/default.conf')]:
        volumes.append({'type': 'bind', 'source': str(dest / name), 'target': target, 'read_only': True})
    return {'services': services}


def validate_ports(data, allowed=()):
    for name, port in data['ports'].items():
        if name in allowed:
            continue
        with socket.socket() as listener:
            try:
                listener.bind(('127.0.0.1', port))
            except OSError:
                raise ValueError('%s port %d is occupied; no fallback or foreign-service adoption.' % (name, port)) from None


def install(root):
    root = Path(root).resolve()
    data = read_manifest(root)
    if not data or data.get('profile') != 'imessage':
        raise ValueError('First run: python3 multi_account.py prepare --slot N')
    require_owner(data)
    if sys.platform != 'darwin':
        raise ValueError('Native iMessage installation requires macOS')
    env = dict(os.environ, BEEPA_INSTALL_ROOT=data['state_root'], **endpoints(data))
    bundled = '/Applications/Docker.app/Contents/Resources/bin'
    env['PATH'] = bundled + os.pathsep + env.get('PATH', '')
    if not shutil.which('docker', path=env['PATH']):
        raise ValueError('Install and start a Docker runtime in this user session, then rerun install.')
    # Current-user runtime only; never chmod or borrow another user's Docker socket.
    endpoint = env.get('DOCKER_HOST') if not env.get('DOCKER_CONTEXT') else None
    if not endpoint:
        inspected = subprocess.run(['docker', 'context', 'inspect', '--format', '{{json .Endpoints.docker.Host}}'],
                                   env=env, check=True, capture_output=True, text=True)
        endpoint = json.loads(inspected.stdout)
    if not isinstance(endpoint, str) or not endpoint.startswith('unix://'):
        raise ValueError('A local Unix-socket Docker context is required; remote engines cannot mount this user state.')
    subprocess.run(['docker', 'info'], env=env, check=True, stdout=subprocess.DEVNULL)
    from beepa_update import Updater
    updater = Updater(root)
    command = dict(updater.compositions(data, root))['teammate']
    running = subprocess.run(command + ['ps', '--services', '--status', 'running'],
                             env=env, check=True, capture_output=True, text=True).stdout.split()
    allowed = {{'synapse': 'synapse', 'views': 'app', 'element': 'element'}[s]
               for s in running if s in ('synapse', 'views', 'element')}
    if data.get('all_networks'):
        for name, port_key in (('gmessages-connect', 'gmessages'), ('session-connect', 'session')):
            import plistlib
            path = Path.home() / 'Library/LaunchAgents' / ('org.beepa.' + name + '.plist')
            service = 'gui/%d/org.beepa.%s' % (os.getuid(), name)
            if subprocess.run(['launchctl', 'print', service], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                actual = plistlib.loads(path.read_bytes())['EnvironmentVariables'].get('BEEPA_INSTALL_ROOT')
                if actual != data['state_root']:
                    raise ValueError('A different installation already owns this user helper')
                allowed.add(port_key)
    agent = 'gui/%d/org.beepa.imessage-daemon' % os.getuid()
    if subprocess.run(['launchctl', 'print', agent], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        # Only a daemon configured for this retained state may occupy its port.
        import plistlib
        installed = Path.home() / 'Library/LaunchAgents/org.beepa.imessage-daemon.plist'
        actual = plistlib.loads(installed.read_bytes())['EnvironmentVariables']['IMESSAGE_CONFIG']
        if actual != str(Path(data['state_root']) / 'imessage/daemon.json'):
            raise ValueError('This macOS user already has a different iMessage agent.')
        allowed.add('imessage')
    validate_ports(data, allowed)
    python = ensure_runtime(root)
    env['BEEPA_PYTHON'] = python
    subprocess.run(['bash', str(root / 'hub/render-hub.sh')], env=env, check=True)
    cfgpath = Path(data['state_root']) / 'imessage/daemon.json'
    cfg = json.loads(cfgpath.read_text())
    if data.get('self_handle'):
        cfg['self_handle'] = data['self_handle']
        atomic_write(cfgpath, json.dumps(cfg, indent=2) + '\n')
    subprocess.run(command + ['up', '-d'], env=env, check=True)
    if data.get('all_networks'):
        # Existing Synapse must reload the newly enabled appservice registrations.
        subprocess.run(command + ['restart', 'synapse'], env=env, check=True)
    subprocess.run(['bash', str(root / 'hub/provision-user.sh')], env=env, check=True)
    subprocess.run(['bash', str(root / 'imessage/build-cli.sh')], env=env, check=True)
    install_agent(root, 'uplink')
    if data.get('all_networks'):
        for name in ('gmessages-connect', 'session-connect', 'contacts-import'):
            install_agent(root, name)
    if data.get('self_handle') and Path(data['imessage_cli_path']).is_file():
        install_agent(root, 'imessage-daemon')
    else:
        print('Hub ready; native bridge pending: provide --handle and the signed CLI, then rerun install.')
    url = 'http://127.0.0.1:%d/apps/user/index.html' % data['ports']['app']
    subprocess.run([python, str(root / 'desktop/install_apps.py'), '--role', 'user', '--user-url', url], env=env, check=True)
    print('Local app: ' + url)
    if cfg.get('receive_only'):
        print('iMessage remains receive-only. Full Disk Access is the receiving permission.')
    else:
        print('Grant the CLI Full Disk Access, Accessibility and Messages Automation in this macOS login.')
    print('Enrollment and actual provider delivery have not been verified by installation.')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', default=str(Path(__file__).resolve().parent))
    sub = p.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--slot', type=int, required=True)
    prep.add_argument('--handle')
    sub.add_parser('install')
    sub.add_parser('enable-networks')
    sub.add_parser('link').add_argument('url')
    sub.add_parser('status')
    sub.add_parser('endpoints')
    sub.add_parser('is-imessage')
    sub.add_parser('filter-render').add_argument('stage')
    args = p.parse_args()
    try:
        data = read_manifest(args.root)
        if args.command == 'prepare':
            data = prepare(args.root, args.slot, args.handle)
            print(json.dumps({k: data.get(k) for k in ('profile', 'slot', 'owner_uid', 'compose_project', 'ports', 'state_root')}, indent=2))
        elif args.command == 'is-imessage':
            return 0 if data and data.get('profile') == 'imessage' else 1
        elif args.command == 'endpoints':
            for k, v in endpoints(data).items():
                print(k + '=' + shlex.quote(v))
        elif args.command == 'filter-render':
            filter_render(data, args.stage)
        elif args.command == 'enable-networks':
            enable_networks(args.root)
        elif args.command == 'install':
            install(args.root)
        elif args.command == 'link':
            if not data or data.get('profile') != 'imessage':
                raise ValueError('Prepare this installation first')
            require_owner(data)
            from install_config import read_env
            import getpass
            creds = read_env(Path(data['state_root']) / 'agents/uplink/local.env.local')
            if any(not creds.get(k) for k in ('LOCAL_USER', 'LOCAL_TOKEN', 'LOCAL_HS_URL')):
                raise ValueError('Install and provision this local hub before enrollment')
            if creds['LOCAL_HS_URL'] != data['local_cs_base'] or creds['LOCAL_USER'] != '@%s:%s' % (data['local_localpart'], data['local_server_name']):
                raise ValueError('Local credentials do not match this installation')
            env = dict(os.environ, **creds, BEEPA_INSTALL_ROOT=data['state_root'])
            code = getpass.getpass('One-time enrollment code: ')
            subprocess.run(['bash', str(Path(args.root) / 'agents/uplink/link.sh'), args.url, code], env=env, check=True)
        else:
            if data:
                require_owner(data)
            print(json.dumps({k: (data or {}).get(k) for k in ('profile', 'slot', 'owner_uid', 'compose_project', 'ports', 'state_root')}, indent=2))
        return 0
    except subprocess.CalledProcessError as exc:
        # Enrollment commands contain one-time credentials; never print argv.
        p.exit(1, 'multi-account: subprocess failed (exit %d); credentials were not printed.\n' % exc.returncode)
    except (ValueError, OSError) as exc:
        p.exit(2 if args.command == 'is-imessage' else 1, 'multi-account: %s\n' % exc)


if __name__ == '__main__':
    sys.exit(main())
