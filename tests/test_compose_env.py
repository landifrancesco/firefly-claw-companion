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

    def test_entrypoint_unsets_flat_secret_env_before_picoclaw_exec(self) -> None:
        entrypoint = Path("docker/entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn('ENTRYPOINT_BUILD_MARKER="config-scrub-v10"', entrypoint)
        self.assertIn("unset_picoclaw_flat_secret_env", entrypoint)
        self.assertIn("unset_picoclaw_render_secret_env", entrypoint)
        self.assertIn('if [[ "$1" == "picoclaw" ]]; then', entrypoint)
        self.assertIn("picoclaw onboard", entrypoint)
        for name in [
            "TELEGRAM_BOT_TOKEN",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENROUTER_API_KEY",
            "GROQ_API_KEY",
            "GOOGLE_API_KEY",
            "PDFAPIHUB_API_KEY",
        ]:
            self.assertIn(f"unset {name}", entrypoint)
        self.assertIn('export PICOCLAW_RENDER_TELEGRAM_BOT_TOKEN="${TELEGRAM_TOKEN}"', entrypoint)
        self.assertIn("python3 /opt/firefly-picoclaw/bin/token_expiry_reminder.py &", entrypoint)
        self.assertIn('export TELEGRAM_BOT_TOKEN="${TELEGRAM_TOKEN}"', entrypoint)
        self.assertIn("PICOCLAW_TELEGRAM_CHANNEL_ENABLED", entrypoint)


if __name__ == "__main__":
    unittest.main()
