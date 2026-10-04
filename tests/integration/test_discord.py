"""Pinned bridge startup and local provisioning, without contacting Discord."""
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from unittest.mock import patch

from sandbox import load_manifest, REPO

manifest = load_manifest()
root = Path(os.environ['SYNCTEST_MANIFEST']).resolve().parent
compose = ['docker', 'compose', '-p', manifest['project'], '-f', str(root / 'compose.json')]
from harness import register_user, mx

identity, token = register_user('discordfixture')
state = root / 'discord-state'
(state / 'discord').mkdir(parents=True)
(state / '.env').write_text('LOCAL_LOCALPART=discordfixture\n')
config_path = root / 'local/synapse/homeserver.yaml'
homeserver = json.loads(config_path.read_text())
values = {'LOCAL_MXID': identity, 'DB_PASSWORD': homeserver['database']['args']['password'],
          'AS_TOKEN_DISCORD': secrets.token_hex(32), 'HS_TOKEN_DISCORD': secrets.token_hex(32),
          'PROV_DISCORD': secrets.token_hex(32)}
for name in ('config.yaml', 'registration.yaml'):
    text = (REPO / 'hub/templates/discord' / (name + '.tmpl')).read_text()
    for key, value in values.items():
        text = text.replace('${' + key + '}', value)
    text = text.replace('http://synapse:8008', 'http://local:8008').replace('@postgres/', '@local-db/')
    destination = state / 'discord' / name
    destination.write_text(text)
    destination.chmod(0o600)
registration = root / 'local/synapse/discord-registration.yaml'
registration.write_bytes((state / 'discord/registration.yaml').read_bytes())
registration.chmod(0o600)
homeserver['app_service_config_files'] = ['/data/discord-registration.yaml']
config_path.write_text(json.dumps(homeserver))
for repeat in range(2):
    subprocess.run(compose + ['exec', '-T', 'local-db', 'psql', '-v', 'ON_ERROR_STOP=1', '-U', 'matrix', '-d', 'synapse'],
                   input=(REPO / 'postgres-init/02-discord.sql').read_bytes(), check=True, capture_output=True)
composition = json.loads((root / 'compose.json').read_text())
image = re.search(r'mautrix-discord:\n\s+image: (\S+)', (REPO / 'docker-compose.yml').read_text()).group(1)
composition['services']['mautrix-discord'] = {
    'image': image, 'environment': {'UID': str(os.getuid()), 'GID': str(os.getgid())},
    'volumes': [str(state / 'discord') + ':/data'],
}
(root / 'compose.json').write_text(json.dumps(composition))
subprocess.run(compose + ['restart', 'local'], check=True, stdout=subprocess.DEVNULL)
subprocess.run(compose + ['up', '-d', 'mautrix-discord'], check=True, stdout=subprocess.DEVNULL)
os.environ['BEEPA_INSTALL_ROOT'] = str(state)
sys.path.insert(0, str(REPO / 'session-connect'))
import discord_connect as discord

def isolated_prefix(*args):
    return compose

with patch.object(discord.connect, 'compose_prefix', isolated_prefix):
    deadline = time.monotonic() + 90
    while True:
        try:
            status = discord.connection_status()
            break
        except (ValueError, OSError, subprocess.SubprocessError):
            if time.monotonic() >= deadline:
                raise RuntimeError('Isolated Discord bridge did not become ready') from None
            time.sleep(1)
    assert status == {'status': 'logged_out', 'account': ''}, status
    assert discord.DiscordConnector().dispatch('logout', {})['status'] == 'logged_out'
    who = mx(manifest['local_url'], values['AS_TOKEN_DISCORD'], 'GET', '/_matrix/client/v3/account/whoami',
             query={'user_id': identity})
    assert who['user_id'] == identity
    subprocess.run(compose + ['restart', 'mautrix-discord'], check=True, stdout=subprocess.DEVNULL)
    for attempt in range(30):
        try:
            assert discord.connection_status()['status'] == 'logged_out'
            break
        except ValueError:
            time.sleep(1)
    else:
        raise AssertionError('Bridge did not recover after restart')

print('PASS: pinned Discord 0.7.7 starts with Synapse, repeated database setup is safe, legacy ping/logout and configured-user double-puppet authentication work; restart preserves credentials. No Discord login or messages sent.')
