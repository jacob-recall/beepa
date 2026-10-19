#!/usr/bin/env python3
"""Install the private Telegram control worker; never print credentials."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'master'))
import enroll


def main():
    state = Path(enroll.DATA_DIR) / 'control.local'
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    env = {}
    for line in (Path(enroll.DATA_DIR) / '.env').read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    previous_path = state / 'config.json'
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else {}
    zone_path = str(Path('/etc/localtime').resolve())
    local_zone = zone_path.split('/zoneinfo/', 1)[1] if '/zoneinfo/' in zone_path else 'UTC'
    timezone = env.get('BEEPA_TIMEZONE') or previous.get('timezone') or local_zone
    from zoneinfo import ZoneInfo
    ZoneInfo(timezone)  # Refuse invalid deployment timezone instead of guessing.
    tokens = enroll._tokens()
    config = {'timezone': timezone, 'account_names': previous.get('account_names', {}),
              'telegram_token': env['TELEGRAM_BOT_TOKEN'], 'openrouter_key': env['OPENROUTER_API_KEY'],
              'model': env.get('BEEPA_LLM_MODEL', 'deepseek/deepseek-v4.1-flash'),
              'manager_token': tokens['MASTER_MANAGER_TOKEN'], 'manager': tokens['MASTER_MANAGER_USER'],
              'roster': {tokens['MASTER_' + n.upper() + '_USER']: tokens['MASTER_SPACE_' + n.upper()]
                         for n in tokens.get('MASTER_TEAMMATES', '').split()},
              'matrix': 'http://host.docker.internal:8018', 'enroll': 'http://host.docker.internal:8019'}
    path = state / 'config.json'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        os.fchmod(f.fileno(), 0o600)
        json.dump(config, f)
    docker = shutil.which('docker') or '/Applications/Docker.app/Contents/Resources/bin/docker'
    # Pin the already-installed Node runtime content, never a moving tag at run time.
    image = subprocess.check_output([docker, 'image', 'inspect', 'node:22-alpine', '--format', '{{.Id}}'], text=True).strip()
    subprocess.run([docker, 'stop', 'beepa-control'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run([docker, 'rm', 'beepa-control'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    args = [docker, 'run', '-d', '--name', 'beepa-control', '--restart', 'unless-stopped', '--read-only',
            '--user', str(os.getuid()) + ':' + str(os.getgid()), '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '-p', '127.0.0.1:8023:8023', '--mount', 'type=bind,src=' + str(state.resolve()) + ',dst=/state']
    files = ['master/control-core.mjs', 'master/control-domain.mjs', 'master/telegram-service.mjs',
             'master/notification-routing.mjs', 'master/notification-routing-adapters.mjs',
             'apps/master/crm-store.js', 'apps/master/crm-members.js', 'apps/master/crm-batches.js',
             'apps/master/crm-account.js', 'shared/model/crm.js', 'shared/model/message_timestamps.js', 'shared/ui/el.js']
    for name in files:
        args += ['--mount', 'type=bind,src=' + str(ROOT / name) + ',dst=/app/' + name + ',readonly']
    args += [image, 'node', '--experimental-default-type=module', '--disable-warning=ExperimentalWarning', '/app/master/telegram-service.mjs']
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL)
    for attempt in range(20):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8023/health', timeout=2) as response:
                if response.status == 200:
                    break
        except OSError:
            if attempt == 19:
                raise RuntimeError('Control health check failed')
            time.sleep(1)
    marker = state / 'enabled'
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    print('Control worker started on loopback port 8023. Check its authenticated status before pairing.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Control setup failed. Check local credentials and Docker availability.', file=sys.stderr)
        sys.exit(1)
