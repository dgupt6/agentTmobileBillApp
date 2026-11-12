# Run: python -m unittest discover tests
import unittest

import pandas as pd

from tmobile_bill_agent.bill_config import (
    ConfigError, find_unmapped, normalize_phone, parse_config, parse_file, parse_toml,
    parse_yaml, to_toml, to_yaml)
from webreadBillpdf import deriveActualAmt

CONFIG = {"billing": {
    "owner_name": "Test Owner",
    "due_day": 6,
    "members": [
        {"name": "Alice", "emails": ["alice@example.com"], "lines": [
            {"phone": "(555) 000-1111", "label": "Alice"},
            {"phone": "555-000-2222", "label": "Alice Spouse"}]},
        {"name": "Bob", "emails": ["bob@example.com", "ALICE@example.com"], "lines": [
            {"phone": "+1 555 000 3333", "label": "Bob"},
            {"phone": "5550004444", "label": "Bob Watch", "split": "own"}]},
    ]}}

BILL = pd.DataFrame([
    ["Account", "Account", 30.00, 0.0, 0.0, 30.00],
    ["(555) 000-1111", "Voice", 10.00, 5.00, 1.00, 16.00],
    ["(555) 000-2222", "Voice", 10.00, 0.00, 0.00, 10.00],
    ["(555) 000-3333", "Voice", 10.00, 0.00, 2.50, 12.50],
    ["(555) 000-4444", "Wearable", 7.00, 0.00, 0.00, 7.00],
], columns=["Phone Number", "Line Type", "Plans", "Equipment", "Services", "Total"])


class ConfigTests(unittest.TestCase):
    def test_parse_and_normalize(self):
        cfg = parse_config(CONFIG)
        self.assertEqual(set(cfg.lines_by_phone),
                         {"5550001111", "5550002222", "5550003333", "5550004444"})
        self.assertEqual(cfg.lines_by_phone["5550004444"].split, "own")
        self.assertEqual(cfg.recipients, ["alice@example.com", "bob@example.com"])
        self.assertEqual(normalize_phone("(555) 000-1111"), "5550001111")

    def test_toml_round_trip(self):
        cfg = parse_config(CONFIG)
        again = parse_toml(to_toml(cfg))
        self.assertEqual(again, cfg)

    def test_yaml_round_trip(self):
        cfg = parse_config(CONFIG)
        self.assertEqual(parse_yaml(to_yaml(cfg)), cfg)
        self.assertEqual(parse_file("x.yml", to_yaml(cfg).encode()), cfg)
        self.assertEqual(parse_file("x.TOML", to_toml(cfg).encode()), cfg)

    def test_hand_written_yaml(self):
        text = """
billing:
  owner_name: Test Owner
  members:
    - name: Alice
      emails: [alice@example.com]
      lines:
        - {phone: 5550001111, label: Alice}       # unquoted number is fine
        - {phone: 555-000-2222, split: own}
"""
        cfg = parse_yaml(text)
        self.assertEqual(cfg.due_day, 6)
        self.assertEqual(cfg.lines_by_phone["5550001111"].label, "Alice")
        self.assertEqual(cfg.lines_by_phone["5550002222"].split, "own")

    def test_yaml_bool_trap(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_yaml("billing:\n  owner_name: T\n  members:\n    - name: no\n")
        self.assertIn("put it in quotes", str(ctx.exception))

    def test_bad_files(self):
        for name, text in [("x.yaml", "billing: [unclosed"), ("x.toml", "billing = ["),
                           ("x.json", "{}"), ("x.yaml", "- just a list")]:
            with self.assertRaises(ConfigError):
                parse_file(name, text.encode())

    def test_problems_are_reported(self):
        bad = {"owner_name": "", "due_day": 40, "members": [
            {"name": "A", "emails": ["not-an-email"], "lines": [
                {"phone": "123"}, {"phone": "5550001111", "split": "weird"}]},
            {"name": "B", "lines": [{"phone": "(555) 000-1111"}]}]}
        with self.assertRaises(ConfigError) as ctx:
            parse_config(bad)
        text = str(ctx.exception)
        for fragment in ("owner_name", "due_day", "not-an-email", "10 digits",
                         "split 'weird'", "listed under both"):
            self.assertIn(fragment, text)

    def test_find_unmapped(self):
        cfg = parse_config(CONFIG)
        self.assertEqual(find_unmapped(["Account", "(555) 000-1111", "(555) 999-0000"], cfg),
                         ["(555) 999-0000"])


class SplitTests(unittest.TestCase):
    def test_split(self):
        result = deriveActualAmt(BILL, parse_config(CONFIG))
        amounts = dict(zip(result["Person"], result["Individual amount"].round(2)))
        # shared pool = 30 account + 3 * 10 plans = 60 -> 20.00 per shared line
        # Alice: (20 + 5 + 1) + 20 = 46.00 ; Bob: (20 + 2.50) + watch 7.00 = 29.50
        self.assertEqual(amounts, {"Alice": 46.00, "Bob": 29.50})
        self.assertAlmostEqual(sum(amounts.values()), BILL["Total"].sum())

    def test_input_not_modified(self):
        before = BILL.copy()
        deriveActualAmt(BILL, parse_config(CONFIG))
        pd.testing.assert_frame_equal(BILL, before)

    def test_unmapped_line_raises(self):
        bill = pd.concat([BILL, pd.DataFrame(
            [["(555) 999-0000", "Voice", 1.0, 0.0, 0.0, 1.0]], columns=BILL.columns)])
        with self.assertRaises(ValueError):
            deriveActualAmt(bill, parse_config(CONFIG))


if __name__ == "__main__":
    unittest.main()
