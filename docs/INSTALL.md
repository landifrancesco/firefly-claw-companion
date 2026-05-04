# Install Guide

This guide covers a Docker-first install of `firefly-claw-companion`: a PicoClaw-based Firefly III companion with a Telegram finance bot, deterministic Firefly REST bridge, OCR receipt parsing, and conservative transaction drafts.

## Prerequisites

You need:

1. Docker with Compose support.
2. A reachable Firefly III instance.
3. A Firefly III personal access token.
4. A Telegram bot token and your numeric Telegram user ID.
5. One AI provider key. Gemini is the default via `GOOGLE_API_KEY`.

## Quick Install

```bash
git clone https://github.com/landifrancesco/firefly-claw-companion
cd firefly-claw-companion
docker compose run --rm setup
docker compose up -d --build
```

The setup wizard writes local runtime files:

- `.env`
- `secrets/firefly_access_token.txt`
- `secrets/telegram_bot_token.txt`
- `workspace/config/mappings.yml`
- `workspace/config/policy.yml`
- PicoClaw config and security files inside the `picoclaw_home` Docker volume

`workspace/config/*.yml` and `secrets/*` are local state and are intentionally not tracked by Git. Templates live at `workspace/config/*.example`.

## Manual Install

```bash
git clone https://github.com/landifrancesco/firefly-claw-companion
cd firefly-claw-companion

cp .env.example .env
mkdir -p secrets
printf '%s' 'your-firefly-token-here' > secrets/firefly_access_token.txt
printf '%s' 'your-telegram-bot-token-here' > secrets/telegram_bot_token.txt
chmod 600 secrets/*.txt
```

Edit `.env`:

```env
FIREFLY_BASE_URL=https://firefly.example.com
FIREFLY_DOCKER_NETWORK_EXTERNAL=false

TELEGRAM_OWNER_ID=123456789
TELEGRAM_TARGET_ID=123456789

GOOGLE_API_KEY=your-google-api-key-here
PICOCLAW_DEFAULT_MODEL=gemini/gemini-2.5-flash
```

Start:

```bash
docker compose up -d --build
```

## Firefly Connectivity

Recommended URL mode:

```env
FIREFLY_BASE_URL=https://firefly.example.com
FIREFLY_DOCKER_NETWORK_EXTERNAL=false
```

Use this when Firefly III is reachable through a normal URL, HTTPS domain, reverse proxy, or host/IP from inside the companion container. Compose creates this app's network automatically.

Advanced Docker-network mode:

```env
FIREFLY_DOCKER_NETWORK=firefly_firefly
FIREFLY_DOCKER_NETWORK_EXTERNAL=true
FIREFLY_BASE_URL=http://firefly_iii_core:8080
```

Use this only when Firefly III is already running in another Compose stack and you want direct container-to-container traffic. The network must already exist.

Find the existing Firefly network:

```bash
docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' firefly_iii_core
```

If `FIREFLY_DOCKER_NETWORK_EXTERNAL=true` and the network name is wrong, startup fails with `network ... declared as external, but could not be found`.

## Verification

```bash
docker compose ps
docker compose logs -f companion
docker compose exec companion python3 -m firefly_companion.cli health
docker compose exec companion python3 -m firefly_companion.cli accounts list --type asset
```

Then open Telegram, message your bot, and run `/start` or `/train`.

## Bootstrap Details

On container start, `docker/entrypoint.sh`:

1. creates `/home/picoclaw/.picoclaw`, workspace, logs, and security directories
2. archives incompatible legacy state if present
3. installs bridge tools, i18n files, and config examples into the workspace
4. reads secrets from env, `*_FILE`, Docker secrets, or `/run/host-secrets/*.txt`
5. renders `/home/picoclaw/.picoclaw/config.json`
6. renders `/home/picoclaw/.picoclaw/.security.yml`
7. writes the Firefly access token to `.security/firefly_access_token`
8. optionally checks Firefly API health
9. starts `picoclaw gateway`

The standalone Telegram bot owns chat commands and natural-language finance flows. PicoClaw's native Telegram channel is disabled by default (`PICOCLAW_TELEGRAM_CHANNEL_ENABLED=false`) to avoid competing for the same bot token.

## Default Model

The default model is Gemini:

```env
PICOCLAW_DEFAULT_MODEL_NAME=gemini
PICOCLAW_DEFAULT_MODEL=gemini/gemini-2.5-flash
GOOGLE_API_KEY=...
```

Supported provider env vars:

- `GOOGLE_API_KEY`
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `OPENROUTER_API_KEY`
- `GROQ_API_KEY`

