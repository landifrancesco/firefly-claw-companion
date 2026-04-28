"""Tests for the receipt_parser module."""
from __future__ import annotations

import unittest
from pathlib import Path

from firefly_companion.receipt_parser import (
    count_visible_transactions,
    detect_receipt_source_hint,
    extract_receipt_candidate,
    extract_receipt_candidates,
    infer_receipt_category,
    infer_receipt_description,
    titlecase_merchant,
    validate_amount,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "ocr"


class ValidateAmountTest(unittest.TestCase):
    def test_valid_amount(self) -> None:
        self.assertEqual(validate_amount("10.50"), "10.50")

    def test_comma_decimal(self) -> None:
        self.assertEqual(validate_amount("10,50"), "10.50")

    def test_zero_rejected(self) -> None:
        self.assertIsNone(validate_amount("0.00"))
        self.assertIsNone(validate_amount("0"))

    def test_negative_rejected(self) -> None:
        self.assertIsNone(validate_amount("-5.00"))

    def test_none_rejected(self) -> None:
        self.assertIsNone(validate_amount(None))

    def test_empty_rejected(self) -> None:
        self.assertIsNone(validate_amount(""))

    def test_huge_amount_rejected(self) -> None:
        self.assertIsNone(validate_amount("999999.00"))

    def test_normal_large_amount_accepted(self) -> None:
        self.assertEqual(validate_amount("5000.00"), "5000.00")

    def test_rounds_to_cents(self) -> None:
        self.assertEqual(validate_amount("12.345"), "12.35")


class CountVisibleTransactionsTest(unittest.TestCase):
    def test_single_pos_payment(self) -> None:
        text = "Pagamento POS di 10,00 EUR presso Bar Centrale"
        self.assertEqual(count_visible_transactions(text), 1)

    def test_three_pos_payments(self) -> None:
        text = "\n".join([
            "Pagamento POS di 0,45 EUR presso Argenta",
            "Pagamento POS di 90,00 EUR presso Piscina",
            "Pagamento POS di 5,65 EUR presso Supermercato",
        ])
        self.assertEqual(count_visible_transactions(text), 3)

    def test_revolut_blocks(self) -> None:
        text = "\n".join([
            "Revolut Caffè 1,20 EUR",
            "Revolut Supermercato 15,50 EUR",
        ])
        self.assertEqual(count_visible_transactions(text), 2)

    def test_empty_text_returns_zero(self) -> None:
        self.assertEqual(count_visible_transactions(""), 0)

    def test_no_financial_content_returns_zero(self) -> None:
        self.assertEqual(count_visible_transactions("Hello world"), 0)

    def test_ai_count_overrides_when_higher(self) -> None:
        text = "Pagamento POS di 10,00 EUR"
        self.assertEqual(count_visible_transactions(text, ai_vision_count=3), 3)

    def test_ai_count_ignored_when_lower(self) -> None:
        text = "\n".join([
            "Pagamento POS di 10,00 EUR presso A",
            "Pagamento POS di 20,00 EUR presso B",
        ])
        self.assertEqual(count_visible_transactions(text, ai_vision_count=1), 2)

    def test_mixed_pos_and_amounts(self) -> None:
        text = "\n".join([
            "Pagamento POS di 5,00 EUR presso Bar",
            "Revolut Supermercato 12,50 EUR",
        ])
        count = count_visible_transactions(text)
        self.assertGreaterEqual(count, 2)


class ExtractReceiptCandidatesTest(unittest.TestCase):
    def test_supermarket_receipt(self) -> None:
        candidate = extract_receipt_candidate(
            "\n".join([
                "IN'S mercato",
                "DOCUMENTO COMMERCIALE",
                "TOTALE COMPLESSIVO EUR 5.65",
                "16.04.26 18:34",
            ])
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["amount"], "5.65")

    def test_three_pos_notifications(self) -> None:
        candidates = extract_receipt_candidates(
            "\n".join([
                "Gruppo BPER Banca",
                "Pagamento POS di 0,45 EUR presso Gruppo Argenta S.P.A.",
                "Gruppo BPER Banca",
                "Pagamento POS di 90,00 EUR presso SOCIETA' NUOTATORI PAD.",
                "Revolut",
                "Supermercato In's",
                "5,65 EUR",
            ])
        )
        self.assertEqual(len(candidates), 3)
        amounts = [c["amount"] for c in candidates]
        self.assertEqual(amounts, ["0.45", "90.00", "5.65"])

    def test_amount_zero_rejected_from_ocr_garbage(self) -> None:
        candidate = extract_receipt_candidate("Total EUR 0.00")
        self.assertIsNone(candidate)

    def test_revolut_notification(self) -> None:
        candidate = extract_receipt_candidate("Revolut\nBar Centrale\n2,50 EUR")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["amount"], "2.50")

    def test_supermarket_realistic_ocr_fixture(self) -> None:
        text = (FIXTURE_DIR / "supermarket_receipt_ocr.txt").read_text(encoding="utf-8")
        candidate = extract_receipt_candidate(text)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["amount"], "4.16")

    def test_pos_slip_realistic_ocr_fixture(self) -> None:
        text = (FIXTURE_DIR / "pos_slip_ocr.txt").read_text(encoding="utf-8")
        candidate = extract_receipt_candidate(text)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["amount"], "12.30")
        self.assertIn("Bar", candidate["merchant"])

    def test_multi_notification_realistic_ocr_fixture(self) -> None:
        text = (FIXTURE_DIR / "bper_notifications_3x_ocr.txt").read_text(encoding="utf-8")
        candidates = extract_receipt_candidates(text)
        self.assertEqual(len(candidates), 3)
        self.assertEqual([item["amount"] for item in candidates], ["0.45", "90.00", "5.65"])


class SourceHintTest(unittest.TestCase):
    def test_detect_revolut(self) -> None:
        self.assertEqual(detect_receipt_source_hint("Revolut payment"), "Revolut")

    def test_detect_bper(self) -> None:
        self.assertEqual(detect_receipt_source_hint("Gruppo BPER Banca"), "BPER")

    def test_no_hint(self) -> None:
        self.assertIsNone(detect_receipt_source_hint("Random text"))


class TopicInferenceTest(unittest.TestCase):
    def test_supermarket_category(self) -> None:
        cat = infer_receipt_category("Supermercato In's", ["Spesa", "Bar"])
        self.assertEqual(cat, "Spesa")

    def test_coffee_category(self) -> None:
        cat = infer_receipt_category("Argenta caffè", ["Spesa", "Bar", "Sport"])
        self.assertEqual(cat, "Bar")

    def test_sport_category(self) -> None:
        cat = infer_receipt_category("Nuotatori padova", ["Sport", "Bar"])
        self.assertEqual(cat, "Sport")

    def test_no_match_returns_none(self) -> None:
        cat = infer_receipt_category("Random merchant", ["Spesa", "Bar"])
        self.assertIsNone(cat)

    def test_description_inference(self) -> None:
        desc = infer_receipt_description("Supermercato", language="it")
        self.assertEqual(desc, "Spesa supermercato")

    def test_description_inference_en(self) -> None:
        desc = infer_receipt_description("Supermercato", language="en")
        self.assertEqual(desc, "Grocery shopping")


class TitlecaseMerchantTest(unittest.TestCase):
    def test_uppercase_gets_titlecased(self) -> None:
        self.assertEqual(titlecase_merchant("SUPERMERCATO IN'S"), "Supermercato In'S")

    def test_none_returns_none(self) -> None:
        self.assertIsNone(titlecase_merchant(None))

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(titlecase_merchant(""))


if __name__ == "__main__":
    unittest.main()
