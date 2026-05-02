"""Tests for the draft_manager module."""
from __future__ import annotations

import unittest
from typing import Any

from firefly_companion.draft_manager import (
    DraftManager,
    DraftPhase,
    DraftSession,
    TransactionDraft,
    load_draft_session,
    save_draft_session,
)
from firefly_companion.object_cache import FireflyObjectCache


class FakeClient:
    """Minimal mock Firefly client for testing."""

    def list_categories(self) -> list[dict[str, Any]]:
        return [
            {"attributes": {"name": "Bar"}},
            {"attributes": {"name": "Spesa"}},
            {"attributes": {"name": "Sport"}},
            {"attributes": {"name": "Casa"}},
        ]

    def list_budgets(self) -> list[dict[str, Any]]:
        return [
            {"attributes": {"name": "Svago"}},
            {"attributes": {"name": "Spesa mensile"}},
            {"attributes": {"name": "Casa"}},
        ]

    def list_accounts(self, kind: str = "all") -> list[dict[str, Any]]:
        return [{"attributes": {"name": "Main Checking", "type": "asset", "current_balance": "1000", "currency_code": "EUR"}}]


def _make_payload(amount: str = "10.00", description: str = "Test", kind: str = "withdrawal") -> dict[str, Any]:
    return {
        "transactions": [{
            "type": kind,
            "amount": amount,
            "date": "2026-04-17",
            "description": description,
            "source_name": "Main Checking",
            "destination_name": "Bar",
            "category_name": "Bar",
        }]
    }


class TransactionDraftTest(unittest.TestCase):
    def test_roundtrip_serialization(self) -> None:
        draft = TransactionDraft(type="withdrawal", amount="10.00", description="Coffee")
        restored = TransactionDraft.from_dict(draft.to_dict())
        self.assertEqual(restored.type, "withdrawal")
        self.assertEqual(restored.amount, "10.00")
        self.assertEqual(restored.description, "Coffee")

    def test_from_payload(self) -> None:
        payload = _make_payload(amount="5.65", description="Spesa supermercato")
        draft = TransactionDraft.from_payload(payload)
        self.assertEqual(draft.amount, "5.65")
        self.assertEqual(draft.description, "Spesa supermercato")
        self.assertEqual(draft.type, "withdrawal")


class DraftSessionTest(unittest.TestCase):
    def test_roundtrip_serialization(self) -> None:
        session = DraftSession(
            phase=DraftPhase.REVIEW,
            drafts=[TransactionDraft(type="withdrawal", amount="10.00", description="Test")],
            language="it",
        )
        restored = DraftSession.from_dict(session.to_dict())
        self.assertEqual(restored.phase, DraftPhase.REVIEW)
        self.assertEqual(len(restored.drafts), 1)
        self.assertEqual(restored.language, "it")

    def test_is_active(self) -> None:
        session = DraftSession(phase=DraftPhase.REVIEW)
        self.assertTrue(session.is_active)
        session.phase = DraftPhase.COMMITTED
        self.assertFalse(session.is_active)

    def test_is_batch(self) -> None:
        session = DraftSession(drafts=[TransactionDraft(), TransactionDraft()])
        self.assertTrue(session.is_batch)


class DraftManagerCategoryFlowTest(unittest.TestCase):
    def _make_manager(self) -> DraftManager:
        cache = FireflyObjectCache(client=FakeClient())
        return DraftManager(
            object_cache=cache,
            category_budget_map={"Bar": "Svago", "Spesa": "Spesa mensile"},
        )

    def test_create_session_starts_at_category_confirm(self) -> None:
        manager = self._make_manager()
        session = manager.create_session([_make_payload()], language="it")
        self.assertEqual(session.phase, DraftPhase.CATEGORY_CONFIRM)

    def test_accept_category_advances_to_budget(self) -> None:
        manager = self._make_manager()
        session = manager.create_session([_make_payload()], language="it")
        response = manager.advance(session, "sì")
        self.assertIn(session.phase, {DraftPhase.BUDGET_SUGGEST, DraftPhase.REVIEW})

    def test_reject_category_shows_selection(self) -> None:
        manager = self._make_manager()
        session = manager.create_session([_make_payload()], language="it")
        response = manager.advance(session, "no")
        self.assertEqual(session.phase, DraftPhase.CATEGORY_SELECT)
        self.assertIn("Scegli", response)

    def test_select_category_by_number(self) -> None:
        manager = self._make_manager()
        session = manager.create_session([_make_payload()], language="it")
        manager.advance(session, "no")  # go to selection
        response = manager.advance(session, "2")  # select "Spesa"
        self.assertEqual(session.drafts[0].category_name, "Spesa")
        self.assertTrue(session.drafts[0].category_confirmed)
        self.assertEqual(session.drafts[0].payload["transactions"][0]["category_name"], "Spesa")

    def test_skip_category(self) -> None:
        manager = self._make_manager()
        session = manager.create_session([_make_payload()], language="it")
        manager.advance(session, "nessuna")
        self.assertIsNone(session.drafts[0].category_name)
        self.assertTrue(session.drafts[0].category_confirmed)

    def test_type_category_name_directly(self) -> None:
        manager = self._make_manager()
        session = manager.create_session([_make_payload()], language="it")
        manager.advance(session, "Sport")
        self.assertEqual(session.drafts[0].category_name, "Sport")
        self.assertTrue(session.drafts[0].category_confirmed)


