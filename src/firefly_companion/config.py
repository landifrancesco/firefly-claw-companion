from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml


class ConfigurationError(RuntimeError):
    """Raised when runtime configuration is missing or invalid."""


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    content = yaml.safe_load(path.read_text(encoding="utf-8"))
    return content if isinstance(content, dict) else {}


@dataclass(slots=True)
class BridgeSettings:
    base_url: str
    api_base_path: str
    access_token: str
    timeout_seconds: float = 15.0
    request_retries: int = 2
    retry_backoff_seconds: float = 0.5
    verify_tls: bool = True
    force_connection_close: bool = True
    default_dry_run: bool = True
    high_value_threshold: Decimal = Decimal("250.00")
    dedupe_window_days: int = 7
    allow_delete: bool = False
    mappings_path: Path = field(default_factory=lambda: Path("workspace/config/mappings.yml"))
    policy_path: Path = field(default_factory=lambda: Path("workspace/config/policy.yml"))
    runtime_env_path: Path = field(default_factory=lambda: Path.home() / ".openclaw" / "firefly-bridge.env")
    mappings: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)

    @property
    def api_url(self) -> str:
        return f"{self.base_url.rstrip('/')}{self.api_base_path}"

    @property
    def account_aliases(self) -> dict[str, str]:
        return {str(key).casefold(): str(value) for key, value in self.mappings.get("account_aliases", {}).items()}

    @property
    def category_aliases(self) -> dict[str, str]:
        return {str(key).casefold(): str(value) for key, value in self.mappings.get("category_aliases", {}).items()}

    @property
    def merchant_rules(self) -> dict[str, Any]:
        return {str(key).casefold(): value for key, value in self.mappings.get("merchant_rules", {}).items()}

    @classmethod
    def from_env(cls) -> "BridgeSettings":
        runtime_env = Path(os.getenv("FIREFLY_RUNTIME_ENV_FILE", Path.home() / ".openclaw" / "firefly-bridge.env"))
        if runtime_env.exists():
            for line in runtime_env.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                os.environ.setdefault(key, value)

        base_url = os.getenv("FIREFLY_BASE_URL", "http://firefly:8080")
        api_base_path = os.getenv("FIREFLY_API_BASE_PATH", "/api/v1")
        access_token = os.getenv("FIREFLY_ACCESS_TOKEN", "")
        if not access_token:
            raise ConfigurationError("FIREFLY_ACCESS_TOKEN is required.")

        workspace_root = Path(os.getenv("OPENCLAW_WORKSPACE", "workspace"))
        mappings_path = Path(os.getenv("FIREFLY_MAPPINGS_PATH", str(workspace_root / "config" / "mappings.yml")))
        policy_path = Path(os.getenv("FIREFLY_POLICY_PATH", str(workspace_root / "config" / "policy.yml")))
        mappings = _load_yaml(mappings_path)
        policy = _load_yaml(policy_path)

        timeout_seconds = float(os.getenv("FIREFLY_TIMEOUT_SECONDS", policy.get("timeouts", {}).get("request_seconds", 15)))
        request_retries = int(os.getenv("FIREFLY_REQUEST_RETRIES", policy.get("timeouts", {}).get("request_retries", 2)))
        retry_backoff_seconds = float(
            os.getenv("FIREFLY_RETRY_BACKOFF_SECONDS", policy.get("timeouts", {}).get("retry_backoff_seconds", 0.5))
        )
        verify_tls = _as_bool(os.getenv("FIREFLY_VERIFY_TLS"), policy.get("security", {}).get("verify_tls", True))
        force_connection_close = _as_bool(
            os.getenv("FIREFLY_FORCE_CONNECTION_CLOSE"),
            policy.get("network", {}).get("force_connection_close", True),
        )
        default_dry_run = _as_bool(
            os.getenv("FIREFLY_DEFAULT_DRY_RUN"),
            policy.get("writes", {}).get("default_dry_run", True),
        )
        high_value_threshold = Decimal(
            str(os.getenv("FIREFLY_HIGH_VALUE_THRESHOLD", policy.get("writes", {}).get("high_value_threshold", "250.00")))
        )
        dedupe_window_days = int(
            os.getenv("FIREFLY_DEDUPE_WINDOW_DAYS", policy.get("writes", {}).get("dedupe_window_days", 7))
        )
        allow_delete = _as_bool(os.getenv("FIREFLY_ALLOW_DELETE"), policy.get("writes", {}).get("allow_delete", False))

        return cls(
            base_url=base_url,
            api_base_path=api_base_path,
            access_token=access_token,
            timeout_seconds=timeout_seconds,
            request_retries=request_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            verify_tls=verify_tls,
            force_connection_close=force_connection_close,
            default_dry_run=default_dry_run,
            high_value_threshold=high_value_threshold,
            dedupe_window_days=dedupe_window_days,
            allow_delete=allow_delete,
            mappings_path=mappings_path,
            policy_path=policy_path,
            runtime_env_path=runtime_env,
            mappings=mappings,
            policy=policy,
        )
