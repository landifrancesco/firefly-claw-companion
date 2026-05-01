from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date

os.environ.setdefault("MPLCONFIGDIR", tempfile.mkdtemp(prefix="mplconfig-"))

from scripts import telegram_firefly_bot as bot


class TelegramLocalizationAndPeriodsTest(unittest.TestCase):
    def test_help_and_commands_follow_configured_language(self) -> None:
        previous = os.environ.get("FIREFLY_CHAT_LANGUAGE")
        try:
            os.environ["FIREFLY_CHAT_LANGUAGE"] = "it"
            help_it = bot.help_text()
            commands_it = bot.commands_text()
            self.assertIn("linguaggio naturale", help_it)
            self.assertIn("Riferimento comandi", commands_it)
            self.assertIn("/comandi", help_it)
            self.assertIn("/saldi", commands_it)
            self.assertNotIn("/balances", commands_it)

            os.environ["FIREFLY_CHAT_LANGUAGE"] = "en"
            help_en = bot.help_text()
            commands_en = bot.commands_text()
            self.assertIn("normal language", help_en)
            self.assertIn("Command reference", commands_en)
        finally:
            if previous is None:
                os.environ.pop("FIREFLY_CHAT_LANGUAGE", None)
            else:
                os.environ["FIREFLY_CHAT_LANGUAGE"] = previous

    def test_period_from_values_supports_custom_range(self) -> None:
        start, end, label = bot.period_from_values(
            {"from": "2026-04-01", "to": "2026-04-15"},
            default_last_month=True,
        )
        self.assertEqual(start, date(2026, 4, 1))
        self.assertEqual(end, date(2026, 4, 15))
        self.assertEqual(label, "01-04-2026 - 15-04-2026")

    def test_period_from_values_supports_month(self) -> None:
        start, end, label = bot.period_from_values({"month": "2026-03"}, default_current_month=True)
        self.assertEqual(start, date(2026, 3, 1))
        self.assertEqual(end, date(2026, 3, 31))
        self.assertEqual(label, "01-03-2026 - 31-03-2026")

    def test_period_from_values_supports_european_custom_range(self) -> None:
        start, end, label = bot.period_from_values(
            {"from": "01-04-2026", "to": "15-04-2026"},
            default_last_month=True,
        )
        self.assertEqual(start, date(2026, 4, 1))
        self.assertEqual(end, date(2026, 4, 15))
        self.assertEqual(label, "01-04-2026 - 15-04-2026")

    def test_explicit_period_queries_skip_shortcuts(self) -> None:
        self.assertIsNone(bot.interpret_natural_command("show me my summary from 2026-04-01 to 2026-04-15"))
        self.assertIsNone(bot.interpret_natural_command("show me my summary from 01-04-2026 to 15-04-2026"))
        self.assertIsNone(bot.interpret_natural_command("make a spending graph for march 2026"))

    def test_natural_intent_payload_handles_spending_range_questions(self) -> None:
        payload = bot.parse_natural_intent_payload("quanto ho speso da gennaio a marzo?")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["intent"], "get_spending_total")
        self.assertEqual(payload["params"]["from"], "2026-01-01")
        self.assertEqual(payload["params"]["to"], "2026-03-31")

    def test_enforce_deterministic_period_overrides_ai_month(self) -> None:
        payload = {
            "intent": "get_summary",
            "params": {"month": "2026-03", "query": "caffe"},
        }
        corrected = bot.enforce_deterministic_period(
            payload,
            "fammi vedere il riepilogo di febbraio 2026",
        )
        self.assertEqual(corrected["params"]["month"], "2026-02")
        self.assertEqual(corrected["params"]["query"], "caffe")

    def test_natural_intent_payload_handles_named_month_top_categories(self) -> None:
        payload = bot.parse_natural_intent_payload("categories usate di piu nel mese di gennaio 2026")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["intent"], "top_spending_categories")
        self.assertEqual(payload["params"]["month"], "2026-01")

    def test_natural_period_values_handle_per_month_year(self) -> None:
        params = bot.parse_natural_period_values("quali sono le categorie in cui ho speso di piu per febbraio 2026")
        self.assertEqual(params["month"], "2026-02")

    def test_natural_period_values_handle_month_range_with_years(self) -> None:
        params = bot.parse_natural_period_values("quanto entrate vs spese ho avuto da gennaio 2026 ad aprile 2026")
        self.assertEqual(params["from"], "2026-01-01")
        self.assertEqual(params["to"], "2026-04-30")

    def test_natural_period_values_handle_full_year_requests(self) -> None:
        params = bot.parse_natural_period_values("quante entrate ho avuto vs spese nel 2026")
        self.assertEqual(params["from"], "2026-01-01")
        self.assertEqual(params["to"], "2026-12-31")

    def test_natural_intent_payload_handles_category_graph_requests(self) -> None:
        payload = bot.parse_natural_intent_payload("make a graph by category")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["intent"], "top_spending_categories")
        self.assertTrue(payload["params"]["with_graph"])

    def test_natural_intent_payload_marks_all_categories_when_requested(self) -> None:
        payload = bot.parse_natural_intent_payload("fammi un grafico per tutte le categorie di spesa")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["intent"], "top_spending_categories")
        self.assertTrue(payload["params"]["with_graph"])
        self.assertTrue(payload["params"]["all_categories"])

    def test_natural_intent_payload_handles_income_vs_spending_questions(self) -> None:
        payload = bot.parse_natural_intent_payload("quante entrate ho avuto e quante uscite a febbraio 2026?")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["intent"], "get_income_vs_spending")
        self.assertEqual(payload["params"]["month"], "2026-02")

    def test_natural_intent_payload_handles_recent_filtered_transactions(self) -> None:
        payload = bot.parse_natural_intent_payload("mostrami i movimenti caffe dal 2026-03-01 al 2026-03-31")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["intent"], "get_recent")
        self.assertEqual(payload["params"]["query"], "caffe")
        self.assertEqual(payload["params"]["from"], "2026-03-01")
        self.assertEqual(payload["params"]["to"], "2026-03-31")

    def test_natural_intent_payload_handles_period_category_queries_without_top_phrase(self) -> None:
        payload = bot.parse_natural_intent_payload("quali sono le categorie di marzo 2026")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["intent"], "top_spending_categories")
        self.assertEqual(payload["params"]["month"], "2026-03")

    def test_natural_intent_payload_handles_receipt_preamble(self) -> None:
        payload = bot.parse_natural_intent_payload("Ti mando una ricevuta tu aggiungi la spesa a riguardo")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["intent"], "clarify")
        self.assertIn("foto", payload["reply"].casefold())

    def test_extract_receipt_candidate_handles_supermarket_receipt(self) -> None:
        candidate = bot.extract_receipt_candidate(
            "\n".join(
                [
                    "IN'S mercato",
                    "DOCUMENTO COMMERCIALE",
                    "TOTALE COMPLESSIVO EUR 5.65",
                    "16.04.26 18:34",
                ]
            )
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["intent"], "create_expense")
        self.assertEqual(candidate["amount"], "5.65")
        self.assertEqual(candidate["merchant"], "Supermercato In's")
        self.assertEqual(candidate["date"], "2026-04-16")

    def test_extract_receipt_candidates_handle_multiple_notifications(self) -> None:
        candidates = bot.extract_receipt_candidates(
            "\n".join(
                [
                    "Gruppo BPER Banca",
                    "Pagamento POS di 0,45 EUR presso Gruppo Argenta S.P.A.",
                    "Gruppo BPER Banca",
                    "Pagamento POS di 90,00 EUR presso SOCIETA' NUOTATORI PAD.",
                    "Revolut",
                    "Supermercato In's",
                    "5,65 EUR",
                ]
            )
        )
        self.assertEqual(len(candidates), 3)
        self.assertEqual([item["amount"] for item in candidates], ["0.45", "90.00", "5.65"])

    def test_build_receipt_fallback_payload_stays_conservative_without_account_match(self) -> None:
        class FakeService:
            def account_balances(self) -> list[dict[str, str]]:
                return [{"name": "Main Checking"}, {"name": "Cash"}]

        payload = bot.build_receipt_fallback_payload(
            FakeService(),
            extracted_text="Revolut\nSupermercato In's\n5,65 EUR",
        )
        self.assertIsNotNone(payload)
        self.assertEqual(payload["intent"], "create_expense")
        self.assertEqual(payload["params"]["description"], "Spesa supermercato")
        self.assertNotIn("source", payload["params"])

    def test_build_receipt_fallback_payload_handles_multiple_notifications(self) -> None:
        class FakeClient:
            def list_categories(self) -> list[dict[str, dict[str, str]]]:
                return [
                    {"attributes": {"name": "Bar"}},
                    {"attributes": {"name": "Sport"}},
                    {"attributes": {"name": "Spesa"}},
                ]

        class FakeService:
            client = FakeClient()

            def account_balances(self) -> list[dict[str, str]]:
                return [{"name": "Main Checking"}]

        payload = bot.build_receipt_fallback_payload(
            FakeService(),
            extracted_text="\n".join(
                [
                    "Gruppo BPER Banca",
                    "Pagamento POS di 0,45 EUR presso Gruppo Argenta S.P.A.",
                    "Gruppo BPER Banca",
                    "Pagamento POS di 90,00 EUR presso SOCIETA' NUOTATORI PAD.",
                    "Revolut",
                    "Supermercato In's",
                    "5,65 EUR",
                ]
            ),
        )
        self.assertIsNotNone(payload)
        self.assertEqual(payload["intent"], "create_transaction_batch")
        transactions = payload["params"]["transactions"]
        self.assertEqual(len(transactions), 3)
        self.assertEqual(transactions[0]["params"]["category"], "Bar")
        self.assertEqual(transactions[1]["params"]["category"], "Sport")
        self.assertEqual(transactions[2]["params"]["category"], "Spesa")
        self.assertEqual(transactions[2]["params"]["description"], "Spesa supermercato")

    def test_top_categories_format_can_be_localized(self) -> None:
        text = bot.format_top_spending_categories(
            [{"type": "withdrawal", "category_name": "Bar", "amount": "10.00"}],
            label="2026-02",
            source_text="it",
        )
        self.assertIn("Categorie di spesa principali", text)

    def test_explicit_language_hint_localizes_chart_caption(self) -> None:
        photo_path, caption = bot.create_spending_chart(
            [{"type": "withdrawal", "category_name": "Bar", "amount": "10.00", "date": "2026-02-10"}],
            days=29,
            label="01-02-2026 - 28-02-2026",
            source_text="it",
        )
        try:
            self.assertIn("Grafico spese per 01-02-2026 - 28-02-2026", caption)
        finally:
            os.remove(photo_path)

    def test_prepare_telegram_text_strips_markdown_artifacts(self) -> None:
        text = "**Bozza preparata:**\n_You can also say: change amount to X._"
        cleaned = bot.prepare_telegram_text(text, limit=200)
        self.assertNotIn("**", cleaned)
        self.assertNotIn("_You can", cleaned)
        self.assertIn("Bozza preparata:", cleaned)

    def test_select_best_ocr_text_prefers_financial_content(self) -> None:
        weak = "receipt maybe unreadable"
        strong = "TOTALE COMPLESSIVO EUR 4,16\nPAGAMENTO POS 4,16"
        best = bot.select_best_ocr_text(weak, strong)
        self.assertIsNotNone(best)
        self.assertIn("TOTALE COMPLESSIVO", best)
        self.assertIn("PAGAMENTO POS", best)


if __name__ == "__main__":
    unittest.main()
