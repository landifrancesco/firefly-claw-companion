# firefly-openclaw-companion

`firefly-openclaw-companion` is a Docker-first companion for Firefly III.
It runs beside Firefly III, talks to it only through the REST API, and exposes
a Telegram-first control flow for balances, summaries, recent transactions, and
safe transaction creation.

## What it does

- connects to an existing Firefly III instance
- runs OpenClaw in a separate container
- starts a dedicated Telegram bot for finance commands
- keeps write actions conservative with dry-run defaults and confirmation gates
- supports receipt and screenshot parsing with local OCR plus AI assistance

## Main components

- `docker-compose.yml`: main deployment
- `docker/entrypoint.sh`: bootstrap and runtime config generation
- `src/firefly_companion/`: Firefly REST client and bridge logic
- `scripts/setup_wizard.py`: guided setup for `.env` and local secrets
- `scripts/telegram_firefly_bot.py`: Telegram bot and receipt/document flow
- `workspace/config/mappings.yml.example`: starter mappings
- `workspace/config/policy.yml.example`: starter safety policy

## Requirements

- Docker with Compose
- a reachable Firefly III instance
- a Firefly III personal access token
- a Telegram bot token
- one supported model provider for OpenClaw

## Quick start

1. Run the setup wizard:

```bash
docker compose --profile setup run --rm setup
```

2. Start the stack:

```bash
docker compose up -d --build
```

If the usual startup path is flaky on your machine, use this stricter rebuild flow instead:

```bash
docker compose build --no-cache companion
docker compose up -d --force-recreate companion
```

3. Check the logs:

```bash
docker compose logs -f companion
```

## Local files

- `.env`: local runtime values, not for Git
- `secrets/`: Firefly token, gateway token, Telegram bot token
- `workspace/config/mappings.yml`: your local account/category mappings
- `workspace/config/policy.yml`: local policy overrides

Examples that are safe to keep in Git:

- `.env.example`
- `workspace/config/mappings.yml.example`
- `workspace/config/policy.yml.example`

## OCR behavior

- image receipts and screenshots use local Tesseract when available
- AI OCR can assist image extraction when enabled
- PDFs use the direct provider path through `PDFAPIHUB_API_KEY` when configured
- there is no OpenClaw OCR plugin dependency in the runtime bootstrap

## Runtime behavior

- the OpenClaw gateway binds to `loopback` by default
- `bonjour` is disabled by default to avoid unnecessary LAN advertisement
- Telegram is the primary operator surface
- Firefly write actions stay in dry-run unless explicitly confirmed

## Useful commands

```bash
docker compose exec companion python3 -m firefly_companion.cli health
docker compose exec companion python3 -m firefly_companion.cli accounts list --type asset
docker compose exec companion python3 -m firefly_companion.cli summary month
docker compose exec companion python3 -m firefly_companion.cli transactions search --days 7
```

## Troubleshooting

- `HTTP 401` from Firefly means the personal access token is wrong, expired, or for the wrong user
- if Telegram tests fail, verify the bot token and numeric chat/user ID
- if the container starts but stays unhealthy, inspect `docker compose logs -f companion`
- if receipt extraction is weak, verify your OCR/provider settings in `.env`

More detailed installation notes are in `docs/INSTALL.md`.
