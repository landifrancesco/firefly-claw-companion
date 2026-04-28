#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from firefly_companion.client import FireflyClient
from firefly_companion.config import BridgeSettings
from firefly_companion.logging_utils import configure_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Emit a month budget report from Firefly III.")
    parser.add_argument("--month", help="Month in YYYY-MM format. Defaults to current month.")
    args = parser.parse_args(argv)

    logger = configure_logging()
    settings = BridgeSettings.from_env()
    client = FireflyClient(settings=settings, logger=logger)

    from datetime import date
    from firefly_companion.bridge import month_window

    reference = date.fromisoformat(f"{args.month}-01") if args.month else None
    start, end = month_window(reference)

    summary = client.summary_basic(start=start, end=end)
    budgets = client.list_budgets()

    print(
        json.dumps(
            {
                "month": start.strftime("%Y-%m"),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "summary": summary,
                "budgets": budgets,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
