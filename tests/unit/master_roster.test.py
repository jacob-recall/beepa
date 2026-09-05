import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('roster', Path(__file__).resolve().parents[2] / 'master/roster.py')
roster = importlib.util.module_from_spec(spec)
spec.loader.exec_module(roster)


class RosterTests(unittest.TestCase):
    def test_additive_and_idempotent(self):
        value = roster.resolve_roster('alice bob', 'alice bob', 'charlie')
        self.assertEqual(value, ['alice', 'bob', 'charlie'])
        self.assertEqual(roster.resolve_roster(' '.join(value), 'alice bob', 'charlie'), value)

    def test_no_author_or_implicit_teammate(self):
        self.assertEqual(roster.resolve_roster('', '', ''), [])

    def test_preserve_tokens_roster_on_legacy_state_gap(self):
        self.assertEqual(roster.resolve_roster('', 'alice bob', 'charlie'), ['alice', 'bob', 'charlie'])

    def test_reject_before_provisioning(self):
        for name in ['manager', 'Alice', "x';touch", 'a/b']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                roster.resolve_roster('alice', '', name)


if __name__ == '__main__':
    unittest.main()
