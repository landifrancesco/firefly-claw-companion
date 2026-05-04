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


