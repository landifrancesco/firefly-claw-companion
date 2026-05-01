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
        accounts = [
            {"attributes": {"name": "Main Checking", "type": "asset"}},
            {"attributes": {"name": "Cash", "type": "asset"}},
            {"attributes": {"name": "Misc Expenses", "type": "expense"}},
            {"attributes": {"name": "Employer", "type": "revenue"}},
        ]
        if kind and kind != "all":
            return [item for item in accounts if item["attributes"]["type"] == kind]
        return accounts

    def health(self) -> dict[str, Any]:
        return {"version": "test"}


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
        self.assertIn("Da quale conto paghi", first.text)
        self.assertIn("Main Checking", first.text)
        self.assertNotIn("Misc Expenses", first.text)

        second = bot.handle_finance_setup_message(service, state, "1")
        self.assertIsNotNone(second)
        self.assertIn("expense account", second.text)
        self.assertIn("Misc Expenses", second.text)
        self.assertNotIn("Main Checking", second.text)

        bot.handle_finance_setup_message(service, state, "1")
        bot.handle_finance_setup_message(service, state, "1")
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

    def test_setup_shows_status_train_starts_wizard(self) -> None:
        service = _FakeService()
        state: dict[str, Any] = {}

        setup = bot.process_message(service, "/setup", state)
        self.assertIn("No complete finance profile", setup.text)
        self.assertNotIn("Training 1/8", setup.text)

        train = bot.process_message(service, "/train", state)
        self.assertIn("Training 1/8", train.text)
        self.assertIn("Main Checking", train.text)

    def test_train_clears_maintenance_before_numeric_reply(self) -> None:
        service = _FakeService()
        state: dict[str, Any] = {"maintenance_mode": {"step": "account", "source_text": "/manutenzione"}}

        train = bot.process_message(service, "/train", state)
        self.assertIn("Training 1/8", train.text)
        self.assertNotIn("maintenance_mode", state)

        reply = bot.process_message(service, "1", state)
        self.assertIn("Training 2/8", reply.text)
        self.assertNotIn("Delete", reply.text)
        self.assertNotIn("eliminare", reply.text.casefold())

    def test_active_training_wins_over_stale_maintenance_state(self) -> None:
        service = _FakeService()
        state: dict[str, Any] = {
            "maintenance_mode": {"step": "account", "source_text": "/manutenzione"},
            "profile_setup": {"step_index": 0, "profile": {}},
        }

        reply = bot.process_message(service, "1", state)

        self.assertIn("Training 2/8", reply.text)
        self.assertNotIn("Delete", reply.text)

    def test_training_account_list_uses_higher_limit(self) -> None:
        class ManyAccountClient(_FakeClient):
            def list_accounts(self, kind: str = "all") -> list[dict[str, Any]]:
                accounts = [
                    {"attributes": {"name": f"Card {index:02d}", "type": "asset"}}
                    for index in range(1, 21)
                ]
                accounts.append({"attributes": {"name": "Misc Expenses", "type": "expense"}})
                if kind and kind != "all":
                    return [item for item in accounts if item["attributes"]["type"] == kind]
                return accounts

        class ManyAccountService:
            client = ManyAccountClient()

        state = {"profile_setup": {"step_index": 4, "profile": {}}}
        prompt = bot.build_finance_setup_prompt(ManyAccountService(), state, source_text="en")

        self.assertIn("Card 01", prompt)
        self.assertIn("Card 20", prompt)

    def test_training_keeps_real_account_named_accounts(self) -> None:
        class AccountsClient(_FakeClient):
            def list_accounts(self, kind: str = "all") -> list[dict[str, Any]]:
                accounts = [
                    {"attributes": {"name": "Accounts", "type": "expense"}},
                    {"attributes": {"name": "Out", "type": "expense"}},
                    {"attributes": {"name": "Accounts", "type": "asset"}},
                    {"attributes": {"name": "Accounts USD", "type": "asset"}},
                    {"attributes": {"name": "Cash", "type": "asset"}},
                ]
                if kind and kind != "all":
                    return [item for item in accounts if item["attributes"]["type"] == kind]
                return accounts

        class AccountsService:
            client = AccountsClient()

        choices = bot.setup_account_choices_for_step(AccountsService(), "expense_source_account")

        self.assertEqual(choices, ["Accounts", "Accounts USD", "Cash"])

    def test_training_does_not_hide_duplicate_account_names(self) -> None:
        class DuplicateAccountClient(_FakeClient):
            def list_accounts(self, kind: str = "all") -> list[dict[str, Any]]:
                accounts = [
                    {"attributes": {"name": "Cash", "type": "asset"}},
                    {"attributes": {"name": "Cash", "type": "asset"}},
                    {"attributes": {"name": "Accounts", "type": "asset"}},
                ]
                if kind and kind != "all":
                    return [item for item in accounts if item["attributes"]["type"] == kind]
                return accounts

        class DuplicateAccountService:
            client = DuplicateAccountClient()

        choices = bot.setup_account_choices_for_step(DuplicateAccountService(), "card_payment_account")

        self.assertEqual(choices, ["Cash", "Cash", "Accounts"])

    def test_health_prefix_falls_back_when_locale_key_missing(self) -> None:
        original = bot.bot_text

        def missing_key(*args: Any, **kwargs: Any) -> str:
            raise KeyError("Missing string locale key: live_ping_ok")

        try:
            bot.bot_text = missing_key
            text = bot.build_health_message(_FakeService(), source_text="en", prefix_key="live_ping_ok")
        finally:
            bot.bot_text = original

        self.assertIn("Scheduled live check", text)
        self.assertIn("Firefly bridge is healthy", text)

    def test_health_message_handles_success(self) -> None:
        text = bot.build_health_message(_FakeService(), source_text="en")
        self.assertIn("Firefly bridge is healthy", text)
        self.assertIn("test", text)

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

    def test_budget_report_shows_left_and_limit_only_budgets(self) -> None:
        text = bot.format_budget_report(
            [{"type": "withdrawal", "budget_name": "Groceries", "amount": "40.00"}],
            label="2026-04",
            budget_limits={"Groceries": 100.0, "Travel": 50.0},
            source_text="en",
        )

        self.assertIn("Groceries: spent 40.00 / limit 100.00 (left: 60.00)", text)
        self.assertIn("Travel: spent 0.00 / limit 50.00 (left: 50.00)", text)

    def test_created_transaction_result_shows_category_budget_and_mode(self) -> None:
        payload = {
            "transactions": [
                {
                    "type": "withdrawal",
                    "amount": "0.90",
                    "date": "2026-05-01",
                    "description": "Caffe",
                    "source_name": "Accounts",
                    "destination_name": "Out",
                    "category_name": "Bar",
                    "budget_name": "Svago",
                }
            ]
        }
        text = bot.format_created_transaction_result({}, fallback_payload=payload, source_text="it")

        self.assertIn("Categoria: Bar", text)
        self.assertIn("Budget: Svago", text)
        self.assertIn("Modalita: live", text)

    def test_duplicate_blocked_rounds_amount(self) -> None:
        text = bot.format_duplicate_blocked(
            {"date": "2026-05-01", "amount": "0.900000000000", "description": "Caffe"},
            source_text="it",
        )

        self.assertIn("EUR 0.90", text)
        self.assertNotIn("0.900000000000", text)

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
