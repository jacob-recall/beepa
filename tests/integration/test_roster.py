"""Repeat real provisioning with a new teammate; prior identities survive."""
import os
from pathlib import Path
import re
import subprocess
from sandbox import load_manifest, REPO

data = load_manifest()
path = Path(data['master_dir']) / 'tokens.local'


def tokens():
    return dict(re.findall(r"^(\w+)='([^']*)'", path.read_text(), re.M))


before = tokens()
for additions in ('charlie', ''):
    env = dict(os.environ, TEAMMATES=additions)
    subprocess.run(['bash', str(REPO / 'master/provision.sh')], env=env, check=True)
    after = tokens()
    assert after['MASTER_TEAMMATES'] == 'alice bob charlie'
    for key, value in before.items():
        if key != 'MASTER_TEAMMATES':
            assert after[key] == value, 'Changed existing account/space/token field: ' + key
print('Roster additive provisioning and repeated setup preserve every existing account, token and space.')
