"""Golden tests for all reported user failures.

Each test case corresponds to a real-world failure from production usage.
These must never regress.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date

os.environ.setdefault("MPLCONFIGDIR", tempfile.mkdtemp(prefix="mplconfig-"))

from scripts import telegram_firefly_bot as bot


class GoldenIntentFailuresTest(unittest.TestCase):
    """F1-F4: Intent misclassification and parsing failures."""

    def test_f1_income_vs_spending_not_balances(self) -> None:
        """'quanti soldi ho guadagnato e quanti ne ho spesi?' must be
        get_income_vs_spending, not get_balances."""
        payload = bot.parse_natural_intent_payload(
            "quanti soldi ho guadagnato e quanti ne ho spesi?"
        )
        self.assertIsNotNone(payload, "Payload should not be None")
        self.assertEqual(payload["intent"], "get_income_vs_spending")

    def test_f2_income_vs_spending_february(self) -> None:
        """'quante entrate ho avuto e quante uscite a febbraio 2026?' must
        understand the intent AND the month."""
        payload = bot.parse_natural_intent_payload(
            "quante entrate ho avuto e quante uscite a febbraio 2026?"
        )
        self.assertIsNotNone(payload, "Payload should not be None")
        self.assertEqual(payload["intent"], "get_income_vs_spending")
        self.assertEqual(payload["params"].get("month"), "2026-02")

    def test_f3_categories_of_march(self) -> None:
        """'quali sono le categorie di marzo 2026' must map to
        top_spending_categories with month=2026-03."""
        payload = bot.parse_natural_intent_payload(
            "quali sono le categorie di marzo 2026"
        )
        self.assertIsNotNone(payload, "Payload should not be None")
        self.assertEqual(payload["intent"], "top_spending_categories")
        self.assertEqual(payload["params"].get("month"), "2026-03")

    def test_f4_recent_coffee_with_date_range(self) -> None:
        """'mostrami i movimenti caffe dal 2026-03-01 al 2026-03-31' must
        be get_recent with query=caffe and the correct date range."""
        payload = bot.parse_natural_intent_payload(
            "mostrami i movimenti caffe dal 2026-03-01 al 2026-03-31"
        )
        self.assertIsNotNone(payload, "Payload should not be None")
        self.assertEqual(payload["intent"], "get_recent")
        self.assertEqual(payload["params"].get("query"), "caffe")
        self.assertEqual(payload["params"].get("from"), "2026-03-01")
        self.assertEqual(payload["params"].get("to"), "2026-03-31")


class GoldenDateFailuresTest(unittest.TestCase):
    """F5-F7: Date/period handling that returned wrong ranges."""

    def test_f5_february_2026_must_be_february(self) -> None:
        """When user asks for February 2026, period must be 2026-02."""
        params = bot.parse_natural_period_values(
            "quante entrate ho avuto a febbraio 2026"
        )
        self.assertEqual(params.get("month"), "2026-02")

    def test_f6_year_2026_must_be_full_year(self) -> None:
        """When user asks for 2026 totals, period must span the full year."""
        params = bot.parse_natural_period_values(
            "quante entrate ho avuto nel 2026"
        )
        self.assertEqual(params.get("from"), "2026-01-01")
        self.assertEqual(params.get("to"), "2026-12-31")

    def test_f7_january_to_april_must_span_both(self) -> None:
        """When user asks from January to April, range must cover both months."""
        params = bot.parse_natural_period_values(
            "da gennaio ad aprile 2026"
        )
        self.assertEqual(params.get("from"), "2026-01-01")
        self.assertEqual(params.get("to"), "2026-04-30")

    def test_explicit_iso_range_preserved(self) -> None:
        """Explicit ISO date range must be preserved exactly."""
        params = bot.parse_natural_period_values(
            "grafico dal 2026-04-01 al 2026-04-15"
        )
        self.assertEqual(params.get("from"), "2026-04-01")
        self.assertEqual(params.get("to"), "2026-04-15")

    def test_january_vs_march_at_least_parses_first_month(self) -> None:
        """'gennaio vs marzo' should at least parse one period.
        Full compare_periods support is Phase 6."""
        params = bot.parse_natural_period_values("gennaio vs marzo")
        # Should at minimum find 'gennaio'
        self.assertTrue(
            params.get("month") is not None or params.get("from") is not None,
            f"Should parse at least one period, got: {params}",
        )


class GoldenReceiptFailuresTest(unittest.TestCase):
    """F8-F9: Receipt and screenshot extraction failures."""

    def test_f8_supermarket_amount_not_zero(self) -> None:
        """Receipt showing €4.16 must extract 4.16, never 0.00."""
        candidate = bot.extract_receipt_candidate(
            "\n".join([
                "Supermercato",
                "TOTALE COMPLESSIVO EUR 4.16",
                "PAGAMENTO ELETTRONICO 4.16",
                "16/04/2026",
            ])
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["amount"], "4.16")

    def test_f9_three_transactions_not_merged(self) -> None:
        """Screenshot with 3 visible POS payments must produce 3 candidates."""
        candidates = bot.extract_receipt_candidates(
            "\n".join([
                "Gruppo BPER Banca",
                "Pagamento POS di 0,45 EUR presso Bar Centrale",
                "Gruppo BPER Banca",
                "Pagamento POS di 12,00 EUR presso Ristorante Milano",
                "Gruppo BPER Banca",
                "Pagamento POS di 5,65 EUR presso Supermercato IN'S",
            ])
        )
        self.assertEqual(len(candidates), 3)
        amounts = [c["amount"] for c in candidates]
        self.assertEqual(amounts, ["0.45", "12.00", "5.65"])

    def test_receipt_with_eur_symbol_extracts_amount(self) -> None:
        """EUR amounts with comma decimal must be extracted correctly."""
        candidate = bot.extract_receipt_candidate(
            "Revolut\nCaffè 1,20 EUR"
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["amount"], "1.20")


class GoldenAdditionalIntentsTest(unittest.TestCase):
    """Additional intent parsing golden tests."""

    def test_how_much_money_is_balances(self) -> None:
        """'quanti soldi ho' (without income/spending words) is get_balances
        via interpret_natural_command shortcut."""
        cmd = bot.interpret_natural_command("quanti soldi ho")
        self.assertEqual(cmd, "/balances")

    def test_spending_total_this_month(self) -> None:
        """'quanto ho speso questo mese' is get_spending_total."""
        payload = bot.parse_natural_intent_payload("quanto ho speso questo mese")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["intent"], "get_spending_total")
        self.assertEqual(payload["params"].get("month"), date.today().strftime("%Y-%m"))

    def test_budget_advice_question_does_not_crash(self) -> None:
        """'Cosa posso fare per migliorare i miei budget date le mie spese?'
        should not crash. It may return None (handed to AI router)."""
        # Just ensure it doesn't raise
        payload = bot.parse_natural_intent_payload(
            "Cosa posso fare per migliorare i miei budget date le mie spese?"
        )
        # This is a complex advisory query — it's OK for deterministic parser
        # to return None (will go to AI router).

    def test_italian_month_typos_are_normalized(self) -> None:
        """Common Italian month typos should be corrected."""
        params = bot.parse_natural_period_values("spese di febbario 2026")
        self.assertEqual(params.get("month"), "2026-02")


class GoldenComparisonQueriesTest(unittest.TestCase):
    def test_compare_periods_intent_detected(self) -> None:
        payload = bot.parse_natural_intent_payload(
            "confronta gennaio 2026 vs marzo 2026 per entrate e uscite"
        )
        self.assertIsNotNone(payload)
        self.assertEqual(payload["intent"], "compare_periods")
        self.assertEqual(payload["params"]["metric"], "income_vs_spending")
        self.assertEqual(payload["params"]["left_period"]["month"], "2026-01")
        self.assertEqual(payload["params"]["right_period"]["month"], "2026-03")

    def test_compare_periods_execution(self) -> None:
        class FakeClient:
            def summary_basic(self, start, end):
                month = str(start)[5:7]
                if month == "01":
                    return {"income": "1000.00", "spent": "400.00"}
                return {"income": "1200.00", "spent": "500.00"}

            def list_transactions(self, start, end, limit=100):
                return []

        class FakeService:
            client = FakeClient()

        payload = {
            "intent": "compare_periods",
            "source_text": "confronta gennaio 2026 vs marzo 2026",
            "params": {
                "metric": "income_vs_spending",
                "left_period": {"month": "2026-01"},
                "right_period": {"month": "2026-03"},
            },
        }
        response = bot.execute_intent(FakeService(), payload, state={})
        self.assertIn("Comparison", response.text)
        self.assertIn("01-01-2026 - 31-01-2026", response.text)
        self.assertIn("01-03-2026 - 31-03-2026", response.text)


if __name__ == "__main__":
    unittest.main()
