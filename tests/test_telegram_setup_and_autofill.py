from __future__ import annotations

import os
import tempfile
import time
import unittest
from typing import Any

os.environ.setdefault("MPLCONFIGDIR", tempfile.mkdtemp(prefix="mplconfig-"))

from scripts import telegram_firefly_bot as bot


class _FakeClient:
    def list_accounts(self, kind: str = "all") -> list[dict[str, Any]]:
        return [
            {"attributes": {"name": "Main Checking", "type": "asset"}},
            {"attributes": {"name": "Cash", "type": "asset"}},
            {"attributes": {"name": "Misc Expenses", "type": "expense"}},
            {"attributes": {"name": "Employer", "type": "revenue"}},
        ]


class _FakeService:
    client = _FakeClient()


class TelegramSetupAndAutofillTest(unittest.TestCase):
    def test_match_choice_supports_number_and_name(self) -> None:
        choices = ["Main Checking", "Cash"]
        self.assertEqual(bot.match_choice("2", choices), "Cash")
        self.assertEqual(bot.match_choice("main checking", choices), "Main Checking")

    def test_finance_setup_flow_completes(self) -> None:
        service = _FakeService()
        state: dict[str, Any] = {}
        first = bot.start_finance_setup(service, state, source_text="it")
        self.assertIn("Training", first.text)

        bot.handle_finance_setup_message(service, state, "1")
        bot.handle_finance_setup_message(service, state, "3")
        bot.handle_finance_setup_message(service, state, "4")
        bot.handle_finance_setup_message(service, state, "1")
        bot.handle_finance_setup_message(service, state, "1")
        bot.handle_finance_setup_message(service, state, "2")
        bot.handle_finance_setup_message(service, state, "yes")
        done = bot.handle_finance_setup_message(service, state, "yes")

        self.assertIsNotNone(done)
        self.assertTrue(bot.finance_profile_ready(state))
        profile = bot.get_finance_profile(state)
        self.assertEqual(profile["expense_source_account"], "Main Checking")
        self.assertEqual(profile["expense_destination_account"], "Misc Expenses")
        self.assertEqual(profile["income_source_account"], "Employer")
        self.assertEqual(profile["income_destination_account"], "Main Checking")
        self.assertEqual(profile["payment_method_accounts"]["card"], "Main Checking")
        self.assertEqual(profile["payment_method_accounts"]["cash"], "Cash")

    def test_apply_profile_and_history_autofill_uses_profile_defaults(self) -> None:
        service = _FakeService()
        state = {
            "finance_profile": {
                "setup_complete": True,
                "expense_source_account": "Main Checking",
                "expense_destination_account": "Misc Expenses",
                "income_destination_account": "Main Checking",
                "ask_budget_when_missing": True,
                "auto_budget_from_history": True,
            }
        }
        params = {"amount": "10.00", "description": "Coffee"}
        updated = bot.apply_profile_and_history_autofill(
            service,
            state,
            transaction_kind="withdrawal",
            params=params,
        )
        self.assertEqual(updated["source"], "Main Checking")
        self.assertEqual(updated["destination"], "Misc Expenses")

    def test_payment_alias_and_description_cleanup(self) -> None:
        service = _FakeService()
        state = {
            "finance_profile": {
                "setup_complete": True,
                "payment_method_accounts": {"card": "Main Checking"},
            }
        }
        self.assertEqual(
            bot.infer_account_from_payment_method(service, "Caffe pagato con carta oggi", locale="it", state=state),
            "Main Checking",
        )
        self.assertEqual(bot.clean_transaction_description("Caffe pagato con carta oggi"), "Caffe")

    def test_apply_profile_and_history_autofill_uses_history_for_category(self) -> None:
        service = _FakeService()
        state = {
            "finance_profile": {
                "setup_complete": True,
                "expense_source_account": "Main Checking",
                "expense_destination_account": "Misc Expenses",
                "income_destination_account": "Main Checking",
                "ask_budget_when_missing": True,
                "auto_budget_from_history": True,
            }
        }
        previous = dict(bot._AUTOFILL_TX_CACHE)
        try:
            bot._AUTOFILL_TX_CACHE = {
                "loaded_at": time.time(),
                "records": [
                    {
                        "type": "withdrawal",
                        "description": "Pranzo lavoro",
                        "source_name": "Main Checking",
                        "destination_name": "Misc Expenses",
                        "category_name": "Cibo",
                        "budget_name": "Svago",
                    }
                ],
            }
            params = {"amount": "12.00", "description": "pranzo lavoro"}
            updated = bot.apply_profile_and_history_autofill(
                service,
                state,
                transaction_kind="withdrawal",
                params=params,
            )
            self.assertEqual(updated.get("category"), "Cibo")
            self.assertEqual(updated.get("budget"), "Svago")
        finally:
            bot._AUTOFILL_TX_CACHE = previous

    def test_extract_text_from_pdfapihub_payload(self) -> None:
        payload = {
            "ok": True,
            "result": {
                "pages": [
                    {"text": "short"},
                    {"text": "longer extracted text here"},
                ]
            },
        }
        text = bot.extract_text_from_pdfapihub_payload(payload)
        self.assertEqual(text, "longer extracted text here")

    def test_parse_relative_date_hint(self) -> None:
        today = bot.date.today()
        self.assertEqual(bot.parse_relative_date_hint("yesterday"), today - bot.timedelta(days=1))
        self.assertEqual(bot.parse_relative_date_hint("2 days ago"), today - bot.timedelta(days=2))
        self.assertEqual(bot.parse_relative_date_hint("2 giorni fa"), today - bot.timedelta(days=2))

    def test_extract_relative_or_explicit_date_from_text(self) -> None:
        value = bot.extract_relative_or_explicit_date_from_text("spent 12 yesterday at bar")
        self.assertIsNotNone(value)
        self.assertRegex(str(value), r"\d{4}-\d{2}-\d{2}")

    def test_add_flow_first_prompt(self) -> None:
        service = _FakeService()
        state: dict[str, Any] = {}
        bot.start_add_flow(state, source_text="en")
        prompt = bot.build_add_flow_prompt(service, state, source_text="en")
        self.assertIn("Add step 1/8", prompt)


if __name__ == "__main__":
    unittest.main()
