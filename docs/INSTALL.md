# Install Guide

## Sidecar Install

1. Clone the repo.

```bash
git clone <repo-url> firefly-picoclaw-companion
cd firefly-picoclaw-companion
```

2. Run setup.

```bash
docker compose run --rm setup
```

The wizard collects Firefly, Telegram, OCR, and safety defaults. It writes `.env`, `secrets/firefly_access_token.txt`, `secrets/telegram_bot_token.txt`, and seeded PicoClaw files in the `picoclaw_home` volume.

For Firefly connectivity, the wizard defaults to URL mode. Use your HTTPS
Firefly domain or another URL reachable from the companion container. This keeps
`FIREFLY_DOCKER_NETWORK_EXTERNAL=false`, so Docker Compose creates this app's
own network and does not require a pre-existing `firefly` network.

Choose the existing Docker network mode only when Firefly III runs in another
Compose stack and you want direct container-to-container traffic. In that mode,
set `FIREFLY_DOCKER_NETWORK` to the network already used by the Firefly
container and use an internal URL such as `http://firefly_iii_core:8080`.

3. Start the companion.

```bash
docker compose up -d --build
```

4. Verify.

```bash
docker compose ps
docker compose logs -f companion
docker compose exec companion python3 -m firefly_companion.cli health
docker compose exec companion python3 -m firefly_companion.cli accounts list --type asset
```

## Default Model

The default generated model uses Google AI Studio with Gemini 2.5 Flash:

```json
{
  "model_name": "gemini",
  "model": "gemini/gemini-2.5-flash",
  "auth_method": "api_key"
}
```

## Bootstrap Details

On every start, `docker/entrypoint.sh`:

1. creates `/home/picoclaw/.picoclaw`, workspace, logs, and security directories
2. archives incompatible legacy state if present
3. installs the Firefly bridge tool and config examples into the workspace
4. reads secrets from env, `*_FILE`, Docker secrets, or `/run/host-secrets/*.txt`
5. renders `/home/picoclaw/.picoclaw/config.json`
6. renders `/home/picoclaw/.picoclaw/.security.yml`
7. writes the Firefly access token to `.security/firefly_access_token`
8. optionally checks Firefly API health
9. starts `picoclaw gateway`

The standalone Telegram command bot owns `/help`, `/commands`, `/balances`, `/summary`, `/recent`, and transaction draft flows. PicoClaw's native Telegram channel is disabled by default to avoid competing for the same bot token.

## Firefly URL vs Docker Network

Recommended:

```env
FIREFLY_BASE_URL=https://firefly.example.com
FIREFLY_DOCKER_NETWORK_EXTERNAL=false
```

This calls Firefly III through its normal HTTP API endpoint. The companion
Compose project owns its own network, so a fresh VPS can start without manually
creating Docker networks.

Advanced:

```env
FIREFLY_DOCKER_NETWORK=<existing-firefly-network>
FIREFLY_DOCKER_NETWORK_EXTERNAL=true
FIREFLY_BASE_URL=http://<firefly-container-name>:8080
```

With `FIREFLY_DOCKER_NETWORK_EXTERNAL=true`, Compose will not create the
network. It must already exist. If the network name is wrong, startup fails with
`network ... declared as external, but could not be found`.

Find the network used by an existing Firefly container with:

```bash
docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' firefly_iii_core
```

## Firefly Bridge

PicoClaw invokes:

```bash
/opt/firefly-picoclaw/bin/firefly-bridge
```

With no arguments it starts an MCP stdio server. With arguments it remains a deterministic CLI:

```bash
docker compose exec companion python3 -m firefly_companion.cli health
docker compose exec companion python3 -m firefly_companion.cli accounts balances
docker compose exec companion python3 -m firefly_companion.cli categories list
docker compose exec companion python3 -m firefly_companion.cli budgets list
docker compose exec companion python3 -m firefly_companion.cli transactions search --days 7
docker compose exec companion python3 -m firefly_companion.cli summary month --month 2026-04
```

## Writes

Writes are conservative by default:

- dry-run is enabled
- duplicate detection runs before commit
- high-value writes require `--yes-high-value`
- deletes require `FIREFLY_ALLOW_DELETE=true`

Example:

```bash
docker compose exec companion python3 -m firefly_companion.cli expense dry-run \
  --amount 42.50 \
  --description "Groceries" \
  --merchant coop
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

Finish Firefly III setup at `http://127.0.0.1:8080`, create a personal access token, put it in `secrets/firefly_access_token.txt`, then restart the companion.

## Troubleshooting

```bash
docker compose exec companion env | grep FIREFLY_
docker compose exec companion cat /home/picoclaw/.picoclaw/config.json
docker compose exec companion python3 -m firefly_companion.cli health
docker compose logs --tail=200 companion
```

Gateway probes use `127.0.0.1:18790` by default.
