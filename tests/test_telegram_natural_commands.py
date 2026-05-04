from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("MPLCONFIGDIR", tempfile.mkdtemp(prefix="mplconfig-"))

from scripts.telegram_firefly_bot import parse_natural_intent_payload, interpret_natural_command
from firefly_companion.conversation import clean_free_text_slot


class TelegramNaturalCommandTest(unittest.TestCase):
    def test_category_queries_do_not_fall_back_to_account_graphs(self) -> None:
        self.assertEqual(interpret_natural_command("make a graph by category"), "/topcategories graph")
        self.assertEqual(interpret_natural_command("make a graph of categories most used"), "/topcategories graph")
        self.assertEqual(
            interpret_natural_command("fammi un grafico delle categorie in cui ho speso di piu nell'ultimo mese"),
            "/topcategories graph",
        )
        self.assertEqual(interpret_natural_command("which are the categories i spent the most the last month"), "/topcategories")
        self.assertEqual(interpret_natural_command("which are the most sued categories"), "/topcategories")

    def test_category_listing_queries_are_recognized(self) -> None:
        self.assertEqual(interpret_natural_command("show me the categories"), "/categories")
        self.assertEqual(interpret_natural_command("show me all teh categories used"), "/categories")

    def test_balance_graph_queries_are_recognized_in_english_and_italian(self) -> None:
        self.assertEqual(interpret_natural_command("Make a graph"), "/graph balances 30")
        self.assertEqual(interpret_natural_command("make a graph on the money i have"), "/graph balances 30")
        self.assertEqual(interpret_natural_command("fammi un grafico del mio saldo"), "/graph balances 30")

    def test_minor_typos_are_normalized(self) -> None:
        self.assertEqual(interpret_natural_command("mistrami il grafico del mio saldo"), "/graph balances 30")

    def test_free_text_cleanup_strips_cash_suffixes(self) -> None:
        self.assertEqual(clean_free_text_slot("bar i ncash"), "bar")
        self.assertEqual(clean_free_text_slot("a coffee made with cash"), "a coffee")

    def test_new_natural_intents_are_detected(self) -> None:
        cases = [
            ("trova caffe", "search_transactions"),
            ("cerca supermercato", "search_transactions"),
            ("increase my groceries budget by 50 this month", "set_budget_limit"),
            ("raise food budget to 400 for april", "set_budget_limit"),
            ("abbassa il budget viaggi di 100", "set_budget_limit"),
            ("add a monthly recurring expense of €50 for gym", "create_recurrence"),
            ("crea una ricorrenza mensile di €80 per affitto", "create_recurrence"),
            ("transfer €100", "create_transfer"),
            ("transfer €100 to savings", "create_transfer"),
            ("transfer €100 from checking to savings", "create_transfer"),
            ("trasferisci 50 euro da cassa a carta", "create_transfer"),
        ]
        for text, expected_intent in cases:
            with self.subTest(text=text):
                payload = parse_natural_intent_payload(text)
                self.assertIsNotNone(payload)
                assert payload is not None
                self.assertEqual(payload["intent"], expected_intent)


if __name__ == "__main__":
    unittest.main()
