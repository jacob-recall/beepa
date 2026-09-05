#!/usr/bin/env python3
"""Unit tests for the pure normalization/parse logic in
agents/enrich/number_resolver.py — no DB, no docker, no network.

Run: python3 tests/unit/number_resolver.test.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "agents", "enrich"))

import number_resolver as nr


class NormPhoneTests(unittest.TestCase):
    def test_unattested_bare_digits_are_ambiguous(self):
        self.assertIsNone(nr._norm_phone("16307461684"))

    def test_already_e164_unchanged(self):
        self.assertEqual(nr._norm_phone("+16307461684"), "+16307461684")

    def test_bare_national_no_country_code_is_none(self):
        # 10-digit bare national number, no country code present anywhere
        # in the source data — must never be guessed.
        self.assertIsNone(nr._norm_phone("6307461684"))

    def test_junk_is_none(self):
        self.assertIsNone(nr._norm_phone("not-a-number"))
        self.assertIsNone(nr._norm_phone(""))
        self.assertIsNone(nr._norm_phone(None))

    def test_00_international_prefix(self):
        self.assertEqual(nr._norm_phone("0016307461684"), "+16307461684")

    def test_leading_zero_after_plus_invalid(self):
        self.assertIsNone(nr._norm_phone("+0123456789"))


class ParseImessageHandleTests(unittest.TestCase):
    def test_phone_handle(self):
        self.assertEqual(
            nr._parse_imessage_handle("any;-;+16307461684"),
            ("+16307461684", "phone"),
        )

    def test_email_handle(self):
        self.assertEqual(
            nr._parse_imessage_handle("any;-;Casey@Example.com"),
            ("casey@example.com", "email"),
        )

    def test_group_chat_shape_is_none(self):
        self.assertIsNone(
            nr._parse_imessage_handle("any;-;+16307461684;-;+12133694910")
        )

    def test_non_any_prefix_is_none(self):
        self.assertIsNone(nr._parse_imessage_handle("group;-;+16307461684"))

    def test_garbage_is_none(self):
        self.assertIsNone(nr._parse_imessage_handle("no-separator-here"))
        self.assertIsNone(nr._parse_imessage_handle(""))
        self.assertIsNone(nr._parse_imessage_handle(None))

    def test_invalid_phone_handle_is_none(self):
        self.assertIsNone(nr._parse_imessage_handle("any;-;+0123"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
