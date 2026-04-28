#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
from typing import Iterable

import requests
from requests.exceptions import RequestException


HOST_ROOT = Path(os.getenv("SETUP_HOST_ROOT", Path.cwd()))
ENV_PATH = HOST_ROOT / ".env"
SECRETS_DIR = HOST_ROOT / "secrets"
WORKSPACE_CONFIG_DIR = HOST_ROOT / "workspace" / "config"
OPENCLAW_HOME = Path(os.getenv("OPENCLAW_HOME", str(Path.home() / ".openclaw")))
BASE_CONFIG_PATH = Path("/opt/firefly-openclaw/config/openclaw.base.json5")


@dataclass(slots=True)
class ProviderChoice:
    key: str
    label: str
    default_model: str
    env_updates: dict[str, str]
    requires_secret_prompt: bool = False
    secret_env_name: str | None = None
    secret_prompt: str | None = None
    requires_oauth_login: bool = False
    oauth_provider: str | None = None


PROVIDERS: list[ProviderChoice] = [
    ProviderChoice(
        key="codex",
        label="Codex OAuth (OpenAI Codex)",
        default_model="openai-codex/gpt-5.4",
        env_updates={},
        requires_oauth_login=True,
        oauth_provider="openai-codex",
    ),
    ProviderChoice(
        key="openai",
        label="OpenAI API key",
        default_model="openai/gpt-5.4",
        env_updates={},
        requires_secret_prompt=True,
        secret_env_name="OPENAI_API_KEY",
        secret_prompt="OpenAI API key",
    ),
    ProviderChoice(
        key="anthropic",
        label="Anthropic API key",
        default_model="anthropic/claude-sonnet-4-6",
        env_updates={},
        requires_secret_prompt=True,
        secret_env_name="ANTHROPIC_API_KEY",
        secret_prompt="Anthropic API key",
    ),
    ProviderChoice(
        key="openrouter",
        label="OpenRouter API key",
        default_model="openrouter/anthropic/claude-sonnet-4-5",
        env_updates={},
        requires_secret_prompt=True,
        secret_env_name="OPENROUTER_API_KEY",
        secret_prompt="OpenRouter API key",
    ),
]


MANAGED_ENV_ORDER = [
    "TZ",
    "FIREFLY_DOCKER_NETWORK",
    "FIREFLY_BASE_URL",
    "FIREFLY_API_BASE_PATH",
    "OPENCLAW_PORT",
    "OPENCLAW_BIND",
    "OPENCLAW_DEFAULT_MODEL",
    "FIREFLY_TIMEOUT_SECONDS",
    "FIREFLY_VERIFY_TLS",
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
    "TELEGRAM_ENABLED",
    "TELEGRAM_OWNER_ID",
    "TELEGRAM_TARGET_ID",
    "TELEGRAM_DM_POLICY",
    "FIREFLY_CHAT_LANGUAGE",
    "FIREFLY_RECEIPT_AI_OCR",
    "FIREFLY_PDF_OCR_PROVIDER_ENABLED",
    "PDFAPIHUB_API_KEY",
    "PDFAPIHUB_BASE_URL",
    "FIREFLY_PDF_OCR_LANG",
    "OPENCLAW_TELEGRAM_CHANNEL_ENABLED",
    "OPENAI_API_KEY",
    "OPENAI_API_KEYS",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "GOOGLE_API_KEY",
    "AI_GATEWAY_API_KEY",
    "EXAMPLE_FIREFLY_APP_URL",
    "EXAMPLE_FIREFLY_DB_NAME",
    "EXAMPLE_FIREFLY_DB_USER",
    "EXAMPLE_FIREFLY_DB_PASSWORD",
    "EXAMPLE_FIREFLY_SITE_OWNER",
]


