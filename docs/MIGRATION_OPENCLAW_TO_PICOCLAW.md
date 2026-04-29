# Migration Plan: OpenClaw → PicoClaw

Status: Approved (scope A, non-root, codex provider, no custom skills, MCP for Firefly bridge)
Target upstream: [`sipeed/picoclaw`](https://github.com/sipeed/picoclaw) — `docker.io/sipeed/picoclaw:latest`

## 0. Why this is a real migration, not a rename

The previous repo was a wrapper around **OpenClaw** (TypeScript/Node, image `ghcr.io/openclaw/openclaw:latest`, config at `~/.openclaw/openclaw.json5`). PicoClaw is an **independent Go reimplementation** by Sipeed with a different config schema, different lifecycle, and a different binary. Therefore:

- All `*.json5` config generation in `docker/entrypoint.sh` is OpenClaw schema and must be discarded.
- The Node plugin staging seen in container logs (`@modelcontextprotocol/sdk`, `playwright-core`, `ws`, `chokidar`, `commander`, `acpx`, `@homebridge/ciao`, `express`) belongs to OpenClaw runtime; PicoClaw has none of it.
- `~/.openclaw/openclaw.json` writes/backups/anomaly logs come from the OpenClaw binary itself; nothing in our wrapper can suppress them while the OpenClaw image is the base.

| | OpenClaw (current) | PicoClaw (target) |
|---|---|---|
| Image | `ghcr.io/openclaw/openclaw:latest` | `docker.io/sipeed/picoclaw:latest` |
| Language | TypeScript/Node | Go (single static binary) |
| Config file | `~/.openclaw/openclaw.json` (JSON5, OpenClaw schema) | `~/.picoclaw/config.json` (JSON v1, PicoClaw schema) + `~/.picoclaw/.security.yml` |
| Init command | implicit | `picoclaw onboard` (explicit, idempotent) |
| Default gateway port | 18789 | 18790 |
| Bind config | `gateway.bind` (`loopback`/`all`) | `gateway.host` (`127.0.0.1`/`0.0.0.0`/`localhost`); env `PICOCLAW_GATEWAY_HOST` |
| Auth | `gateway.auth.{mode,token,allowTailscale}` | none of those exist; gateway is host-bound; launcher uses `PICOCLAW_LAUNCHER_TOKEN` |
| Telegram block | `channels.telegram.{botToken,dmPolicy,allowFrom,defaultTo,groupPolicy}` (camelCase) | `channels.telegram.{token,allow_from,reasoning_channel_id,streaming.enabled,use_markdown_v2}` (snake_case) |
| Model selection | `agents.defaults.model.primary: "openai/gpt-5.4-mini"` | `agents.defaults.model_name` referencing an entry in flat `model_list[]` |
| Plugin entries | `plugins.entries.bonjour.enabled` etc. | n/a (no Node plugins) |
| Tool deny | `tools.deny: ["browser","canvas","nodes","cron"]` | per-tool `enabled: true/false` keys (`exec`, `cron`, `web`, `i2c`, `serial`, `spawn`, `subagent`, `read_file`, `write_file`, `edit_file`, `append_file`, `list_dir`, `message`, `web_fetch`, `find_skills`, `install_skill`, `mcp`, `skills`, `hooks`, `media_cleanup`, …) |
| Skills | inline `workspace/skills/*/SKILL.md` loaded by OpenClaw | `tools.skills` with ClawHub/GitHub registries; **we will not author custom skills** |
| Firefly bridge integration | env-driven shell wrapper | **MCP server** registered via `picoclaw mcp add firefly-bridge -- /opt/firefly-picoclaw/bin/firefly-bridge` |
| Container user | non-root `picoclaw` (UID 1000), `HOME=/home/picoclaw` (kept) | upstream runs as root; we keep our hardened non-root layout |

## 1. Decisions (locked)

1. **(A) Full migration** to PicoClaw. No label-only rename.
2. **Non-root** `picoclaw` user (UID 1000) preserved. Mount lives at `/home/picoclaw/.picoclaw`.
3. **Default model** = OpenAI Codex via OAuth: `model_name: "codex"`, `model: "openai-codex/gpt-5.4"`, `auth_method: "oauth"` (no static API key in config).
4. **No custom skills.** Drop `workspace/skills/upstream_firefly_iii/`. Disable user-skills install at runtime (`tools.skills.enabled: false`) unless explicitly needed for ClawHub.
5. **Firefly bridge as MCP server.** Replace ad-hoc env-driven launch with `tools.mcp.servers.firefly-bridge` registered via `picoclaw mcp add` during entrypoint setup.

## 2. Phased work breakdown

### Phase 1 — Image & build

- `Dockerfile`:
  - `ARG PICOCLAW_IMAGE=docker.io/sipeed/picoclaw:latest`.
  - **Switch base** to `debian:stable-slim` (or keep current Debian/Alpine logic) and use a multi-stage `COPY --from=docker.io/sipeed/picoclaw:latest /usr/local/bin/picoclaw /usr/local/bin/picoclaw`. Rationale: PicoClaw upstream image is minimal (Go static binary), but we still need Python 3, tesseract (OCR), curl, tini for our Firefly + Telegram + token-reminder side processes.
  - Drop the `openclaw → picoclaw` symlink shim (lines 23–25).
  - Keep user creation, `tini`, OCR languages (`tesseract-ocr-eng`, `tesseract-ocr-ita`).
  - Update `COPY` paths: rename `picoclaw.secure.json5.example` → `config.example.json` matching PicoClaw v0 template (the upstream auto-migrates v0 → v1 + `.security.yml`).
  - `CMD` stays `["picoclaw", "gateway"]`.
- `docker-compose.yml`:
  - `PICOCLAW_IMAGE` default → `docker.io/sipeed/picoclaw:latest` (in both `companion` and `setup`).
  - Replace `PICOCLAW_BIND` env → `PICOCLAW_GATEWAY_HOST` (default `127.0.0.1`).
  - Default `PICOCLAW_PORT` → `18790`.
  - Drop `PICOCLAW_GATEWAY_TOKEN` (PicoClaw gateway has no token auth; it's host-bound). Keep optional `PICOCLAW_LAUNCHER_TOKEN` only if/when launcher profile is added later.
  - Volumes already correctly named `picoclaw_home` / `picoclaw_workspace`.
- `.gitignore`: `openclaw_home/` → `picoclaw_home/`.

### Phase 2 — Entrypoint rewrite (`docker/entrypoint.sh`)

Discard everything from the start of config generation through `firefly-bridge.env` (lines ~117–441). New flow:

1. Ensure dirs: `/home/picoclaw/.picoclaw` (700), `/home/picoclaw/.picoclaw/workspace` (700), `/home/picoclaw/.picoclaw/logs` (750).
2. Read secrets (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `GOOGLE_API_KEY`, `TELEGRAM_BOT_TOKEN`, `FIREFLY_ACCESS_TOKEN`, `PDFAPIHUB_API_KEY`) from env or `_FILE` or `/run/secrets/*` or `/run/host-secrets/*.txt`. Same `read_secret` helper as today.
3. **First-run init**: if `~/.picoclaw/config.json` is absent, run `picoclaw onboard` (it bootstraps default config + workspace), then immediately overwrite `config.json` and `.security.yml` with our generated content. Idempotent: on subsequent boots, only re-render the parts we own (model + channels + MCP) and preserve user edits where possible. Simplest first cut: **always re-render** since this is a containerized deployment; users edit env, not the JSON.
4. Generate `~/.picoclaw/config.json` (PicoClaw v1 schema) — see Phase 3 template.
5. Generate `~/.picoclaw/.security.yml` (chmod 600) with sensitive values referenced by name from `config.json`.
6. Register the Firefly MCP server idempotently: `picoclaw mcp add firefly-bridge --command /opt/firefly-picoclaw/bin/firefly-bridge` (use `picoclaw mcp list` to detect existing entry; if Sipeed's CLI lacks check-mode, write the entry into `config.json` directly under `tools.mcp.servers.firefly-bridge`).
7. Side processes (unchanged in spirit): `token_expiry_reminder.py`, `telegram_firefly_bot.py` — keep these as our own helpers, they wrap the Firefly side, not PicoClaw. Verify they read from `firefly-bridge.env` (or replace with direct env passthrough).
8. Optional Firefly health probe (`python3 -m firefly_companion.cli health`) — keep as bootstrap warning logic.
9. **Migration of leftover OpenClaw state**: if `/home/picoclaw/.openclaw/` exists in the persisted volume from a prior deployment, rename it to `/home/picoclaw/.openclaw.legacy.<timestamp>/` and log a single warning (don't auto-port: schemas are incompatible).
10. `exec picoclaw gateway` (or `tini` already wraps it via Dockerfile `ENTRYPOINT`).

### Phase 3 — `config.json` template (PicoClaw v1)

Generated by entrypoint. Values from env vars in `${...}`:

```json
{
  "agents": {
    "defaults": {
      "workspace": "/home/picoclaw/.picoclaw/workspace",
      "restrict_to_workspace": true,
      "model_name": "${PICOCLAW_DEFAULT_MODEL_NAME:-codex}",
      "max_tokens": 8192,
      "context_window": 131072,
      "temperature": 0.7,
      "max_tool_iterations": 20
    }
  },
  "model_list": [
    {
      "model_name": "codex",
      "model": "openai-codex/gpt-5.4",
      "auth_method": "oauth"
    }
  ],
  "channels": {
    "telegram": {
      "enabled": "${TELEGRAM_ENABLED:-true}",
      "token": "ref:telegram_bot_token",
      "allow_from": ["${TELEGRAM_OWNER_ID}"],
      "use_markdown_v2": false,
      "streaming": { "enabled": true }
    }
  },
  "tools": {
    "exec":   { "enabled": false },
    "cron":   { "enabled": false },
    "web":    { "enabled": false },
    "i2c":    { "enabled": false },
    "serial": { "enabled": false },
    "send_tts": { "enabled": false },
    "skills": { "enabled": false },
    "find_skills": { "enabled": false },
    "install_skill": { "enabled": false },
    "spawn":    { "enabled": true },
    "subagent": { "enabled": true },
    "message":  { "enabled": true },
    "list_dir": { "enabled": true },
    "read_file":   { "enabled": true, "mode": "bytes" },
    "write_file":  { "enabled": true },
    "edit_file":   { "enabled": true },
    "append_file": { "enabled": true },
    "web_fetch":   { "enabled": false },
    "media_cleanup": { "enabled": true, "max_age_minutes": 30, "interval_minutes": 5 },
    "mcp": {
      "enabled": true,
      "servers": {
        "firefly-bridge": {
          "enabled": true,
          "command": "/opt/firefly-picoclaw/bin/firefly-bridge",
          "env": {
            "FIREFLY_BASE_URL":          "${FIREFLY_BASE_URL}",
            "FIREFLY_API_BASE_PATH":     "${FIREFLY_API_BASE_PATH}",
            "FIREFLY_TIMEOUT_SECONDS":   "${FIREFLY_TIMEOUT_SECONDS}",
            "FIREFLY_VERIFY_TLS":        "${FIREFLY_VERIFY_TLS}",
            "FIREFLY_DEFAULT_DRY_RUN":   "${FIREFLY_DEFAULT_DRY_RUN}",
            "FIREFLY_HIGH_VALUE_THRESHOLD": "${FIREFLY_HIGH_VALUE_THRESHOLD}",
            "FIREFLY_DEDUPE_WINDOW_DAYS":   "${FIREFLY_DEDUPE_WINDOW_DAYS}",
            "FIREFLY_ALLOW_DELETE":      "${FIREFLY_ALLOW_DELETE}",
            "FIREFLY_MAPPINGS_PATH":     "/home/picoclaw/.picoclaw/workspace/config/mappings.yml",
            "FIREFLY_POLICY_PATH":       "/home/picoclaw/.picoclaw/workspace/config/policy.yml",
            "FIREFLY_ACCESS_TOKEN_FILE": "/home/picoclaw/.picoclaw/.security/firefly_access_token"
          }
        }
      }
    }
  },
  "hooks": { "enabled": true },
  "heartbeat": { "enabled": true, "interval": 30 },
  "gateway": {
    "host": "${PICOCLAW_GATEWAY_HOST:-127.0.0.1}",
    "port": "${PICOCLAW_PORT:-18790}",
    "log_level": "${PICOCLAW_LOG_LEVEL:-info}"
  }
}
```

`.security.yml` (chmod 600) holds:

```yaml
telegram_bot_token: "${TELEGRAM_BOT_TOKEN}"
openai_api_key:     "${OPENAI_API_KEY}"
anthropic_api_key:  "${ANTHROPIC_API_KEY}"
openrouter_api_key: "${OPENROUTER_API_KEY}"
google_api_key:     "${GOOGLE_API_KEY}"
```

Firefly access token is written separately to `~/.picoclaw/.security/firefly_access_token` (mode 600) and consumed by the MCP server via `FIREFLY_ACCESS_TOKEN_FILE` to keep it out of the YAML namespace.

### Phase 4 — Python code updates

Files to audit and update:

- `src/firefly_companion/config.py` — drop any JSON5 OpenClaw config reading; rely on env vars only (the MCP server gets env from `tools.mcp.servers.firefly-bridge.env`).
- `src/firefly_companion/conversation.py` — remove any reference to OpenClaw gateway token / endpoints; the bot now talks to the PicoClaw gateway over HTTP at `127.0.0.1:18790` (no token, host-bound).
- `src/firefly_companion/ai_router.py` — verify no OpenClaw-specific routing; align with `openai-codex/` provider naming if currently hardcoded for `openai/`.
- `scripts/setup_wizard.py` — rewrite end-to-end (see Phase 5).
- `scripts/telegram_firefly_bot.py` — keep as standalone Telegram bot? Decision: **PicoClaw owns the Telegram channel natively** via `channels.telegram`. Therefore delete or reduce `telegram_firefly_bot.py` to a thin shim only if we still need custom commands (e.g. `/firefly_health`) that PicoClaw skills don't cover. **Default: delete it; route all chat through PicoClaw gateway**, and expose Firefly operations as MCP tools only.
- `scripts/token_expiry_reminder.py` — keep (it watches `FIREFLY_ACCESS_TOKEN_EXPIRES_ON` and DMs the owner). Verify it sends via direct Telegram API (it can, using `TELEGRAM_BOT_TOKEN` + `TELEGRAM_OWNER_ID`) without going through PicoClaw, so it remains independent of agent state.
- `scripts/install_bundle_to_workspace.sh` — purge `skills/` install logic; only install `tools/firefly-bridge` (the MCP entrypoint binary/script) and `config/{mappings.yml,policy.yml}`.
- `scripts/verify_setup.sh` — runtime check: `curl -fsS http://127.0.0.1:${PICOCLAW_PORT:-18790}/healthz` (verify exact PicoClaw health path; otherwise probe TCP).
- `scripts/setup-check.sh` (new untracked file) — review/integrate or delete.

### Phase 5 — `setup_wizard.py` rewrite

- Goal: interactive CLI run via `docker compose run --rm setup` that produces `.env` (or directly seeds `~/.picoclaw/config.json` + `.security.yml`).
- Prompts:
  1. Firefly base URL, API base path, access token, expiry date, dry-run default, high-value threshold, allow-delete flag.
  2. Telegram enabled? bot token, owner Telegram user ID, target chat ID.
  3. OCR provider (PDFAPIHUB) optional.
  4. Default model: hardcode to `codex` (no static key needed; OAuth flow happens at first PicoClaw run via `picoclaw auth login`).
- Output: write `.env` at host repo root + persist same values into `picoclaw_home` volume.
- Drop all OpenClaw plugin/auth/Tailscale prompts.

### Phase 6 — Workspace cleanup

- Delete `workspace/skills/upstream_firefly_iii/` entirely (per decision 4).
- Update `workspace/AGENTS.md`: remove OpenClaw references, document MCP-based Firefly integration and PicoClaw config layout.
- Audit `workspace/i18n/telegram_bot.{en,it}.json` for any OpenClaw strings.
- Move `workspace/tools/firefly-bridge` (if exists) under `/opt/firefly-picoclaw/bin/firefly-bridge` install path; ensure it's executable and its `--help` describes its MCP interface.

### Phase 7 — Docs

- `README.md` — full rewrite of quick-start: image, port, `picoclaw onboard`, `docker compose up`, `picoclaw auth login` for Codex OAuth.
- `docs/INSTALL.md` — same.
- Update model table / provider list to reflect Codex OAuth path.
- Add a "Migrating from the OpenClaw build" section noting that `~/.openclaw/` is auto-archived on first boot.

### Phase 8 — Validation

```bash
docker compose build --no-cache
docker compose run --rm setup            # interactive wizard
docker compose up                         # foreground first
docker compose logs companion | rg -i 'openclaw'   # MUST be 0 hits
curl -fsS http://127.0.0.1:18790/healthz || curl -fsS http://127.0.0.1:18790/   # gateway live
rg -n 'openclaw|OpenClaw|OPENCLAW' .      # MUST be 0 hits in tracked files
```

Smoke tests:
- Send `/help` to the Telegram bot via PicoClaw's native channel; verify response.
- Send "list my Firefly accounts" → confirms MCP `firefly-bridge` server is invoked and returns data.
- Restart container; verify `~/.picoclaw/config.json` is regenerated, no OpenClaw artifacts appear, no `Config write anomaly` lines in logs.

## 3. File-level change list (executable summary)

| File | Action |
|---|---|
| `Dockerfile` | Multi-stage: `COPY --from=docker.io/sipeed/picoclaw:latest`. Drop symlink shim. Update `COPY` of config example. |
| `docker-compose.yml` | New default image. New env names (`PICOCLAW_GATEWAY_HOST`, port 18790). Drop `PICOCLAW_GATEWAY_TOKEN`. |
| `docker/entrypoint.sh` | Full rewrite of config generation block; add OpenClaw legacy archival; register MCP firefly-bridge. |
| `picoclaw.secure.json5.example` (untracked) → `config.example.json` | Replace with PicoClaw v1 template (subset of upstream). |
| `.gitignore` | `openclaw_home/` → `picoclaw_home/`. |
| `scripts/setup_wizard.py` | Rewrite to produce `.env` + seed PicoClaw config. |
| `scripts/verify_setup.sh` | Switch health check to `:18790`. |
| `scripts/install_bundle_to_workspace.sh` | Drop skills install; install firefly-bridge tool + config YAMLs only. |
| `scripts/telegram_firefly_bot.py` | **Delete** (replaced by PicoClaw native Telegram channel). |
| `scripts/token_expiry_reminder.py` | Keep; verify env names. |
| `src/firefly_companion/config.py` | Strip OpenClaw config schema reads; env-only. |
| `src/firefly_companion/conversation.py` | Remove gateway-token auth; talk to `127.0.0.1:18790` host-bound. |
| `src/firefly_companion/ai_router.py` | Default to `openai-codex/` provider; drop OpenClaw assumptions. |
| `workspace/skills/upstream_firefly_iii/` | **Delete directory.** |
| `workspace/AGENTS.md` | Rewrite. |
| `workspace/i18n/telegram_bot.{en,it}.json` | Audit/update strings. |
| `README.md`, `docs/INSTALL.md` | Rewrite quick-start. |
| `.env.example` | Drop `PICOCLAW_GATEWAY_TOKEN`, `PICOCLAW_BIND`; add `PICOCLAW_GATEWAY_HOST`, change default port to 18790. |

## 4. Open questions / future work

- Verify the exact PicoClaw `--allow-unconfigured` equivalent (or whether it's needed at all given onboard).
- Confirm whether `picoclaw mcp add` supports `--env KEY=VAL` flags or only `--env-file`; if env-file only, we materialize a small env file under `~/.picoclaw/.security/firefly-bridge.env`.
- Optional follow-up: add `launcher` profile to `docker-compose.yml` for the WebUI on port 18800 (not needed for headless deployments).
- Optional follow-up: add a `picoclaw auth login` one-shot service for the Codex OAuth device-code flow, since OAuth requires user interaction the first time.
