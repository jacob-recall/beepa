#!/usr/bin/env python3
"""Master-only operations; never reset teammate stores or native authorization."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import urllib.error
import urllib.parse

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT))
from install_config import atomic_write, read_env, read_manifest, install_agent
try:
    from .recovery import Registry
except ImportError:
    from recovery import Registry


class MasterOps:
    def __init__(self, root, run=None, compose=None, database_service='postgres', matrix_service='synapse', agent_installer=None):
        self.install_root = Path(root).resolve()
        self.manifest = read_manifest(self.install_root) or {}
        self.root = (Path(self.manifest['state_root']).expanduser().resolve()
                     if self.manifest.get('state_initialized') else self.install_root)
        self.code_root = Path(self.manifest.get('code_root') or CODE_ROOT).resolve()
        self.master = Path(os.environ.get('BEEPA_MASTER_STATE_DIR')
                           or self.manifest.get('master_state_root') or self.root/'master').expanduser().resolve()
        self.run = run or subprocess.run
        self.registry = Registry(self.master / 'recovery.local.json')
        self.maintenance = self.master / 'runtime/maintenance'
        self.journal = self.master / 'runtime/operation.json'
        self.database_service, self.matrix_service = database_service, matrix_service
        self.agent_installer = agent_installer or install_agent
        self.compose = compose or ['docker', 'compose', '-p', self.manifest.get('master_compose_project', 'matrix-master'),
                        '--project-directory', str(self.master), '--env-file', str(self.master/'.env'),
                        '-f', str(self.code_root/'master/docker-compose.master.yml')]
        self.env = dict(os.environ, BEEPA_INSTALL_ROOT=str(self.root), BEEPA_MASTER_STATE_DIR=str(self.master))

    def phase(self, state, phase):
        state['phase'] = phase
        atomic_write(self.journal, json.dumps(state, indent=2)+'\n')

    def pending_operation(self):
        if self.journal.exists():
            state = json.loads(self.journal.read_text())
            if state.get('phase') != 'complete':
                return state
        return None

    def restart(self):
        with self.lock():
            if self.pending_operation() or self.maintenance.exists():
                raise ValueError('Master maintenance is active; retry the recorded backup/restore operation first')
            self.command(self.compose+['up','-d',self.database_service,self.matrix_service])
            self.command(self.compose+['restart',self.database_service,self.matrix_service])
            self.wait_database()
            self.wait_health()
            if sys.platform == 'darwin':
                for name in ('master-enroll', 'master-gateway'):
                    self.agent_installer(self.root, name, code_root=self.code_root)

    def rebuild_archive(self):
        with self.lock():
            state = self.pending_operation()
            if state and state.get('operation') != 'rebuild':
                raise ValueError('Master maintenance is active; finish the recorded operation first')
            if not state:
                if self.maintenance.exists():
                    raise ValueError('Master maintenance is already active')
                self.wait_health()
                state = {'format': 1, 'operation': 'rebuild', 'source': 'archive',
                         'agents': self.running_agents(), 'services': [self.database_service, self.matrix_service],
                         'maintenance_existed': False, 'before_epoch': self.registry.manifest()['master_data_epoch']}
                self.phase(state, 'prepared')
            atomic_write(self.maintenance, 'Master archive rebuild in progress\n')
            self.stop_tailnet_ingress()
            running = {label for _name, label in self.running_agents()}
            for name, label in state['agents']:
                if name == 'master-enroll' and label in running:
                    self.command(['launchctl', 'bootout', label])
            if any(name == 'master-gateway' and label in running for name, label in state['agents']):
                self.wait_gateway_drained()
            if 'retirements' not in state:
                state['retirements'] = self.discover_archive_rooms()
                self.phase(state, 'retiring_archive')
            for item in state['retirements']:
                self.retire_archive_room(state, item)
            identity = self.registry.manifest()
            if identity['master_data_epoch'] == state['before_epoch']:
                identity = self.registry.bump_epoch()
            state['result'] = dict(identity, retired_rooms=len(state['retirements']))
            self.phase(state, 'archive_retired')
            self.resume(state)
            return dict(state['result'], tailnet_ingress='off; run master/tailscale-serve.sh after local verification')

    def matrix(self, token, method, path, body=None):
        base = self.env.get('MASTER_CS_BASE', 'http://127.0.0.1:8018').rstrip('/')
        raw = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(base+path, data=raw, method=method,
                                         headers={'Authorization':'Bearer '+token, 'Content-Type':'application/json'})
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)

    def archive_credentials(self, user):
        tokens = read_env(self.master/'tokens.local')
        key = user.upper()
        return tokens['MASTER_'+key+'_TOKEN'], tokens['MASTER_'+key+'_USER'], tokens['MASTER_MANAGER_USER']

    def discover_archive_rooms(self):
        tokens = read_env(self.master/'tokens.local')
        result = []
        enc = lambda value: urllib.parse.quote(value, safe='')
        for user in tokens.get('MASTER_TEAMMATES', '').split():
            token, owner, _manager = self.archive_credentials(user)
            who = self.matrix(token, 'GET', '/_matrix/client/v3/account/whoami')
            if who.get('user_id') != owner:
                raise ValueError('Archive owner credential mismatch')
            space = tokens['MASTER_SPACE_'+user.upper()]
            events = self.matrix(token, 'GET', '/_matrix/client/v3/rooms/'+enc(space)+'/state')
            for child in events:
                if child.get('type') != 'm.space.child' or not (child.get('content') or {}).get('via'):
                    continue
                room = child.get('state_key')
                states = self.matrix(token, 'GET', '/_matrix/client/v3/rooms/'+enc(room)+'/state')
                creation = next((ev for ev in states if ev.get('type') == 'm.room.create' and ev.get('state_key') == ''), {})
                if creation.get('sender') != owner:
                    continue
                managed = bool((creation.get('content') or {}).get('com.jkali.mirror_of')) or any(
                    ev.get('sender') == owner and ev.get('type') in ('com.jkali.contacts','com.jkali.proposals')
                    for ev in states)
                if managed:
                    result.append({'user':user, 'owner':owner, 'space':space, 'room':room, 'step':0})
        return result

    def retire_archive_room(self, state, item):
        token, owner, manager = self.archive_credentials(item['user'])
        if owner != item['owner']:
            raise ValueError('Archive owner changed during retirement')
        enc = lambda value: urllib.parse.quote(value, safe='')
        room = '/_matrix/client/v3/rooms/'+enc(item['room'])
        if item['step'] < 1:
            self.matrix(token, 'PUT', '/_matrix/client/v3/rooms/'+enc(item['space'])+'/state/m.space.child/'+enc(item['room']), {})
            item['step'] = 1
            self.phase(state, 'retiring_archive')
        if item['step'] < 2:
            try:
                member = self.matrix(token, 'GET', room+'/state/m.room.member/'+enc(manager))
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
                member = {}
            if member.get('membership') in ('join','invite','knock'):
                self.matrix(token, 'POST', room+'/kick', {'user_id':manager,'reason':'Master archive rebuild'})
            item['step'] = 2
            self.phase(state, 'retiring_archive')
        if item['step'] < 3:
            try:
                self.matrix(token, 'POST', room+'/leave', {})
            except urllib.error.HTTPError:
                joined = self.matrix(token, 'GET', '/_matrix/client/v3/joined_rooms').get('joined_rooms', [])
                if item['room'] in joined:
                    raise
            item['step'] = 3
            self.phase(state, 'retiring_archive')

    def command(self, args, **kwargs):
        return self.run([str(a) for a in args], env=self.env, check=True, **kwargs)

    @contextlib.contextmanager
    def lock(self):
        path = self.root / '.beepa-update/operation.lock'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('Another update or master operation is active')
            yield

    def running_agents(self):
        result = []
        if sys.platform == 'darwin':
            for name in ('master-enroll', 'master-gateway'):
                for prefix in ('org.beepa.', 'com.jkali.'):
                    label = 'gui/%d/%s%s' % (os.getuid(), prefix, name)
                    if self.run(['launchctl', 'print', label], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL).returncode == 0:
                        result.append((name, label))
        return result

    def quiesce(self, operation='backup', source=''):
        state = self.pending_operation()
        if state and (state.get('operation') != operation or state.get('source') != str(source)):
            raise ValueError('Finish the recorded master operation before starting another')
        if not state:
            running = self.command(self.compose+['ps', '--status', 'running', '--services'], capture_output=True).stdout.decode().split()
            # Explicit service scope matters even if a custom compose file also
            # contains the teammate stack (as our disposable integration does).
            state = {'format': 1, 'operation': operation, 'source': str(source),
                     'agents': self.running_agents(),
                     'services': [s for s in running if s in (self.database_service, self.matrix_service)],
                     'maintenance_existed': self.maintenance.exists()}
            self.phase(state, 'prepared')
        atomic_write(self.maintenance, 'Master maintenance in progress\n')
        self.phase(state, 'quiescing')
        # An interrupted retry uses the original inventory, but does not bootout
        # agents that have already been stopped.
        running_labels = {label for _name, label in self.running_agents()}
        for name, label in state['agents']:
            if name == 'master-enroll' and label in running_labels:
                self.command(['launchctl', 'bootout', label])
        if any(name == 'master-gateway' and label in running_labels for name, label in state['agents']):
            self.wait_gateway_drained()
            for name, label in state['agents']:
                if name == 'master-gateway' and label in running_labels:
                    self.command(['launchctl', 'bootout', label])
        writers = [s for s in state['services'] if s == self.matrix_service]
        if writers:
            self.command(self.compose + ['stop'] + writers)
        self.phase(state, 'quiesced')
        return state

    def resume(self, state):
        self.phase(state, 'resuming')
        services = state['services']
        if services:
            self.command(self.compose + ['up', '-d'] + services)
        if self.matrix_service in services:
            self.wait_health()
        for name, _label in state['agents']:
            self.agent_installer(self.root, name, code_root=self.code_root)
        if not state.get('maintenance_existed'):
            self.maintenance.unlink(missing_ok=True)
        self.phase(state, 'complete')

    def wait_gateway_drained(self):
        base = 'http://127.0.0.1:' + self.env.get('MASTER_GATEWAY_PORT', '8017')
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            try:
                try:
                    response = urllib.request.urlopen(base+'/health', timeout=2)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    if json.load(response).get('active_upstreams') == 0:
                        return
            except (OSError, ValueError):
                # A stopped/unreachable gateway has no new ingress; Synapse is
                # stopped next before any database operation.
                return
            time.sleep(.2)
        raise RuntimeError('Gateway requests did not drain; maintenance remains enabled')

    def wait_database(self):
        deadline = time.monotonic()+90
        while time.monotonic() < deadline:
            result = self.run(self.compose+['exec','-T',self.database_service,'pg_isready','-U','matrix','-d','postgres'],
                              env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode == 0:
                return
            time.sleep(1)
        raise RuntimeError('Master Postgres did not become ready; maintenance remains enabled')

    def wait_health(self):
        base = self.env.get('MASTER_CS_BASE', 'http://127.0.0.1:8018')
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(base+'/health', timeout=2) as response:
                    if response.status == 200:
                        return
            except OSError:
                time.sleep(1)
        raise RuntimeError('Master did not become healthy; maintenance remains enabled')

    def backup(self, output):
        output = Path(output).expanduser().resolve()
        pending = self.pending_operation()
        retry = pending and pending.get('operation') == 'backup' and pending.get('source') == str(output)
        if (output.exists() and not retry) or output == self.master or self.master in output.parents:
            raise ValueError('Choose a new backup directory outside master runtime state')
        with self.lock():
            identity = self.registry.manifest()
            if retry and (output/'backup.json').exists():
                self.verify_backup(output)
                self.resume(pending)
                return output
            state = self.quiesce('backup', output)
            try:
                if self.database_service not in state['services']:
                    raise ValueError('Master Postgres must be running to take a backup')
                self.wait_database()
                self.phase(state, 'copying_backup')
                output.mkdir(parents=True, mode=0o700, exist_ok=bool(retry))
                with (output/'database.dump').open('wb') as target:
                    os.chmod(target.name, 0o600)
                    self.command(self.compose+['exec', '-T', self.database_service, 'pg_dump', '-U', 'matrix',
                                              '-Fc', 'synapse'], stdout=target)
                if not (output/'database.dump').stat().st_size:
                    raise ValueError('Empty database backup')
                with tarfile.open(output/'master.tar.gz', 'w:gz') as archive:
                    for relative in ('master/.env', 'master/synapse', 'master/.beepa-config', 'master/tokens.local',
                                     'master/.provision-state.local', 'master/enrollments.local',
                                     'master/recovery.local.json', '.beepa-install.json',
                                     '.beepa-update/installed.json'):
                        path = (self.master/Path(relative).relative_to('master')
                                if relative.startswith('master/') else self.root/relative)
                        if path.exists():
                            archive.add(path, arcname=relative)
                    if (self.code_root/'release.json').exists():
                        archive.add(self.code_root/'release.json', arcname='release.json')
                (output/'master.tar.gz').chmod(0o600)
                info = dict(identity, format=1, created_at=int(time.time()),
                            checksums={name: digest(output/name) for name in ('database.dump','master.tar.gz')})
                atomic_write(output/'backup.json', json.dumps(info, indent=2)+'\n')
                self.verify_backup(output)
            finally:
                self.resume(state)
        return output

    @staticmethod
    def verify_backup(backup):
        backup = Path(backup)
        info = json.loads((backup/'backup.json').read_text())
        if info.get('format') != 1:
            raise ValueError('Unsupported backup version')
        for name in ('database.dump','master.tar.gz'):
            if digest(backup/name) != info.get('checksums',{}).get(name):
                raise ValueError('Backup checksum mismatch: '+name)
        with tarfile.open(backup/'master.tar.gz') as archive:
            for member in archive:
                path = Path(member.name)
                if (path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir())
                        or not backup_member_allowed(path)):
                    raise ValueError('Backup contains an unsafe path or link')
            try:
                registry = json.load(archive.extractfile('master/recovery.local.json'))
            except (KeyError, TypeError):
                raise ValueError('Backup is missing recovery identity material')
            if registry.get('master_authority_id') != info.get('master_authority_id'):
                raise ValueError('Backup authority manifest does not match recovery material')
        return info

    def stop_tailnet_ingress(self):
        ts = shutil.which('tailscale')
        app = Path('/Applications/Tailscale.app/Contents/MacOS/Tailscale')
        if not ts and app.exists():
            ts = str(app)
        if ts:
            # Existing legacy 8443 points straight at enrollment; turn off both
            # master mappings before restoring any historical authorization.
            for port in ('443','8443'):
                self.command([ts, 'serve', '--https='+port, 'off'])

    def restore(self, backup):
        backup = Path(backup).resolve()
        info = self.verify_backup(backup)
        with self.lock():
            current = json.loads(self.registry.path.read_text()) if self.registry.path.exists() else None
            if current and current['master_authority_id'] != info['master_authority_id']:
                raise ValueError('Backup belongs to another master authority')
            state = self.quiesce('restore', backup)
            # Freeze enrollment before capturing the latest roster/registry.
            # A pairing issued while quiescing must not be overwritten by an
            # earlier in-memory snapshot or omitted during reprovisioning.
            current = json.loads(self.registry.path.read_text()) if self.registry.path.exists() else None
            if current and current['master_authority_id'] != info['master_authority_id']:
                raise ValueError('Master authority changed while entering maintenance')
            current_tokens = read_env(self.master/'tokens.local')
            if 'retained_roster' not in state:
                state['retained_roster'] = current_tokens.get('MASTER_TEAMMATES', '').split()
                state['retained_endpoints'] = {key: current_tokens[key] for key in ('MASTER_PUBLIC_URL', 'ENROLL_PUBLIC_URL')
                                               if current_tokens.get(key)}
                self.phase(state, 'retained_current_identity')
            current_names = state['retained_roster']
            self.stop_tailnet_ingress()
            self.phase(state, 'restoring_files')
            existing_password = read_env(self.master/'.env').get('MASTER_POSTGRES_PASSWORD')
            # Failures intentionally leave maintenance on. Repeat restore with
            # the same verified backup after fixing the underlying problem.
            with tempfile.TemporaryDirectory(prefix='beepa-master-restore-') as temporary:
                staged = Path(temporary)
                with tarfile.open(backup/'master.tar.gz') as archive:
                    for member in archive:
                        if not member.isfile():
                            continue
                        destination = staged/member.name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with archive.extractfile(member) as source, destination.open('wb') as target:
                            shutil.copyfileobj(source,target)
                        destination.chmod(0o600)
                for path in (staged/'master').rglob('*'):
                    if not path.is_file() or path.name == 'recovery.local.json':
                        continue
                    relative = path.relative_to(staged/'master')
                    if relative == Path('.env') and (self.master/'.env').exists():
                        # Existing Postgres role password belongs to this cluster.
                        continue
                    if relative == Path('enrollments.local'):
                        if (self.master/relative).exists():
                            # Consumed/invalidated codes must not become usable
                            # again by restoring their earlier unused state.
                            continue
                        codes = json.loads(path.read_text())
                        for record in codes.get('codes', {}).values():
                            record['used_at'] = int(time.time())
                        atomic_write(self.master/relative, json.dumps(codes)+'\n')
                        continue
                    destination = self.master/relative
                    destination.parent.mkdir(parents=True,exist_ok=True)
                    if self.master not in destination.resolve().parents:
                        raise ValueError('Restore destination escapes master state')
                    if relative == Path('synapse/homeserver.yaml') and existing_password:
                        atomic_write(destination, with_database_password(path.read_text(), existing_password))
                        continue
                    shutil.copyfile(path,destination)
                    destination.chmod(0o600)
                if current is None:
                    recovered = json.loads((staged/'master/recovery.local.json').read_text())
                    # A stale backup cannot prove which pairings were revoked
                    # later. Keep the identity, quarantine all old credentials.
                    for record in recovered['installs'].values():
                        record['revoked'] = True
                    atomic_write(self.registry.path,json.dumps(recovered))
            self.phase(state, 'restoring_database')
            self.command(self.compose+['up','-d',self.database_service])
            self.wait_database()
            self.command(self.compose+['exec','-T',self.database_service,'dropdb','-U','matrix','--force','--if-exists','synapse'])
            self.command(self.compose+['exec','-T',self.database_service,'createdb','-U','matrix','--template=template0',
                                      '--encoding=UTF8','--lc-collate=C','--lc-ctype=C','synapse'])
            with (backup/'database.dump').open('rb') as stream:
                self.command(self.compose+['exec','-T',self.database_service,'pg_restore','-U','matrix','--no-owner','--exit-on-error',
                                          '-d','synapse'],stdin=stream)
            self.registry.bump_epoch()
            self.phase(state, 'sanitizing_auth')
            self.command(self.compose+['up','-d',self.matrix_service])
            self.wait_health()
            # Invalidate every managed restored token, including tokens revoked
            # after the snapshot through ordinary Matrix logout. Recovery then
            # issues current scoped sessions; never expose old restored tokens.
            self.command([sys.executable,self.code_root/'master/lifecycle.py','--root',self.root,'sanitize-restored-auth'])
            registry = json.loads(self.registry.path.read_text())
            revoked = set(registry.get('revoked_users', {}))
            # Keep valid post-snapshot enrollments, but never let an inherited
            # TEAMMATES shell override silently recreate revoked accounts.
            names = set(current_names) | {r['user'] for r in registry['installs'].values() if not r.get('revoked')}
            previous_roster = self.env.get('TEAMMATES')
            self.env['TEAMMATES'] = ' '.join(sorted(names - revoked))
            try:
                self.command(['/bin/bash',self.code_root/'master/provision.sh'])
            finally:
                if previous_roster is None:
                    self.env.pop('TEAMMATES', None)
                else:
                    self.env['TEAMMATES'] = previous_roster
            endpoints = state['retained_endpoints']
            if endpoints:
                text = (self.master/'tokens.local').read_text()
                for key, value in endpoints.items():
                    if "'" in value or '\n' in value or '\r' in value:
                        raise ValueError('Invalid retained public endpoint')
                    text = re.sub(r'^'+key+r'=.*\n?', '', text, flags=re.M) + key+"='"+value+"'\n"
                atomic_write(self.master/'tokens.local', text)
            toks = read_env(self.master/'tokens.local')
            atomic_write(self.root/'apps/master/session.local.json',json.dumps({
                'user_id':toks['MASTER_MANAGER_USER'],'access_token':toks['MASTER_MANAGER_TOKEN']}))
            state['services'] = sorted(set(state['services']) | {self.database_service,self.matrix_service})
            self.resume(state)
            return {'restored':True,'tailnet_ingress':'off; run master/tailscale-serve.sh after local verification',
                    **self.registry.manifest()}

    def sanitize_restored_auth(self):
        if not self.maintenance.exists():
            raise ValueError('Authentication recovery requires maintenance mode')
        import enroll
        registry = json.loads(self.registry.path.read_text())
        revoked = set(registry.get('revoked_users',{}))
        users = enroll.known_teammates()
        for name in ['manager']+users:
            token = enroll._login(enroll._cs_base(),name,enroll.derive_password('manager' if name=='manager' else 'teammate',name))
            if name in revoked:
                enroll._deactivate_account(enroll._cs_base(),token,name,enroll.derive_password('teammate',name))
            else:
                status,_ = enroll._request('POST',enroll._cs_base()+'/_matrix/client/v3/logout/all',
                    headers={'Authorization':'Bearer '+token},data=b'{}')
                if status != 200:
                    raise ValueError('Could not invalidate restored account sessions')
        for name in revoked:
            U=enroll._key(name)
            enroll._remove_shell_vars(enroll.TOKENS_FILE,['MASTER_%s_USER'%U,'MASTER_%s_TOKEN'%U,'MASTER_SPACE_%s'%U])
            enroll._remove_shell_vars(enroll.STATE_FILE,['SPACE_%s'%U])
        roster=' '.join(name for name in users if name not in revoked)
        enroll._upsert_shell_vars(enroll.STATE_FILE,{'TEAMMATES':roster},'# Master provisioning state')
        enroll._upsert_shell_vars(enroll.TOKENS_FILE,{'MASTER_TEAMMATES':roster},'# Master scoped tokens')


def backup_member_allowed(path):
    name = path.as_posix()
    return (name in {'master/.env', 'master/tokens.local', 'master/.provision-state.local',
                     'master/enrollments.local', 'master/recovery.local.json', '.beepa-install.json',
                     '.beepa-update/installed.json', 'release.json', 'master/synapse', 'master/.beepa-config'}
            or name.startswith(('master/synapse/', 'master/.beepa-config/')))


def with_database_password(text, password):
    """Retain the target cluster's password in JSON or generated YAML config."""
    if text.lstrip().startswith('{'):
        data = json.loads(text)
        data['database']['args']['password'] = password
        return json.dumps(data, indent=2)+'\n'
    # Our generated YAML has one top-level database block. Only its password
    # scalar changes; unrelated auth keys, paths and operator settings survive.
    block = re.search(r'^database:\s*\n(?:[ \t]+[^\n]*\n|\n)*', text, re.M)
    if not block:
        raise ValueError('Cannot locate database settings in restored Synapse config')
    replacement, count = re.subn(r'^(\s+password:)\s*[^\n]*',
                                 lambda m: m[1]+' '+json.dumps(password), block[0], flags=re.M)
    if count != 1:
        raise ValueError('Cannot identify restored database password')
    return text[:block.start()]+replacement+text[block.end():]


