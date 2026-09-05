#!/usr/bin/env python3
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from phone_numbers import normalize_phone


class PhoneTests(unittest.TestCase):
    def test_trunk_prefixes_and_significant_italian_zero(self):
        for raw, region, expected in [
            ('020 7946 0018', 'GB', '+442079460018'),
            ('030 123456', 'DE', '+4930123456'),
            ('01 42 68 53 00', 'FR', '+33142685300'),
            ('02 9374 4000', 'AU', '+61293744000'),
            ('02 36618 300', 'IT', '+390236618300'),
            ('044 668 18 00', 'CH', '+41446681800'),
            ('(202) 555-0123', 'US', '+12025550123'),
        ]:
            with self.subTest(region=region):
                self.assertEqual(normalize_phone(raw, region), expected)

    def test_international_prefixes_and_foreign_number_in_local_region(self):
        self.assertEqual(normalize_phone('0044 20 7946 0018', 'US'), '+442079460018')
        self.assertEqual(normalize_phone('+44 (0)20 7946 0018', 'US'), '+442079460018')
        self.assertEqual(normalize_phone('011 44 20 7946 0018', 'US'), '+442079460018')

    def test_extensions_and_postdial_are_never_collapsed_to_someone_else(self):
        for raw in ('+1 202 555 0123 ext 9', '+1 202 555 0123 x9', '+12025550123;ext=9',
                    '+12025550123,9', '+12025550123#9'):
            self.assertIsNone(normalize_phone(raw, 'US'), raw)

    def test_ambiguous_national_and_junk_are_refused(self):
        for raw, region in [('02079460018', None), ('12025550123', None), ('02079460018', 'US'),
                            ('++12025550123', 'US'), ('junk 12025550123', 'US'), ('911', 'US')]:
            self.assertIsNone(normalize_phone(raw, region), (raw, region))

    def test_provider_id_is_explicitly_international(self):
        self.assertEqual(normalize_phone('12025550123', provider=True), '+12025550123')
        self.assertIsNone(normalize_phone('12025550123 extra', provider=True))


if __name__ == '__main__':
    unittest.main()
