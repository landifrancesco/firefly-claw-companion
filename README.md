# firefly-claw-companion

<img src="docs/assets/firefly_claw_companion.png" alt="firefly-claw-companion" width="520">

A Telegram bot companion for [Firefly III](https://www.firefly-iii.org/) that understands plain language. You can ask things like "how much did I spend on food last month" or "add an expense of 12 euros for coffee" and it will handle it. Write operations are dry-run by default so nothing gets saved until you confirm.

---

## What it does

- Connects to your existing Firefly III instance through its REST API.
- Runs a Telegram bot that accepts both natural language and slash commands.
- Parses receipts and bank screenshots with OCR and optional AI vision.
- Keeps all write operations behind a confirmation step, with duplicate detection and high-value thresholds.
- Supports English and Italian out of the box, with a modular translation system that lets you add any language by dropping a single JSON file.

## How the natural language works

When you send a message, the bot tries three things in order:

1. A fast deterministic parser that handles common patterns without calling any AI.
2. If that fails, it calls your configured AI model (Gemini, GPT, Claude, etc.) to interpret the intent.
3. If the AI is unavailable, a regex fallback handles the most explicit transaction sentences.

This means most everyday requests work instantly and cheaply, and the AI is only called when the request is genuinely ambiguous.

---

## Prerequisites

You need these four things before you start:

1. **Docker** with Compose support (Docker Desktop on Mac/Windows, or Docker Engine + the compose plugin on Linux).
2. A running **Firefly III** instance, reachable over HTTP or HTTPS from your server.
3. A **Firefly III personal access token** (see below).
4. A **Telegram bot token** and your **Telegram user ID** (see below).
5. An **AI provider API key** for at least one provider (see below). The bot uses Google Gemini by default because it has a generous free tier.

---

## Getting your API keys and tokens

### Firefly III personal access token

1. Log into your Firefly III instance and go to `https://firefly.example.com/profile/oauth` (replace `firefly.example.com` with your actual domain or IP).
2. Scroll down to **Personal Access Tokens** and click **Create new token**.
3. Give it a name like "companion" and leave the expiry empty if you want it to last indefinitely.
4. Copy the token immediately. You will not see it again after you close that page.

If you are running Firefly III locally with the default Docker setup, the URL is `http://localhost:8080` and the OAuth page is at `http://localhost:8080/profile/oauth`.

### Telegram bot token

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot` and follow the prompts. Pick any name and username.
3. BotFather will give you a token that looks like `7123456789:AABBcc...`. Copy it.

### Your Telegram user ID

The bot only accepts messages from your account. You need your numeric Telegram user ID.

1. Open Telegram and search for **@userinfobot** (or **@getidsbot**).
2. Send `/start` or any message.
3. It replies with your numeric ID, something like `123456789`.

Both `TELEGRAM_OWNER_ID` and `TELEGRAM_TARGET_ID` in the config usually get set to this same number unless you want the bot to send startup messages to a different chat.

### AI provider API key

Pick one provider. The bot uses Google Gemini by default because Gemini 2.5 Flash has a free tier that is enough for personal use.

| Provider | Where to get the key | Env variable |
|---|---|---|
| **Google Gemini** (default) | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | `GOOGLE_API_KEY` |
| OpenAI | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) | `OPENAI_API_KEY` |
| Anthropic | [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys) | `ANTHROPIC_API_KEY` |
| OpenRouter | [openrouter.ai/settings/keys](https://openrouter.ai/settings/keys) | `OPENROUTER_API_KEY` |
| Groq | [console.groq.com/keys](https://console.groq.com/keys) | `GROQ_API_KEY` |

To switch from the default Gemini to another provider, set `PICOCLAW_DEFAULT_MODEL` in your `.env`:

```env
# Examples:
PICOCLAW_DEFAULT_MODEL=gemini/gemini-2.5-flash         # default
PICOCLAW_DEFAULT_MODEL=openai/gpt-4o-mini
PICOCLAW_DEFAULT_MODEL=anthropic/claude-haiku-4-5-20251001
PICOCLAW_DEFAULT_MODEL=groq/llama-3.1-8b-instant
PICOCLAW_DEFAULT_MODEL=openrouter/google/gemini-flash-1.5
```

### Optional: PDF and image OCR key

If you want to send PDF bank statements or higher-quality receipt parsing, you can set a `PDFAPIHUB_API_KEY`. This is completely optional. The bot falls back to local Tesseract OCR if no key is provided.

Get a key at [pdfapihub.com](https://pdfapihub.com).

---

## Setup

### Option A: Setup wizard (recommended)

The wizard asks you questions and writes all the config files for you.

```bash
git clone https://github.com/landifrancesco/firefly-claw-companion
cd firefly-claw-companion
docker compose --profile setup run --rm setup
docker compose up -d --build
```

The wizard will ask for your Firefly URL, personal access token, Telegram bot token, Telegram user ID, and AI provider key. It writes `.env`, `secrets/firefly_access_token.txt`, and `secrets/telegram_bot_token.txt`.

### Option B: Manual setup

Copy the example files and edit them:

```bash
git clone https://github.com/landifrancesco/firefly-claw-companion
cd firefly-claw-companion

cp .env.example .env

mkdir -p secrets
printf '%s' 'your-firefly-token-here' > secrets/firefly_access_token.txt
printf '%s' 'your-telegram-bot-token-here' > secrets/telegram_bot_token.txt
chmod 600 secrets/*.txt
```

Then open `.env` and fill in the required fields:

```env
FIREFLY_BASE_URL=https://firefly.example.com

TELEGRAM_OWNER_ID=123456789
TELEGRAM_TARGET_ID=123456789

GOOGLE_API_KEY=your-google-api-key-here
# or whichever provider you chose
```

Then start the stack:

```bash
docker compose up -d --build
```

---

## Connecting to Firefly III

### Standard setup (recommended)

This works for most people. Firefly III is reachable through a URL from your server or Docker host:

```env
FIREFLY_BASE_URL=https://firefly.example.com
FIREFLY_DOCKER_NETWORK_EXTERNAL=false
```

With `FIREFLY_DOCKER_NETWORK_EXTERNAL=false`, Docker Compose creates a private network for this app automatically. No manual network setup needed.

### Advanced: same Docker host as Firefly III

If Firefly III is already running in a separate Compose stack on the same machine and you want the companion to talk to it directly over an internal Docker network (no internet hop, no TLS needed):

```env
FIREFLY_DOCKER_NETWORK=firefly_firefly       # the network name from the Firefly stack
FIREFLY_DOCKER_NETWORK_EXTERNAL=true
FIREFLY_BASE_URL=http://firefly_iii_core:8080
```

To find the network name used by your existing Firefly container:

```bash
docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' firefly_iii_core
```

Use the exact name it prints. If the name is wrong, startup will fail with `network ... declared as external, but could not be found`.

---

## Verification

After starting, check everything is working:

```bash
# Check container status
docker compose ps

# Follow logs in real time
docker compose logs -f companion

# Test the Firefly API connection
docker compose exec companion python3 -m firefly_companion.cli health

# List your accounts
docker compose exec companion python3 -m firefly_companion.cli accounts list --type asset
```

Then open Telegram, find your bot, and send `/start`. It should reply with a welcome message and prompt you to run a short training flow that teaches it your default accounts.

---

## What you can ask

Send any message to your bot. Here are examples of things that work:

### Balances and summaries

```
how much money do i have
show me my accounts
show my summary for this month
show my summary for last month
show my summary from 01-04-2026 to 15-04-2026
```

### Spending queries

```
how much did i spend this month
how much did i spend on food last month
income vs spending for march
income vs spending from 01-03-2026 to 31-03-2026
```

### Adding expenses

```
add an expense of 12 euros for coffee
paid 8.50 for lunch
add expense €45 at the pharmacy paid with card
bought groceries for 32 euros
```

### Adding income

```
received salary 2200 this month
income 500 from freelance work
add income of 1800 for salary
```

### Transfers between accounts

```
transfer 200 from checking to savings
move 500 from main account to emergency fund
transfer €100 from wallet to card
```

### ATM withdrawals

```
withdraw 50 euros from ATM
withdrew €100 at the ATM
withdrawal of 80 euros
```

### Graphs and charts

```
make a graph of my balances
show me a spending chart for last month
cashflow chart for this month
make a graph of my budget usage
top categories chart for march
```

### Categories

```
show me the categories i spent the most on this month
top categories for last month with a graph
show all categories for march
```

### Budgets

```
show my budgets
how much is left in my groceries budget
set groceries budget to 350 this month
increase food budget by 50
lower transport budget to 100
```

### Recurring transactions

```
add a monthly recurring expense of 80 for rent
add a yearly recurring expense of 120 for netflix
show my recurring transactions
```

### Receipts and bank screenshots

Send a photo of a receipt or a bank app screenshot directly to the chat. The bot will try to read it with OCR and prepare a draft. You can add a caption like "receipt for coffee paid with card" to help it understand the context.

### Searching past transactions

```
find coffee
search for amazon
search for pharmacy last month
```

---

## Slash commands

You can also use explicit slash commands if you prefer:

```
/help              - natural language examples
/commands          - full command list
/balances          - account balances
/summary           - monthly summary (add month=2026-04 for a specific month)
/recent            - recent transactions (add days=30 or a date range)
/topcategories     - top spending categories
/budgetreport      - budget usage report
/graph             - balance, spending, or cashflow chart
/expense           - add an expense draft
/income            - add an income draft
/transfer          - add a transfer draft
/recurrences       - list recurring transactions
/search <keyword>  - search recent transactions
/backup            - download a JSON backup of your Firefly data
/setup             - view or reset your finance profile
/train             - re-run the account defaults training
/add               - guided step-by-step transaction entry
/undo              - restore the last cancelled draft
```

Italian equivalents are also available: `/saldi`, `/riepilogo`, `/recenti`, `/spesa`, `/entrata`, `/trasferimento`, `/budget`, `/ricorrenze`, `/cerca`, and so on.

---

## Adding a language

English and Italian are included out of the box. The bot detects the language automatically from what you write. If you want to add French, German, Spanish, or any other language, you only need to create one file.

All UI strings live in `workspace/i18n/`. The naming convention is `telegram_bot.{language-code}.json`. So for French you would create `workspace/i18n/telegram_bot.fr.json`.

Start by copying the English file as a base:

```bash
cp workspace/i18n/telegram_bot.en.json workspace/i18n/telegram_bot.fr.json
```

Then open `telegram_bot.fr.json` and translate the values inside the `"strings"` and `"lists"` objects. Do not change the keys, only the values. For example:

```json
{
  "strings": {
    "help_title": "Je peux vous aider avec Firefly en langage naturel.",
    "draft_committed": "Transaction enregistree avec succes.",
    "draft_discarded": "Brouillon annule.",
    ...
  },
  "lists": {
    "help_natural_examples": [
      "combien d'argent ai-je",
      "montre-moi mon resume du mois",
      ...
    ]
  }
}
```

Then set the language in your `.env` so the bot uses it:

```env
FIREFLY_CHAT_LANGUAGE=fr
```

Or leave it as `auto` if you want the bot to detect the language per message. Detection for languages other than English and Italian is handled by the AI router, which understands most major languages regardless of the UI file.

Restart after adding the file:

```bash
docker compose restart companion
```

---

## Safety defaults

All write operations are dry-run by default. When the bot prepares a transaction it shows you a preview and waits for you to say "ok" or "confirm" before saving anything.

Additional safety features:

- Transactions above `FIREFLY_HIGH_VALUE_THRESHOLD` (default: 250.00) require an extra confirmation.
- Duplicate detection rejects transactions that look identical to something already saved within `FIREFLY_DEDUPE_WINDOW_DAYS` (default: 7 days).
- Delete operations are disabled by default. Set `FIREFLY_ALLOW_DELETE=true` to enable them.

To commit a write for real in a single command without the review step, add `live=yes` to your message. Example: `add expense €12 for coffee live=yes`.

---

## Mapping and policy files

After the first run, two config files appear in `workspace/config/`. You can edit them to teach the bot your account structure:

**`workspace/config/mappings.yml`** - account defaults and merchant rules:

```yaml
defaults:
  expense_source_account: Main Checking
  expense_destination_account: Misc Expenses
  income_source_account: Income Source
  income_destination_account: Main Checking

merchant_rules:
  coop:
    destination_account: Coop
    category: Groceries
  amazon:
    destination_account: Amazon
    category: Shopping
```

**`workspace/config/policy.yml`** - safety thresholds:

```yaml
writes:
  default_dry_run: true
  high_value_threshold: "250.00"
  dedupe_window_days: 7
  allow_delete: false
```

After editing either file, restart the companion:

```bash
docker compose restart companion
```

---

## Environment variables reference

These are the variables you are most likely to need. Copy `.env.example` to `.env` as a starting point.

| Variable | Required | Description |
|---|---|---|
| `FIREFLY_BASE_URL` | Yes | Full URL to your Firefly III instance |
| `TELEGRAM_OWNER_ID` | Yes | Your numeric Telegram user ID |
| `TELEGRAM_TARGET_ID` | Yes | Chat ID where the bot sends startup messages (usually same as owner) |
| `GOOGLE_API_KEY` | Yes (or another provider) | Google AI Studio API key for Gemini |
| `OPENAI_API_KEY` | Optional | OpenAI API key |
| `ANTHROPIC_API_KEY` | Optional | Anthropic API key |
| `OPENROUTER_API_KEY` | Optional | OpenRouter API key |
| `GROQ_API_KEY` | Optional | Groq API key |
| `PICOCLAW_DEFAULT_MODEL` | Optional | Override the AI model, e.g. `openai/gpt-4o-mini` |
| `FIREFLY_CHAT_LANGUAGE` | Optional | Force language: `en`, `it`, or `auto` (default) |
| `FIREFLY_DEFAULT_DRY_RUN` | Optional | Set to `false` to skip dry-run globally (not recommended) |
| `FIREFLY_HIGH_VALUE_THRESHOLD` | Optional | Amount above which extra confirmation is required (default: `250.00`) |
| `FIREFLY_ALLOW_DELETE` | Optional | Set to `true` to allow delete operations |
| `PDFAPIHUB_API_KEY` | Optional | API key for enhanced PDF/image OCR |
| `TZ` | Optional | Timezone for the container, e.g. `Europe/Rome` |

Firefly credentials and the Telegram token are read from files in `secrets/`, not directly from `.env`:

```
secrets/firefly_access_token.txt    - your Firefly III personal access token
secrets/telegram_bot_token.txt      - your Telegram bot token
```

---

## Token rotation

To update a token without going through setup again:

```bash
printf '%s' 'new-firefly-token' > secrets/firefly_access_token.txt
printf '%s' 'new-telegram-token' > secrets/telegram_bot_token.txt
chmod 600 secrets/*.txt
docker compose restart companion
```

The entrypoint rewrites the internal config files on every restart, so the new tokens are picked up automatically.

---

## Running the companion next to a test Firefly instance

If you want to run everything locally for testing and do not have a Firefly III instance yet:

```bash
docker compose -f docker-compose.yml -f docker-compose.example-firefly.yml --profile example-firefly up -d --build
```

This starts a local Firefly III at `http://127.0.0.1:8080`. Finish the Firefly setup in your browser, create a personal access token from the profile menu, save it to `secrets/firefly_access_token.txt`, then restart the companion:

```bash
docker compose restart companion
```

---

## CLI commands

You can run any operation directly from the command line inside the container:

```bash
# Health check
docker compose exec companion python3 -m firefly_companion.cli health

# List accounts
docker compose exec companion python3 -m firefly_companion.cli accounts list --type asset
docker compose exec companion python3 -m firefly_companion.cli accounts balances

# Categories and budgets
docker compose exec companion python3 -m firefly_companion.cli categories list
docker compose exec companion python3 -m firefly_companion.cli budgets list

# Summaries and transactions
docker compose exec companion python3 -m firefly_companion.cli summary month --month 2026-04
docker compose exec companion python3 -m firefly_companion.cli transactions search --days 7 --query groceries

# Dry-run expense
docker compose exec companion python3 -m firefly_companion.cli expense dry-run \
  --amount 42.50 \
  --description "Groceries" \
  --merchant coop
```

---

## Troubleshooting

**The bot does not respond on Telegram.**
Check that `TELEGRAM_OWNER_ID` matches your actual Telegram user ID. The bot ignores all messages from unknown IDs. Also confirm the bot token is correct with `docker compose logs -f companion`.

**HTTP 401 from Firefly III.**
Your personal access token is invalid, expired, or belongs to a different Firefly user. Generate a new one and update `secrets/firefly_access_token.txt`, then restart.

**The container stays unhealthy.**
Run `docker compose logs --tail=200 companion` to see the startup errors. Common causes: wrong Firefly URL, wrong Docker network name, or the AI provider key is missing.

**OCR on receipts is poor.**
Add a short text caption to the photo when you send it, like "supermarket receipt paid with card". The bot uses the caption as context. For better results, set `PDFAPIHUB_API_KEY` in your `.env`.

**The bot understood the request but used the wrong account.**
Run `/train` in the chat to re-configure your default expense and income accounts. You can also edit `workspace/config/mappings.yml` directly and restart.

**I want to debug what the bot is doing.**
```bash
docker compose logs -f companion
docker compose exec companion env | grep FIREFLY_
docker compose exec companion cat /home/picoclaw/.picoclaw/config.json
```

---

## Project layout

```
scripts/telegram_firefly_bot.py     - Telegram bot, NL parser, and intent routing
src/firefly_companion/              - Firefly API client, AI router, bridge logic
  ai_router.py                      - direct AI provider calls (Gemini, GPT, Claude, etc.)
  intent_parser.py                  - deterministic NL parsing
  conversation.py                   - language detection and i18n
  client.py                         - Firefly III REST API client
  bridge.py                         - MCP bridge between PicoClaw and Firefly
workspace/
  config/mappings.yml               - account defaults and merchant rules (local)
  config/policy.yml                 - safety policy (local)
  i18n/telegram_bot.en.json         - English UI strings
  i18n/telegram_bot.it.json         - Italian UI strings
  i18n/telegram_bot.{lang}.json     - add your own language here (e.g. telegram_bot.fr.json)
docker/entrypoint.sh                - container bootstrap
.env.example                        - environment variable template
```

---

## License

See [LICENSE](LICENSE).
