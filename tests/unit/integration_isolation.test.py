import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('sandbox', ROOT / 'tests/integration/sandbox.py')
sandbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sandbox)


class IsolationTests(unittest.TestCase):
    def test_no_default_production_credentials(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(RuntimeError):
            sandbox.load_manifest()

    def test_manifest_checks_container_paths_and_ports(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            project = 'beepa-test-0123456789ab'
            data = dict(project=project, master_container=project+'-master-1',
                        master_dir=str(root/'master'), state_dir=str(root/'state'),
                        local_url='http://127.0.0.1:50001', master_url='http://127.0.0.1:50002')
            (root/'.beepa-test-root').write_text(json.dumps({'project': project}))
            path = root/'manifest.json'
            path.write_text(json.dumps(data))
            with patch.dict(os.environ, {'SYNCTEST_MANIFEST': str(path)}):
                self.assertEqual(sandbox.load_manifest(), data)
                for key, value in [('master_container','matrix-master-synapse-1'),
                                   ('master_url','http://127.0.0.1:8018'),
                                   ('master_dir', str(ROOT/'master')),
                                   ('local_url','https://remote.example')]:
                    path.write_text(json.dumps(dict(data, **{key:value})))
                    with self.subTest(key=key), self.assertRaises(RuntimeError):
                        sandbox.load_manifest()


if __name__ == '__main__':
    unittest.main()
