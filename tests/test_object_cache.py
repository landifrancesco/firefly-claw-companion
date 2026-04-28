"""Tests for the object_cache module."""
from __future__ import annotations

import unittest
from typing import Any

from firefly_companion.object_cache import FireflyObjectCache


class FakeClient:
    def list_categories(self) -> list[dict[str, Any]]:
        return [
            {"attributes": {"name": "Bar"}},
            {"attributes": {"name": "Spesa"}},
            {"attributes": {"name": "Cibo"}},
            {"attributes": {"name": "Sport"}},
            {"attributes": {"name": "Casa"}},
            {"attributes": {"name": "Servizi web"}},
        ]

    def list_budgets(self) -> list[dict[str, Any]]:
        return [
            {"attributes": {"name": "Svago"}},
            {"attributes": {"name": "Spesa mensile"}},
            {"attributes": {"name": "Casa"}},
        ]

    def list_accounts(self, kind: str = "all") -> list[dict[str, Any]]:
        return [
            {"attributes": {"name": "Main Checking", "type": "asset", "current_balance": "1500.00", "currency_code": "EUR"}},
            {"attributes": {"name": "Cash", "type": "asset", "current_balance": "50.00", "currency_code": "EUR"}},
            {"attributes": {"name": "Savings", "type": "asset", "current_balance": "5000.00", "currency_code": "EUR"}},
        ]


class CategoryResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = FireflyObjectCache(client=FakeClient())

    def test_exact_match(self) -> None:
        self.assertEqual(self.cache.find_category("Bar"), "Bar")

    def test_case_insensitive(self) -> None:
        self.assertEqual(self.cache.find_category("bar"), "Bar")
        self.assertEqual(self.cache.find_category("SPESA"), "Spesa")

    def test_no_match(self) -> None:
        self.assertIsNone(self.cache.find_category("Nonexistent"))

    def test_fuzzy_substring(self) -> None:
        self.assertEqual(self.cache.find_category_fuzzy("servizi"), "Servizi web")

    def test_resolve_with_candidates(self) -> None:
        result = self.cache.resolve_category("Groceries", candidates=["Spesa", "Cibo"])
        self.assertEqual(result, "Spesa")

    def test_resolve_exact_wins_over_candidates(self) -> None:
        result = self.cache.resolve_category("Bar", candidates=["Spesa"])
        self.assertEqual(result, "Bar")

    def test_list_categories(self) -> None:
        cats = self.cache.categories()
        self.assertIn("Bar", cats)
        self.assertIn("Spesa", cats)
        self.assertEqual(len(cats), 6)


class BudgetResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = FireflyObjectCache(client=FakeClient())

    def test_exact_match(self) -> None:
        self.assertEqual(self.cache.find_budget("Svago"), "Svago")

    def test_case_insensitive(self) -> None:
        self.assertEqual(self.cache.find_budget("svago"), "Svago")

    def test_find_budgets_for_category_explicit_map(self) -> None:
        results = self.cache.find_budgets_for_category(
            "Bar", category_budget_map={"Bar": "Svago"}
        )
        self.assertIn("Svago", results)

    def test_find_budgets_for_category_name_affinity(self) -> None:
        results = self.cache.find_budgets_for_category("Casa")
        self.assertIn("Casa", results)

    def test_find_budgets_for_category_substring(self) -> None:
        results = self.cache.find_budgets_for_category("Spesa")
        self.assertIn("Spesa mensile", results)

    def test_find_budgets_for_unrelated_category(self) -> None:
        results = self.cache.find_budgets_for_category("Sport")
        self.assertEqual(results, [])

    def test_list_budgets(self) -> None:
        budgets = self.cache.budgets()
        self.assertEqual(len(budgets), 3)


class AccountResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = FireflyObjectCache(client=FakeClient())

    def test_exact_match(self) -> None:
        self.assertEqual(self.cache.find_account("Main Checking"), "Main Checking")

    def test_case_insensitive(self) -> None:
        self.assertEqual(self.cache.find_account("cash"), "Cash")

    def test_resolve_with_alias(self) -> None:
        result = self.cache.resolve_account("contanti", aliases={"contanti": "Cash"})
        self.assertEqual(result, "Cash")

    def test_account_balances(self) -> None:
        balances = self.cache.account_balances()
        self.assertEqual(len(balances), 3)
        names = [b["name"] for b in balances]
        self.assertIn("Main Checking", names)


class CacheControlTest(unittest.TestCase):
    def test_invalidate_forces_refresh(self) -> None:
        cache = FireflyObjectCache(client=FakeClient(), ttl_seconds=9999)
        _ = cache.categories()  # trigger initial load
        cache.invalidate()
        self.assertEqual(cache._loaded_at, 0.0)

    def test_stale_cache_refreshes(self) -> None:
        cache = FireflyObjectCache(client=FakeClient(), ttl_seconds=0)
        cats1 = cache.categories()
        cats2 = cache.categories()
        self.assertEqual(cats1, cats2)


if __name__ == "__main__":
    unittest.main()
