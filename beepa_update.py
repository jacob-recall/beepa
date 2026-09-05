#!/usr/bin/env python3
"""Apply a clean, committed release while preserving installed account state.

Default: inspect only. --apply explicitly runs the journaled update. --rollback
activates the previous managed code release only if its declared state contract
supports the active state version. This never restores an old send ledger.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from install_config import AGENTS, MANIFEST, atomic_write, ensure_manifest, ensure_runtime, install_agent, read_manifest, read_env, save_manifest

STATE_PATHS = (
    '.env', MANIFEST, 'synapse', 'whatsapp', 'meta', 'gmessages', 'linkedin', 'twitter',
    'element/config.json', 'hub/.local-user.local', '.beepa-config',
    'master/.env', 'master/.beepa-config', 'master/synapse', 'master/tokens.local', 'master/.provision-state.local',
    'master/enrollments.local', 'master/identity', 'master/recovery', 'master/recovery.local.json',
    'agents/uplink/local.env.local', 'agents/uplink/uplink.env.local',
    'agents/uplink/state.db', 'agents/contacts/contacts.db',
    'imessage/daemon.json', 'imessage/state.db',
    'apps/user/session.local.json', 'apps/master/session.local.json', 'apps/user/connect.local.json',
)
SECRET_PATHS = ('.env', 'master/.env', 'synapse/.hub-secrets.local',
                'synapse/localhost.signing.key', 'master/synapse/.secrets.local',
                'master/synapse/master.signing.key', 'hub/.local-user.local',
                'agents/uplink/local.env.local', 'agents/uplink/uplink.env.local',
                'apps/user/session.local.json', 'apps/master/session.local.json')


def runtime_path(root, relative):
    root = Path(root).resolve()
    manifest = read_manifest(root) or {}
    state = Path(manifest['state_root']).resolve() if manifest.get('state_initialized') else root
    if relative.startswith('master/'):
        master = Path(os.environ.get('BEEPA_MASTER_STATE_DIR', manifest.get('master_state_root', state / 'master'))).resolve()
        return master / relative[len('master/'):]
    return state / relative


def inventory(root):
    """Fingerprints are safe for local diagnostics; never log credential text."""
    root = Path(root)
    result = {}
    for relative in SECRET_PATHS:
        path = runtime_path(root, relative)
        if path.is_file():
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    data = read_manifest(root)
    if data:
        cli = Path(data['imessage_cli_path'])
        if cli.is_file():
            st = cli.stat()
            result['native_cli'] = {'path': str(cli), 'inode': st.st_ino,
                                    'sha256': hashlib.sha256(cli.read_bytes()).hexdigest()}
        result['identity'] = {k: data[k] for k in ('install_id', 'local_localpart', 'local_server_name',
                                                  'compose_project', 'master_compose_project')}
    return result


def check_compatibility(current, target):
    version = (current or {}).get('state_version', 1)
    if target.get('update_format', 1) != 1 or version not in target.get('reads_state_versions', []):
        raise ValueError('Release is not compatible with the installed state; use a supported intermediate release')
    if not isinstance(target.get('state_version'), int):
        raise ValueError('Release has no state compatibility contract')


class Journal:
    def __init__(self, path, identity):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}
        if self.data and not self.data.get('complete') and self.data.get('target') != identity['target']:
            raise ValueError('An unfinished update exists; resume that release before pulling another')
        if not self.data or (self.data.get('complete') and self.data.get('target') != identity['target']):
            self.data = dict(identity, steps=[], started_at=int(time.time()), attempt_id=uuid.uuid4().hex[:12])
            self.save()

    def save(self):
        atomic_write(self.path, json.dumps(self.data, indent=2, sort_keys=True) + '\n')

    def step(self, name, operation):
        if name in self.data['steps']:
            return
        self.data['active_step'] = name
        self.save()
        operation()
        self.data['steps'].append(name)
        self.data.pop('active_step', None)
        self.save()


def changed_config_services(paths):
    mapping = {'whatsapp': 'mautrix-whatsapp', 'meta': 'mautrix-meta', 'gmessages': 'mautrix-gmessages',
               'linkedin': 'mautrix-linkedin', 'twitter': 'mautrix-twitter', 'synapse': 'synapse'}
    return sorted({mapping[p.split('/')[0]] for p in paths if p.split('/')[0] in mapping})


def views_overlay(root, release):
    root, release = Path(root), Path(release)
    volumes = [{'type': 'bind', 'source': str(release / sub), 'target': target, 'read_only': True}
               for sub, target in [('apps', '/usr/share/nginx/html/apps'), ('shared', '/usr/share/nginx/html/shared'),
                                    ('views/nginx.conf', '/etc/nginx/conf.d/default.conf')]]
    # Mount the runtime DIRECTORY so newly created and atomically replaced
    # bootstrap files are visible without rebinding a stale file inode.
    volumes.append({'type': 'bind', 'source': str((root / 'apps').resolve()),
                    'target': '/usr/share/nginx/runtime/apps', 'read_only': True})
    services = {'views': {'volumes': volumes},
                'postgres': {'volumes': [{'type': 'bind', 'source': str(release / 'postgres-init'), 'target': '/docker-entrypoint-initdb.d', 'read_only': True}]},
                'element': {'volumes': [{'type': 'bind', 'source': str(root / 'element/config.json'), 'target': '/app/config.json', 'read_only': True},
                                        {'type': 'bind', 'source': str(release / 'element/nginx-default.conf.template'), 'target': '/etc/nginx/templates/default.conf.template', 'read_only': True}]}}
    for service, relative in [('synapse', 'synapse'), ('mautrix-whatsapp', 'whatsapp'),
                              ('mautrix-meta', 'meta'), ('mautrix-gmessages', 'gmessages'),
                              ('mautrix-linkedin', 'linkedin'), ('mautrix-twitter', 'twitter')]:
        services[service] = {'volumes': [{'type': 'bind', 'source': str((root / relative).resolve()), 'target': '/data'}]}
    return {'services': services}



class Updater:
    def __init__(self, root, run=None):
        self.root = Path(root).resolve()
        self.run = run or subprocess.run
        self.meta = self.root / '.beepa-update'
        self.installed_path = self.meta / 'installed.json'

    def command(self, args, **kwargs):
        return self.run([str(a) for a in args], cwd=str(self.root), check=True, **kwargs)

    def inspect(self):
        # Tracked modifications (including staged edits) are not release input.
        dirty = self.command(['git', 'status', '--porcelain', '--untracked-files=no'], capture_output=True).stdout
        if dirty.strip():
            raise ValueError('Tracked files have local changes; commit/review them before applying a release')
        sha = self.command(['git', 'rev-parse', 'HEAD'], capture_output=True).stdout.decode().strip()
        target = json.loads((self.root / 'release.json').read_text())
        installed = json.loads(self.installed_path.read_text()) if self.installed_path.exists() else None
        check_compatibility(installed, target)
        return sha, target, installed

    @contextlib.contextmanager
    def lock(self):
        self.meta.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.meta / 'operation.lock').open('a') as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('Another update is active')
            yield

    def stage(self, sha):
        release = (self.meta / 'releases' / sha).resolve()
        if (release / '.staged').exists():
            return release
        release.mkdir(parents=True, exist_ok=True)
        # Snapshot the immutable tree, not files that another git pull could
        # replace between reading HEAD and copying the final source file.
        with tempfile.TemporaryFile() as stream:
            self.command(['git', 'archive', '--format=tar', sha], stdout=stream)
            stream.seek(0)
            with tarfile.open(fileobj=stream, mode='r:') as archive:
                for member in archive:
                    path = Path(member.name)
                    if path.is_absolute() or '..' in path.parts:
                        raise ValueError('Unsafe tracked path')
                    if member.isdir():
                        continue
                    if not member.isfile():
                        raise ValueError('Release staging refuses links/devices: ' + member.name)
                    dest = release / path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as source, dest.open('wb') as target:
                        shutil.copyfileobj(source, target)
                    dest.chmod(member.mode & 0o777)
        # Parse Python without creating __pycache__ in the user's checkout.
        for path in release.rglob('*.py'):
            compile(path.read_bytes(), str(path), 'exec')
        atomic_write(release / '.staged', sha + '\n')
        return release

    def compositions(self, manifest, release):
        result = []
        if 'teammate' in manifest['roles']:
            result.append(('teammate', manifest['compose_project'], 'docker-compose.yml', '.env'))
        if 'master' in manifest['roles']:
            result.append(('master', manifest['master_compose_project'], 'master/docker-compose.master.yml', 'master/.env'))
            if 'teammate' not in manifest['roles']:
                result.append(('local-ui', manifest['compose_project'], 'docker-compose.apps.yml', 'master/.env'))
        rows = []
        state = Path(manifest['state_root']).resolve() if manifest.get('state_initialized') else self.root
        for role, project, filename, envfile in result:
            # Compose paths remain rooted at original runtime directories.
            project_dir = Path(os.environ.get('BEEPA_MASTER_STATE_DIR', manifest.get('master_state_root', state / 'master'))).resolve() if role == 'master' else release
            cmd = ['docker', 'compose', '-p', project, '--project-directory', str(project_dir)]
            if envfile:
                env_path = runtime_path(self.root, 'master/.env') if role in ('master', 'local-ui') else state / envfile
                cmd += ['--env-file', str(env_path)]
            cmd += ['-f', str(release / filename)]
            if role in ('teammate', 'local-ui'):
                overlay = self.meta / ('views-' + release.name + '.json')
                view_config = views_overlay(state, release)
                if role == 'local-ui':
                    view_config['services'] = {'views': view_config['services']['views']}
                atomic_write(overlay, json.dumps(view_config))
                cmd.extend(['-f', str(overlay)])
                if role == 'teammate':
                    cmd.extend(['--profile', 'bridge', '--profile', 'client'])
            rows.append((role, cmd))
        return rows

    def running_agents(self):
        names = []
        if sys.platform != 'darwin':
            return names
        for name in AGENTS:
            for prefix in ('org.beepa.', 'com.jkali.'):
                result = self.run(['launchctl', 'print', 'gui/%d/%s%s' % (os.getuid(), prefix, name)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if result.returncode == 0:
                    names.append(name)
                    break
        return names

    def quiesce(self, journal, compositions):
        if 'running' not in journal.data:
            running = {}
            for role, cmd in compositions:
                services = self.command(cmd + ['ps', '--status', 'running', '--services'], capture_output=True).stdout.decode().split()
                running[role] = services
            journal.data.update(running=running, agents=self.running_agents())
            journal.save()
        for name in journal.data['agents']:
            for prefix in ('org.beepa.', 'com.jkali.'):
                label = 'gui/%d/%s%s' % (os.getuid(), prefix, name)
                result = self.run(['launchctl', 'print', label], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if result.returncode == 0:
                    self.command(['launchctl', 'bootout', label])
        for role, cmd in compositions:
            writers = [s for s in journal.data['running'][role] if s != 'postgres']
            if writers:
                self.command(cmd + ['stop'] + writers)

    def backup(self, journal, compositions):
        backup = self.meta / 'backups' / (journal.data['target'] + '-' + journal.data['attempt_id'])
        backup.mkdir(parents=True, exist_ok=True, mode=0o700)
        for role, cmd in compositions:
            if role == 'local-ui':
                continue
            if 'postgres' not in journal.data['running'].get(role, []):
                raise ValueError('Postgres must be running for a consistent update backup: ' + role)
            dest = backup / (role + '-postgres.sql')
            temporary = dest.with_suffix('.tmp')
            with temporary.open('wb') as stream:
                os.chmod(temporary, 0o600)
                self.command(cmd + ['exec', '-T', 'postgres', 'pg_dumpall', '-U', 'matrix'], stdout=stream)
            if temporary.stat().st_size == 0:
                raise ValueError('Database backup was empty')
            os.replace(temporary, dest)
        tar_path = backup / 'runtime.tar.gz'
        manifest = read_manifest(self.root) or {}
        state = Path(manifest['state_root']).resolve() if manifest.get('state_initialized') else self.root
        with tempfile.TemporaryDirectory(dir=str(backup)) as scratch, tarfile.open(str(tar_path) + '.tmp', 'w:gz', dereference=True) as archive:
            for relative in STATE_PATHS:
                path = runtime_path(self.root, relative)
                if not path.exists():
                    continue
                if path.is_file() and path.suffix == '.db':
                    # A stopped process may leave committed rows in its WAL.
                    # SQLite backup includes them; copying only .db silently loses them.
                    snapshot = Path(scratch) / (uuid.uuid4().hex + '.db')
                    source = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
                    target = sqlite3.connect(str(snapshot))
                    try:
                        source.backup(target)
                    finally:
                        target.close()
                        source.close()
                    archive.add(snapshot, arcname=relative)
                else:
                    archive.add(path, arcname=relative)
        os.chmod(str(tar_path) + '.tmp', 0o600)
        os.replace(str(tar_path) + '.tmp', tar_path)
        sums = {p.name: hash_file(p) for p in backup.iterdir() if p.is_file() and p.name != 'checksums.json'}
        atomic_write(backup / 'checksums.json', json.dumps(sums, indent=2) + '\n')
        journal.data['backup'] = str(backup)
        journal.save()

    def activate(self, release, manifest, compositions, journal):
        state = Path(manifest['state_root']) if manifest.get('state_initialized') else self.root
        env = dict(os.environ, BEEPA_INSTALL_ROOT=str(state), OUT_ROOT=str(state),
                   BEEPA_MASTER_STATE_DIR=str(runtime_path(self.root, 'master/.env').parent))
        if 'teammate' in manifest['roles']:
            self.command(['/bin/bash', release / 'hub/render-hub.sh'], env=env)
        if 'master' in manifest['roles']:
            self.command(['/bin/bash', release / 'master/setup.sh'], env=dict(env, BEEPA_UPDATE='1'))
        # SQLite migrations belong to each daemon, are idempotent and run only
        # when it next starts. Native executable and provisioning are untouched.
        for role, cmd in compositions:
            services = journal.data['running'].get(role, [])
            if services:
                self.command(cmd + ['up', '-d'] + services)
                if role == 'teammate':
                    rendered = self.root / '.beepa-config/last-render.json'
                    changes = json.loads(rendered.read_text()).get('changed', []) if rendered.exists() else []
                    restart = [s for s in changed_config_services(changes) if s in services]
                    if restart:
                        self.command(cmd + ['restart'] + restart)
                if role == 'master' and 'synapse' in services:
                    master_state = Path(env.get('BEEPA_MASTER_STATE_DIR', state / 'master'))
                    rendered = master_state / '.beepa-config/last-render.json'
                    if rendered.exists() and 'synapse/homeserver.yaml' in json.loads(rendered.read_text()).get('changed', []):
                        self.command(cmd + ['restart', 'synapse'])
        agents = list(journal.data.get('agents', []))
        if sys.platform == 'darwin' and 'master' in manifest['roles'] and (release / 'master/gateway.py').exists():
            agents = sorted(set(agents) | {'master-gateway'})
        for name in agents:
            install_agent(self.root, name, code_root=release)
        if sys.platform == 'darwin' and (release / 'desktop/install_apps.py').exists():
            for role in manifest['roles']:
                self.command([manifest['python_path'], release / 'desktop/install_apps.py', '--role', 'user' if role == 'teammate' else 'master'])

    def health(self, manifest):
        failures = []
        urls = []
        if 'teammate' in manifest['roles']:
            urls.append(manifest['local_cs_base'].rstrip('/') + '/health')
        if 'master' in manifest['roles']:
            urls.append('http://127.0.0.1:8018/health')
        for url in urls:
            ok = False
            for attempt in range(15):
                try:
                    with urllib.request.urlopen(url, timeout=3) as response:
                        ok = response.status == 200
                    if ok:
                        break
                except OSError:
                    pass
                time.sleep(2)
            if not ok:
                failures.append(url)
        if failures:
            raise ValueError('Local service health failed; update is resumable: ' + ', '.join(failures))
        credentials = []
        if 'teammate' in manifest['roles']:
            local = read_env(self.root / 'agents/uplink/local.env.local')
            if not local.get('LOCAL_TOKEN'):
                local = read_env(self.root / 'agents/uplink/uplink.env.local')
            credentials.append((local.get('LOCAL_HS_URL', manifest['local_cs_base']), local.get('LOCAL_USER'), local.get('LOCAL_TOKEN')))
        if 'master' in manifest['roles']:
            master = read_env(runtime_path(self.root, 'master/tokens.local'))
            credentials.append((master.get('MASTER_CS_BASE', 'http://127.0.0.1:8018'), master.get('MASTER_MANAGER_USER'), master.get('MASTER_MANAGER_TOKEN')))
        for base, user, token in credentials:
            if not user or not token:
                raise ValueError('Installed authentication credentials are missing; update never re-provisions them')
            request = urllib.request.Request(base.rstrip('/') + '/_matrix/client/v3/account/whoami',
                                             headers={'Authorization': 'Bearer ' + token})
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    actual = json.load(response).get('user_id')
            except OSError:
                raise ValueError('Installed account authentication failed; credentials were retained for diagnosis') from None
            if actual != user:
                raise ValueError('Installed token belongs to an unexpected account')

    def apply(self):
        sha, target, previous = self.inspect()
        with self.lock():
            manifest = read_manifest(self.root)
            if manifest is None:
                roles = (['teammate'] if (self.root / '.env').exists() else []) + (['master'] if (self.root / 'master/.env').exists() else [])
                if not roles:
                    raise ValueError('No installation found; run the installer first')
                for role in roles:
                    manifest = ensure_manifest(self.root, role)
            journal_path = self.meta / 'journal.json'
            if journal_path.exists():
                old = json.loads(journal_path.read_text())
                # Reapplying after a successful rollback is a NEW update,
                # preserving the original backup and taking a fresh snapshot.
                if old.get('complete') and (previous or {}).get('git_sha') != sha:
                    atomic_write(self.meta / 'history' / (uuid.uuid4().hex + '.json'), journal_path.read_bytes())
                    journal_path.unlink()
            journal = Journal(journal_path, {'target': sha, 'release': target, 'previous': previous})
            if journal.data.get('complete'):
                print('This release is already applied.')
                return
            release = self.stage(sha)
            if json.loads((release / 'release.json').read_text()) != target:
                raise ValueError('Release metadata differs from the committed source tree')
            compositions = self.compositions(manifest, release)
            def prepare():
                requirements = release / 'requirements-host.txt'
                if requirements.exists():
                    manifest['python_path'] = ensure_runtime(self.root, requirements)
                for _, cmd in compositions:
                    self.command(cmd + ['config', '--quiet'])
                    self.command(cmd + ['pull'])
                journal.data['inventory'] = inventory(self.root)
                journal.save()
            journal.step('prepare', prepare)
            # Every resume reestablishes quiescence: the user may have restarted
            # a service after an interrupted update. Backups are never taken live.
            self.quiesce(journal, compositions)
            journal.step('backup', lambda: self.backup(journal, compositions))
            # Repeating activation is safe: no provisioning, credential rotation,
            # logout, volume removal, native build or native replacement occurs.
            self.activate(release, manifest, compositions, journal)
            self.health(manifest)
            after = inventory(self.root)
            if any(after.get(key) != value for key, value in journal.data['inventory'].items()):
                raise ValueError('Credential, account or native executable identity changed during update; inspect the backup before continuing')
            installed = dict(target, git_sha=sha, code_root=str(release),
                             previous=journal.data.get('previous'), applied_at=int(time.time()),
                             remote_verification='pending: inspect uplink status for authenticated delivery progress')
            manifest['code_root'] = str(release)
            save_manifest(self.root, manifest)
            atomic_write(self.installed_path, json.dumps(installed, indent=2) + '\n')
            journal.data['complete'] = True
            journal.save()
            print('Applied %s. Accounts and native binary preserved. Backup: %s' % (target['release'], journal.data['backup']))
            print('Remote synchronization verification remains visible in uplink status; an offline master does not trigger re-enrollment.')

    def rollback(self):
        with self.lock():
            active_path = self.meta / 'journal.json'
            if active_path.exists() and not json.loads(active_path.read_text()).get('complete'):
                raise ValueError('Resume the unfinished update before attempting code rollback')
            current = json.loads(self.installed_path.read_text())
            previous = current.get('previous')
            if not previous:
                raise ValueError('No previous managed release; initial adoption requires a separately verified source/state restore')
            check_compatibility(current, previous)
            release = Path(previous['code_root'])
            if not (release / '.staged').exists():
                raise ValueError('Previous release artifacts are missing')
            manifest = read_manifest(self.root)
            if (release / 'requirements-host.txt').exists():
                manifest['python_path'] = ensure_runtime(self.root, release / 'requirements-host.txt')
            rollback_path = self.meta / 'rollback.json'
            if rollback_path.exists() and json.loads(rollback_path.read_text()).get('complete'):
                atomic_write(self.meta / 'history' / (uuid.uuid4().hex + '.json'), rollback_path.read_bytes())
                rollback_path.unlink()
            journal = Journal(rollback_path, {'target': previous['git_sha']})
            compositions = self.compositions(manifest, release)
            self.quiesce(journal, compositions)
            self.activate(release, manifest, compositions, journal)
            self.health(manifest)
            manifest['code_root'] = str(release)
            save_manifest(self.root, manifest)
            atomic_write(self.installed_path, json.dumps(dict(previous, previous=None), indent=2) + '\n')
            journal.data['complete'] = True
            journal.save()
            print('Previous compatible code activated. Send ledgers and account state were not rolled back.')


def hash_file(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', default=str(Path(__file__).resolve().parent))
    choice = p.add_mutually_exclusive_group()
    choice.add_argument('--apply', action='store_true')
    choice.add_argument('--rollback', action='store_true')
    args = p.parse_args()
    updater = Updater(args.root)
    try:
        if args.apply:
            updater.apply()
        elif args.rollback:
            updater.rollback()
        else:
            sha, target, current = updater.inspect()
            print(json.dumps({'target': sha, 'release': target, 'installed': current,
                              'action': 'Review, then run ./update.sh --apply'}, indent=2))
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        p.exit(1, 'update: %s\n' % exc)


if __name__ == '__main__':
    main()
