"""Cached Firefly III object lookups.

Provides category, budget, and account resolution with caching and
fuzzy matching so that the bot doesn't hammer the Firefly API on every
request, and can intelligently suggest categories/budgets for transactions.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Sequence


@dataclass
class FireflyObjectCache:
    """Cached access to Firefly categories, budgets, and accounts.

    All data is refreshed from the Firefly API after ``ttl_seconds``
    have elapsed since the last fetch.
    """

    client: Any  # FireflyClient, typed as Any to avoid circular imports
    ttl_seconds: float = 300.0

    _categories: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _budgets: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _accounts: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _loaded_at: float = field(default=0.0, repr=False)

    # ---- cache control --------------------------------------------------

    def _refresh_if_stale(self) -> None:
        if time.monotonic() - self._loaded_at < self.ttl_seconds:
            return
        try:
            self._categories = self.client.list_categories() or []
        except Exception:
            self._categories = self._categories or []
        try:
            self._budgets = self.client.list_budgets() or []
        except Exception:
            self._budgets = self._budgets or []
        try:
            self._accounts = self.client.list_accounts("all") or []
        except Exception:
            self._accounts = self._accounts or []
        self._loaded_at = time.monotonic()

    def invalidate(self) -> None:
        """Force a re-fetch on next access."""
        self._loaded_at = 0.0

    # ---- category helpers -----------------------------------------------

    def categories(self) -> list[str]:
        """Return a list of existing Firefly category names."""
        self._refresh_if_stale()
        return [
            str(c.get("attributes", {}).get("name", "")).strip()
            for c in self._categories
            if str(c.get("attributes", {}).get("name", "")).strip()
        ]

    def find_category(self, name: str | None) -> str | None:
        """Find an exact or case-insensitive category match."""
        if not name:
            return None
        needle = name.strip().casefold()
        for cat_name in self.categories():
            if cat_name.casefold() == needle:
                return cat_name
        return None

    def find_category_fuzzy(self, name: str | None) -> str | None:
        """Find a category with fuzzy matching (substring, alias)."""
        if not name:
            return None
        exact = self.find_category(name)
        if exact:
            return exact
        needle = name.strip().casefold()
        # Substring match: "Spesa" matches "Spesa mensile"
        for cat_name in self.categories():
            if needle in cat_name.casefold() or cat_name.casefold() in needle:
                return cat_name
        return None

    def resolve_category(
        self,
        name: str | None,
        candidates: Sequence[str] = (),
    ) -> str | None:
        """Resolve a category name against existing Firefly categories.

        Tries:
        1. Exact match of ``name``
        2. Each candidate in ``candidates`` in order
        3. Fuzzy match of ``name``

        Returns the matched Firefly category name, or ``None``.
        """
        if name:
            exact = self.find_category(name)
            if exact:
                return exact
        for candidate in candidates:
            matched = self.find_category(candidate)
            if matched:
                return matched
        if name:
            return self.find_category_fuzzy(name)
        return None

    # ---- budget helpers -------------------------------------------------

    def budgets(self) -> list[str]:
        """Return a list of existing Firefly budget names."""
        self._refresh_if_stale()
        return [
            str(b.get("attributes", {}).get("name", "")).strip()
            for b in self._budgets
            if str(b.get("attributes", {}).get("name", "")).strip()
        ]

    def find_budget(self, name: str | None) -> str | None:
        """Find an exact or case-insensitive budget match."""
        if not name:
            return None
        needle = name.strip().casefold()
        for budget_name in self.budgets():
            if budget_name.casefold() == needle:
                return budget_name
        return None

    def find_budgets_for_category(
        self,
        category: str,
        category_budget_map: dict[str, str] | None = None,
    ) -> list[str]:
        """Find budgets related to a category.

        Uses:
        1. Explicit ``category_budget_map`` (from mappings.yml)
        2. Name substring match (e.g. budget "Spesa mensile" matches category "Spesa")

        Returns a list of matching budget names sorted by relevance.
        """
        results: list[tuple[int, str]] = []
        cat_lower = category.casefold()
        cat_budget_map = category_budget_map or {}

        for budget_name in self.budgets():
            score = 0
            # Explicit mapping
            mapped = cat_budget_map.get(category)
            if mapped and mapped.casefold() == budget_name.casefold():
                score += 3
            # Name affinity
            if cat_lower in budget_name.casefold() or budget_name.casefold() in cat_lower:
                score += 2
            if score > 0:
                results.append((score, budget_name))

        results.sort(key=lambda x: -x[0])
        return [name for _, name in results[:3]]

    # ---- account helpers ------------------------------------------------

    def accounts(self) -> list[str]:
        """Return a list of existing Firefly account names."""
        self._refresh_if_stale()
        return [
            str(a.get("attributes", {}).get("name", "")).strip()
            for a in self._accounts
            if str(a.get("attributes", {}).get("name", "")).strip()
        ]

    def account_balances(self) -> list[dict[str, str]]:
        """Return account balances in a uniform format."""
        self._refresh_if_stale()
        results: list[dict[str, str]] = []
        for account in self._accounts:
            attrs = account.get("attributes") or {}
            name = str(attrs.get("name", "")).strip()
            balance = str(attrs.get("current_balance", "0")).strip()
            currency = str(attrs.get("currency_code", "EUR")).strip()
            account_type = str(attrs.get("type", "")).strip()
            if name and account_type in {"asset", "default"}:
                results.append({
                    "name": name,
                    "balance": balance,
                    "currency": currency,
                    "type": account_type,
                })
        return results

    def find_account(self, name: str | None) -> str | None:
        """Find an exact or case-insensitive account match."""
        if not name:
            return None
        needle = name.strip().casefold()
        for account_name in self.accounts():
            if account_name.casefold() == needle:
                return account_name
        return None

    def resolve_account(
        self,
        name: str | None,
        aliases: dict[str, str] | None = None,
    ) -> str | None:
        """Resolve an account name, checking aliases first."""
        if not name:
            return None
        alias_map = aliases or {}
        # Check alias
        aliased = alias_map.get(name.strip().casefold())
        if aliased:
            found = self.find_account(aliased)
            if found:
                return found
        return self.find_account(name)
