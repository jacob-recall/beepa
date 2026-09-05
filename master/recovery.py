"""Scoped pairing recovery state, retained outside the resettable Synapse DB."""
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid


class RecoveryError(ValueError):
    pass


class Registry:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path) + '.lock', 'a') as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.path.exists():
                state = json.loads(self.path.read_text())
                if state.get('version') != 1:
                    raise RecoveryError('Unsupported recovery registry version')
            else:
                state = {'version': 1, 'master_authority_id': str(uuid.uuid4()),
                         'master_data_epoch': str(uuid.uuid4()), 'installs': {}, 'revoked_users': {}}
            before = json.dumps(state, sort_keys=True)
            yield state
            if not self.path.exists() or before != json.dumps(state, sort_keys=True):
                fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix='.recovery-')
                try:
                    with os.fdopen(fd, 'w') as target:
                        os.fchmod(target.fileno(), 0o600)
                        json.dump(state, target, sort_keys=True)
                        target.flush()
                        os.fsync(target.fileno())
                    os.replace(temporary, self.path)
                    fd = os.open(self.path.parent, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)

    @staticmethod
    def public(state):
        return dict({key: state[key] for key in ('master_authority_id', 'master_data_epoch')},
                    wire_version=1, reads_wire_versions=[1])

    def manifest(self):
        with self.transaction() as state:
            return self.public(state)

    def bump_epoch(self):
        with self.transaction() as state:
            state['master_data_epoch'] = str(uuid.uuid4())
            return self.public(state)

    @staticmethod
    def validate(install_id, token):
        if not isinstance(install_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', install_id):
            raise RecoveryError('Invalid installation ID')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', token):
            raise RecoveryError('Recovery credential must contain at least 32 random URL-safe characters')

    def issue(self, user, install_id, token):
        self.validate(install_id, token)
        with self.transaction() as state:
            if user in state['revoked_users']:
                raise RecoveryError('Pairing revoked')
            prior = state['installs'].get(install_id)
            if prior and (prior['user'] != user or prior.get('revoked')):
                raise RecoveryError('Installation is bound to another or revoked pairing')
            state['installs'][install_id] = {'user': user, 'verifier': hashlib.sha256(token.encode()).hexdigest(),
                                           'updated_at': int(time.time()), 'revoked': False}
            return dict(self.public(state), install_id=install_id)

    @contextmanager
    def authorized(self, install_id, token, authority):
        self.validate(install_id, token)
        # Keep revoke serialized with the entire issuance operation.
        with self.transaction() as state:
            record = state['installs'].get(install_id)
            if (authority != state['master_authority_id'] or not record or record.get('revoked')
                    or record['user'] in state['revoked_users'] or not hmac.compare_digest(
                        record['verifier'], hashlib.sha256(token.encode()).hexdigest())):
                raise RecoveryError('Unknown, revoked or changed pairing')
            yield record['user'], self.public(state)

    def revoke_user(self, user):
        with self.transaction() as state:
            state['revoked_users'][user] = int(time.time())
            for record in state['installs'].values():
                if record['user'] == user:
                    record['revoked'] = True
