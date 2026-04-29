from __future__ import annotations

from pathlib import Path
import unittest


class ComposeEnvTest(unittest.TestCase):
    def test_compose_passes_chat_language_to_companion(self) -> None:
        compose = Path("docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("FIREFLY_CHAT_LANGUAGE: ${FIREFLY_CHAT_LANGUAGE:-auto}", compose)

    def test_compose_uses_picoclaw_gateway_host(self) -> None:
        compose = Path("docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("PICOCLAW_GATEWAY_HOST: ${PICOCLAW_GATEWAY_HOST:-127.0.0.1}", compose)
        self.assertNotIn("PICOCLAW_" + "GATEWAY_TOKEN", compose)
        self.assertNotIn("PICOCLAW_" + "BIND", compose)


if __name__ == "__main__":
    unittest.main()
