"""AI router - direct provider calls.

Calls AI providers directly for text inference and vision.
Env vars:
  PICOCLAW_DEFAULT_MODEL  provider/model-name (e.g. gemini/gemini-2.5-flash, anthropic/claude-sonnet-4-6)
  OPENAI_API_KEY / OPENAI_API_KEYS  (for openai/ and openai-codex/ models)
  ANTHROPIC_API_KEY       (for anthropic/ models)
  OPENROUTER_API_KEY      (for openrouter/ models)
  GROQ_API_KEY            (for groq/ models)
  GOOGLE_API_KEY          (for google/ models)
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any

import requests
from requests.exceptions import RequestException


class RouterGatewayError(RuntimeError):
    """Raised when an AI provider call fails."""


# ---------------------------------------------------------------------------
# Provider helpers
# ---------------------------------------------------------------------------

def _provider_and_model() -> tuple[str, str]:
    raw = os.getenv("PICOCLAW_DEFAULT_MODEL", "gemini/gemini-2.5-flash").strip() or "gemini/gemini-2.5-flash"
    if "/" in raw:
        provider, model = raw.split("/", 1)
        return provider.lower(), model
    return "openai", raw


def _api_key_for(provider: str) -> str:
    if provider in ("openai", "openai-codex"):
        key = os.getenv("OPENAI_API_KEY", "").strip()
        if not key:
            keys_csv = os.getenv("OPENAI_API_KEYS", "").strip()
            if keys_csv:
                key = keys_csv.split(",")[0].strip()
        if not key:
            raise RouterGatewayError("OPENAI_API_KEY is not set.")
        return key
    if provider == "anthropic":
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise RouterGatewayError("ANTHROPIC_API_KEY is not set.")
        return key
    if provider == "openrouter":
        key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise RouterGatewayError("OPENROUTER_API_KEY is not set.")
        return key
    if provider in {"google", "gemini"}:
        key = os.getenv("GOOGLE_API_KEY", "").strip()
        if not key:
            raise RouterGatewayError("GOOGLE_API_KEY is not set.")
        return key
    if provider == "groq":
        key = os.getenv("GROQ_API_KEY", "").strip()
        if not key:
            raise RouterGatewayError("GROQ_API_KEY is not set.")
        return key
    raise RouterGatewayError(
        f"Unsupported AI provider: {provider!r}. Set PICOCLAW_DEFAULT_MODEL to a supported format (e.g. gemini/gemini-2.5-flash)."
    )


def _chat_completions_url_for(provider: str) -> str:
    urls: dict[str, str] = {
        "openai": "https://api.openai.com/v1/chat/completions",
        "openai-codex": "https://api.openai.com/v1/chat/completions",
        "openrouter": "https://openrouter.ai/api/v1/chat/completions",
        "groq": "https://api.groq.com/openai/v1/chat/completions",
        "google": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    }
    return urls.get(provider, "https://api.openai.com/v1/chat/completions")


# ---------------------------------------------------------------------------
# Low-level API helpers
# ---------------------------------------------------------------------------

def _post_chat_completions(
    model: str,
    messages: list[dict[str, Any]],
    url: str,
    api_key: str,
    max_tokens: int = 400,
) -> str:
    payload: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        resp = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=(10, 60),
        )
    except RequestException as exc:
        raise RouterGatewayError(f"Provider request failed: {exc}") from exc
    try:
        body = resp.json()
    except ValueError as exc:
        raise RouterGatewayError(f"Provider returned non-JSON: {resp.text[:300]}") from exc
    if resp.status_code >= 400:
        raise RouterGatewayError(str(body))
    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise RouterGatewayError(f"Unexpected response shape: {body}") from exc


def _post_anthropic(
    model: str,
    system: str,
    user_content: list[dict[str, Any]],
    api_key: str,
    max_tokens: int = 400,
) -> str:
    payload: dict[str, Any] = {
        "model": model,
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
        "max_tokens": max_tokens,
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=payload,
            timeout=(10, 60),
        )
    except RequestException as exc:
        raise RouterGatewayError(f"Anthropic request failed: {exc}") from exc
    try:
        body = resp.json()
    except ValueError as exc:
        raise RouterGatewayError(f"Anthropic returned non-JSON: {resp.text[:300]}") from exc
    if resp.status_code >= 400:
        raise RouterGatewayError(str(body))
    try:
        return body["content"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise RouterGatewayError(f"Unexpected Anthropic response: {body}") from exc


# ---------------------------------------------------------------------------
# Public call helpers
# ---------------------------------------------------------------------------

def call_ai_text(instructions: str, user_text: str, max_tokens: int = 400) -> str:
    """Call the configured AI provider with a text prompt. Returns the response text."""
    provider, model = _provider_and_model()
    api_key = _api_key_for(provider)
    if provider == "anthropic":
        return _post_anthropic(model, instructions, [{"type": "text", "text": user_text}], api_key, max_tokens)
    messages = [{"role": "system", "content": instructions}, {"role": "user", "content": user_text}]
    return _post_chat_completions(model, messages, _chat_completions_url_for(provider), api_key, max_tokens)


def call_ai_vision(
    instructions: str,
    user_text: str,
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    max_tokens: int = 400,
) -> str:
    """Call the configured AI provider with an image + text prompt. Returns the response text."""
    provider, model = _provider_and_model()
    api_key = _api_key_for(provider)
    encoded = base64.b64encode(image_bytes).decode("ascii")
    if provider == "anthropic":
        user_content: list[dict[str, Any]] = [
            {"type": "text", "text": user_text},
            {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": encoded}},
        ]
        return _post_anthropic(model, instructions, user_content, api_key, max_tokens)
    openai_content: list[dict[str, Any]] = [
        {"type": "text", "text": user_text},
        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}},
    ]
    messages = [{"role": "system", "content": instructions}, {"role": "user", "content": openai_content}]
    return _post_chat_completions(model, messages, _chat_completions_url_for(provider), api_key, max_tokens)


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
    """Track recent AI call failures and auto-disable on repeated errors."""

    def __init__(self) -> None:
        self.is_healthy: bool = True
        self.failure_count: int = 0
        self.disabled_until: float = 0

    def check(self) -> bool:
        import time
        if time.time() < self.disabled_until:
            return False
        try:
            provider, _ = _provider_and_model()
            _api_key_for(provider)
            self.is_healthy = True
            return True
        except RouterGatewayError:
            self.failure_count += 1
            if self.failure_count >= 3:
                self.disabled_until = time.time() + 600
            self.is_healthy = False
            return False

    def record_failure(self) -> None:
        import time
        self.failure_count += 1
        if self.failure_count >= 3:
            self.disabled_until = time.time() + 600
        self.is_healthy = False

    def record_success(self) -> None:
        self.failure_count = 0
        self.is_healthy = True

    def status_text(self) -> str:
        import time
        if time.time() < self.disabled_until:
            remaining = int(self.disabled_until - time.time())
            return f"⏸️  Disabled ({remaining}s)"
        return "\U0001f7e2 Healthy" if self.is_healthy else "\U0001f534 Down"


router_health = RouterHealthCheck()


# ---------------------------------------------------------------------------
# Public router functions
# ---------------------------------------------------------------------------

def run_picoclaw_router(service: Any, text: str, state: dict[str, Any]) -> dict[str, Any] | None:
    """Send text to AI for intent routing. Returns parsed JSON or None on failure."""
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

    try:
        raw_text = call_ai_text(instructions, text, max_tokens=400)
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
    """Send receipt image to AI vision for parsing."""
    accounts = service.client.list_accounts("all")
    categories = service.client.list_categories()

    account_names = [str(x.get("attributes", {}).get("name") or "") for x in accounts]
    category_names = [str(x.get("attributes", {}).get("name") or "") for x in categories]

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

    try:
        raw_text = call_ai_vision(instructions, user_text, image_bytes, mime_type, max_tokens=400)
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
