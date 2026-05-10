"""Stable contracts between ``docker-compose.yml``, ``docker/entrypoint.sh``, and bootstrap docs."""

from __future__ import annotations

# Must match ENTRYPOINT_BUILD_MARKER in docker/entrypoint.sh line-for-line assignment.
ENTRYPOINT_MARKER = 'ENTRYPOINT_BUILD_MARKER="config-scrub-v10"'

# PicoClaw must not ingest these as flattened config/env keys — kept in scrub + unwind passes.
FORBIDDEN_FLAT_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "telegram_bot_token",
        "openai_api_key",
        "anthropic_api_key",
        "openrouter_api_key",
        "groq_api_key",
        "google_api_key",
        "pdfapihub_api_key",
    }
)

# Environment keys injected into the firefly-bridge MCP server stanza — keep aligned with
# ``scripts/setup_wizard.py`` (`seed_picoclaw_config` firefly-bridge env).
FIREFLY_BRIDGE_MCP_ENV_KEYS: tuple[str, ...] = (
    "FIREFLY_BASE_URL",
    "FIREFLY_API_BASE_PATH",
    "FIREFLY_TIMEOUT_SECONDS",
    "FIREFLY_REQUEST_RETRIES",
    "FIREFLY_RETRY_BACKOFF_SECONDS",
    "FIREFLY_VERIFY_TLS",
    "FIREFLY_FORCE_CONNECTION_CLOSE",
    "FIREFLY_DEFAULT_DRY_RUN",
    "FIREFLY_HIGH_VALUE_THRESHOLD",
    "FIREFLY_DEDUPE_WINDOW_DAYS",
    "FIREFLY_ALLOW_DELETE",
    "FIREFLY_MAPPINGS_PATH",
    "FIREFLY_POLICY_PATH",
    "FIREFLY_ACCESS_TOKEN_FILE",
)

# Compose ``companion.environment`` logical groups — order within file should follow this grouping.
COMPANION_ENV_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("core", ("TZ",)),
    (
        "picoclaw",
        (
            "PICOCLAW_PORT",
            "PICOCLAW_GATEWAY_HOST",
            "PICOCLAW_LOG_LEVEL",
            "PICOCLAW_HEALTHCHECK_INTERVAL",
            "PICOCLAW_HEALTHCHECK_TIMEOUT",
            "PICOCLAW_HEALTHCHECK_RETRIES",
            "PICOCLAW_HEALTHCHECK_START_PERIOD",
            "PICOCLAW_DEFAULT_MODEL_NAME",
            "PICOCLAW_DEFAULT_MODEL",
            "PICOCLAW_HOME",
            "PICOCLAW_WORKSPACE",
        ),
    ),
    (
        "firefly",
        (
            "FIREFLY_BASE_URL",
            "FIREFLY_API_BASE_PATH",
            "FIREFLY_ACCESS_TOKEN",
            "FIREFLY_TIMEOUT_SECONDS",
            "FIREFLY_REQUEST_RETRIES",
            "FIREFLY_RETRY_BACKOFF_SECONDS",
            "FIREFLY_VERIFY_TLS",
            "FIREFLY_FORCE_CONNECTION_CLOSE",
            "FIREFLY_DEFAULT_DRY_RUN",
            "FIREFLY_HIGH_VALUE_THRESHOLD",
            "FIREFLY_DEDUPE_WINDOW_DAYS",
            "FIREFLY_ALLOW_DELETE",
            "FIREFLY_RUNTIME_VERIFY_ON_BOOT",
            "FIREFLY_ACCESS_TOKEN_EXPIRES_ON",
            "FIREFLY_TOKEN_REMINDER_DAYS",
            "FIREFLY_TOKEN_REMINDER_CHECK_INTERVAL_SECONDS",
            "FIREFLY_MAPPINGS_PATH",
            "FIREFLY_POLICY_PATH",
        ),
    ),
    (
        "telegram",
        (
            "TELEGRAM_ENABLED",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_OWNER_ID",
            "TELEGRAM_TARGET_ID",
            "TELEGRAM_STARTUP_HEALTHCHECK_ENABLED",
            "TELEGRAM_LIVE_PING_ENABLED",
            "TELEGRAM_LIVE_PING_TIME",
            "TELEGRAM_SETUP_ACCOUNT_LIMIT",
            "PICOCLAW_TELEGRAM_CHANNEL_ENABLED",
        ),
    ),
    (
        "i18n_and_ocr",
        (
            "FIREFLY_CHAT_LANGUAGE",
            "FIREFLY_RECEIPT_AI_OCR",
            "FIREFLY_PDF_OCR_PROVIDER_ENABLED",
            "PDFAPIHUB_API_KEY",
            "PDFAPIHUB_BASE_URL",
            "FIREFLY_PDF_OCR_LANG",
        ),
    ),
    (
        "llm_provider_keys",
        (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENROUTER_API_KEY",
            "GROQ_API_KEY",
            "GOOGLE_API_KEY",
        ),
    ),
)
