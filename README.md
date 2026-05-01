# firefly-openclaw-companion

`firefly-openclaw-companion` is a Docker-first companion for Firefly III.
It runs beside Firefly III, talks only through the REST API, and provides a
Telegram-first workflow for balances, summaries, recent transactions, and safe
transaction creation.

## What this project does

- Connects to an existing Firefly III instance.
- Runs OpenClaw in an isolated companion container.
- Starts a dedicated Telegram bot for finance operations.
- Keeps write operations conservative with dry-run defaults and confirmation gates.
- Supports receipt/screenshot parsing with local OCR and optional AI OCR assistance.

## Repository layout

- `docker-compose.yml`: main runtime stack (`companion` + optional `setup` profile).
- `docker/entrypoint.sh`: runtime bootstrap, secrets loading, startup checks.
- `scripts/setup_wizard.py`: guided local setup that writes `.env` and token files.
- `src/firefly_companion/`: Firefly API client, bridge logic, CLI commands.
- `workspace/config/mappings.yml.example`: starter aliases/mapping template.
- `workspace/config/policy.yml.example`: starter runtime safety policy template.
- `docs/INSTALL.md`: extended install/reference documentation.

## Prerequisites

Before starting, make sure you have:

1. Docker Desktop (or Docker Engine) with Compose enabled.
2. A reachable Firefly III instance (local or remote).
3. A Firefly III personal access token.
4. A Telegram bot token.
5. At least one supported model provider credential (for OpenClaw).

## Step-by-step setup

### Step 1 - Clone and enter the project

```bash
git clone <your-fork-or-repo-url> firefly-openclaw-companion
cd firefly-openclaw-companion
```

**What these commands do**
- `git clone ...`: downloads the repository to your machine.
- `cd firefly-openclaw-companion`: enters the project folder.

### Step 2 - Run the setup wizard (recommended)

```bash
docker compose --profile setup run --rm setup
```

**What this command does**
- Starts the interactive setup container once.
- Generates local runtime files such as:
  - `.env`
  - `secrets/firefly_access_token.txt`
  - `secrets/openclaw_gateway_token.txt`
  - `secrets/telegram_bot_token.txt`
- Can validate connectivity and send a Telegram test message.
- `--rm` removes the temporary setup container after completion.
- Defaults to connecting to Firefly III through a normal URL or HTTPS domain.
  The wizard keeps `FIREFLY_DOCKER_NETWORK_EXTERNAL=false` unless you explicitly
  choose the advanced Docker-network mode.

### Step 3 - Start the companion stack

```bash
docker compose up -d --build
```

**What this command does**
- `--build`: builds the image if required (or if sources changed).
- `-d`: runs the services in detached/background mode.
- Starts the main `companion` service from `docker-compose.yml`.

## Firefly connection and Docker networks

The companion only talks to Firefly III through the REST API. It does not need
database access or shared volumes.

Recommended setup:

```env
FIREFLY_BASE_URL=https://firefly.example.com
FIREFLY_DOCKER_NETWORK_EXTERNAL=false
```

Use this when Firefly III is reachable through a domain, HTTPS reverse proxy, or
any URL the companion container can call. With `FIREFLY_DOCKER_NETWORK_EXTERNAL=false`,
Docker Compose creates this app's own network automatically. This is the default
and avoids startup failures on a fresh VPS.

Advanced internal Docker setup:

```env
FIREFLY_DOCKER_NETWORK=<existing-firefly-network>
FIREFLY_DOCKER_NETWORK_EXTERNAL=true
FIREFLY_BASE_URL=http://<firefly-container-name>:8080
```

Use this only when Firefly III is already running in another Compose stack and
you want direct container-to-container traffic. In this mode Compose expects the
network to already exist and will fail with `network ... declared as external,
but could not be found` if the name is wrong or the network has not been created.

To find the network used by an existing Firefly container:

```bash
docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' firefly_iii_core
```

### Step 4 - Verify startup and health

```bash
docker compose ps
docker compose logs -f companion
docker compose exec companion python3 -m firefly_companion.cli health
```

**What these commands do**
- `docker compose ps`: shows container status (`running`, `healthy`, etc.).
- `docker compose logs -f companion`: follows companion logs in real time.
- `... cli health`: runs an application-level Firefly API health check.

### Step 5 - Run your first read-only checks

```bash
docker compose exec companion python3 -m firefly_companion.cli accounts list --type asset
docker compose exec companion python3 -m firefly_companion.cli summary month
docker compose exec companion python3 -m firefly_companion.cli transactions search --days 7
```

**What these commands do**
- `accounts list --type asset`: lists asset accounts from Firefly.
- `summary month`: prints current month financial summary.
- `transactions search --days 7`: lists recent transactions in the last 7 days.

## If normal startup is flaky

Use a strict rebuild path:

```bash
docker compose build --no-cache companion
docker compose up -d --force-recreate companion
```

**What these commands do**
- `build --no-cache`: rebuilds the companion image from scratch.
- `up ... --force-recreate`: recreates the container even if config seems unchanged.

## Local-only files (not committed)

These files are intentionally local and ignored by Git:

- `.env`
- `secrets/` token files
- `workspace/config/mappings.yml`
- `workspace/config/policy.yml`
- `workspace/logs/`

Safe templates to commit:

- `.env.example`
- `workspace/config/mappings.yml.example`
- `workspace/config/policy.yml.example`

## Command reference (quick)

### Docker lifecycle

- `docker compose --profile setup run --rm setup`: run interactive setup wizard.
- `docker compose up -d --build`: build (if needed) and start in background.
- `docker compose down`: stop and remove containers from this compose project.
- `docker compose restart companion`: restart only the companion service.
- `docker compose logs -f companion`: stream live logs for troubleshooting.

### Companion CLI

- `python3 -m firefly_companion.cli health`: API/runtime health check.
- `python3 -m firefly_companion.cli accounts list --type asset`: list asset accounts.
- `python3 -m firefly_companion.cli accounts balances`: show account balances.
- `python3 -m firefly_companion.cli categories list`: list Firefly categories.
- `python3 -m firefly_companion.cli budgets list`: list budgets.
- `python3 -m firefly_companion.cli summary month --month YYYY-MM`: month summary.
- `python3 -m firefly_companion.cli transactions search --days 7 --query groceries`: filtered search.

Run any CLI command inside the container with:

```bash
docker compose exec companion <your-command>
```

## Safety defaults

- Write operations default to dry-run mode.
- High-value transaction confirmation is required above threshold.
- Delete operations are disabled by default.
- OpenClaw binds to loopback by default (`OPENCLAW_BIND=loopback`).

## Troubleshooting

- `HTTP 401` from Firefly: token is invalid, expired, or for the wrong user.
- Telegram setup fails: verify bot token and numeric owner/target IDs.
- Service stays unhealthy: inspect `docker compose logs -f companion`.
- OCR quality is weak: review OCR/provider settings in `.env`.

For full details and advanced scenarios, see `docs/INSTALL.md`.
