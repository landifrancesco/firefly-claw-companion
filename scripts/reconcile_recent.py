#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from firefly_companion.bridge import dedupe_signature, flatten_transactions
from firefly_companion.client import FireflyClient
from firefly_companion.config import BridgeSettings
from firefly_companion.logging_utils import configure_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report recent duplicate candidates from Firefly III.")
    parser.add_argument("--days", type=int, default=14)
    args = parser.parse_args(argv)

    logger = configure_logging()
    settings = BridgeSettings.from_env()
    client = FireflyClient(settings=settings, logger=logger)

    from datetime import date, timedelta

    end = date.today()
    start = end - timedelta(days=args.days)
    transactions = flatten_transactions(client.list_transactions(start=start, end=end, limit=100))
    grouped: dict[str, list[dict[str, str]]] = {}
    for item in transactions:
        key = dedupe_signature(
            {
                "type": str(item.get("type", "")),
                "date": str(item.get("date", "")),
                "amount": str(item.get("amount", "")),
                "description": str(item.get("description", "")),
                "source_name": str(item.get("source_name", "")),
                "destination_name": str(item.get("destination_name", "")),
            }
        )
        grouped.setdefault(key, []).append(item)

    duplicates = [items for items in grouped.values() if len(items) > 1]
    print(json.dumps({"days": args.days, "duplicate_candidates": duplicates}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