The entrypoint writes provider keys to nested PicoClaw security storage, not to `config.json`.

## What Works In Telegram

Examples:

```text
how much money do i have
show my summary for this month
show recent coffee transactions
find amazon
transfer €100
transfer €100 to savings
transfer €100 from checking to savings
withdraw €50 from ATM
received salary 1500 today
increase groceries budget by 50 this month
add a monthly recurring expense of €50 for gym
```

Write operations are drafts by default. If a transaction is missing an account, the bot asks for it and shows available accounts. Transfers use asset accounts for both source and destination.

## CLI Bridge

PicoClaw can invoke:

```bash
/opt/firefly-picoclaw/bin/firefly-bridge
```

With no arguments it starts an MCP stdio server. With CLI arguments it remains deterministic:

```bash
docker compose exec companion python3 -m firefly_companion.cli health
docker compose exec companion python3 -m firefly_companion.cli accounts balances
docker compose exec companion python3 -m firefly_companion.cli categories list
docker compose exec companion python3 -m firefly_companion.cli budgets list
docker compose exec companion python3 -m firefly_companion.cli transactions search --days 7 --query groceries
docker compose exec companion python3 -m firefly_companion.cli summary month --month 2026-04
```

## Writes And Safety

Writes are conservative by default:

- dry-run is enabled
- duplicate detection runs before commit
- high-value writes require extra confirmation
- deletes require `FIREFLY_ALLOW_DELETE=true`

Example dry-run:

```bash
docker compose exec companion python3 -m firefly_companion.cli expense dry-run \
  --amount 42.50 \
  --description "Groceries" \
  --merchant coop
```

## Local Config Files

Runtime config is generated from examples:

- `workspace/config/mappings.yml.example` -> `workspace/config/mappings.yml`
- `workspace/config/policy.yml.example` -> `workspace/config/policy.yml`

Edit the generated local files to customize accounts, merchant aliases, safety thresholds, or delete behavior, then restart:

```bash
docker compose restart companion
```

## Token Rotation

```bash
printf '%s' 'new-firefly-token' > secrets/firefly_access_token.txt
printf '%s' 'new-telegram-token' > secrets/telegram_bot_token.txt
chmod 600 secrets/*.txt
docker compose restart companion
```

The entrypoint rewrites PicoClaw config and security files on restart.

## Local Test Firefly

```bash
docker compose -f docker-compose.yml -f docker-compose.example-firefly.yml --profile example-firefly up -d --build
```

Finish Firefly III setup at `http://127.0.0.1:8080`, create a personal access token, save it to `secrets/firefly_access_token.txt`, then restart the companion.

## Troubleshooting

### Bot Does Not Reply

Check:

```bash
docker compose logs --tail=200 companion
```

Common causes:

- `TELEGRAM_BOT_TOKEN` or `secrets/telegram_bot_token.txt` is wrong.
- `TELEGRAM_OWNER_ID` does not match your Telegram user ID.
- Another process is polling the same Telegram bot token.

### Firefly Returns HTTP 401

The Firefly personal access token is invalid, expired, or belongs to another user.

```bash
printf '%s' 'new-firefly-token' > secrets/firefly_access_token.txt
docker compose restart companion
```

### Container Is Unhealthy

```bash
docker compose ps
docker compose logs --tail=200 companion
docker compose exec companion python3 -m firefly_companion.cli health
```

Common causes:

- wrong `FIREFLY_BASE_URL`
- wrong external Docker network name
- missing AI provider key for the selected `PICOCLAW_DEFAULT_MODEL`
- Firefly is not reachable from inside the container

### External Docker Network Error

If you see:

```text
network ... declared as external, but could not be found
```

Either set:

```env
FIREFLY_DOCKER_NETWORK_EXTERNAL=false
```

or set `FIREFLY_DOCKER_NETWORK` to an existing network name.

### Wrong Transfer Account

Send a more explicit request:

```text
transfer €100 from Main Checking to Savings
```

If you omit one side, the bot asks for it and shows asset accounts. Re-run `/train` if your account defaults are wrong.

### Receipt OCR Is Poor

Add a caption like:

```text
receipt for coffee paid with card
```

For better OCR, set `PDFAPIHUB_API_KEY`; otherwise the bot falls back to local Tesseract OCR.

### Inspect Generated PicoClaw Config

```bash
docker compose exec companion cat /home/picoclaw/.picoclaw/config.json
docker compose exec companion sh -c 'ls -la /home/picoclaw/.picoclaw/.security*'
```

Do not commit generated config, `.env`, or files under `secrets/`.

