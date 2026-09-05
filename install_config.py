#!/usr/bin/env python3
"""Installation identity and launchd configuration; stdlib, no live I/O on import.

The manifest adopts existing state locations. It is not a credential store and
must never change an existing Matrix identity as a side effect of an update.
"""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile
import uuid

MANIFEST = '.beepa-install.json'
LOCALPART = re.compile(r'[a-z0-9._=/-]+\Z')
AGENTS = {
    'gmessages-connect': ('gmessages-connect/run-connect.sh', 'gmessages-connect/logs/connect', None),
    'session-connect': ('session-connect/run-connect.sh', 'session-connect/logs/connect', None),
    'uplink': ('agents/uplink/run-uplink.sh', 'agents/uplink/logs/uplink', None),
    'contacts-import': ('agents/contacts/run-import.sh', 'agents/contacts/logs/import', 3600),
    'imessage-daemon': ('imessage/daemon.py', 'imessage/logs/daemon', None),
    'master-enroll': ('master/run-enroll.sh', 'master/logs/enroll', None),
    'master-gateway': ('master/gateway.py', 'master/logs/gateway', None),
}
RUNTIME_DIRS = (
    'synapse', 'whatsapp', 'meta', 'gmessages', 'linkedin', 'twitter',
    '.beepa-update', '.beepa-config', '.beepa-venvs', 'master/synapse',
    'master/runtime', 'master/.beepa-config', 'imessage/bin', 'imessage/tmp',
)
RUNTIME_FILES = (
    '.env', 'master/.env', 'master/tokens.local', 'master/.provision-state.local',
    'master/enrollments.local', 'master/recovery.local.json', 'hub/.local-user.local',
    'agents/uplink/local.env.local', 'agents/uplink/uplink.env.local',
    'agents/uplink/state.db', 'agents/contacts/contacts.db', 'imessage/daemon.json',
    'imessage/state.db', 'element/config.json', 'apps/user/session.local.json',
    'apps/master/session.local.json', 'apps/user/connect.local.json',
)
LOG_DIRS = ('master/logs', 'imessage/logs', 'agents/uplink/logs', 'agents/contacts/logs',
            'session-connect/logs', 'gmessages-connect/logs')


