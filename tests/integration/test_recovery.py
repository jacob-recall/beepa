"""Scoped recovery against disposable real Synapse, including database loss."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import urllib.request
import urllib.error
import urllib.parse
import sys
from test_enroll import serve, http_json, TOK, MASTER_HS, enroll, SANDBOX
from sandbox import REPO
from harness import fresh_env
sys.path.insert(0, str(REPO / 'agents/uplink'))
from uplink import Config, Uplink, MasterUnreachable

install_id = 'integration-install-' + secrets.token_hex(12)
credential = secrets.token_urlsafe(32)
payload = {'install_id': install_id, 'recovery_token': credential}
with serve() as base:
    status, manifest = http_json(base + '/enroll/recovery/issue', method='POST',
                                token=TOK['MASTER_ALICE_TOKEN'], body=payload)
    assert status == 200, (status, manifest)
    request = dict(payload, master_authority_id=manifest['master_authority_id'])
    status, _ = http_json(base + '/enroll/recovery/issue', method='POST',
                         token=TOK['MASTER_BOB_TOKEN'], body=payload)
    assert status == 403, 'Another teammate must not take this installation'
    status, _ = http_json(MASTER_HS + '/_matrix/client/v3/logout/all', method='POST',
                         token=TOK['MASTER_ALICE_TOKEN'], body={})
    assert status == 200
    status, result = http_json(base + '/enroll/recovery', method='POST', body=request)
    assert status == 200 and result['master_user'] == '@alice:master', (status, result)
    status, _ = http_json(MASTER_HS + '/_matrix/client/v3/account/whoami', token=result['master_token'])
    assert status == 200
    status, _ = http_json(base + '/enroll/recovery', method='POST', body=dict(request, master_authority_id='wrong'))
    assert status == 403

    # Exercise the actual teammate client over HTTP to BOTH isolated servers.
    # No fake recovery transport: URL routing, bearer issuance, account-data
    # persistence, 401 recovery and identity checks all execute production code.
    client_local = fresh_env('recovery_client')
    daemon = Uplink(Config(dict(LOCAL_HS_URL=SANDBOX['local_url'],
        LOCAL_USER=client_local['tuser_id'], LOCAL_TOKEN=client_local['tuser_tok'],
        MASTER_HS_URL=MASTER_HS, MASTER_USER=result['master_user'], MASTER_TOKEN=result['master_token'],
        MASTER_SPACE=result['master_space'], MANAGER_MXID=result['manager_mxid'],
        MASTER_ENROLL_URL=base, UPLINK_DB=client_local['db_path'])))
    assert daemon.refresh_master_config()
    daemon.refresh_recovery()
    assert daemon.meta_get('recovery_issued') == '1'
    assert daemon.meta_get('recovery_authority') == manifest['master_authority_id']
    daemon.db.execute("INSERT INTO proposal_map VALUES ('$already-sent','$record','sent')")
    daemon.db.execute("INSERT INTO direct_send_log VALUES (1,'fixture-room-hash')")
    daemon.db.commit()
    status, _ = http_json(MASTER_HS + '/_matrix/client/v3/logout/all', method='POST',
                         token=daemon.cfg.master_token, body={})
    assert status == 200
    try:
        daemon.master('GET', '/_matrix/client/v3/account/whoami')
        raise AssertionError('First invalid-token request should schedule reconciliation')
    except MasterUnreachable:
        pass
    assert daemon.master('GET', '/_matrix/client/v3/account/whoami')['user_id'] == '@alice:master'
    recovered_client_token = daemon.cfg.master_token
    daemon.db.close()
    daemon = Uplink(daemon.cfg)
    assert daemon.refresh_master_config()
    assert daemon.cfg.master_token == recovered_client_token, 'runtime overlay must survive client restart'
    try:
        daemon.local('GET', '/_matrix/client/v3/user/' + urllib.parse.quote(client_local['tuser_id'], safe='')
                     + '/account_data/com.jkali.master_link')
        raise AssertionError('Legacy env recovery must not create a user control record')
    except urllib.error.HTTPError as error:
        assert error.code == 404

    # Destroy ONLY the database belonging to this marked, uniquely named test.
    compose = Path(os.environ['SYNCTEST_MANIFEST']).parent / 'compose.json'
    command = ['docker', 'compose', '-p', SANDBOX['project'], '-f', str(compose)]
    subprocess.run(command + ['stop', 'master'], check=True)
    subprocess.run(command + ['exec', '-T', 'master-db', 'dropdb', '-U', 'matrix', '--force', 'synapse'], check=True)
    subprocess.run(command + ['exec', '-T', 'master-db', 'createdb', '-U', 'matrix', '--template=template0',
                             '--encoding=UTF8', '--lc-collate=C', '--lc-ctype=C', 'synapse'], check=True)
    epoch = enroll._recovery_registry().bump_epoch()
    subprocess.run(command + ['start', 'master'], check=True)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(MASTER_HS + '/health', timeout=2) as response:
                if response.status == 200:
                    break
        except (OSError, ValueError):
            time.sleep(1)
    else:
        raise RuntimeError('Disposable master did not restart')
    subprocess.run(['bash', str(REPO / 'master/provision.sh')], env=os.environ, check=True)
    status, recovered = http_json(base + '/enroll/recovery', method='POST', body=request)
    assert status == 200 and recovered['master_user'] == '@alice:master', (status, recovered)
    assert recovered['master_authority_id'] == manifest['master_authority_id']
    assert recovered['master_data_epoch'] == epoch['master_data_epoch'] != manifest['master_data_epoch']
    status, _ = http_json(MASTER_HS + '/_matrix/client/v3/account/whoami', token=recovered['master_token'])
    assert status == 200
    try:
        daemon.master('GET', '/_matrix/client/v3/account/whoami')
        raise AssertionError('Database replacement must invalidate the previous token')
    except MasterUnreachable:
        pass
    assert daemon.master('GET', '/_matrix/client/v3/account/whoami')['user_id'] == '@alice:master'
    assert daemon.cfg.master_data_epoch == epoch['master_data_epoch']
    assert daemon.db.execute('SELECT outcome FROM proposal_map').fetchone()[0] == 'sent'
    assert daemon.db.execute('SELECT count(*) FROM direct_send_log').fetchone()[0] == 1
    daemon.db.close()
    enroll._recovery_registry().revoke_user('alice')
    status, _ = http_json(base + '/enroll/recovery', method='POST', body=request)
    assert status == 403
print('Recovery: scoped rotation, foreign-install refusal, database loss, fresh epoch and revocation pass; '
      'production Uplink client recovers across token loss and database replacement with Direct ledgers intact.')