def print_banner() -> None:
    print(
        r"""
+--------------------------------------------------------------+
|                  FIREFLY OPENCLAW COMPANION                  |
|                      FIRST-RUN SETUP                         |
+--------------------------------------------------------------+
| This wizard writes host-side config and secrets for:         |
|   - Firefly III REST bridge                                  |
|   - OpenClaw model/provider setup                            |
|   - Telegram control channel                                 |
+--------------------------------------------------------------+
"""
    )


def print_section(title: str) -> None:
    line = "+" + "-" * 62 + "+"
    print("")
    print(line)
    print(f"| {title[:58].ljust(58)} |")
    print(line)


def print_hint(lines: list[str]) -> None:
    print("")
    print("  Hints:")
    for line in lines:
        print(f"    - {line}")


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def prompt_text(
    message: str,
    *,
    default: str | None = None,
    secret_input: bool = False,
    allow_blank: bool = False,
    plain_secrets: bool = False,
) -> str:
    import getpass

    suffix = f" [{default}]" if default not in (None, "") else ""
    while True:
        prompt_value = f"{message}{suffix}: "
        value = getpass.getpass(prompt_value) if secret_input and not plain_secrets else input(prompt_value)
        value = value.strip()
        if not value and default is not None:
            return default
        if value or allow_blank:
            return value
        print("A value is required.", file=sys.stderr)


def prompt_choice(message: str, options: Iterable[tuple[str, str]], *, default: str) -> str:
    rendered = list(options)
    print(message)
    for key, label in rendered:
        marker = " (default)" if key == default else ""
        print(f"  {key}. {label}{marker}")
    while True:
        value = input(f"Choice [{default}]: ").strip() or default
        for key, _label in rendered:
            if value == key:
                return value
        print("Choose one of the listed options.", file=sys.stderr)


