from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import logging
from typing import Any

from .client import FireflyClient
from .config import BridgeSettings


class BridgeValidationError(RuntimeError):
    """Raised when user input cannot be mapped safely."""


def normalize_name(value: str | None) -> str:
    return " ".join((value or "").strip().split())


def normalize_amount(value: str | Decimal) -> str:
    if isinstance(value, Decimal):
        amount = value
    else:
        try:
            amount = Decimal(str(value))
        except InvalidOperation as exc:
            raise BridgeValidationError(f"Invalid amount: {value}") from exc

    return f"{amount.quantize(Decimal('0.01'))}"


def normalize_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc).replace(microsecond=0)

    if "T" in value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = datetime.fromisoformat(f"{value}T12:00:00+00:00")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.replace(microsecond=0)


def month_window(reference: date | None = None) -> tuple[date, date]:
    today = reference or date.today()
    start = today.replace(day=1)
    if start.month == 12:
        next_month = start.replace(year=start.year + 1, month=1)
    else:
        next_month = start.replace(month=start.month + 1)
    end = next_month - timedelta(days=1)
    return start, end


def flatten_transactions(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for record in records:
        attributes = record.get("attributes", {})
        transactions = attributes.get("transactions", [])
        for transaction in transactions:
            flattened.append(
                {
                    "journal_id": record.get("id"),
                    "transaction_id": transaction.get("transaction_journal_id") or transaction.get("transaction_group_id"),
                    "type": transaction.get("type"),
                    "date": transaction.get("date"),
                    "amount": str(transaction.get("amount")),
                    "description": transaction.get("description") or attributes.get("group_title"),
                    "source_name": transaction.get("source_name"),
                    "destination_name": transaction.get("destination_name"),
                    "category_name": transaction.get("category_name"),
                    "budget_name": transaction.get("budget_name"),
                    "notes": transaction.get("notes"),
                }
            )
    return flattened


def dedupe_signature(candidate: dict[str, str]) -> str:
    digest = "|".join(
        [
            normalize_name(candidate.get("type")),
            normalize_name(candidate.get("date", ""))[:10],
            normalize_amount(candidate.get("amount", "0")),
            normalize_name(candidate.get("description")),
            normalize_name(candidate.get("source_name")).casefold(),
            normalize_name(candidate.get("destination_name")).casefold(),
        ]
    )
    return hashlib.sha256(digest.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class BridgeService:
    client: FireflyClient
    settings: BridgeSettings
    logger: logging.Logger

    def resolve_account(self, value: str | None) -> str | None:
        if not value:
            return None
        alias = self.settings.account_aliases.get(value.casefold())
        return alias or value

    def resolve_category(self, value: str | None) -> str | None:
        if not value:
            return None
        alias = self.settings.category_aliases.get(value.casefold())
        return alias or value

    def resolve_merchant_rule(self, merchant: str | None) -> dict[str, Any]:
        if not merchant:
            return {}
        return self.settings.merchant_rules.get(merchant.casefold(), {})

    def account_balances(self) -> list[dict[str, Any]]:
        balances: list[dict[str, Any]] = []
        for account in self.client.list_accounts("asset"):
            attributes = account.get("attributes", {})
            balances.append(
                {
                    "id": account.get("id"),
                    "name": attributes.get("name"),
                    "type": attributes.get("type"),
                    "currency_code": attributes.get("currency_code"),
                    "current_balance": attributes.get("current_balance") or attributes.get("native_current_balance"),
                    "current_balance_date": attributes.get("current_balance_date"),
                    "active": attributes.get("active"),
                }
            )
        return balances

    def recent_transactions(self, days: int, query: str | None, limit: int) -> list[dict[str, Any]]:
        end = date.today()
        start = end - timedelta(days=max(days, 1))
        flattened = flatten_transactions(self.client.list_transactions(start=start, end=end, limit=limit))
        if not query:
            return flattened[:limit]

        q = query.casefold()
        filtered = []
        for record in flattened:
            haystack = " ".join(
                [
                    str(record.get("description", "")),
                    str(record.get("source_name", "")),
                    str(record.get("destination_name", "")),
                    str(record.get("category_name", "")),
                    str(record.get("budget_name", "")),
                ]
            ).casefold()
            if q in haystack:
                filtered.append(record)
        return filtered[:limit]

    def monthly_summary(self, month: str | None) -> dict[str, Any]:
        if month:
            reference = date.fromisoformat(f"{month}-01")
        else:
            reference = None
        start, end = month_window(reference)
        summary = self.client.summary_basic(start=start, end=end)
        return {"month": start.strftime("%Y-%m"), "start": start.isoformat(), "end": end.isoformat(), "summary": summary}

    def _candidate_payload(
        self,
        *,
        transaction_type: str,
        amount: str,
        description: str,
        transaction_date: str | None,
        source_name: str | None,
        destination_name: str | None,
        category_name: str | None,
        budget_name: str | None,
        notes: str | None,
        tags: list[str] | None,
        currency_code: str | None,
    ) -> dict[str, Any]:
        if not description.strip():
            raise BridgeValidationError("description is required")

        payload = {
            "error_if_duplicate_hash": False,
            "apply_rules": True,
            "fire_webhooks": False,
            "transactions": [
                {
                    "type": transaction_type,
                    "date": normalize_date(transaction_date).isoformat(),
                    "amount": normalize_amount(amount),
                    "description": normalize_name(description),
                    "source_name": normalize_name(source_name) or None,
                    "destination_name": normalize_name(destination_name) or None,
                    "category_name": normalize_name(category_name) or None,
                    "budget_name": normalize_name(budget_name) or None,
                    "notes": normalize_name(notes) or None,
                    "tags": tags or [],
                    "currency_code": normalize_name(currency_code) or None,
                }
            ],
        }
        transaction = payload["transactions"][0]
        payload["dedupe_signature"] = dedupe_signature(transaction)
        payload["dedupe_reference"] = {
            "type": transaction["type"],
            "date": transaction["date"][:10],
            "amount": transaction["amount"],
            "description": transaction["description"],
            "source_name": transaction.get("source_name") or "",
            "destination_name": transaction.get("destination_name") or "",
        }
        return payload

    def build_transaction(
        self,
        *,
        transaction_kind: str,
        amount: str,
        description: str,
        transaction_date: str | None,
        source_name: str | None,
        destination_name: str | None,
        category_name: str | None,
        budget_name: str | None,
        notes: str | None,
        tags: list[str] | None,
        merchant: str | None,
        currency_code: str | None,
    ) -> dict[str, Any]:
        rules = self.resolve_merchant_rule(merchant)
        defaults = self.settings.mappings.get("defaults", {})

        if transaction_kind == "withdrawal":
            source_name = self.resolve_account(source_name or defaults.get("expense_source_account"))
            destination_name = self.resolve_account(
                destination_name or rules.get("destination_account") or defaults.get("expense_destination_account")
            )
            category_name = self.resolve_category(category_name or rules.get("category"))
        elif transaction_kind == "deposit":
            source_name = self.resolve_account(source_name or rules.get("source_account") or defaults.get("income_source_account"))
            destination_name = self.resolve_account(destination_name or defaults.get("income_destination_account"))
            category_name = self.resolve_category(category_name or rules.get("category"))
        elif transaction_kind == "transfer":
            source_name = self.resolve_account(source_name)
            destination_name = self.resolve_account(destination_name)
        else:
            raise BridgeValidationError(f"Unsupported transaction kind: {transaction_kind}")

        if not source_name or not destination_name:
            raise BridgeValidationError("Both source and destination accounts must resolve to concrete names.")

        return self._candidate_payload(
            transaction_type=transaction_kind,
            amount=amount,
            description=description,
            transaction_date=transaction_date,
            source_name=source_name,
            destination_name=destination_name,
            category_name=category_name,
            budget_name=budget_name,
            notes=notes,
            tags=tags,
            currency_code=currency_code,
        )

    def find_duplicate(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        return None

    def commit_transaction(self, payload: dict[str, Any], *, dry_run: bool, confirm_high_value: bool) -> dict[str, Any]:
        transaction = payload["transactions"][0]
        amount = Decimal(transaction["amount"])
        if not dry_run and amount >= self.settings.high_value_threshold and not confirm_high_value:
            raise BridgeValidationError(
                f"Amount {amount} meets or exceeds the high-value threshold {self.settings.high_value_threshold}."
            )

        duplicate = self.find_duplicate(payload)
        if duplicate:
            return {"status": "duplicate_blocked", "duplicate": duplicate, "payload": payload}

        if dry_run:
            return {"status": "dry_run", "payload": payload}

        created = self.client.create_transaction({"transactions": payload["transactions"]})
        return {"status": "created", "payload": payload, "result": created}
