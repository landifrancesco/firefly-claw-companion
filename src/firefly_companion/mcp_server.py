from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from typing import Any

from . import cli


TOOLS: list[dict[str, Any]] = [
    {
        "name": "firefly_health",
        "description": "Check Firefly III API reachability.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "firefly_accounts_list",
        "description": "List Firefly III accounts.",
        "inputSchema": {
            "type": "object",
            "properties": {"type": {"type": "string", "enum": ["all", "asset", "expense", "revenue", "liability"]}},
            "additionalProperties": False,
        },
    },
    {
        "name": "firefly_account_balances",
        "description": "Return account balances.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "firefly_categories_list",
        "description": "List Firefly III categories.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "firefly_budgets_list",
        "description": "List Firefly III budgets.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "firefly_transactions_search",
        "description": "Search recent Firefly III transactions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1},
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "firefly_summary_month",
        "description": "Summarize income and spending for a month.",
        "inputSchema": {
            "type": "object",
            "properties": {"month": {"type": "string", "description": "YYYY-MM month. Defaults to current month."}},
            "additionalProperties": False,
        },
    },
]


def _cli_json(argv: list[str]) -> dict[str, Any]:
    output = io.StringIO()
    with redirect_stdout(output):
        exit_code = cli.main(argv)
    text = output.getvalue().strip()
    payload = json.loads(text) if text else {}
    if exit_code != cli.EXIT_OK:
        raise RuntimeError(json.dumps(payload, sort_keys=True))
    return payload


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "firefly_health":
        return _cli_json(["health"])
    if name == "firefly_accounts_list":
        return _cli_json(["accounts", "list", "--type", str(arguments.get("type", "all"))])
    if name == "firefly_account_balances":
        return _cli_json(["accounts", "balances"])
    if name == "firefly_categories_list":
        return _cli_json(["categories", "list"])
    if name == "firefly_budgets_list":
        return _cli_json(["budgets", "list"])
    if name == "firefly_transactions_search":
        argv = ["transactions", "search"]
        if "days" in arguments:
            argv.extend(["--days", str(arguments["days"])])
        if arguments.get("query"):
            argv.extend(["--query", str(arguments["query"])])
        if "limit" in arguments:
            argv.extend(["--limit", str(arguments["limit"])])
        return _cli_json(argv)
    if name == "firefly_summary_month":
        argv = ["summary", "month"]
        if arguments.get("month"):
            argv.extend(["--month", str(arguments["month"])])
        return _cli_json(argv)
    raise ValueError(f"Unknown tool: {name}")


def _response(message_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": message_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    return payload


def _handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    try:
        if method == "initialize":
            return _response(
                message_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "firefly-bridge", "version": "0.1.0"},
                },
            )
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return _response(message_id, {"tools": TOOLS})
        if method == "tools/call":
            name = str(params.get("name", ""))
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            result = _call_tool(name, arguments)
            return _response(
                message_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, indent=2, sort_keys=True),
                        }
                    ]
                },
            )
        return _response(message_id, error={"code": -32601, "message": f"Method not found: {method}"})
    except Exception as exc:
        return _response(message_id, error={"code": -32000, "message": str(exc)})


def main() -> int:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = _handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
