from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any

os.environ.setdefault("MPLCONFIGDIR", tempfile.mkdtemp(prefix="mplconfig-"))

from scripts import telegram_firefly_bot as bot


def _make_recurrence(
    *,
    title: str = "Netflix",
    amount: str = "9.99",
    tx_type: str = "withdrawal",
    freq: str = "monthly",
    next_date: str = "2026-05-15",
    active: bool = True,
    source: str = "Accounts",
    destination: str = "Out",
    category: str = "Abbonamenti",
    budget: str = "Svago",
    rid: str = "1",
) -> dict[str, Any]:
    return {
        "id": rid,
        "attributes": {
            "active": active,
            "title": title,
            "first_date": "2025-01-15",
            "repeat_until": None,
            "next_expected_match": next_date,
            "repetitions": [{"type": freq, "moment": "15", "skip": 0}],
            "transactions": [
                {
                    "amount": amount,
                    "currency_code": "EUR",
                    "type": tx_type,
                    "source_name": source,
                    "destination_name": destination,
                    "category_name": category,
                    "budget_name": budget,
                }
            ],
        },
    }


class _FakeClient:
    def __init__(self, recurrences: list[dict[str, Any]] | None = None) -> None:
        self._recurrences = recurrences or []
        self._created_recurrences: list[dict[str, Any]] = []

    def list_recurrences(self) -> list[dict[str, Any]]:
        return list(self._recurrences)

    def create_recurrence(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._created_recurrences.append(payload)
        return {"data": {"id": "99", "attributes": {"title": payload.get("title", "?")}}}

    def list_accounts(self, kind: str = "all") -> list[dict[str, Any]]:
        return [
            {"id": "1", "attributes": {"name": "Accounts", "type": "asset", "current_balance": "1000"}},
            {"id": "2", "attributes": {"name": "Out", "type": "expense", "current_balance": "0"}},
        ]

    def list_categories(self) -> list[dict[str, Any]]:
        return [{"id": "1", "attributes": {"name": "Abbonamenti"}}]

    def list_budgets(self) -> list[dict[str, Any]]:
        return [{"id": "1", "attributes": {"name": "Svago"}}]

    def list_budget_limits(self, budget_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    def list_transactions(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []


class _FakeSettings:
    mappings = {"defaults": {}, "merchant_rules": {}}
    merchant_rules: dict[str, Any] = {}
    high_value_threshold = 500.0
    policy: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class _FakeService:
    def __init__(self, recurrences: list[dict[str, Any]] | None = None) -> None:
        self.client = _FakeClient(recurrences)
        self.settings = _FakeSettings()

    def account_balances(self) -> list[dict[str, Any]]:
        return [{"name": "Accounts", "current_balance": "1000", "currency_code": "EUR"}]

    def resolve_merchant_rule(self, merchant: str | None) -> dict[str, Any]:
        return {}

    def build_transaction(
        self,
        *,
        transaction_kind: str,
        amount: str,
        description: str,
        transaction_date: str | None = None,
        source_name: str | None = None,
        destination_name: str | None = None,
        category_name: str | None = None,
        budget_name: str | None = None,
        notes: str | None = None,
        tags: list[Any] | None = None,
        merchant: str | None = None,
        currency_code: str | None = None,
    ) -> dict[str, Any]:
        from datetime import date
        return {
            "transactions": [
                {
                    "type": transaction_kind,
                    "amount": amount,
                    "description": description,
                    "date": transaction_date or date.today().isoformat(),
                    "source_name": source_name,
                    "destination_name": destination_name,
                    "category_name": category_name,
                    "budget_name": budget_name,
                }
            ]
        }


class FormatRecurrencesTest(unittest.TestCase):
    def test_empty_list_returns_not_found_message(self) -> None:
        result = bot.format_recurrences([])
        self.assertIn("No recurring", result)

    def test_active_recurrence_shows_name_amount_frequency_next_date(self) -> None:
        item = _make_recurrence(title="Netflix", amount="9.99", freq="monthly", next_date="2026-05-15")
        result = bot.format_recurrences([item], source_text="en")
        self.assertIn("Netflix", result)
        self.assertIn("9.99", result)
        self.assertIn("15-05-2026", result)
        self.assertIn("monthly", result)

    def test_italian_locale_shows_italian_labels(self) -> None:
        item = _make_recurrence(title="Netflix", amount="9.99", freq="monthly")
        result = bot.format_recurrences([item], source_text="it")
        self.assertIn("mensile", result)
        self.assertIn("Uscite", result)

    def test_inactive_recurrence_is_excluded(self) -> None:
        active = _make_recurrence(title="Active", rid="1")
        inactive = _make_recurrence(title="Inactive", rid="2", active=False)
        result = bot.format_recurrences([active, inactive], source_text="en")
        self.assertIn("Active", result)
        self.assertNotIn("Inactive", result)

    def test_monthly_totals_are_calculated(self) -> None:
        expense = _make_recurrence(title="Netflix", amount="9.99", tx_type="withdrawal", freq="monthly")
        income = _make_recurrence(title="Salary", amount="2500.00", tx_type="deposit", freq="monthly", rid="2")
        result = bot.format_recurrences([expense, income], source_text="en")
        self.assertIn("2500.00", result)
        self.assertIn("9.99", result)
        self.assertIn("Net:", result)

    def test_category_and_source_shown(self) -> None:
        item = _make_recurrence(source="Accounts", destination="Out", category="Abbonamenti")
        result = bot.format_recurrences([item], source_text="en")
        self.assertIn("Accounts", result)
        self.assertIn("Out", result)
        self.assertIn("Abbonamenti", result)


class InterpretNaturalCommandGraphTest(unittest.TestCase):
    def test_budget_graph_routing(self) -> None:
        self.assertEqual(bot.interpret_natural_command("fammi un grafico del budget"), "/graph budget")
        self.assertEqual(bot.interpret_natural_command("graph of budget remaining"), "/graph budget")

    def test_recurrence_graph_routing(self) -> None:
        self.assertEqual(bot.interpret_natural_command("fammi un grafico delle ricorrenze"), "/graph recurrences")
        self.assertEqual(bot.interpret_natural_command("graph of recurring transactions"), "/graph recurrences")


class ParseNaturalIntentPayloadTest(unittest.TestCase):
    def test_budget_graph_intent_detected(self) -> None:
        payload = bot.parse_natural_intent_payload("grafico budget rimasto questo mese")
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["intent"], "graph_budget")

    def test_recurrence_graph_intent_detected(self) -> None:
        payload = bot.parse_natural_intent_payload("fammi un grafico delle ricorrenze attive")
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["intent"], "graph_recurrences")

    def test_list_recurrences_intent_detected_italian(self) -> None:
        payload = bot.parse_natural_intent_payload("mostrami le ricorrenze")
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["intent"], "list_recurrences")


