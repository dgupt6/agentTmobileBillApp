# Run: python -m unittest discover tests
import unittest

import pandas as pd

from tmobile_bill_agent.bill_chat import (
    MAX_HISTORY_MESSAGES, build_bill_context, build_messages, select_statement_text)
from tmobile_bill_agent.bill_config import parse_config

CONFIG = parse_config({"owner_name": "Test Owner", "members": [
    {"name": "Alice", "lines": [{"phone": "(555) 000-1111", "label": "Alice Phone"}]},
    {"name": "Bob", "lines": [{"phone": "(555) 000-4444", "label": "Bob Watch", "split": "own"}]}]})

BILL = pd.DataFrame([
    ["Account", "Account", 30.0, 0.0, 0.0, 0.0, 30.0],
    ["(555) 000-1111", "Voice", 10.0, 5.0, 1.0, 0.0, 16.0],
    ["(555) 000-4444", "Wearable", 7.0, 0.0, 0.0, 0.0, 7.0],
    ["(555) 000-9999", "Voice", 1.0, 0.0, 0.0, 0.0, 1.0],
], columns=["Phone Number", "Line Type", "Plans", "Equipment", "Services", "One-time", "Total"])


class ContextTests(unittest.TestCase):
    def test_context_uses_labels_not_full_numbers(self):
        text = build_bill_context(BILL, 54.0, config=CONFIG)
        self.assertIn("Alice Phone (x1111)", text)
        self.assertIn("Bob Watch (x4444)", text)
        self.assertIn("Unassigned line x9999", text)
        self.assertIn("Statement total: $54.00", text)
        for full in ("555", "000-1111", "5550001111"):
            self.assertNotIn(full, text)

    def test_per_person_and_history(self):
        per_person = pd.DataFrame({"Person": ["Alice", "Bob"], "Individual amount": [46.0, 8.0]})
        history = pd.DataFrame({"Month": ["2026-09", "2026-10"], "Total": [250.0, 260.5]})
        text = build_bill_context(per_person=per_person, history=history)
        self.assertIn("PER PERSON AMOUNTS", text)
        self.assertIn("2026-10", text)
        self.assertIn("shared", text)

    def test_empty(self):
        self.assertIn("No bill", build_bill_context(history=pd.DataFrame()))


class MessageTests(unittest.TestCase):
    def test_history_is_trimmed_and_context_in_system(self):
        history = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i)}
                   for i in range(40)]
        msgs = build_messages("CTX", history, "Q?")
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("CTX", msgs[0]["content"])
        self.assertEqual(msgs[-1], {"role": "user", "content": "Q?"})
        self.assertEqual(len(msgs), MAX_HISTORY_MESSAGES + 2)


PAGES = ["Account number: 123456789  Statement date: Nov 1  " + "x" * 300,
         "THIS BILL SUMMARY ... Totals $60.00 " + "y" * 300,
         "DETAILED CHARGES Regulatory Programs fee $3.18 Federal Universal Service " + "z" * 300,
         "Equipment installment plan iPhone balance remaining " + "w" * 300]


class StatementTextTests(unittest.TestCase):
    def test_everything_when_it_fits(self):
        text = select_statement_text(PAGES, "anything", budget_chars=10_000)
        self.assertIn("pages 1, 2, 3, 4 of 4", text)
        self.assertNotIn("left out", text)

    def test_page_one_plus_best_match_when_over_budget(self):
        text = select_statement_text(PAGES, "What is the regulatory programs fee?",
                                     budget_chars=800)
        self.assertIn("Account number: 123456789", text)       # page 1 always
        self.assertIn("Regulatory Programs fee $3.18", text)   # best match
        self.assertNotIn("iPhone", text)
        self.assertIn("left out", text)

    def test_budget_is_respected(self):
        text = select_statement_text(PAGES, "fee iphone summary", budget_chars=500)
        body = "".join(line for line in text.splitlines()
                       if not line.startswith(("---", "STATEMENT TEXT")))
        self.assertLessEqual(len(body), 500)
        self.assertIn("cut short", text)

    def test_no_pages(self):
        self.assertEqual(select_statement_text([], "q"), "")
        self.assertEqual(select_statement_text(None, "q"), "")

    def test_statement_goes_into_system_message(self):
        msgs = build_messages("CTX", [], "Q", "STATEMENT TEXT here")
        self.assertIn("STATEMENT TEXT here", msgs[0]["content"])


if __name__ == "__main__":
    unittest.main()
