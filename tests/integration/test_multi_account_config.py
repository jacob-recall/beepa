#!/usr/bin/env python3
"""Validate two effective Compose projects without starting a Docker engine."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from multi_account import prepare
from beepa_update import Updater


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compose', help='standalone Compose executable; default: docker compose')
    args = parser.parse_args()
    configs = []
    with tempfile.TemporaryDirectory(prefix='beepa-two-configs-') as temp:
        for slot in (1, 2):
            root = Path(temp) / ('user-%d' % slot)
            root.mkdir()
            with patch.dict(os.environ, {'BEEPA_STATE_ROOT': str(root / 'state'), 'BEEPA_LOG_ROOT': str(root / 'logs')}):
                manifest = prepare(root, slot)
            cmd = dict(Updater(root).compositions(manifest, ROOT))['teammate']
            if args.compose:
                cmd = [args.compose] + cmd[2:]
            result = subprocess.run(cmd + ['config', '--format', 'json'], check=True, capture_output=True, text=True)
            config = json.loads(result.stdout)
            assert set(config['services']) == {'postgres', 'synapse', 'views'}
            for service, port in [('synapse', 8008 + slot * 100), ('views', 8011 + slot * 100)]:
                ports = config['services'][service]['ports']
                assert len(ports) == 1, ports
                assert str(ports[0]['published']) == str(port), ports
                assert ports[0]['host_ip'] == '127.0.0.1', ports
            for service in config['services'].values():
                for mount in service.get('volumes', []):
                    if mount['type'] == 'bind' and mount['target'] in ('/data', '/usr/share/nginx/runtime/apps'):
                        assert mount['source'].startswith(str(root.resolve())), mount['target']
            configs.append(config)
        assert configs[0]['name'] != configs[1]['name']
        assert configs[0]['volumes']['postgres-data']['name'] != configs[1]['volumes']['postgres-data']['name']
    print('PASS: two effective Compose projects have isolated ports, runtime mounts and database volumes')


if __name__ == '__main__':
    main()
