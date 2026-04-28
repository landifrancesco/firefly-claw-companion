#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from firefly_companion.client import FireflyClient
from firefly_companion.config import BridgeSettings
from firefly_companion.logging_utils import configure_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a starter mappings.yml from live Firefly III data.")
    parser.add_argument("--output", default="workspace/config/mappings.generated.yml")
    args = parser.parse_args(argv)

    logger = configure_logging()
    settings = BridgeSettings.from_env()
    client = FireflyClient(settings=settings, logger=logger)

    accounts = client.list_accounts("asset")
    categories = client.list_categories()
    budgets = client.list_budgets()

    payload = {
        "defaults": {
            "expense_source_account": accounts[0]["attributes"]["name"] if accounts else "Main Checking",
            "expense_destination_account": "Misc Expenses",
            "income_source_account": "Income Source",
            "income_destination_account": accounts[0]["attributes"]["name"] if accounts else "Main Checking",
        },
        "account_aliases": {
            item["attributes"]["name"].casefold().replace(" ", "_"): item["attributes"]["name"]
            for item in accounts[:20]
        },
        "category_aliases": {
            item["attributes"]["name"].casefold().replace(" ", "_"): item["attributes"]["name"]
            for item in categories[:20]
        },
        "budget_aliases": {
            item["attributes"]["name"].casefold().replace(" ", "_"): item["attributes"]["name"]
            for item in budgets[:20]
        },
        "merchant_rules": {},
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    print(str(output_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
