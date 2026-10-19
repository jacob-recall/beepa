#!/usr/bin/env python3
"""Exercise the actual harness scanners without creating any homeservers."""
import ast
import glob
import os
from pathlib import Path
import re
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
source = ast.parse((ROOT / 'tests/integration/harness.py').read_text())
FUNCTIONS = {'strip_js_comments', '_balanced_args', '_split_top_args', '_literal_fragments',
             'js_write_calls', 'scan_apps_master_write_surface', 'scan_master_import_graph'}
CONSTANTS = {'MASTER_WRITE_ALLOWLIST', 'MASTER_FILE_WRITE_ALLOWLIST'}
nodes = [node for node in source.body if
         isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS or
         isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in CONSTANTS for t in node.targets)]
assert len(nodes) == len(FUNCTIONS) + len(CONSTANTS)
namespace = {'os': os, 'glob': glob, 're': re, 'REPO': str(ROOT)}
exec(compile(ast.Module(body=nodes, type_ignores=[]), 'actual-harness-scanners', 'exec'), namespace)


class MasterImportSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        namespace['REPO'] = str(self.root)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def graph(self):
        return namespace['scan_master_import_graph']()

    def scan(self):
        return namespace['scan_apps_master_write_surface']()

    def test_actual_master_modules_have_no_native_send_import_graph(self):
        namespace['REPO'] = str(ROOT)
        files, violations = self.graph()
        self.assertIn('apps/master/crm-store.js', files)
        self.assertIn('shared/model/crm.js', files)
        self.assertEqual(violations, [])
        self.assertEqual(self.scan()[3], [])

    def test_transitive_multiline_import_and_reexport_detects_user_send_module(self):
        self.write('apps/master/crm.js', "import {\n view\n} from './presentation.js';")
        self.write('apps/master/presentation.js', "export { view } from '../../shared/model/leaf.js';")
        self.write('shared/model/leaf.js', "import '../ui/chat.js';")
        self.write('shared/ui/chat.js', 'export const view = null;')
        files, violations = self.graph()
        self.assertIn('shared/ui/chat.js', files)
        self.assertIn('native-send dependency: shared/ui/chat.js', violations)

    def test_literal_dynamic_sources_import_is_rejected(self):
        self.write('apps/master/crm.js', "const next = () => import('../../shared/ui/sources.js');")
        self.write('shared/ui/sources.js', 'export const x = 1;')
        self.assertTrue(any('native-send dependency' in item for item in self.graph()[1]))

    def test_unknown_dynamic_external_missing_and_outside_imports_fail_closed(self):
        for code in ["import(path);", "import x from 'package';", "import './missing.js';",
                     "import '../../missing.js';", "import '../../../outside.js';"]:
            with self.subTest(code=code):
                self.write('apps/master/crm.js', code)
                self.assertTrue(self.graph()[1])

    def test_cycles_and_comment_only_send_paths_are_allowed(self):
        self.write('apps/master/crm.js', "import './other.js'; // /send/m.room.message/\n")
        self.write('apps/master/other.js', "import './crm.js'; /* import '../../shared/ui/chat.js'; */")
        self.assertEqual(len(self.graph()[0]), 2)
        self.assertEqual(self.graph()[1], [])

    def test_native_send_hidden_in_other_shared_leaf_is_detected(self):
        self.write('apps/master/crm.js', "import '../../shared/model/sneaky.js';")
        self.write('shared/model/sneaky.js', "api('PUT', '/rooms/' + room + '/send/m.room.message/' + txn, body);")
        self.assertIn('native-send code: shared/model/sneaky.js', self.graph()[1])
        self.assertTrue(self.scan()[3])

    def test_crm_writes_are_only_allowed_in_named_adapters(self):
        put = "api('PUT', '/_matrix/client/v3/rooms/' + enc(room) + '/send/com.jkali.crm.operation/' + enc('crm_' + op), body);"
        post = "fetch(ENROLL_BASE + '/admin/crm-room', {method: 'POST', body: '{}'});"
        self.write('apps/master/crm-store.js', put)
        self.write('apps/master/main.js', post)
        self.assertEqual(self.scan()[3], [])
        self.write('apps/master/other.js', put + '\n' + post)
        violations = self.scan()[3]
        self.assertEqual(len(violations), 2)
        self.assertTrue(all(item.startswith('other.js:') for item in violations))

    def test_destination_type_transaction_prefix_and_method_are_narrow(self):
        cases = [
            "api('PUT', '/_matrix/client/v3/rooms/' + room + '/send/com.jkali.crm.operation/' + txn, body);",
            "api('PUT', '/_matrix/client/v3/rooms/' + room + '/send/com.jkali.crm.admin/' + txn, body);",
            "api(method, '/_matrix/client/v3/rooms/' + room + '/send/com.jkali.crm.operation/' + txn, body);",
            "api('PUT', '/_matrix/client/v3/user/' + id + '/account_data/' + key, body);",
        ]
        for code in cases:
            with self.subTest(code=code):
                self.write('apps/master/crm-store.js', code)
                self.assertTrue(self.scan()[3])

    def test_direct_requests_only_allowed_in_members_adapter_with_fixed_prefix(self):
        put = "api('PUT', '/_matrix/client/v3/rooms/' + enc(room) + '/send/com.jkali.crm.direct_request/' + enc('crm_direct_' + op + '_' + phase), body);"
        self.write('apps/master/crm-members.js', put)
        self.assertEqual(self.scan()[3], [])
        self.write('apps/master/other.js', put)
        self.assertEqual(len(self.scan()[3]), 1)
        self.write('apps/master/other.js', '')
        self.write('apps/master/crm-members.js', put.replace('crm_direct_', 'arbitrary_'))
        self.assertTrue(self.scan()[3])

    def test_batch_submit_cancel_and_staging_require_the_named_adapter_and_prefix(self):
        for event, transaction in [
            ('com.jkali.proposal', "'crm_proposal_' + child + '_' + item"),
            ('com.jkali.crm.batch_request', "'crm_batch_' + child + '_submit'"),
            ('com.jkali.crm.batch_request', "'crm_batch_' + child + '_cancel'"),
        ]:
            with self.subTest(event=event, transaction=transaction):
                put = "api('PUT', '/_matrix/client/v3/rooms/' + enc(room) + '/send/" + event + "/' + enc(" + transaction + "), body);"
                self.write('apps/master/other.js', '')
                self.write('apps/master/crm-batches.js', put)
                self.assertEqual(self.scan()[3], [])
                self.write('apps/master/other.js', put)
                self.assertEqual(len(self.scan()[3]), 1)
        self.write('apps/master/other.js', '')
        self.write('apps/master/crm-batches.js', put.replace('_cancel', '_anything'))
        self.assertTrue(self.scan()[3])


if __name__ == '__main__':
    unittest.main()