def digest(path):
    result=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            result.update(chunk)
    return result.hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(os.environ.get('BEEPA_INSTALL_ROOT',CODE_ROOT)))
    sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('status');sub.add_parser('restart')
    backup=sub.add_parser('backup');backup.add_argument('--output',required=True,type=Path)
    restore=sub.add_parser('restore');restore.add_argument('--backup',required=True,type=Path)
    restore.add_argument('--confirm-restore',action='store_true')
    rebuild=sub.add_parser('rebuild-archive');rebuild.add_argument('--confirm-rebuild',action='store_true')
    sub.add_parser('sanitize-restored-auth')
    args=parser.parse_args()
    try:
        ops=MasterOps(args.root)
        # A pull does not activate a release. Run the operation implementation
        # recorded by the installer/updater, as well as that release's services.
        if ops.code_root != CODE_ROOT:
            active = ops.code_root/'master/lifecycle.py'
            if not active.is_file():
                raise ValueError('Active release has no master lifecycle command; apply a compatible update first')
            os.execv(sys.executable, [sys.executable, str(active), *sys.argv[1:]])
        if args.action=='status':
            ops.command(ops.compose+['ps'])
            print(json.dumps({'maintenance':ops.maintenance.exists(), 'operation':ops.pending_operation(),
                              **ops.registry.manifest()}))
        elif args.action=='restart':
            ops.restart()
        elif args.action=='backup':
            print(ops.backup(args.output))
        elif args.action=='restore':
            if not args.confirm_restore:
                raise ValueError('Restore replaces only the master database; pass --confirm-restore after verifying the backup')
            print(json.dumps(ops.restore(args.backup)))
        elif args.action=='rebuild-archive':
            if not args.confirm_rebuild:
                raise ValueError('Pass --confirm-rebuild to retire current mirror generations and replay current shares')
            print(json.dumps(ops.rebuild_archive()))
        else:
            ops.sanitize_restored_auth()
    except (OSError,ValueError,RuntimeError,subprocess.CalledProcessError) as error:
        parser.exit(1,str(error)+'\n')


if __name__=='__main__':
    main()
