# Install Guide

## Deployment Modes

This repository supports two modes:

1. Sidecar against an existing Firefly III deployment on an existing Docker network.
2. Local testing with the included `docker-compose.example-firefly.yml`.

The primary supported path is the sidecar model.

## Existing Firefly III Sidecar Install

### 1. Clone And Prepare Files

```bash
git clone <your fork or local clone> firefly-openclaw-companion
cd firefly-openclaw-companion
```

### 2. Run The Guided Setup

Recommended first-run path:

```bash
docker compose --profile setup run --rm setup
```

The setup wizard writes:

- `.env`
- `secrets/firefly_access_token.txt`
- `secrets/openclaw_gateway_token.txt`
- `secrets/telegram_bot_token.txt`

It can also:

- validate the Firefly III personal access token
- configure Telegram as the required main control channel
- send a Telegram test message that you should receive immediately
- store the Firefly token expiry date so the companion can send Telegram reminders
- launch Codex OAuth login when you choose the Codex provider path

For Telegram, use your personal numeric user ID as the owner ID. If you want reminders delivered somewhere else, set a separate delivery target ID.

Ways to find your Telegram owner ID:

- Use a helper bot such as `@username_to_id_bot`
- Or message your bot and inspect `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
- In private chat flows, `from.id` is the value you want
- Do not use a group chat ID as the owner ID

### 3. Manual Alternative

If you do not want the wizard, create `.env` and the secret files manually. At minimum you need:

- `FIREFLY_DOCKER_NETWORK`
- `FIREFLY_BASE_URL`
  Use `http://firefly:8080` for a same-host Docker deployment or `https://firefly.example.com` for a remote VPS.
- `OPENCLAW_DEFAULT_MODEL`
- provider credentials such as `OPENAI_API_KEY`
- `secrets/firefly_access_token.txt`
- `secrets/openclaw_gateway_token.txt`
- `secrets/telegram_bot_token.txt`

### 4. Start

```bash
docker compose up -d --build
```

### 5. First Verification

```bash
docker compose ps
docker compose logs -f companion
docker compose exec companion python3 -m firefly_companion.cli health
docker compose exec companion python3 -m firefly_companion.cli accounts list --type asset
```

Expected behavior:

- You should receive the Telegram setup test message during the wizard.
- After `docker compose up`, you should receive a Telegram startup message from the companion.
- If you do not receive either message, stop and fix Telegram configuration before relying on the stack.

Useful Telegram commands after startup:

- `/help`
- `/balances`
- `/summary`
- `/recent 7`
- `/graph spending 30`
- `/graph cashflow 30`
- `/expense amount=12.50 description="Lunch" merchant=coop`

## Example Local Firefly III Stack

Use the example override only for local testing:

```bash
docker compose -f docker-compose.yml -f docker-compose.example-firefly.yml --profile example-firefly up -d --build
```

After Firefly III is running locally on `http://127.0.0.1:8080`, finish the normal Firefly III first-run setup in the UI, then create a personal access token and place it in `secrets/firefly_access_token.txt`. Restart the companion if needed:

```bash
docker compose restart companion
```

## How Bootstrap Works

On every container start, `docker/entrypoint.sh`:

1. Creates OpenClaw config and workspace directories with restrictive permissions.
2. Copies bundled skills, tools, and config only when the target files are absent.
3. Reads secrets from the host `./secrets` directory, `*_FILE`, or direct env vars.
4. Reads optional Telegram bot secrets from `./secrets/telegram_bot_token.txt`.
5. Writes `openclaw.runtime.json5` and `firefly-bridge.env` with current runtime values.
6. Optionally verifies Firefly III API health before launching OpenClaw.
7. Starts a dedicated Telegram Firefly bot with the configured Telegram token.
8. Starts OpenClaw non-interactively.
9. Sends a Telegram startup message when the gateway becomes healthy.

This makes restarts idempotent while preserving persistent state.

## Bridge Command Reference

### Read operations

```bash
docker compose exec companion python3 -m firefly_companion.cli health
docker compose exec companion python3 -m firefly_companion.cli accounts list --type asset
docker compose exec companion python3 -m firefly_companion.cli accounts balances
docker compose exec companion python3 -m firefly_companion.cli categories list
docker compose exec companion python3 -m firefly_companion.cli budgets list
docker compose exec companion python3 -m firefly_companion.cli transactions search --days 7 --query groceries
docker compose exec companion python3 -m firefly_companion.cli summary month --month 2026-04
```

### Write operations

All writes are conservative:

- dry-run is on by default
- dedupe runs before commit
- high-value transactions require explicit confirmation

Examples:

```bash
docker compose exec companion python3 -m firefly_companion.cli expense dry-run \
  --amount 42.50 \
  --description "Groceries" \
  --merchant coop

docker compose exec companion python3 -m firefly_companion.cli income create \
  --amount 2500.00 \
  --description "Salary April" \
  --source Employer \
  --destination "Main Checking" \
  --category Salary \
  --no-dry-run \
  --yes-high-value

docker compose exec companion python3 -m firefly_companion.cli transfer create \
  --amount 100.00 \
  --description "Move to savings" \
  --source "Main Checking" \
  --destination Savings \
  --no-dry-run
```

## Mappings And Policies

### `workspace/config/mappings.yml`

Use this file to:

- define account aliases
- define category aliases
- set default destination/source accounts
- add merchant shortcuts

It is the local working copy. The setup flow can seed it from `workspace/config/mappings.yml.example`, while the example file stays safe to commit.

### `workspace/config/policy.yml`

Use this file to:

- keep dry-run enabled by default
- set request timeouts
- set the high-value confirmation threshold

It is also a working copy, seeded from `workspace/config/policy.yml.example` when missing.
- keep delete disabled
- document secure behavior for operators

## Token Rotation

Rotate Firefly III or gateway tokens with this sequence:

```bash
printf '%s' 'new-token' > secrets/firefly_access_token.txt
printf '%s' 'new-gateway-token' > secrets/openclaw_gateway_token.txt
printf '%s' 'new-telegram-token' > secrets/telegram_bot_token.txt
chmod 600 secrets/*.txt
docker compose restart companion
```

The entrypoint rewrites runtime files on restart, so rotation is immediate.

The safe rotation path is the host secret file above. Avoid sending the new Firefly token through Telegram chat as the normal workflow.

## Expiry Reminders

If `FIREFLY_ACCESS_TOKEN_EXPIRES_ON` is set, the companion sends Telegram reminders before expiry. Default thresholds:

- 60 days
- 30 days
- 14 days
- 7 days
- 3 days
- 1 day

These are configurable through:

- `FIREFLY_TOKEN_REMINDER_DAYS`
- `FIREFLY_TOKEN_REMINDER_CHECK_INTERVAL_SECONDS`

## Backup Notes

Back up both named volumes:

- `openclaw_home`
- `openclaw_workspace`

Also keep a copy of:

- `.env`
- `secrets/` from your host

## Localhost-Only Exposure

The default stack is Telegram-first. It does not publish the OpenClaw control UI on the host, and the default runtime keeps the dashboard disabled.

## Troubleshooting Checklist

- `docker compose --profile setup run --rm setup`
- `docker compose exec companion env | grep FIREFLY_`
- `docker compose exec companion python3 -m firefly_companion.cli health`
- `docker compose exec companion python3 -m firefly_companion.cli accounts list --type asset`
- `docker compose exec companion cat /home/openclaw/.openclaw/openclaw.runtime.json5`
- `docker compose logs --tail=200 companion`
