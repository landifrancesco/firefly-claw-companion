"""Security posture encoded in scrub passes, forbidden keys, and env hygiene."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests.docker.constants import (
    FIREFLY_BRIDGE_MCP_ENV_KEYS,
    FORBIDDEN_FLAT_SECRET_KEYS,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _extract_forbidden_assignment_bodies(script: str) -> list[str]:
    """Greedy-but-safe extraction: each forbidden set is flat single-brace literals only."""
    return re.findall(r"forbidden\s*=\s*\{([^}]*)\}", script, flags=re.MULTILINE | re.DOTALL)


class TestEntrypointForbiddenKeyParity(unittest.TestCase):
    """Every PicoClaw sanitization snippet must scrub the same provider keys."""

    ep: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.ep = (_repo_root() / "docker" / "entrypoint.sh").read_text(encoding="utf-8")

    def test_three_distinct_flat_forbidden_definitions(self) -> None:
        bodies = _extract_forbidden_assignment_bodies(self.ep)
        self.assertEqual(len(bodies), 3)

    def test_each_forbidden_block_matches_contract_constants(self) -> None:
        for body in _extract_forbidden_assignment_bodies(self.ep):
            found = frozenset(re.findall(r'"([a-z][a-z0-9_]*)"', body))
            self.assertFalse(
                found - FORBIDDEN_FLAT_SECRET_KEYS,
                f"Forbidden block introduced unknown symbols: {found - FORBIDDEN_FLAT_SECRET_KEYS}",
            )
            self.assertSetEqual(found, FORBIDDEN_FLAT_SECRET_KEYS, "Drift versus FORBIDDEN_FLAT_SECRET_KEYS")

    def test_scrub_recursive_helpers_present(self) -> None:
        self.assertIn("def scrub(", self.ep)
        self.assertIn("def find_forbidden(", self.ep)
        self.assertIn("scrub_picoclaw_resources()", self.ep)

    def test_flat_unset_helper_covers_each_forbidden_provider_token(self) -> None:
        match = re.search(
            r"unset_picoclaw_flat_secret_env\(\)\s*\{([^}]+)\}",
            self.ep,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match, "Missing unset_picoclaw_flat_secret_env helper")
        body = match.group(1)
        for snake in sorted(FORBIDDEN_FLAT_SECRET_KEYS):
            env_upper = "_".join(part.upper() for part in snake.split("_"))
            self.assertRegex(
                body,
                re.compile(re.escape(f"unset {env_upper}") + rf"(?:\s+{env_upper}_FILE)?"),
            )


class TestEntrypointConfigEmbedding(unittest.TestCase):
    """Embedded JSON/Python must stay compatible with Firefly MCP bridge."""

    ep: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.ep = (_repo_root() / "docker" / "entrypoint.sh").read_text(encoding="utf-8")

    def test_config_writer_declares_schema_version_two(self) -> None:
        gen = self.ep.index("# Generate dynamic config files")
        win = self.ep[gen : gen + 8000]
        self.assertRegex(win, r'"version"\s*:\s*2\b')

    def test_firefly_bridge_command_and_every_mcp_env_key(self) -> None:
        gen = self.ep.index("# Generate dynamic config files")
        win = self.ep[gen : gen + 12000]
        self.assertIn('"/opt/firefly-picoclaw/bin/firefly-bridge"', win)
        missing = [key for key in FIREFLY_BRIDGE_MCP_ENV_KEYS if f'"{key}"' not in win]
        self.assertFalse(missing, f"MCP stanza drift — missing env keys: {missing}")

    def test_security_yaml_uses_render_pipeline_not_flat_env_reads(self) -> None:
        gen = self.ep.index("# Model keys for .security.yml use PICOCLAW_RENDER_*")
        win = self.ep[gen : gen + 2000]
        self.assertIn("PICOCLAW_RENDER_TELEGRAM_BOT_TOKEN", win)
        self.assertNotIn(
            'os.getenv("TELEGRAM_BOT_TOKEN"',
            win,
            "flat TELEGRAM_BOT_TOKEN must not bypass render indirection near file write",
        )


if __name__ == "__main__":
    unittest.main()