class DraftManagerBudgetFlowTest(unittest.TestCase):
    def _make_manager(self) -> DraftManager:
        cache = FireflyObjectCache(client=FakeClient())
        return DraftManager(
            object_cache=cache,
            category_budget_map={"Bar": "Svago", "Spesa": "Spesa mensile"},
            skip_budget_threshold=5.0,
        )

    def test_budget_suggested_after_category(self) -> None:
        manager = self._make_manager()
        session = manager.create_session([_make_payload()], language="it")
        manager.advance(session, "sì")  # confirm category "Bar"
        self.assertEqual(session.phase, DraftPhase.BUDGET_SUGGEST)

    def test_accept_budget(self) -> None:
        manager = self._make_manager()
        session = manager.create_session([_make_payload()], language="it")
        manager.advance(session, "sì")  # confirm category
        manager.advance(session, "sì")  # accept budget
        self.assertEqual(session.phase, DraftPhase.REVIEW)
        self.assertEqual(session.drafts[0].budget_name, "Svago")
        self.assertEqual(session.drafts[0].payload["transactions"][0]["budget_name"], "Svago")

    def test_skip_budget(self) -> None:
        manager = self._make_manager()
        session = manager.create_session([_make_payload()], language="it")
        manager.advance(session, "sì")
        manager.advance(session, "no")  # skip budget
        self.assertEqual(session.phase, DraftPhase.REVIEW)
        self.assertIsNone(session.drafts[0].budget_name)

    def test_low_amount_still_suggests_budget_when_category_matches(self) -> None:
        manager = self._make_manager()
        payload = _make_payload(amount="2.00")
        session = manager.create_session([payload], language="it")
        manager.advance(session, "sì")  # confirm category
        self.assertEqual(session.phase, DraftPhase.BUDGET_SUGGEST)


class DraftManagerReviewTest(unittest.TestCase):
    def _make_manager(self) -> DraftManager:
        cache = FireflyObjectCache(client=FakeClient())
        return DraftManager(object_cache=cache, category_budget_map={})

    def test_review_message_contains_draft_fields(self) -> None:
        manager = self._make_manager()
        session = DraftSession(
            phase=DraftPhase.REVIEW,
            drafts=[TransactionDraft(type="withdrawal", amount="10.00", description="Coffee", category_name="Bar")],
            language="it",
        )
        msg = manager.build_review_message(session)
        self.assertIn("10.00", msg)
        self.assertIn("Coffee", msg)
        self.assertIn("Bar", msg)
        self.assertIn("ok", msg)

    def test_batch_review_shows_count(self) -> None:
        manager = self._make_manager()
        session = DraftSession(
            phase=DraftPhase.REVIEW,
            drafts=[
                TransactionDraft(type="withdrawal", amount="10.00", description="A"),
                TransactionDraft(type="withdrawal", amount="20.00", description="B"),
            ],
            language="it",
        )
        msg = manager.build_review_message(session)
        self.assertIn("2 transazioni", msg)


class DraftManagerCorrectionTest(unittest.TestCase):
    def _make_manager(self) -> DraftManager:
        return DraftManager()

    def test_change_amount(self) -> None:
        manager = self._make_manager()
        session = DraftSession(
            phase=DraftPhase.REVIEW,
            drafts=[TransactionDraft(amount="10.00", description="Test", payload={"transactions": [{"amount": "10.00"}]})],
            language="it",
        )
        result = manager.apply_correction(session, "cambia importo a 15.50")
        self.assertEqual(session.drafts[0].amount, "15.50")

    def test_change_category_goes_to_selection(self) -> None:
        manager = self._make_manager()
        session = DraftSession(
            phase=DraftPhase.REVIEW,
            drafts=[TransactionDraft(amount="10.00", category_confirmed=True)],
        )
        manager.apply_correction(session, "cambia categoria")
        self.assertEqual(session.phase, DraftPhase.CATEGORY_SELECT)

    def test_reread_returns_marker(self) -> None:
        manager = self._make_manager()
        session = DraftSession(phase=DraftPhase.REVIEW, drafts=[TransactionDraft()])
        result = manager.apply_correction(session, "rileggi")
        self.assertEqual(result, "__REREAD__")

    def test_is_correction_detection(self) -> None:
        manager = self._make_manager()
        self.assertTrue(manager.is_correction("cambia importo a 20"))
        self.assertTrue(manager.is_correction("rileggi"))
        self.assertTrue(manager.is_correction("change category"))
        self.assertFalse(manager.is_correction("quanti soldi ho"))


class StatePersistenceTest(unittest.TestCase):
    def test_save_and_load_draft_session(self) -> None:
        session = DraftSession(
            phase=DraftPhase.CATEGORY_CONFIRM,
            drafts=[TransactionDraft(amount="5.00", description="Test")],
            language="it",
        )
        state: dict[str, Any] = {}
        save_draft_session(state, session)
        self.assertIn("draft", state)

        loaded = load_draft_session(state)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.phase, DraftPhase.CATEGORY_CONFIRM)
        self.assertEqual(loaded.drafts[0].amount, "5.00")

    def test_load_from_old_pending_transaction(self) -> None:
        """Backward compatibility with old pending_transaction format."""
        state = {
            "pending_transaction": {
                "transactions": [{
                    "type": "withdrawal",
                    "amount": "12.50",
                    "description": "Lunch",
                }]
            }
        }
        loaded = load_draft_session(state)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.phase, DraftPhase.REVIEW)
        self.assertEqual(loaded.drafts[0].amount, "12.50")

    def test_load_from_old_pending_action(self) -> None:
        state = {
            "pending_action": {
                "kind": "category_create",
                "payload": {"name": "Test"},
                "preview": "Category draft prepared.",
            }
        }
        loaded = load_draft_session(state)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.pending_action_kind, "category_create")

    def test_discard_clears_state(self) -> None:
        session = DraftSession(phase=DraftPhase.DISCARDED)
        state: dict[str, Any] = {"draft": {"phase": "review"}}
        save_draft_session(state, session)
        self.assertNotIn("draft", state)


if __name__ == "__main__":
    unittest.main()
