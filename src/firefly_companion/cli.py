from __future__ import annotations

import argparse
from decimal import Decimal
import json
import sys
from typing import Any

from .bridge import BridgeService, BridgeValidationError
from .client import FireflyAPIError, FireflyClient
from .config import BridgeSettings, ConfigurationError
from .logging_utils import configure_logging


EXIT_OK = 0
EXIT_CONFIG = 10
EXIT_API = 11
EXIT_VALIDATION = 12
EXIT_DUPLICATE = 20
EXIT_CONFIRMATION = 21


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Object of type {type(value)!r} is not JSON serializable")


def emit(payload: dict[str, Any], exit_code: int = EXIT_OK) -> int:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True, default=_json_default)
    sys.stdout.write("\n")
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="firefly-bridge", description="Deterministic Firefly III REST bridge.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logs on stderr.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("health")

    accounts = subparsers.add_parser("accounts")
    accounts_sub = accounts.add_subparsers(dest="accounts_command", required=True)
    accounts_list = accounts_sub.add_parser("list")
    accounts_list.add_argument("--type", default="all", choices=["all", "asset", "expense", "revenue", "liability"])
    accounts_sub.add_parser("balances")

    categories = subparsers.add_parser("categories")
    categories_sub = categories.add_subparsers(dest="categories_command", required=True)
    categories_sub.add_parser("list")

    budgets = subparsers.add_parser("budgets")
    budgets_sub = budgets.add_subparsers(dest="budgets_command", required=True)
    budgets_sub.add_parser("list")

    transactions = subparsers.add_parser("transactions")
    transactions_sub = transactions.add_subparsers(dest="transactions_command", required=True)
    transactions_search = transactions_sub.add_parser("search")
    transactions_search.add_argument("--days", type=int, default=7)
    transactions_search.add_argument("--query")
    transactions_search.add_argument("--limit", type=int, default=50)

    summary = subparsers.add_parser("summary")
    summary_sub = summary.add_subparsers(dest="summary_command", required=True)
    summary_month = summary_sub.add_parser("month")
    summary_month.add_argument("--month", help="Month in YYYY-MM format. Defaults to current month.")

    for noun, transaction_kind in [("expense", "withdrawal"), ("income", "deposit"), ("transfer", "transfer")]:
        noun_parser = subparsers.add_parser(noun)
        noun_sub = noun_parser.add_subparsers(dest=f"{noun}_command", required=True)
        for action in ("create", "dry-run"):
            action_parser = noun_sub.add_parser(action)
            action_parser.set_defaults(transaction_kind=transaction_kind, force_dry_run=(action == "dry-run"))
            action_parser.add_argument("--amount", required=True)
            action_parser.add_argument("--description", required=True)
            action_parser.add_argument("--date", dest="transaction_date")
            action_parser.add_argument("--source")
            action_parser.add_argument("--destination")
            action_parser.add_argument("--category")
            action_parser.add_argument("--budget")
            action_parser.add_argument("--merchant")
            action_parser.add_argument("--currency-code")
            action_parser.add_argument("--notes")
            action_parser.add_argument("--tag", dest="tags", action="append", default=[])
            action_parser.add_argument("--no-dry-run", action="store_true", help="Override default dry-run policy.")
            action_parser.add_argument(
                "--yes-high-value",
                action="store_true",
                help="Explicitly confirm creation above the high-value threshold.",
            )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logger = configure_logging(args.verbose)

    try:
        settings = BridgeSettings.from_env()
        client = FireflyClient(settings=settings, logger=logger)
        bridge = BridgeService(client=client, settings=settings, logger=logger)

        if args.command == "health":
            payload = client.health()
            return emit({"status": "ok", "firefly": payload})

        if args.command == "accounts":
            if args.accounts_command == "list":
                return emit({"accounts": client.list_accounts(args.type), "type": args.type})
            return emit({"balances": bridge.account_balances()})

        if args.command == "categories":
            return emit({"categories": client.list_categories()})

        if args.command == "budgets":
            return emit({"budgets": client.list_budgets()})

        if args.command == "transactions":
            transactions = bridge.recent_transactions(days=args.days, query=args.query, limit=args.limit)
            return emit({"days": args.days, "query": args.query, "transactions": transactions})

        if args.command == "summary":
            return emit(bridge.monthly_summary(args.month))

        dry_run = settings.default_dry_run and not args.no_dry_run
        dry_run = dry_run or args.force_dry_run

        payload = bridge.build_transaction(
            transaction_kind=args.transaction_kind,
            amount=args.amount,
            description=args.description,
            transaction_date=args.transaction_date,
            source_name=args.source,
            destination_name=args.destination,
            category_name=args.category,
            budget_name=args.budget,
            notes=args.notes,
            tags=args.tags,
            merchant=args.merchant,
            currency_code=args.currency_code,
        )
        result = bridge.commit_transaction(payload, dry_run=dry_run, confirm_high_value=args.yes_high_value)

        if result["status"] == "duplicate_blocked":
            return emit(result, EXIT_DUPLICATE)
        if result["status"] == "dry_run":
            return emit(result, EXIT_OK)
        return emit(result, EXIT_OK)

    except ConfigurationError as exc:
        logger.error("%s", exc)
        return emit({"status": "configuration_error", "error": str(exc)}, EXIT_CONFIG)
    except BridgeValidationError as exc:
        logger.error("%s", exc)
        exit_code = EXIT_CONFIRMATION if "high-value threshold" in str(exc) else EXIT_VALIDATION
        return emit({"status": "validation_error", "error": str(exc)}, exit_code)
    except FireflyAPIError as exc:
        logger.error("%s", exc)
        return emit({"status": "api_error", "error": str(exc)}, EXIT_API)


if __name__ == "__main__":
    raise SystemExit(main())
