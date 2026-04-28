"""AI router interaction.

Handles calling the OpenClaw AI gateway for natural language understanding
when deterministic regex parsing fails, and handles screenshot analysis
via Vision models.

The gateway is accessed directly via HTTP using env vars:
  OPENCLAW_ROUTER_BASE_URL  (default: http://127.0.0.1:{OPENCLAW_PORT})
  OPENCLAW_PORT             (default: 18789)
  OPENCLAW_GATEWAY_TOKEN    (required)
  OPENCLAW_DEFAULT_MODEL    (default: openai-codex/gpt-5.4)
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any

import requests
from requests.exceptions import RequestException


class RouterGatewayError(RuntimeError):
    """Raised when the OpenClaw gateway call fails."""


# ---------------------------------------------------------------------------
# Gateway helpers
# ---------------------------------------------------------------------------

def _gateway_url() -> str:
    port = os.getenv("OPENCLAW_PORT", "18789").strip() or "18789"
    base = os.getenv("OPENCLAW_ROUTER_BASE_URL", f"http://127.0.0.1:{port}").rstrip("/")
    return f"{base}/v1/responses"


def _gateway_headers() -> dict[str, str]:
    token = os.getenv("OPENCLAW_GATEWAY_TOKEN", "").strip()
    if not token:
        raise RouterGatewayError("OPENCLAW_GATEWAY_TOKEN is not set.")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-OpenClaw-Agent-Id": "main",
    }


def _model_name() -> str:
    return os.getenv("OPENCLAW_DEFAULT_MODEL", "openai-codex/gpt-5.4").strip() or "openai-codex/gpt-5.4"


def _post_gateway(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        response = requests.post(
            _gateway_url(),
            headers=_gateway_headers(),
            json=payload,
            timeout=(10, 60),
        )
    except RequestException as exc:
        raise RouterGatewayError(f"Gateway request failed: {exc}") from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise RouterGatewayError(f"Gateway returned non-JSON: {response.text[:300]}") from exc
    if response.status_code >= 400:
        raise RouterGatewayError(str(body))
    return body


def _extract_text(response_body: dict[str, Any]) -> str:
    output_text = response_body.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    chunks: list[str] = []
    for item in response_body.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            text_val = part.get("text")
            if isinstance(text_val, str) and text_val.strip():
                chunks.append(text_val)
        text_val = item.get("text")
        if isinstance(text_val, str) and text_val.strip():
            chunks.append(text_val)
    text = "\n".join(chunk.strip() for chunk in chunks if chunk.strip()).strip()
    if not text:
        raise RouterGatewayError("Gateway returned no text output.")
    return text


def _extract_json(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON object found in router response.")
    return json.loads(text[start:end])


# ---------------------------------------------------------------------------
# Router health checking
# ---------------------------------------------------------------------------


class RouterHealthCheck:
    """Monitor router health and auto-disable on repeated failures."""

    def __init__(self) -> None:
        self.is_healthy: bool = True
        self.failure_count: int = 0
        self.disabled_until: float = 0

    def check(self) -> bool:
        """Health check: returns True if router is healthy, False if disabled or down."""
        import time

        if time.time() < self.disabled_until:
            return False

        try:
            response = requests.get(
                _gateway_url().replace("/v1/responses", "/health"),
                headers=_gateway_headers(),
                timeout=3,
            )
            if response.status_code == 200:
                self.failure_count = 0
                self.is_healthy = True
                return True
        except RequestException:
            self.failure_count += 1
            if self.failure_count >= 3:
                self.disabled_until = time.time() + 600

        self.is_healthy = False
        return False

    def status_text(self) -> str:
        """Return human-readable status."""
        import time

        if time.time() < self.disabled_until:
            remaining = int(self.disabled_until - time.time())
            return f"⏸️  Disabled ({remaining}s)"
        return "🟢 Healthy" if self.is_healthy else "🔴 Down"


router_health = RouterHealthCheck()


# ---------------------------------------------------------------------------
# Public router functions
# ---------------------------------------------------------------------------

def run_openclaw_router(service: Any, text: str, state: dict[str, Any]) -> dict[str, Any] | None:
    """Send text to OpenClaw routing completion.

    ``service`` is a BridgeService; ``state`` is the bot's mutable state dict
    (used for session tracking by the caller, not mutated here).
    """
    accounts = service.client.list_accounts("all")
    categories = service.client.list_categories()
    budgets = service.client.list_budgets()

    account_names = [str(x.get("attributes", {}).get("name") or "") for x in accounts]
    category_names = [str(x.get("attributes", {}).get("name") or "") for x in categories]
    budget_names = [str(x.get("attributes", {}).get("name") or "") for x in budgets]

    instructions = (
        "You are a Firefly III finance assistant. Parse the user's request and return ONLY a JSON object.\n"
        "Required fields: intent (string), confidence (float 0-1), params (object), reply (string).\n"
        "If the request is in Italian, the reply field must be in Italian.\n"
        f"Known account names: {account_names}\n"
        f"Known category names: {category_names}\n"
        f"Known budget names: {budget_names}\n"
    )
    payload: dict[str, Any] = {
        "model": _model_name(),
        "input": text,
        "instructions": instructions,
        "max_output_tokens": 400,
        "store": False,
    }

    try:
        body = _post_gateway(payload)
        raw_text = _extract_text(body)
        parsed = _extract_json(raw_text)
    except (RouterGatewayError, ValueError, json.JSONDecodeError):
        return None

    intent = str(parsed.get("intent", "")).strip()
    if not intent:
        return None
    return parsed


def run_receipt_router(
    service: Any,
    image_bytes: bytes,
    *,
    mime_type: str = "image/jpeg",
    caption: str | None = None,
    extracted_text: str | None = None,
) -> dict[str, Any] | None:
    """Send receipt image to OpenClaw vision completion."""
    accounts = service.client.list_accounts("all")
    categories = service.client.list_categories()

    account_names = [str(x.get("attributes", {}).get("name") or "") for x in accounts]
    category_names = [str(x.get("attributes", {}).get("name") or "") for x in categories]

    encoded = base64.b64encode(image_bytes).decode("ascii")
    instructions = (
        "You are a Firefly III receipt parser. Analyze the image and return ONLY a JSON object.\n"
        "Required fields: intent, confidence, params (with amount, description, merchant, date, category), reply.\n"
        f"Known account names: {account_names}\n"
        f"Known category names: {category_names}\n"
    )
    parts = [caption or "Analyze this receipt and prepare the safest Firefly action."]
    if extracted_text:
        parts.append(f"OCR text:\n{extracted_text}")
    user_text = "\n\n".join(part for part in parts if part)

    payload: dict[str, Any] = {
        "model": _model_name(),
        "instructions": instructions,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": user_text},
                    {"type": "input_image", "image_url": f"data:{mime_type};base64,{encoded}"},
                ],
            }
        ],
        "store": False,
    }

    try:
        body = _post_gateway(payload)
        raw_text = _extract_text(body)
        parsed = _extract_json(raw_text)
    except (RouterGatewayError, ValueError, json.JSONDecodeError):
        return None

    intent = str(parsed.get("intent", "")).strip()
    if not intent:
        return None
    return parsed


# ---------------------------------------------------------------------------
# OCR-based fallback
# ---------------------------------------------------------------------------

def build_receipt_fallback_payload(
    service: Any,
    *,
    caption: str | None = None,
    extracted_text: str | None = None,
) -> dict[str, Any] | None:
    """Build a payload from OCR text when Vision is unavailable or fails."""
    from firefly_companion.receipt_parser import extract_receipt_candidates

    candidates = extract_receipt_candidates(extracted_text, caption=caption)
    if not candidates:
        return None

    transactions = []
    for candidate in candidates:
        source_hint = candidate.pop("source_hint", None)
        topic_text = candidate.pop("topic_hint_text", None)

        source, _ = resolve_receipt_source_account(service, source_hint)
        category = resolve_receipt_fallback_category(service, topic_text)

        tx_params: dict[str, Any] = {
            "amount": candidate["amount"],
            "description": candidate["description"],
            "merchant": candidate["merchant"],
        }
        if candidate.get("notes"):
            tx_params["notes"] = candidate["notes"]
        if candidate.get("date"):
            tx_params["date"] = candidate["date"]
        if source:
            tx_params["source"] = source
        if category:
            tx_params["category"] = category

        transactions.append({"intent": candidate["intent"], "params": tx_params})

    if len(transactions) == 1:
        tx = transactions[0]
        return {"intent": tx["intent"], "confidence": 0.8, "params": tx["params"]}

    return {
        "intent": "create_transaction_batch",
        "confidence": 0.8,
        "params": {"transactions": transactions},
    }


def resolve_receipt_source_account(service: Any, hint: str | None) -> tuple[str | None, list[str]]:
    """Resolve a source account from a bank/provider hint."""
    from firefly_companion.conversation import normalize_natural_text

    accounts = service.client.list_accounts("asset")
    names = [str(a.get("attributes", {}).get("name") or "").strip() for a in accounts]
    account_names = [n for n in names if n]
    if not hint:
        return None, account_names

    hint_folded = normalize_natural_text(hint)
    exact = [n for n in account_names if normalize_natural_text(n) == hint_folded]
    if exact:
        return exact[0], account_names

    fuzzy = [
        n for n in account_names
        if hint_folded in normalize_natural_text(n) or normalize_natural_text(n) in hint_folded
    ]
    return (fuzzy[0] if len(fuzzy) == 1 else None), account_names


def resolve_receipt_fallback_category(service: Any, topic_text: str | None) -> str | None:
    """Infer a category from receipt topic text against existing Firefly categories."""
    from firefly_companion.receipt_parser import infer_receipt_category

    if not topic_text:
        return None
    categories = service.client.list_categories()
    category_names = [str(c.get("attributes", {}).get("name") or "").strip() for c in categories]
    return infer_receipt_category(topic_text, [n for n in category_names if n])
