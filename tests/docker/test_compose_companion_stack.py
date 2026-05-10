"""``docker-compose.yml`` structure for the companion and setup helper services."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from tests.docker.constants import COMPANION_ENV_GROUPS


class TestComposeCompanionStack(unittest.TestCase):
    """Companion service volumes, networking, healthcheck, and ordered environment."""

    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[2]
        cls.repo_root = root
        compose_path = root / "docker-compose.yml"
        cls.compose_text = compose_path.read_text(encoding="utf-8")
        cls.data = yaml.safe_load(cls.compose_text)

    def test_yaml_parses_with_companion_and_setup_services(self) -> None:
        services = self.data.get("services", {})
        self.assertIn("companion", services)
        self.assertIn("setup", services)

    def test_companion_publishes_picoclaw_gateway_port_through_single_variable(self) -> None:
        companion = self.data["services"]["companion"]
        port_spec = companion.get("ports", [])
        self.assertEqual(
            len(port_spec),
            1,
            "Companion should expose one published port mapping for PicoClaw",
        )
        published = port_spec[0]
        self.assertRegex(
            published,
            r"\$\{PICOCLAW_PORT:-18790\}:\$\{PICOCLAW_PORT:-18790\}",
            "Host and container PicoClaw port must resolve from PICOCLAW_PORT",
        )

    def test_companion_volumes_three_way_wiring(self) -> None:
        companion = self.data["services"]["companion"]
        vols = companion.get("volumes", [])
        self.assertIn("picoclaw_home:/home/picoclaw/.picoclaw", vols)
        self.assertIn("picoclaw_workspace:/home/picoclaw/.picoclaw/workspace", vols)
        self.assertTrue(
            any(":/run/host-secrets" in str(v) and "secrets" in str(v) for v in vols),
            "Companion should bind-mount host secrets for token files",
        )

    def test_companion_healthcheck_uses_bundle_verify_script(self) -> None:
        companion = self.data["services"]["companion"]
        hc = companion.get("healthcheck", {})
        test = hc.get("test")
        self.assertEqual(
            test,
            ["CMD-SHELL", "/opt/firefly-picoclaw/bin/verify_setup.sh --runtime"],
        )

    def test_setup_service_profiles_and_host_repo_mount(self) -> None:
        setup = self.data["services"]["setup"]
        self.assertEqual(setup.get("profiles"), ["setup"])
        vols = setup.get("volumes", [])
        self.assertTrue(any(str(v).startswith(".:/host-repo") for v in vols))

    def test_network_external_flag_parameterized_firefly_net(self) -> None:
        net = self.data["networks"]["firefly_net"]
        self.assertEqual(net.get("name"), "${FIREFLY_DOCKER_NETWORK:-firefly}")
        self.assertEqual(net.get("external"), "${FIREFLY_DOCKER_NETWORK_EXTERNAL:-false}")

    def test_companion_environment_ordered_groups_present(self) -> None:
        """Environment keys appear in YAML in the grouped order documented in constants."""
        companion = self.data["services"]["companion"]
        env_section = companion.get("environment", {})
        if not isinstance(env_section, dict):
            self.fail("companion.environment should be a mapping")
        c_start = self.compose_text.index("  companion:")
        c_end = self.compose_text.index("\n  setup:", c_start)
        companion_doc = self.compose_text[c_start:c_end]
        flattened = []
        groups = COMPANION_ENV_GROUPS
        for group_name, keys in groups:
            positions = []
            for key in keys:
                self.assertIn(
                    key,
                    env_section,
                    f"[{group_name}] missing compose key '{key}'",
                )
                # Position in YAML text (order within file among declared keys).
                pos = companion_doc.find(f"\n      {key}:")
                self.assertGreater(
                    pos,
                    -1,
                    f"[{group_name}] key '{key}' not found in companion environment block",
                )
                positions.append((key, pos))
            flat_order = sorted(positions, key=lambda t: t[1])
            self.assertSequenceEqual(
                [k for k, _ in flat_order],
                list(keys),
                f"[{group_name}] keys should appear in YAML in this order: {keys!r}",
            )
            flattened.extend(keys)
        duplicates = sorted({k for k in flattened if flattened.count(k) > 1})
        self.assertFalse(duplicates, f"Companion env declares duplicate logical keys? {duplicates}")

    def test_companion_rejects_obsolete_gateway_token_env(self) -> None:
        self.assertNotIn("PICOCLAW_GATEWAY_TOKEN", self.compose_text)
        self.assertNotIn("PICOCLAW_BIND", self.compose_text)


if __name__ == "__main__":
    unittest.main()