def atomic_write(path, data, mode=0o600):
    path = Path(path)
    # Preserve intentional new-install projections when replacing a file atomically.
    if path.is_symlink():
        path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), mode)
            f.write(data.encode() if isinstance(data, str) else data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_env(path):
    """Parse literal installer assignments, never execute a shell file."""
    result = {}
    try:
        lines = Path(path).read_text().splitlines()
    except FileNotFoundError:
        return result
    for line in lines:
        match = re.match(r'^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=(.*)$', line)
        if not match:
            continue
        value = match[2].strip()
        if value.startswith(('"', "'")):
            parts = shlex.split(value, comments=True)
            if len(parts) != 1:
                raise ValueError('Invalid literal assignment in ' + str(path))
            value = parts[0]
        result[match[1]] = value
    return result


def validate_localpart(value):
    if not isinstance(value, str) or not LOCALPART.fullmatch(value) or len(value) > 128:
        raise ValueError('Invalid installation identity; use a lowercase Matrix localpart')
    return value


def _legacy(root):
    env = read_env(root / '.env')
    identities = []
    if env.get('LOCAL_LOCALPART'):
        identities.append((validate_localpart(env['LOCAL_LOCALPART']), 'localhost'))
    for relative in ('agents/uplink/local.env.local', 'agents/uplink/uplink.env.local'):
        user = read_env(root / relative).get('LOCAL_USER')
        if user:
            match = re.fullmatch(r'@([^:]+):([^\s]+)', user)
            if not match:
                raise ValueError('Invalid existing installation identity')
            identities.append((validate_localpart(match[1]), match[2]))
    # User session files contain the actual account, never infer from bot IDs.
    session = root / 'apps/user/session.local.json'
    if session.exists():
        user = json.loads(session.read_text()).get('user_id', '')
        match = re.fullmatch(r'@([^:]+):([^\s]+)', user)
        if match:
            identities.append((validate_localpart(match[1]), match[2]))
    if len(set(identities)) > 1:
        raise ValueError('Conflicting existing installation identity; reconcile configuration before setup')
    return (identities[0] if identities else None), env


def read_manifest(root):
    path = Path(root).resolve() / MANIFEST
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if data.get('config_version') != 1:
        raise ValueError('Unsupported installation manifest version')
    validate_localpart(data.get('local_localpart'))
    if not data.get('install_id') or not data.get('state_root'):
        raise ValueError('Incomplete installation manifest')
    return data


def save_manifest(root, data):
    encoded = json.dumps(data, indent=2, sort_keys=True) + '\n'
    atomic_write(Path(root) / MANIFEST, encoded)
    state = Path(data['state_root'])
    if data.get('state_initialized') and state.resolve() != Path(root).resolve():
        atomic_write(state / MANIFEST, encoded)


def configured_identity(root):
    """Read-only identity lookup for helpers; no OS fallback, no provisioning."""
    root = Path(root).resolve()
    data = read_manifest(root)
    identity, _ = _legacy(root)
    if data:
        stored = (data['local_localpart'], data['local_server_name'])
        if identity and stored != identity:
            raise ValueError('Conflicting installation identity; rerun setup after reconciliation')
        identity = stored
    if not identity:
        raise ValueError('Installation identity missing; run setup.sh first')
    return '@%s:%s' % identity


def compose_prefix(root, code_root=None):
    root = Path(root).resolve()
    data = read_manifest(root) or {}
    return ['docker', 'compose', '-p', data.get('compose_project', 'matrix-wa'),
            '--project-directory', str(Path(code_root or root)), '--env-file', str(root / '.env'),
            '-f', str(Path(code_root or root) / 'docker-compose.yml')]


def ensure_manifest(root, role='teammate', env=None):
    root = Path(root).resolve()
    env = os.environ if env is None else env
    existing = read_manifest(root)
    requested_state = env.get('BEEPA_STATE_ROOT')
    if existing and requested_state and Path(requested_state).expanduser().resolve() != Path(existing['state_root']).resolve():
        raise ValueError('Existing runtime cannot be relocated by changing BEEPA_STATE_ROOT')
    if existing is None and requested_state:
        existing = read_manifest(Path(requested_state).expanduser())
    legacy, old_env = _legacy(root)
    supplied = env.get('LOCAL_LOCALPART')
    if supplied:
        validate_localpart(supplied)
    if existing:
        stored = (existing['local_localpart'], existing['local_server_name'])
        if (legacy and legacy != stored) or (supplied and supplied != stored[0]):
            raise ValueError('Conflicting installation identity; changing accounts requires explicit migration')
        data = existing
    else:
        if legacy and supplied and supplied != legacy[0]:
            raise ValueError('Supplied identity conflicts with existing installation identity')
        suggestion = re.sub(r'[^a-z0-9._=/-]+', '-', getpass.getuser().lower()).strip('-_') or 'user'
        localpart, domain = legacy or (supplied or suggestion, 'localhost')
        has_state = any((root / relative).exists() for relative in
                        RUNTIME_FILES + ('synapse/homeserver.yaml', 'imessage/bin/imessage-cli'))
        if has_state and requested_state and Path(requested_state).expanduser().resolve() != root:
            raise ValueError('Existing runtime must be adopted in place; external relocation requires a separate migration')
        install_id = str(uuid.uuid4())
        state_root = root if has_state else Path(requested_state or Path.home() / 'Library/Application Support/Beepa' / install_id).expanduser().resolve()
        logs_root = root if has_state else Path(env.get('BEEPA_LOG_ROOT') or Path.home() / 'Library/Logs/Beepa' / install_id).expanduser().resolve()
        cli = state_root / 'imessage/bin/imessage-cli'
        imsg = root / 'imessage/daemon.json'
        if imsg.exists():
            cli = Path(json.loads(imsg.read_text()).get('cli_path', str(cli))).expanduser()
        data = {
            'config_version': 1, 'install_id': install_id, 'roles': [],
            'local_localpart': localpart, 'local_server_name': domain,
            'display_name': old_env.get('LOCAL_DISPLAYNAME') or env.get('LOCAL_DISPLAYNAME') or localpart,
            'local_cs_base': 'http://127.0.0.1:8008',
            'state_root': str(state_root), 'code_root': str(root),
            'logs_root': str(logs_root), 'state_initialized': has_state,
            'compose_project': 'matrix-wa', 'master_compose_project': 'matrix-master',
            'imessage_cli_path': str(cli), 'python_path': sys.executable,
        }
    if any(ord(c) < 32 or ord(c) == 127 for c in data['display_name']):
        raise ValueError('Display name must not contain control characters')
    if env.get('BEEPA_MASTER_STATE_DIR'):
        requested_master = str(Path(env['BEEPA_MASTER_STATE_DIR']).expanduser().resolve())
        if data.get('master_state_root') and data['master_state_root'] != requested_master:
            raise ValueError('Existing master state cannot be relocated through setup')
        data['master_state_root'] = requested_master
    if env.get('PHONE_REGION'):
        region = env['PHONE_REGION'].upper()
        if not re.fullmatch(r'[A-Z]{2}', region):
            raise ValueError('PHONE_REGION must be a two-letter region code')
        data['phone_region'] = region
    data['roles'] = sorted(set(data.get('roles', [])) | {role})
    if role not in ('teammate', 'master'):
        raise ValueError('Unknown installation role')
    serialized = json.dumps(data, indent=2, sort_keys=True) + '\n'
    path = root / MANIFEST
    if not path.exists() or path.read_text() != serialized:
        save_manifest(root, data)
    return data


def initialize_state(root):
    """Project NEW external runtime paths into the checkout, without moving data."""
    root = Path(root).resolve()
    data = read_manifest(root)
    if data is None:
        raise ValueError('Run installation identity setup first')
    state = Path(data['state_root']).resolve()
    if state == root:
        return data
    logs = Path(data['logs_root']).resolve()
    mappings = [(relative, state / relative, True) for relative in RUNTIME_DIRS]
    mappings += [(relative, state / relative, False) for relative in RUNTIME_FILES]
    mappings += [(prefix + suffix, logs / (prefix + suffix), False)
                 for _, prefix, _ in AGENTS.values() for suffix in ('.log', '.err')]
    for relative, target, _ in mappings:
        source = root / relative
        if source.is_symlink():
            if source.resolve() != target.resolve():
                raise ValueError('Existing runtime link points elsewhere: ' + relative)
        elif source.exists():
            raise ValueError('Refusing to move/replace existing runtime: ' + relative)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    for relative, target, directory in mappings:
        source = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if directory:
            target.mkdir(parents=True, exist_ok=True)
        source.parent.mkdir(parents=True, exist_ok=True)
        if not source.is_symlink():
            source.symlink_to(target, target_is_directory=directory)
    data['state_initialized'] = True
    save_manifest(root, data)
    return data


def write_plist(root, name, dest, code_root=None):
    root = Path(root).resolve()
    script, logs, interval = AGENTS[name]
    data = read_manifest(root)
    code_root = Path(code_root or (data or {}).get('code_root', root)).resolve()
    state = Path(data['state_root']) if data and data.get('state_initialized') else root
    logs_root = Path(data.get('logs_root', root)) if data and data.get('state_initialized') else root
    python = (data or {}).get('python_path', sys.executable)
    args = [python if script.endswith('.py') else '/bin/bash', str(code_root / script)]
    config = {
        'Label': 'org.beepa.' + name, 'ProgramArguments': args,
        'WorkingDirectory': str(state), 'RunAtLoad': True, 'Umask': 63,
        'StandardOutPath': str(logs_root / (logs + '.log')),
        'StandardErrorPath': str(logs_root / (logs + '.err')),
        'EnvironmentVariables': {
            'BEEPA_INSTALL_ROOT': str(state), 'BEEPA_PYTHON': python,
            'BEEPA_CODE_ROOT': str(code_root),
            'BEEPA_MASTER_STATE_DIR': str((data or {}).get('master_state_root', state / 'master')),
            'UPLINK_DB': str(state / 'agents/uplink/state.db'),
            'CONTACTS_DB': str(state / 'agents/contacts/contacts.db'),
            'UPLINK_CONTACTS_DB': str(state / 'agents/contacts/contacts.db'),
            'IMESSAGE_CONFIG': str(state / 'imessage/daemon.json'),
            'IMESSAGE_STATE_DIR': str(state / 'imessage'),
        },
    }
    if interval:
        config['StartInterval'] = interval
    else:
        config.update(KeepAlive=True, ThrottleInterval=15)
    if data and data.get('phone_region'):
        config['EnvironmentVariables']['PHONE_REGION'] = data['phone_region']
    (logs_root / logs).parent.mkdir(parents=True, exist_ok=True)
    atomic_write(dest, plistlib.dumps(config))
    return config


def ensure_runtime(root, requirements=None):
    """Install pinned pure-Python dependencies in a versioned private venv."""
    root = Path(root).resolve()
    requirements = Path(requirements or root / 'requirements-host.txt').resolve()
    lock_hash = hashlib.sha256(requirements.read_bytes()).hexdigest()[:16]
    data = read_manifest(root)
    if data is None:
        raise ValueError('Run installation identity setup before installing the runtime')
    runtime_root = Path(data['state_root']) if data.get('state_initialized') else root
    runtime = runtime_root / '.beepa-venvs' / lock_hash
    python = runtime / 'bin/python3'
    complete = runtime / '.dependencies-installed'
    if not complete.exists() or not python.exists():
        if not python.exists():
            subprocess.run([sys.executable, '-m', 'venv', str(runtime)], check=True)
        subprocess.run([str(python), '-m', 'pip', '--disable-pip-version-check', 'install',
                        '--require-hashes', '--no-deps', '--only-binary=:all:', '-r', str(requirements)], check=True, stdout=sys.stderr)
        subprocess.run([str(python), '-c', 'import phonenumbers; assert phonenumbers.__version__ == "9.0.38"'], check=True)
        atomic_write(complete, lock_hash + '\n')
    if data.get('python_path') != str(python):
        data['python_path'] = str(python)
        save_manifest(root, data)
    return str(python)


def install_agent(root, name, launch_dir=None, code_root=None):
    """Explicit installer action. Boot out legacy label before enabling replacement."""
    launch_dir = Path(launch_dir or Path.home() / 'Library/LaunchAgents')
    target = launch_dir / ('org.beepa.' + name + '.plist')
    legacy = launch_dir / ('com.jkali.' + name + '.plist')
    domain = 'gui/%d' % os.getuid()
    # Resolve exactly our two labels. Do not kill process-name matches.
    for label in ('com.jkali.' + name, 'org.beepa.' + name):
        service = domain + '/' + label
        found = subprocess.run(['launchctl', 'print', service], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if found.returncode == 0:
            subprocess.run(['launchctl', 'bootout', service], check=True)
    write_plist(root, name, target, code_root=code_root)
    subprocess.run(['launchctl', 'bootstrap', domain, str(target)], check=True)
    if legacy.exists():
        legacy.unlink()
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=str(Path(__file__).resolve().parent))
    sub = parser.add_subparsers(dest='command', required=True)
    ensure = sub.add_parser('ensure')
    ensure.add_argument('--role', choices=('teammate', 'master'), default='teammate')
    ensure.add_argument('--shell', action='store_true')
    sub.add_parser('identity')
    sub.add_parser('runtime')
    sub.add_parser('initialize-state')
    compose = sub.add_parser('compose')
    compose.add_argument('--role', choices=('teammate', 'master', 'local-ui'), default='teammate')
    compose.add_argument('args', nargs=argparse.REMAINDER)
    agent = sub.add_parser('install-agent')
    agent.add_argument('name', choices=sorted(AGENTS))
    args = parser.parse_args()
    try:
        if args.command == 'ensure':
            data = ensure_manifest(args.root, args.role)
            if args.shell:
                for key, value in {'LOCAL_LOCALPART': data['local_localpart'], 'LOCAL_DISPLAYNAME': data['display_name']}.items():
                    print('%s=%s' % (key, shlex.quote(value)))
            else:
                print(json.dumps(data, indent=2))
        elif args.command == 'identity':
            print(configured_identity(args.root))
        elif args.command == 'runtime':
            print(ensure_runtime(args.root))
        elif args.command == 'initialize-state':
            print(initialize_state(args.root)['state_root'])
        elif args.command == 'compose':
            from beepa_update import Updater
            updater = Updater(args.root)
            data = read_manifest(args.root)
            if not data:
                raise ValueError('Installation manifest required')
            selected = dict(data, roles=['master'] if args.role in ('master', 'local-ui') else ['teammate'])
            commands = dict(updater.compositions(selected, Path(args.root).resolve()))
            extra = args.args[1:] if args.args[:1] == ['--'] else args.args
            subprocess.run(commands[args.role] + extra, check=True)
        else:
            print(install_agent(args.root, args.name))
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, 'install: %s\n' % exc)


if __name__ == '__main__':
    main()
