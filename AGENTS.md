# Agent Instructions

## Project

This repository builds `firefly-picoclaw-companion`, a Docker-first companion
for Firefly III. It runs a PicoClaw-based gateway, a deterministic Firefly REST
bridge, and a Telegram finance bot for balances, summaries, budget reports,
receipt parsing, and transaction drafts.

## Key Paths

- `docker-compose.yml`: main runtime stack.
- `Dockerfile`: builds the companion image from the upstream PicoClaw binary.
- `docker/entrypoint.sh`: container bootstrap, PicoClaw config generation,
  secret loading, token reminders, and Telegram bot startup.
- `scripts/telegram_firefly_bot.py`: Telegram bot command handling,
  natural-language routing, setup/training flow, budget reports, and receipt
  handling.
- `scripts/setup_wizard.py`: interactive first-run setup that writes `.env`,
  secrets, and PicoClaw config.
- `src/firefly_companion/`: Firefly client, bridge service, config, draft
  manager, intent parsing, object cache, and MCP server.
- `workspace/i18n/`: Telegram bot localization files.
- `tests/`: pytest suite for bridge, Telegram, setup, routing, and draft flows.

## Common Commands

```bash
docker compose build --no-cache companion
docker compose up -d --force-recreate companion
docker compose logs -f companion
docker compose exec companion python3 -m firefly_companion.cli health
```

```bash
python -m pytest tests/test_telegram_setup_and_autofill.py
python -m pytest tests/test_telegram_localization_and_periods.py
python -m pytest tests/test_telegram_natural_commands.py tests/test_golden_intent_fixtures.py tests/test_golden_failures.py
```

```bash
python -c "import ast, pathlib; ast.parse(pathlib.Path('scripts/telegram_firefly_bot.py').read_text(encoding='utf-8'))"
python -c "import json, pathlib; json.loads(pathlib.Path('config.example.json').read_text(encoding='utf-8'))"
```

## Docker Network Rule

For a Firefly III instance reached through HTTPS/domain, use:

Set `FIREFLY_DOCKER_NETWORK_EXTERNAL=false` and point `FIREFLY_BASE_URL` at
the HTTPS Firefly domain.

Use `FIREFLY_DOCKER_NETWORK_EXTERNAL=true` only when joining an existing Docker
network created by another Firefly compose stack. If true, Docker will not
create the network and startup fails when the named network does not exist.

## PicoClaw Config Rule

Newer PicoClaw rejects old flat secret keys in `config.json`, including
`telegram_bot_token`, `google_api_key`, `openai_api_key`, `anthropic_api_key`,
`openrouter_api_key`, `groq_api_key`, and `pdfapihub_api_key`.

`config.json` must use the current schema with `version: 2` and no static
secret values. Secrets belong in nested `.security.yml`, for example:

```yaml
model_list:
  gemini:
    api_keys:
      - ...
channels:
  telegram:
    token: ...
```

When changing PicoClaw startup behavior, edit both `docker/entrypoint.sh` and
`scripts/setup_wizard.py` so runtime boot and first-run setup stay consistent.

## Telegram Bot Notes

- Keep Italian and English command references in `workspace/i18n/` aligned.
- Prefer grouped command listings over duplicate alias-heavy menus.
- For setup/training prompts, distinguish Firefly account roles clearly:
  asset account means card/checking/cash; expense account means merchant/out
  side; revenue account means income source.
- Budget reports should include spent, limit, and remaining/over-limit when
  Firefly budget limits are available.

## Engineering Rules

- Preserve existing local style and keep edits scoped.
- Use focused tests for changed behavior; broaden tests when touching shared
  bot routing or bridge code.
- Do not commit secrets or generated runtime files.
- `.env`, `secrets/`, workspace runtime config, and logs are local state.
- Prefer `rg` for searches and `pytest` for verification.

## Caliber

Caliber pre-commit sync is installed manually for this worktree. Caliber
provider-backed generation is not used because the user wants subscription or
OAuth-backed tools, not API-key billing.
