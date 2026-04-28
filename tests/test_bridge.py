from __future__ import annotations

import unittest
from decimal import Decimal

from firefly_companion.bridge import dedupe_signature, normalize_amount


class BridgeHelpersTest(unittest.TestCase):
    def test_normalize_amount_rounds_deterministically(self) -> None:
        self.assertEqual(normalize_amount("12.345"), "12.34")
        self.assertEqual(normalize_amount(Decimal("12.349")), "12.35")

    def test_dedupe_signature_ignores_spacing(self) -> None:
        first = dedupe_signature(
            {
                "type": "withdrawal",
                "date": "2026-04-15T12:00:00+00:00",
                "amount": "10.00",
                "description": "Lunch  with team",
                "source_name": "Checking",
                "destination_name": "Restaurant",
            }
        )
        second = dedupe_signature(
            {
                "type": "withdrawal",
                "date": "2026-04-15",
                "amount": "10.0",
                "description": "Lunch with team",
                "source_name": "Checking",
                "destination_name": "Restaurant",
            }
        )
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
