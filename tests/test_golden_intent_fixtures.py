from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml

from scripts import telegram_firefly_bot as bot


FIXTURE_DIR = Path(__file__).resolve().parent / "golden" / "intents"


def _load_fixtures() -> list[dict[str, Any]]:
    fixtures: list[dict[str, Any]] = []
    for path in sorted(FIXTURE_DIR.glob("*.yml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload["_path"] = path.name
            fixtures.append(payload)
    return fixtures


def _assert_expected_period(params: dict[str, Any], expected: Any) -> None:
    if expected == "current_month":
        parsed_month = params.get("month")
        assert parsed_month in {None, date.today().strftime("%Y-%m")}
        return
    if isinstance(expected, dict):
        for key, value in expected.items():
            assert params.get(key) == value, f"Expected period {key}={value}, got {params.get(key)}"


def _assert_expected_params(params: dict[str, Any], expected: dict[str, Any]) -> None:
    for key, value in expected.items():
        actual = params.get(key)
        assert actual == value, f"Expected params.{key}={value}, got {actual}"


def test_golden_intent_fixtures() -> None:
    fixtures = _load_fixtures()
    assert fixtures, f"No golden fixtures found in {FIXTURE_DIR}"

    for fixture in fixtures:
        payload = bot.parse_natural_intent_payload(str(fixture.get("input") or ""))
        assert payload is not None, f"Failed to parse fixture {fixture['_path']}"
        assert payload.get("intent") == fixture.get("expected_intent"), (
            f"Fixture {fixture['_path']}: expected intent {fixture.get('expected_intent')} "
            f"but got {payload.get('intent')}"
        )
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        if "expected_period" in fixture:
            _assert_expected_period(params, fixture["expected_period"])
        if "expected_params" in fixture:
            _assert_expected_params(params, fixture["expected_params"])