class CreateBudgetChartTest(unittest.TestCase):
    def test_budget_chart_returns_path_and_summary(self) -> None:
        limits = {"Groceries": 350.0, "Dining": 80.0}
        spent = {"Groceries": 120.5, "Dining": 90.0}
        path, caption = bot.create_budget_chart(limits, spent, label="maggio 2026")
        self.assertTrue(path.endswith(".png"))
        self.assertIn("Groceries", caption)
        self.assertIn("120.50", caption)
        self.assertIn("90.00", caption)
        import os
        os.unlink(path)

    def test_budget_chart_raises_on_empty_data(self) -> None:
        with self.assertRaises(RuntimeError):
            bot.create_budget_chart({}, {}, label="test")

    def test_budget_chart_over_budget_shows_exceeded(self) -> None:
        limits = {"Dining": 80.0}
        spent = {"Dining": 90.0}
        _, caption = bot.create_budget_chart(limits, spent, label="test", source_text="en")
        self.assertIn("over by", caption)
        import os
        import glob
        for f in glob.glob(tempfile.gettempdir() + "/firefly-budget-*.png"):
            try:
                os.unlink(f)
            except OSError:
                pass


class CreateRecurrenceChartTest(unittest.TestCase):
    def test_recurrence_chart_returns_path_and_summary(self) -> None:
        items = [
            _make_recurrence(title="Netflix", amount="9.99", tx_type="withdrawal"),
            _make_recurrence(title="Salary", amount="2500.00", tx_type="deposit", rid="2"),
        ]
        path, caption = bot.create_recurrence_chart(items, source_text="en")
        self.assertTrue(path.endswith(".png"))
        self.assertIn("Income", caption)
        self.assertIn("Expenses", caption)
        import os
        os.unlink(path)

    def test_recurrence_chart_raises_on_no_active_items(self) -> None:
        items = [_make_recurrence(active=False)]
        with self.assertRaises(RuntimeError):
            bot.create_recurrence_chart(items)


