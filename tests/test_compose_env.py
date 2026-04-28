from __future__ import annotations

from pathlib import Path
import unittest


class ComposeEnvTest(unittest.TestCase):
    def test_compose_passes_chat_language_to_companion(self) -> None:
        compose = Path("docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("FIREFLY_CHAT_LANGUAGE: ${FIREFLY_CHAT_LANGUAGE:-auto}", compose)


if __name__ == "__main__":
    unittest.main()