def prompt_bool(message: str, *, default: bool) -> bool:
    default_label = "Y/n" if default else "y/N"
    while True:
        value = input(f"{message} [{default_label}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Please answer yes or no.", file=sys.stderr)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_secret(path: Path, value: str) -> None:
    ensure_parent(path)
    path.write_text(value.strip() + ("\n" if value.strip() else ""), encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def write_env_file(path: Path, values: dict[str, str]) -> None:
    ensure_parent(path)
    lines = ["# Generated by scripts/setup_wizard.py", ""]
    for key in MANAGED_ENV_ORDER:
        lines.append(f"{key}={values.get(key, '')}")
    remaining = sorted(set(values).difference(MANAGED_ENV_ORDER))
    if remaining:
        lines.extend(["", "# Unmanaged existing values"])
        for key in remaining:
            lines.append(f"{key}={values[key]}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_openclaw_config_scaffold() -> None:
    OPENCLAW_HOME.mkdir(parents=True, exist_ok=True)
    base_target = OPENCLAW_HOME / "openclaw.base.json5"
    include_target = OPENCLAW_HOME / "openclaw.json"
    if BASE_CONFIG_PATH.exists() and not base_target.exists():
        base_target.write_text(BASE_CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    if not include_target.exists():
        include_target.write_text(
            "{\n  $include: [\"./openclaw.base.json5\", \"./openclaw.runtime.json5\"],\n}\n",
            encoding="utf-8",
        )


def verify_firefly(base_url: str, api_path: str, token: str, timeout_seconds: float, verify_tls: bool) -> None:
    url = f"{base_url.rstrip('/')}{api_path}/about"
    response = requests.get(
        url,
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        timeout=timeout_seconds,
        verify=verify_tls,
    )
    response.raise_for_status()
    payload = response.json()
    version = payload.get("data", {}).get("version") or payload.get("version") or "unknown"
    print(f"Firefly III API check succeeded against {url} (version: {version}).")


def verify_telegram(bot_token: str, target_id: str) -> None:
    test_message = (
        "firefly-openclaw-companion setup test: Telegram is configured.\n"
        "You should receive this message now.\n"
        "If you do not receive it, your Telegram bot token or chat ID is wrong."
    )
    base = f"https://api.telegram.org/bot{bot_token}"
    me_response = requests.get(f"{base}/getMe", timeout=15)
    me_response.raise_for_status()
    me_payload = me_response.json()
    if not me_payload.get("ok"):
        raise RuntimeError(f"Telegram getMe failed: {me_payload}")

    send_response = requests.post(
        f"{base}/sendMessage",
        data={"chat_id": target_id, "text": test_message},
        timeout=15,
    )
    try:
        send_payload = send_response.json()
    except ValueError:
        send_payload = {"ok": False, "description": send_response.text}
    if send_response.status_code >= 400 or not send_payload.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: {send_payload}")

    bot_username = me_payload.get("result", {}).get("username", "<unknown>")
    print(f"Telegram bot validation succeeded for @{bot_username}.")
    print("You should receive the Telegram setup test message now. If not, stop here and fix the bot token or chat ID.")


def print_firefly_check_failure(base_url: str, api_path: str, exc: Exception) -> None:
    print("")
    print("+--------------------------------------------------------------+")
    print("| Firefly Check Failed                                         |")
    print("+--------------------------------------------------------------+")
    print(f"  URL: {base_url.rstrip('/')}{api_path}")
    print(f"  Error: {exc}")
    print("")
    print("  Common causes:")
    print("    - the hostname cannot be resolved from inside Docker")
    print("    - the remote VPS is not reachable from this machine")
    print("    - the HTTPS certificate is self-signed and TLS verification is enabled")
    print("    - the API base URL or path is wrong")
    print("")
    print("  What to do next:")
    print("    - if this Firefly host is remote, verify the domain resolves from inside Docker")
    print("    - try the Firefly server IP temporarily instead of the hostname")
    print("    - rerun setup with --skip-firefly-check if you only want to save config first")
    print("    - later test with: docker-compose exec companion python3 -m firefly_companion.cli health")
    print("")


def normalize_chat_id(value: str) -> str:
    value = value.strip()
    if value.startswith("tg:"):
        return value[3:]
    return value


def run_codex_login() -> None:
    ensure_openclaw_config_scaffold()
    print("Starting Codex OAuth login inside the setup container.")
    print("If the environment is headless, OpenClaw may print a URL and ask you to paste the redirect result.")
    subprocess.run(["openclaw", "models", "auth", "login", "--provider", "openai-codex"], check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Interactive first-run setup for firefly-openclaw-companion.")
    parser.add_argument("--skip-firefly-check", action="store_true", help="Skip the Firefly III API validation step.")
    parser.add_argument("--skip-telegram-test", action="store_true", help="Skip the Telegram sendMessage test.")
    parser.add_argument("--skip-codex-login", action="store_true", help="Do not launch the Codex OAuth login flow.")
    parser.add_argument(
        "--plain-secrets",
        action="store_true",
        help="Use normal visible input instead of hidden getpass prompts for secrets. Useful if paste is unreliable.",
    )
    args = parser.parse_args(argv)

    existing = parse_env_file(ENV_PATH)
    print_banner()
    print(f"Host repository root: {HOST_ROOT}")

    print_section("OpenClaw Provider")
    provider_index = prompt_choice(
        "Choose the OpenClaw model/provider setup:",
        [(str(index + 1), provider.label) for index, provider in enumerate(PROVIDERS)],
        default="1",
    )
    provider = PROVIDERS[int(provider_index) - 1]
    provider_values = dict(provider.env_updates)
    if provider.requires_secret_prompt and provider.secret_env_name and provider.secret_prompt:
        provider_values[provider.secret_env_name] = prompt_text(
            provider.secret_prompt,
            secret_input=True,
            plain_secrets=args.plain_secrets,
        )

    print_section("Firefly III")
    print_hint(
        [
            "If Firefly III runs beside the companion on the same Docker host, use the internal URL, usually http://firefly:8080.",
            "If Firefly III runs on another VPS, use its reachable URL, for example https://firefly.example.com.",
            "The API base path is usually /api/v1.",
            "The Docker network name is only meaningful for local same-host deployments; leave the default if Firefly is remote.",
            "Create the personal access token in Firefly III from the profile area.",
            "If you know the token expiry date, enter it later so Telegram reminders can warn you before it expires.",
        ]
    )
    timezone_value = prompt_text("Timezone", default=existing.get("TZ", "Europe/Rome"))
    network_name = prompt_text("Docker network where Firefly III is reachable", default=existing.get("FIREFLY_DOCKER_NETWORK", "firefly"))
    firefly_base_url = prompt_text(
        "Firefly III base URL (examples: http://firefly:8080 or https://firefly.example.com)",
        default=existing.get("FIREFLY_BASE_URL", "http://firefly:8080"),
    )
    firefly_api_path = prompt_text("Firefly III API base path", default=existing.get("FIREFLY_API_BASE_PATH", "/api/v1"))
    firefly_token = prompt_text(
        "Firefly III personal access token",
        secret_input=True,
        plain_secrets=args.plain_secrets,
    )
    firefly_token_expires_on = prompt_text(
        "Firefly III token expiry date in YYYY-MM-DD (leave blank if unknown)",
        default=existing.get("FIREFLY_ACCESS_TOKEN_EXPIRES_ON", ""),
        allow_blank=True,
    )
    timeout_value = prompt_text("Firefly request timeout in seconds", default=existing.get("FIREFLY_TIMEOUT_SECONDS", "15"))
    verify_tls = prompt_bool("Verify Firefly TLS certificates", default=existing.get("FIREFLY_VERIFY_TLS", "true").lower() != "false")

    print_section("Telegram")
    print_hint(
        [
            "Create a bot with @BotFather and copy the bot token here.",
            "Start a chat with your bot before testing.",
            "For the common case, use your personal numeric Telegram user ID as the owner ID.",
            "You can get it from helper bots such as @username_to_id_bot.",
            "You can also DM your bot, then open:",
            "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates",
            "For private chats, message.chat.id and from.id are usually the same positive integer.",
            "For groups, message.chat.id is the group chat ID, but from.id is still your personal user ID.",
            "OpenClaw DM authorization needs your personal user ID, not a group ID.",
            "Leave the delivery target blank unless you want startup/reminder messages to go somewhere else.",
            "The wizard will send a test message. If you do not receive it, the Telegram configuration is wrong.",
        ]
    )
    telegram_enabled = True
    telegram_bot_token = prompt_text(
        "Telegram bot token",
        secret_input=True,
        plain_secrets=args.plain_secrets,
    )
    telegram_owner_id = normalize_chat_id(
        prompt_text(
            "Telegram owner/user ID allowed to control the bot",
            default=existing.get("TELEGRAM_OWNER_ID", existing.get("TELEGRAM_TARGET_ID", "")),
        )
    )
    telegram_target_id = normalize_chat_id(
        prompt_text(
            "Optional Telegram delivery chat ID for startup/reminder messages (blank = use owner ID)",
            default=existing.get("TELEGRAM_TARGET_ID", telegram_owner_id),
            allow_blank=True,
        )
    )
    if not telegram_target_id:
        telegram_target_id = telegram_owner_id
    telegram_dm_policy = prompt_choice(
        "Telegram DM policy:",
        [("allowlist", "Only the configured Telegram ID can use the bot"), ("pairing", "Require pairing before use")],
        default="allowlist" if existing.get("TELEGRAM_DM_POLICY") not in {"allowlist", "pairing"} else existing["TELEGRAM_DM_POLICY"],
    )
    chat_language = prompt_choice(
        "Preferred Telegram chat language:",
        [("auto", "Auto-detect from each message"), ("en", "English"), ("it", "Italiano")],
        default=existing.get("FIREFLY_CHAT_LANGUAGE", "auto") if existing.get("FIREFLY_CHAT_LANGUAGE") in {"auto", "en", "it"} else "auto",
    )

    print_section("Companion Defaults")
    print_hint(
        [
            "The gateway token protects the internal OpenClaw gateway.",
            "Dry-run should normally stay enabled by default.",
            "Use a conservative high-value threshold so writes require explicit confirmation.",
        ]
    )
    default_model = prompt_text("Default OpenClaw model", default=existing.get("OPENCLAW_DEFAULT_MODEL", provider.default_model))
    gateway_token = prompt_text(
        "OpenClaw gateway token",
        default=existing.get("OPENCLAW_GATEWAY_TOKEN", secrets.token_urlsafe(24)),
        secret_input=True,
        plain_secrets=args.plain_secrets,
    )
    dry_run_default = prompt_bool("Default bridge writes to dry-run mode", default=existing.get("FIREFLY_DEFAULT_DRY_RUN", "true").lower() != "false")
    high_value_threshold = prompt_text("High-value confirmation threshold", default=existing.get("FIREFLY_HIGH_VALUE_THRESHOLD", "250.00"))
    verify_on_boot = prompt_bool("Verify Firefly health during container bootstrap", default=existing.get("FIREFLY_RUNTIME_VERIFY_ON_BOOT", "true").lower() != "false")

    if not args.skip_firefly_check:
        try:
            verify_firefly(
                base_url=firefly_base_url,
                api_path=firefly_api_path,
                token=firefly_token,
                timeout_seconds=float(timeout_value),
                verify_tls=verify_tls,
            )
        except RequestException as exc:
            print_firefly_check_failure(firefly_base_url, firefly_api_path, exc)
            return 2

    if not args.skip_telegram_test:
        try:
            verify_telegram(telegram_bot_token, telegram_target_id)
        except Exception as exc:
            print("")
            print("Telegram validation failed.")
            print(f"Error: {exc}")
            print("")
            print("Most common causes:")
            print("  - the chat ID is wrong")
            print("  - you have not started the bot yet in that private chat")
            print("  - for groups, the bot is not added to the group")
            print("  - you copied a username instead of the numeric chat ID")
            print("If you did not receive the test message, fix the bot token or chat ID before continuing.")
            return 3

    if provider.requires_oauth_login and not args.skip_codex_login:
        run_codex_login()

    env_values = parse_env_file(ENV_PATH)
    env_values.update(
        {
            "TZ": timezone_value,
            "FIREFLY_DOCKER_NETWORK": network_name,
            "FIREFLY_BASE_URL": firefly_base_url,
            "FIREFLY_API_BASE_PATH": firefly_api_path,
            "OPENCLAW_PORT": "18789",
            "OPENCLAW_BIND": env_values.get("OPENCLAW_BIND", "loopback"),
            "OPENCLAW_DEFAULT_MODEL": default_model,
            "FIREFLY_TIMEOUT_SECONDS": timeout_value,
            "FIREFLY_VERIFY_TLS": "true" if verify_tls else "false",
            "FIREFLY_DEFAULT_DRY_RUN": "true" if dry_run_default else "false",
            "FIREFLY_HIGH_VALUE_THRESHOLD": high_value_threshold,
            "FIREFLY_DEDUPE_WINDOW_DAYS": env_values.get("FIREFLY_DEDUPE_WINDOW_DAYS", "7"),
            "FIREFLY_ALLOW_DELETE": "false",
            "FIREFLY_RUNTIME_VERIFY_ON_BOOT": "true" if verify_on_boot else "false",
            "FIREFLY_ACCESS_TOKEN_EXPIRES_ON": firefly_token_expires_on,
            "FIREFLY_TOKEN_REMINDER_DAYS": env_values.get("FIREFLY_TOKEN_REMINDER_DAYS", "60,30,14,7,3,1"),
            "FIREFLY_TOKEN_REMINDER_CHECK_INTERVAL_SECONDS": env_values.get(
                "FIREFLY_TOKEN_REMINDER_CHECK_INTERVAL_SECONDS",
                "21600",
            ),
            "FIREFLY_MAPPINGS_PATH": "/home/openclaw/workspace/config/mappings.yml",
            "FIREFLY_POLICY_PATH": "/home/openclaw/workspace/config/policy.yml",
            "TELEGRAM_ENABLED": "true",
            "TELEGRAM_OWNER_ID": telegram_owner_id,
            "TELEGRAM_TARGET_ID": telegram_target_id,
            "TELEGRAM_DM_POLICY": telegram_dm_policy,
            "FIREFLY_CHAT_LANGUAGE": chat_language,
            "FIREFLY_RECEIPT_AI_OCR": env_values.get("FIREFLY_RECEIPT_AI_OCR", "true"),
            "FIREFLY_PDF_OCR_PROVIDER_ENABLED": env_values.get("FIREFLY_PDF_OCR_PROVIDER_ENABLED", "true"),
            "PDFAPIHUB_API_KEY": env_values.get("PDFAPIHUB_API_KEY", ""),
            "PDFAPIHUB_BASE_URL": env_values.get("PDFAPIHUB_BASE_URL", "https://pdfapihub.com/api"),
            "FIREFLY_PDF_OCR_LANG": env_values.get("FIREFLY_PDF_OCR_LANG", "ita+eng"),
            "OPENCLAW_TELEGRAM_CHANNEL_ENABLED": "false",
            "OPENAI_API_KEY": "",
            "OPENAI_API_KEYS": env_values.get("OPENAI_API_KEYS", ""),
            "ANTHROPIC_API_KEY": "",
            "OPENROUTER_API_KEY": "",
            "GOOGLE_API_KEY": env_values.get("GOOGLE_API_KEY", ""),
            "AI_GATEWAY_API_KEY": env_values.get("AI_GATEWAY_API_KEY", ""),
            "EXAMPLE_FIREFLY_APP_URL": env_values.get("EXAMPLE_FIREFLY_APP_URL", "http://localhost:8080"),
            "EXAMPLE_FIREFLY_DB_NAME": env_values.get("EXAMPLE_FIREFLY_DB_NAME", "firefly"),
            "EXAMPLE_FIREFLY_DB_USER": env_values.get("EXAMPLE_FIREFLY_DB_USER", "firefly"),
            "EXAMPLE_FIREFLY_DB_PASSWORD": env_values.get("EXAMPLE_FIREFLY_DB_PASSWORD", "firefly-change-me"),
            "EXAMPLE_FIREFLY_SITE_OWNER": env_values.get("EXAMPLE_FIREFLY_SITE_OWNER", "owner@example.test"),
        }
    )
    env_values.update(provider_values)

    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    write_secret(SECRETS_DIR / "firefly_access_token.txt", firefly_token)
    write_secret(SECRETS_DIR / "openclaw_gateway_token.txt", gateway_token)
    write_secret(SECRETS_DIR / "telegram_bot_token.txt", telegram_bot_token)

    WORKSPACE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    example_mappings = WORKSPACE_CONFIG_DIR / "mappings.yml.example"
    example_policy = WORKSPACE_CONFIG_DIR / "policy.yml.example"
    target_mappings = WORKSPACE_CONFIG_DIR / "mappings.yml"
    target_policy = WORKSPACE_CONFIG_DIR / "policy.yml"
    if example_mappings.exists() and not target_mappings.exists():
        target_mappings.write_text(example_mappings.read_text(encoding="utf-8"), encoding="utf-8")
    if example_policy.exists() and not target_policy.exists():
        target_policy.write_text(example_policy.read_text(encoding="utf-8"), encoding="utf-8")

    write_env_file(ENV_PATH, env_values)

    print("")
    print_section("Setup Complete")
    print("  Files written:")
    print("    - .env")
    print("    - secrets/firefly_access_token.txt")
    print("    - secrets/openclaw_gateway_token.txt")
    print("    - secrets/telegram_bot_token.txt")
    print("")
    print("  Next commands:")
    print("    1. docker compose up -d --build")
    print("    2. Wait for the automatic Telegram startup message from the companion.")
    print("    3. Send a Telegram message to your bot from the configured chat.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
