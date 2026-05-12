from __future__ import annotations

import unittest
from datetime import date
from typing import Any

from scripts import telegram_firefly_bot as bot


def _make_transaction_record(
    *,
    group_id: str,
    journal_id: str,
    description: str,
    amount: str,
    tx_date: str,
    tx_type: str = "withdrawal",
    source: str = "Accounts",
    destination: str = "Out",
    category: str = "Food",
) -> dict[str, Any]:
    return {
        "id": group_id,
        "attributes": {
            "transactions": [
                {
                    "transaction_journal_id": journal_id,
                    "type": tx_type,
                    "date": tx_date,
                    "amount": amount,
                    "description": description,
                    "source_name": source,
                    "destination_name": destination,
                    "category_name": category,
                }
            ]
        },
    }


class _CloneSplitClient:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records
        self.update_calls: list[tuple[int, dict[str, Any]]] = []
        self.created_payloads: list[dict[str, Any]] = []

    def list_transactions(self, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self._records)

    def update_transaction(self, transaction_id: int, updates: dict[str, Any]) -> bool:
        self.update_calls.append((transaction_id, updates))
        return True

    def create_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.created_payloads.append(payload)
        tx = payload["transactions"][0]
        return {
            "data": {
                "id": "99",
                "attributes": {
                    "transactions": [tx],
                },
            }
        }


class _CloneSplitService:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.client = _CloneSplitClient(records)
        self.settings = type(
            "Settings",
            (),
            {
                "mappings": {"defaults": {}, "merchant_rules": {}},
                "merchant_rules": {},
                "high_value_threshold": 500.0,
                "policy": {},
            },
        )()

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
                    "notes": notes,
                }
            ]
        }

    def commit_transaction(self, payload: dict[str, Any], *, dry_run: bool, confirm_high_value: bool) -> dict[str, Any]:
        if dry_run:
            return {"status": "dry_run"}
        return {"status": "created", "result": self.client.create_transaction(payload)}


class CloneAndSplitFlowTest(unittest.TestCase):
    def _service(self) -> _CloneSplitService:
        records = [
            _make_transaction_record(
                group_id="42",
                journal_id="101",
                description="Ristorante",
                amount="6.00",
                tx_date="2026-05-11",
            ),
            _make_transaction_record(
                group_id="41",
                journal_id="100",
                description="Caffe",
                amount="2.20",
                tx_date="2026-05-10",
            ),
        ]
        return _CloneSplitService(records)

    def test_split_last_transaction_uses_firefly_latest(self) -> None:
        service = self._service()
        state = {
            "last_committed_txn": {
                "id": "41",
                "description": "Caffe",
                "amount": "2.20",
                "type": "withdrawal",
            }
        }

        response = bot.process_message(service, "dividi per 2 l'ultima transazione", state)

        self.assertIn("Ristorante", response.text)
        pending = state.get("pending_action")
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(pending["payload"]["txn_id"], "42")
        self.assertEqual(pending["payload"]["tx_type"], "withdrawal")

        commit_response = bot.process_message(service, "si", state)
        self.assertIn("transaction updated", commit_response.text.lower())
        self.assertEqual(service.client.update_calls, [(42, {"type": "withdrawal", "amount": "3.00"})])

    def test_split_without_latest_hint_asks_before_using_last_committed(self) -> None:
        service = self._service()
        state = {
            "last_committed_txn": {
                "id": "41",
                "description": "Caffe",
                "amount": "2.20",
                "type": "withdrawal",
            }
        }

        response = bot.process_message(service, "dividi per 2", state)

        self.assertIn("Latest Firefly transaction", response.text)
        self.assertIn("pending_split_latest_confirm", state)

    def test_split_latest_confirm_no_shows_picker(self) -> None:
        service = self._service()
        state = {
            "last_committed_txn": {
                "id": "41",
                "description": "Caffe",
                "amount": "2.20",
                "type": "withdrawal",
            }
        }

        bot.process_message(service, "dividi per 2", state)
        response = bot.process_message(service, "no", state)

        self.assertIn("Choose which transaction to split", response.text)
        self.assertIn("pending_split_selection", state)

    def test_split_hint_for_older_transaction_opens_picker(self) -> None:
        service = self._service()
        state: dict[str, Any] = {}

        response = bot.process_message(service, "dividi per 2 il caffe", state)

        self.assertIn("Caffe", response.text)
        self.assertIn("pending_split_selection", state)

    def test_clone_command_duplicates_selected_transaction_with_today_date(self) -> None:
        service = self._service()
        state: dict[str, Any] = {}

        response = bot.process_message(service, "/clona", state)
        self.assertIn("Ristorante", response.text)
        self.assertIn("pending_clone_selection", state)

        created = bot.process_message(service, "2", state)
        self.assertNotIn("pending_clone_selection", state)
        self.assertIn("transaction created successfully", created.text.lower())
        self.assertEqual(len(service.client.created_payloads), 1)
        tx = service.client.created_payloads[0]["transactions"][0]
        self.assertEqual(tx["description"], "Caffe")
        self.assertEqual(tx["date"], date.today().isoformat())
