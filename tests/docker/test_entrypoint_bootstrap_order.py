"""Critical phase ordering inside ``docker/entrypoint.sh`` (secrets → config → scrub → gateway → exec)."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from tests.docker.constants import ENTRYPOINT_MARKER


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


class TestEntrypointBootstrapOrder(unittest.TestCase):
    """Guards regressions where steps are reordered and PicoClaw or companion bots mis-boot."""

    ep: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.ep = (_repo_root() / "docker" / "entrypoint.sh").read_text(encoding="utf-8")

    def test_build_marker_pins_known_scrub_generation(self) -> None:
        self.assertIn(
            ENTRYPOINT_MARKER,
            self.ep,
            "Bump ENTRYPOINT_MARKER in constants.py whenever docker/entrypoint.sh marker changes.",
        )

    def test_phase_secret_reads_then_firefly_gate_then_exports(self) -> None:
        p0 = self.ep.find('PDFAPIHUB_TOKEN="$(secret_from_standard_locations PDFAPIHUB_API_KEY')
        p1 = self.ep.find("FIREFLY_ACCESS_TOKEN is required", p0)
        p2 = self.ep.find('export PICOCLAW_DEFAULT_MODEL_NAME="${PICOCLAW_DEFAULT_MODEL_NAME:-gemini}"', p1)
        self.assertLess(p0, p1)
        self.assertLess(p1, p2)

    def test_phase_validation_onboard_then_clear_flat_env_then_render_exports(self) -> None:
        p_case = self.ep.find('case "${PICOCLAW_DEFAULT_MODEL_NAME}" in')
        p_onboard = self.ep.find("picoclaw onboard", p_case)
        p_unset = self.ep.find("unset_picoclaw_flat_secret_env", p_onboard)
        p_render = self.ep.find('export PICOCLAW_RENDER_TELEGRAM_BOT_TOKEN="${TELEGRAM_TOKEN}"', p_unset)
        for a, b in (
            (p_case, p_onboard),
            (p_onboard, p_unset),
            (p_unset, p_render),
        ):
            self.assertLess(a, b)

    def test_phase_config_writer_after_render_exports(self) -> None:
        p_render = self.ep.find('export PICOCLAW_RENDER_PDFAPIHUB_API_KEY="${PDFAPIHUB_TOKEN}"')
        p_gen = self.ep.find("# Generate dynamic config files", p_render)
        self.assertLess(p_render, p_gen)

    def test_phase_sync_home_config_then_scrub_then_health_probe(self) -> None:
        p_cp = self.ep.find('cp "${PICOCLAW_CONFIG_DIR}/config.json" "${PICOCLAW_HOME}/config.json"')
        p_scrub = self.ep.find('scrub_picoclaw_resources "before-startup"', p_cp)
        p_root_strip = self.ep.find("Strip legacy flat secret keys only at JSON root", p_scrub)
        p_health = self.ep.find('if [[ "${VERIFY_ON_BOOT}" == "true" ]]', p_root_strip)
        for a, b in ((p_cp, p_scrub), (p_scrub, p_root_strip), (p_root_strip, p_health)):
            self.assertLess(a, b)

    def test_phase_gateway_strips_configs_before_python_bots(self) -> None:
        gate = self.ep.find('if [[ "$1" == "picoclaw" && "${2:-}" == "gateway" ]]')
        bots = self.ep.find("python3 /opt/firefly-picoclaw/bin/token_expiry_reminder.py &", gate)
        self.assertLess(gate, bots)
        # Inner JSON-unwind touches both config locations before bots start.
        window = self.ep[gate:bots]
        cfg_path_snip = 'Path(os.getenv("PICOCLAW_CONFIG_DIR"'
        self.assertGreaterEqual(window.count(cfg_path_snip), 1)
        self.assertIn('payload.pop(key, None)', window)

    def test_phase_resync_home_config_before_final_scrub(self) -> None:
        marker = 'if [[ -f "${PICOCLAW_CONFIG_DIR}/config.json" ]]; then'
        p_sync = self.ep.find(marker)
        p_scrub = self.ep.find('scrub_picoclaw_resources "before-exec"', p_sync)
        self.assertLess(p_sync, p_scrub)

    def test_phase_final_scrub_unset_then_exec(self) -> None:
        b = self.ep.find('scrub_picoclaw_resources "before-exec"')
        p_if = self.ep.find('if [[ "$1" == "picoclaw" ]]; then', b)
        p_unset_flat = self.ep.find("unset_picoclaw_flat_secret_env", p_if)
        p_unset_render = self.ep.find("unset_picoclaw_render_secret_env", p_unset_flat)
        p_exec = self.ep.find('\nexec "$@"', p_unset_render)
        for prev, cur in (
            (b, p_if),
            (p_if, p_unset_flat),
            (p_unset_flat, p_unset_render),
            (p_unset_render, p_exec),
        ):
            self.assertLess(prev, cur)

    def test_default_command_chain_only_when_argv_empty(self) -> None:
        self.assertRegex(
            self.ep,
            re.compile(
                r'if \[\[ "\$\#" -eq 0 \]\]; then\s+set -- picoclaw gateway;\s+fi',
                re.MULTILINE,
            ),
        )

    @unittest.skipUnless(
        sys.platform != "win32" and bool(shutil.which("bash")),
        "bash -n is exercised on Linux/macOS CI where bash is POSIX (Windows Git shims are unreliable)",
    )
    def test_entrypoint_passes_bash_syntax_check(self) -> None:
        path = _repo_root() / "docker" / "entrypoint.sh"
        subprocess.run(["bash", "-n", str(path)], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