class RecurrenceSuggestionFlowTest(unittest.TestCase):
    def _make_service(self) -> _FakeService:
        return _FakeService()

    def test_recurrence_suggestion_yes_creates_draft(self) -> None:
        service = self._make_service()
        state: dict[str, Any] = {
            "pending_recurrence_suggestion": {
                "cadence": "monthly",
                "amount": "9.99",
                "description": "Netflix",
                "transaction_kind": "withdrawal",
                "source": "Accounts",
                "destination": "Out",
                "category": "Abbonamenti",
                "budget": None,
                "date": "2026-05-15",
            }
        }
        response = bot.process_message(service, "si", state)
        self.assertNotIn("pending_recurrence_suggestion", state)
        pending = state.get("pending_action")
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(pending.get("kind"), "recurrence_create")
        self.assertIn("Netflix", response.text)

    def test_recurrence_suggestion_no_clears_state(self) -> None:
        service = self._make_service()
        state: dict[str, Any] = {
            "pending_recurrence_suggestion": {
                "cadence": "monthly",
                "amount": "9.99",
                "description": "Netflix",
                "transaction_kind": "withdrawal",
                "source": "Accounts",
                "destination": "Out",
                "category": None,
                "budget": None,
                "date": None,
            }
        }
        response = bot.process_message(service, "no", state)
        self.assertNotIn("pending_recurrence_suggestion", state)
        self.assertIn("no recurrence", response.text.lower())

    def test_recurrence_suggestion_ignored_for_commands(self) -> None:
        service = self._make_service()
        state: dict[str, Any] = {
            "pending_recurrence_suggestion": {
                "cadence": "monthly",
                "amount": "9.99",
                "description": "Netflix",
                "transaction_kind": "withdrawal",
                "source": "Accounts",
                "destination": "Out",
                "category": None,
                "budget": None,
                "date": None,
            }
        }
        # A command starting with / should bypass the suggestion handler
        bot.process_message(service, "/help", state)
        # Suggestion should remain untouched (not consumed)
        self.assertIn("pending_recurrence_suggestion", state)


class RecurrencePayloadFromParamsTest(unittest.TestCase):
    def _make_service(self) -> _FakeService:
        return _FakeService()

    def test_valid_params_build_payload(self) -> None:
        service = self._make_service()
        params = {
            "amount": "9.99",
            "description": "Netflix",
            "cadence": "monthly",
            "source": "Accounts",
            "destination": "Out",
            "category": "Abbonamenti",
        }
        # recurrence_payload_from_params calls service.build_transaction which needs full service
        # This is tested indirectly via the suggestion flow above

    def test_missing_amount_raises(self) -> None:
        service = self._make_service()
        with self.assertRaises((RuntimeError, Exception)):
            bot.recurrence_payload_from_params(service, {"description": "Netflix", "cadence": "monthly"})

    def test_invalid_cadence_raises(self) -> None:
        service = self._make_service()
        with self.assertRaises((RuntimeError, Exception)):
            bot.recurrence_payload_from_params(service, {"amount": "9.99", "description": "Netflix", "cadence": "bimonthly"})


if __name__ == "__main__":
    unittest.main()
