#!/usr/bin/env python3
"""Real backup/restore drill in a marked disposable local+master environment."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tarfile

from sandbox import Sandbox, load_manifest, REPO

if __name__ == '__main__' and not os.environ.get('SYNCTEST_MANIFEST'):
    sandbox = Sandbox()
    try:
        env = sandbox.prepare()
        result = subprocess.run([sys.executable, __file__], env=env)
        sys.exit(result.returncode)
    finally:
        sandbox.close()

data = load_manifest()
root = Path(os.environ['SYNCTEST_MANIFEST']).resolve().parent
assert json.loads((root/'.beepa-test-root').read_text())['project'] == data['project']
assert Path(data['master_dir']).resolve() == root/'master'
sys.path.insert(0, str(REPO/'master'))
from lifecycle import MasterOps
from test_enroll import enroll, http_json, load_tokens
import control_recovery_fixture as control_fixture

compose = ['docker', 'compose', '-p', data['project'], '-f', str(root/'compose.json')]


class IsolatedOps(MasterOps):
    def command(self, args, **kwargs):
        if 'pg_dump' in args or 'dropdb' in args:
            worker = self.control_worker()
            assert worker and not worker['running'], 'Control writer remained active during backup/restore'
            assert (self.master/'control.local/stopped').exists(), 'Control SQLite writer did not close'
        return super().command(args, **kwargs)

    def running_agents(self):
        return []  # Never discover or modify this Mac's installed LaunchAgents.

    def stop_tailnet_ingress(self):
        self.ingress_disabled = True  # No real Tailscale command is permitted.


ops = IsolatedOps(root, compose=compose, database_service='master-db', matrix_service='master',
                  control_container=data['project']+'-control',
                  agent_installer=lambda *a, **k: (_ for _ in ()).throw(AssertionError('live LaunchAgent operation')))
ops.env['MASTER_CS_BASE'] = data['master_url']
config_path = root/'master/synapse/homeserver.yaml'
config = json.loads(config_path.read_text())
(root/'master/.env').write_text('MASTER_POSTGRES_PASSWORD='+config['database']['args']['password']+'\n')

# A real teammate-side account and event must remain intact through every
# master-only stop/drop/restore. The local homeserver is in the same disposable
# compose file deliberately, exercising the explicit master-service filter.
status, nonce = http_json(data['local_url']+'/_synapse/admin/v1/register')
assert status == 200
username, password = 'lifecyclefixture', secrets.token_urlsafe(24)
mac = hmac.new(data['local_secret'].encode(), digestmod=hashlib.sha1)
mac.update((nonce['nonce']+'\0'+username+'\0'+password+'\0notadmin').encode())
status, local = http_json(data['local_url']+'/_synapse/admin/v1/register', 'POST',
                         body={'nonce':nonce['nonce'],'username':username,'password':password,'admin':False,'mac':mac.hexdigest()})
assert status == 200, status
status, room = http_json(data['local_url']+'/_matrix/client/v3/createRoom', 'POST', local['access_token'], {'preset':'private_chat'})
assert status == 200
status, event = http_json(data['local_url']+'/_matrix/client/v3/rooms/'+room['room_id']+'/send/m.room.message/fixture',
                          'PUT', local['access_token'], {'msgtype':'m.text','body':'teammate fixture survives master restore'})
assert status == 200

before = load_tokens()
control_state = control_fixture.install(root, data['project'], compose, before)
ledger_path = root/'agents/uplink/state.db'
ledger_path.parent.mkdir(parents=True)
with sqlite3.connect(ledger_path) as ledger:
    ledger.execute('CREATE TABLE sent (event_id TEXT, outcome TEXT)')
    ledger.execute("INSERT INTO sent VALUES ('$fixture','ambiguous')")
ledger_before = ledger_path.read_bytes()
native_path = root/'imessage/bin/imessage-cli'
native_path.parent.mkdir(parents=True)
native_path.write_bytes(b'fixture native binary never executed')
native_before = native_path.stat()
os.environ['ENROLL_STORE'] = str(root/'master/enrollments.local')
consumed_code = enroll.mint('alice')
alice_pair = ('lifecycle-alice-install', secrets.token_urlsafe(32))
bob_pair = ('lifecycle-bob-installxx', secrets.token_urlsafe(32))
identity = ops.registry.issue('alice', *alice_pair)
ops.registry.issue('bob', *bob_pair)
revoked_token = enroll._login(data['master_url'],'alice',enroll.derive_password('teammate','alice'))
backup = ops.backup(root/'backup-fixture')
assert ops.control_worker()['running']
with tarfile.open(backup/'master.tar.gz') as archive:
    names = archive.getnames()
    assert {'master/control.local/control.db','master/control.local/config.json',
            'master/notifications.local/notifications.db','master/notifications.local/vapid.pem'} <= set(names)
    assert not any(name.endswith(('-wal','-shm','heartbeat.json','stopped')) for name in names)
# New operational evidence after the snapshot must win over the older archive.
with sqlite3.connect(control_state/'control.db') as db:
    db.execute("UPDATE outbox SET state='accepted' WHERE id='notice:0'")
    db.execute("UPDATE kv SET v=? WHERE k='operator'",(json.dumps({'user':8,'chat':8}),))
with sqlite3.connect(root/'master/notifications.local/notifications.db') as db:
    db.execute("UPDATE deliveries SET state='accepted'")
enroll.exchange(consumed_code)
assert not ops.maintenance.exists()
assert json.loads(ops.journal.read_text())['phase'] == 'complete'

# Revoke a token and a pairing AFTER the snapshot, then add another scoped
# account after it. A restore must not reverse either revocation or enrollment.
assert http_json(data['master_url']+'/_matrix/client/v3/logout', 'POST', revoked_token, {})[0] == 200
ops.registry.revoke_user('bob')
env = dict(os.environ, TEAMMATES='charlie')
subprocess.run(['/bin/bash',str(REPO/'master/provision.sh')], env=env, check=True, stdout=subprocess.DEVNULL)
charlie_pair = ('lifecycle-charlie-install', secrets.token_urlsafe(32))
ops.registry.issue('charlie', *charlie_pair)
registry_before = json.loads(ops.registry.path.read_text())

# Simulate an independently rotated PostgreSQL role password after the backup.
# Its current value stays local to this disposable environment.
new_password = secrets.token_hex(32)
subprocess.run(compose+['exec','-T','master-db','psql','-U','matrix','-d','postgres'],
               input=("ALTER ROLE matrix PASSWORD '"+new_password+"';\n").encode(), check=True, stdout=subprocess.DEVNULL)
config['database']['args']['password'] = new_password
config_path.write_text(json.dumps(config))
(root/'master/.env').write_text('MASTER_POSTGRES_PASSWORD='+new_password+'\n')

result = ops.restore(backup)
assert ops.control_worker()['running']
assert result['control_pairing_preserved'] and result['browser_connections_preserved']
assert not result['control_setup_required']
control_fixture.assert_restored(root, load_tokens(), current=True)
assert result['restored'] and ops.ingress_disabled
assert result['master_authority_id'] == identity['master_authority_id']
assert result['master_data_epoch'] != identity['master_data_epoch']
assert not ops.maintenance.exists()
after_registry = json.loads(ops.registry.path.read_text())
assert after_registry['installs'] == registry_before['installs']
assert after_registry['revoked_users'] == registry_before['revoked_users']
assert json.loads(config_path.read_text())['database']['args']['password'] == new_password

for old_token in (revoked_token, before['MASTER_ALICE_TOKEN'], before['MASTER_BOB_TOKEN'], before['MASTER_MANAGER_TOKEN']):
    assert http_json(data['master_url']+'/_matrix/client/v3/account/whoami', token=old_token)[0] == 401
for pair in (alice_pair, charlie_pair):
    recovered = enroll.recover_pairing(*pair, identity['master_authority_id'])
    assert recovered['master_data_epoch'] == result['master_data_epoch']
    status, who = http_json(data['master_url']+'/_matrix/client/v3/account/whoami', token=recovered['master_token'])
    assert status == 200 and who['user_id'] == recovered['master_user']
try:
    enroll.recover_pairing(*bob_pair, identity['master_authority_id'])
except enroll.RecoveryError:
    pass
else:
    raise AssertionError('Revoked pairing recovered after restoring old data')
assert load_tokens()['MASTER_TEAMMATES'] == 'alice charlie'
try:
    enroll.exchange(consumed_code)
except enroll.EnrollError:
    pass
else:
    raise AssertionError('A code consumed after the backup became usable again')

# Archive refresh retires only verified managed mirrors before announcing a new
# epoch. Existing accounts/tokens and unrelated private rooms remain untouched.
tokens_before_rebuild = load_tokens()
alice_token = tokens_before_rebuild['MASTER_ALICE_TOKEN']
manager_token = tokens_before_rebuild['MASTER_MANAGER_TOKEN']
status, mirror = http_json(data['master_url']+'/_matrix/client/v3/createRoom','POST',alice_token,{
    'preset':'private_chat','invite':['@manager:master'],
    'creation_content':{'com.jkali.mirror_of':room['room_id']},
    'power_level_content_override':{'users':{'@alice:master':100,'@manager:master':0},'events_default':50,'kick':100}})
assert status == 200
status, unrelated = http_json(data['master_url']+'/_matrix/client/v3/createRoom','POST',alice_token,{'preset':'private_chat'})
assert status == 200
for child in (mirror, unrelated):
    assert http_json(data['master_url']+'/_matrix/client/v3/rooms/'+tokens_before_rebuild['MASTER_SPACE_ALICE']+'/state/m.space.child/'+child['room_id'],
                     'PUT',alice_token,{'via':['master']})[0] == 200
assert http_json(data['master_url']+'/_matrix/client/v3/rooms/'+mirror['room_id']+'/join','POST',manager_token,{})[0] == 200
refreshed = ops.rebuild_archive()
assert refreshed['retired_rooms'] == 1
assert refreshed['master_data_epoch'] != result['master_data_epoch']
assert load_tokens() == tokens_before_rebuild
for token in (alice_token, manager_token):
    status, joined = http_json(data['master_url']+'/_matrix/client/v3/joined_rooms',token=token)
    assert status == 200 and mirror['room_id'] not in joined['joined_rooms']
status, linked = http_json(data['master_url']+'/_matrix/client/v3/rooms/'+tokens_before_rebuild['MASTER_SPACE_ALICE']+'/state/m.space.child/'+mirror['room_id'],token=alice_token)
assert status == 200 and linked == {}
assert http_json(data['master_url']+'/_matrix/client/v3/rooms/'+unrelated['room_id']+'/state',token=alice_token)[0] == 200
assert ledger_path.read_bytes() == ledger_before
assert native_path.read_bytes() == b'fixture native binary never executed'
assert native_path.stat().st_ino == native_before.st_ino

# Disaster recovery: only the snapshot survived. Remove only synthetic feature
# state while its unique container is stopped; no installed worker is touched.
subprocess.run(compose+['stop','control'],check=True,stdout=subprocess.DEVNULL)
shutil.rmtree(control_state)
shutil.rmtree(root/'master/notifications.local')
control_state.mkdir(mode=0o700)
# The stopped fixture already closed its writer; preserve that test evidence.
(control_state/'stopped').write_text('closed')
disaster = ops.restore(backup)
assert not ops.control_worker()['running'], 'Originally stopped worker was started'
assert not disaster['control_pairing_preserved'] and not disaster['browser_connections_preserved']
control_fixture.assert_restored(root, load_tokens(), current=False)
subprocess.run(compose+['up','-d','control'],check=True,stdout=subprocess.DEVNULL)
heartbeat = control_fixture.wait_worker(control_state)
assert heartbeat['operator'] is None and heartbeat['outbox']['notice:0'] == 'canceled'

status, who = http_json(data['local_url']+'/_matrix/client/v3/account/whoami', token=local['access_token'])
assert status == 200 and who['user_id'] == local['user_id']
status, retained = http_json(data['local_url']+'/_matrix/client/v3/rooms/'+room['room_id']+'/event/'+event['event_id'],
                           token=local['access_token'])
assert status == 200 and retained['content']['body'] == 'teammate fixture survives master restore'
print('Lifecycle PASS: real backup/restore and snapshot-only recovery; stopped scoped control writer; current evidence retained; stale work quarantined; fresh manager credentials; revoked access retained; preserved local event/send ledger/native binary.')
