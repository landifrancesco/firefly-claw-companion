#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import requests
from requests import Response
from requests.exceptions import RequestException

from firefly_companion import conversation as conversation_module
from firefly_companion.ai_router import (
    RouterGatewayError as AIRouterGatewayError,
    call_ai_text,
    call_ai_vision,
    router_health,
)
from firefly_companion.bridge import BridgeService, flatten_transactions
from firefly_companion.client import FireflyAPIError, FireflyClient
from firefly_companion.config import BridgeSettings, ConfigurationError
from firefly_companion.conversation import build_clarification_prompt
from firefly_companion.draft_manager import DraftManager, DraftPhase, load_draft_session, save_draft_session
from firefly_companion.intent_parser import (
    extract_payment_method,
    extract_recurrence,
    fuzzy_match_category,
    parse_amount_from_text as _parse_amount_from_text,
    parse_deterministic_with_fallback,
    parse_natural_intent_payload as _parse_natural_intent_payload,
)
from firefly_companion.logging_utils import configure_logging
from firefly_companion.object_cache import FireflyObjectCache
from firefly_companion.receipt_parser import count_visible_transactions


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


STATE_PATH = Path(os.getenv("PICOCLAW_CONFIG_DIR", str(Path.home() / ".picoclaw"))) / "telegram-bot-state.json"
POLL_TIMEOUT_SECONDS = 30
ALLOWED_PRIVATE_CHAT_TYPES = {"private"}
TELEGRAM_TEXT_LIMIT = 3900
TELEGRAM_CAPTION_LIMIT = 1024
SETUP_ACCOUNT_CHOICE_LIMIT = env_int("TELEGRAM_SETUP_ACCOUNT_LIMIT", 50)
STARTUP_HEALTHCHECK_ENABLED = os.getenv("TELEGRAM_STARTUP_HEALTHCHECK_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
LIVE_PING_ENABLED = os.getenv("TELEGRAM_LIVE_PING_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
LIVE_PING_TIME = os.getenv("TELEGRAM_LIVE_PING_TIME", "09:00").strip()
ITALIC_MARKER_PATTERN = re.compile(r"(?<!\w)_(?!\s)([^_\n]+?)(?<!\s)_(?!\w)")
AUTOFILL_CACHE_SECONDS = 600
OFFLINE_RETRY_DELAY_SECONDS = 60 * 60
OFFLINE_RETRY_MAX_ATTEMPTS = 3
PROFILE_SETUP_STEPS = (
    "expense_source_account",
    "expense_destination_account",
    "income_source_account",
    "income_destination_account",
    "card_payment_account",
    "cash_payment_account",
    "ask_budget_when_missing",
    "auto_budget_from_history",
)
_AUTOFILL_TX_CACHE: dict[str, Any] = {"loaded_at": 0.0, "records": []}


class TelegramBotError(RuntimeError):
    pass


@dataclass(slots=True)
class BotResponse:
    text: str
    photo_path: str | None = None
    document_path: str | None = None
    document_filename: str | None = None
    # Future: buttons: list[list[dict]] for inline keyboard support
    # Requires callback_query handling in polling loop


@dataclass(slots=True)
class RouterContext:
    accounts: list[str]
    categories: list[str]
    budgets: list[str]
    merchant_shortcuts: list[str]
    loaded_at: float


INTENT_VALUES = {
    "help",
    "get_balances",
    "get_spending_total",
    "get_income_vs_spending",
    "list_accounts",
    "list_categories",
    "list_budgets",
    "get_summary",
    "get_recent",
    "compare_periods",
    "graph_balances",
    "graph_spending",
    "graph_cashflow",
    "graph_budget",
    "graph_recurrences",
    "top_spending_categories",
    "budget_report",
    "set_budget_limit",
    "list_recurrences",
    "create_recurrence",
    "delete_recurrence",
    "create_category",
    "create_budget",
    "create_account",
    "create_expense",
    "create_income",
    "create_transfer",
    "search_transactions",
    "clarify",
}

ROUTER_CONTEXT_CACHE_SECONDS = 300
_ROUTER_CONTEXT: RouterContext | None = None
LOCALE_DIR = Path(os.getenv("FIREFLY_BOT_LOCALE_DIR", str(Path(os.getenv("PICOCLAW_WORKSPACE", "workspace")) / "i18n")))

COMMON_TEXT_NORMALIZATIONS = (
    ("mistrami", "mostrami"),
    ("spesodi", "speso di"),
    ("spesoper", "speso per"),
    ("entratevs", "entrate vs"),
    ("uscitevs", "uscite vs"),
    ("teh", "the"),
    ("febbario", "febbraio"),
    ("genniao", "gennaio"),
    ("setembre", "settembre"),
    ("novenbre", "novembre"),
    ("dicenbre", "dicembre"),
)
DISPLAY_DATE_FORMAT = "%d-%m-%Y"
DATE_TOKEN_PATTERN = r"(?:\d{4}[-/.]\d{2}[-/.]\d{2}|\d{2}[-/.]\d{2}[-/.]\d{2,4})"
RECEIPT_SOURCE_HINTS = {
    "revolut": "Revolut",
    "bper": "BPER",
    "paypal": "PayPal",
    "n26": "N26",
    "wise": "Wise",
    "intesa": "Intesa",
    "unicredit": "UniCredit",
    "postepay": "Postepay",
    "hype": "Hype",
}
IGNORED_RECEIPT_MERCHANT_LINES = {
    "notification centre",
    "show less",
    "gruppo bper banca",
    "revolut",
    "documento commerciale",
    "di vendita o prestazione",
    "descrizione",
    "iva prezzo",
    "firma elettronica",
    "dettaglio pagamenti",
    "verified by device",
    "transazione eseguita",
    "grazie e arrivederci",
}
DEFAULT_CATEGORY_NAMES = [
    "Casa",
    "Spesa",
    "Cibo",
    "Bar",
    "Servizi web",
    "Cultura",
    "Viaggi",
    "Telefonia",
    "Acquisti",
    "Regali",
    "Salute",
    "Sport",
    "Groceries",
    "Housing",
    "Travel",
    "Health",
]
RECEIPT_TOPIC_RULES = (
    {
        "keywords": ("supermercato", "mercato", "grocery", "grocer", "coop", "carrefour", "esselunga", "lidl", "aldi", "in's", "ins "),
        "description_it": "Spesa supermercato",
        "description_en": "Grocery shopping",
        "categories": ("Spesa", "Groceries", "Cibo", "Acquisti"),
    },
    {
        "keywords": ("caffe", "caffè", "coffee", "bar", "argenta", "vending"),
        "description_it": "Caffè",
        "description_en": "Coffee",
        "categories": ("Bar", "Cibo", "Acquisti"),
    },
    {
        "keywords": ("ristorante", "restaurant", "pizza", "pizzeria", "trattoria", "pub", "bistro"),
        "description_it": "Ristorante",
        "description_en": "Restaurant",
        "categories": ("Cibo", "Bar", "Acquisti"),
    },
    {
        "keywords": ("nuotatori", "swim", "swimming", "pool", "piscina", "gym", "fitness", "sport"),
        "description_it": "Sport",
        "description_en": "Sport",
        "categories": ("Sport", "Salute", "Health"),
    },
    {
        "keywords": ("bus", "train", "metro", "taxi", "uber", "parking", "parcheggio", "autostrade", "trenitalia", "italo"),
        "description_it": "Trasporto",
        "description_en": "Transport",
        "categories": ("Viaggi", "Travel", "Acquisti"),
    },
    {
        "keywords": ("farmacia", "pharmacy", "doctor", "dentist", "medic", "salute", "health"),
        "description_it": "Salute",
        "description_en": "Health",
        "categories": ("Salute", "Health"),
    },
    {
        "keywords": ("amazon", "ebay", "online", "shop", "store", "negozio"),
        "description_it": "Acquisto online",
        "description_en": "Online purchase",
        "categories": ("Acquisti", "Acquisti online", "Shopping"),
    },
    {
        "keywords": ("openai", "vercel", "github", "digitalocean", "aws", "google", "icloud", "netlify"),
        "description_it": "Servizi web",
        "description_en": "Web services",
        "categories": ("Servizi web",),
    },
    {
        "keywords": ("tim", "vodafone", "iliad", "wind", "telefon", "phone", "fastweb"),
        "description_it": "Telefonia",
        "description_en": "Phone",
        "categories": ("Telefonia",),
    },
)


def ascii_fold(value: str) -> str:
    return conversation_module.ascii_fold(value)


def normalize_natural_text(text: str | None) -> str:
    normalized = conversation_module.normalize_natural_text(text)
    for source, target in COMMON_TEXT_NORMALIZATIONS:
        normalized = normalized.replace(source, target)
    return " ".join(normalized.split())


def clip_text(value: str | None, limit: int = 280) -> str | None:
    return conversation_module.clip_text(value, limit)


def parse_flexible_date(value: str | date | datetime | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    cleaned = str(value).strip()
    if not cleaned:
        return None
    if "T" in cleaned:
        cleaned = cleaned.split("T", 1)[0]

    relative = parse_relative_date_hint(cleaned)
    if relative is not None:
        return relative

    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d/%m/%y", "%d.%m.%y", "%d-%m-%y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def parse_date_safe(date_str: str) -> tuple[date | None, str]:
    """Parse date and detect ambiguity (DD/MM vs MM/DD).

    Returns (date, confidence) where confidence is:
    - 'certain': unambiguous (year present, or one part > 12)
    - 'ambiguous': could be day/month or month/day (e.g., 22/04)
    - 'invalid': parsing failed
    """
    result = parse_flexible_date(date_str)
    if result:
        return result, "certain"

    # Check for ambiguous DD/MM pattern
    match = re.match(r"(\d+)[./](\d+)", date_str.strip())
    if match:
        a, b = int(match.group(1)), int(match.group(2))
        # If one part > 12, it can only be day
        if a > 12:
            try:
                parsed = datetime.strptime(date_str, "%d/%m").replace(year=date.today().year).date()
                return parsed, "certain"
            except ValueError:
                pass
        elif b > 12:
            try:
                parsed = datetime.strptime(date_str, "%m/%d").replace(year=date.today().year).date()
                return parsed, "certain"
            except ValueError:
                pass
        # Both could be valid day and month
        elif 1 <= a <= 31 and 1 <= b <= 12 and a != b:
            return None, "ambiguous"

    return None, "invalid"


def parse_relative_date_hint(text: str | None) -> date | None:
    lowered = normalize_natural_text(text)
    if not lowered:
        return None
    today = date.today()
    if lowered in {"today", "oggi"}:
        return today
    if lowered in {"yesterday", "ieri"}:
        return today - timedelta(days=1)
    if lowered in {"day before yesterday", "l altro ieri", "altro ieri"}:
        return today - timedelta(days=2)

    match_days_ago = re.search(r"\b(\d{1,3})\s+days?\s+ago\b", lowered)
    if match_days_ago:
        return today - timedelta(days=max(int(match_days_ago.group(1)), 0))

    match_days_ago_it = re.search(r"\b(\d{1,3})\s+giorni?\s+fa\b", lowered)
    if match_days_ago_it:
        return today - timedelta(days=max(int(match_days_ago_it.group(1)), 0))
    return None


def extract_relative_or_explicit_date_from_text(text: str) -> str | None:
    lowered = normalize_natural_text(text)
    today = date.today()
    if "day before yesterday" in lowered or "altro ieri" in lowered or "l altro ieri" in lowered:
        return (today - timedelta(days=2)).isoformat()
    if "yesterday" in lowered or "ieri" in lowered:
        return (today - timedelta(days=1)).isoformat()
    if "today" in lowered or "oggi" in lowered:
        return today.isoformat()
    relative_match = re.search(r"\b(\d{1,3})\s+(?:days?\s+ago|giorni?\s+fa)\b", lowered)
    if relative_match:
        parsed = parse_relative_date_hint(relative_match.group(0))
        if parsed:
            return parsed.isoformat()

    explicit = re.search(rf"\b({DATE_TOKEN_PATTERN})\b", lowered)
    if explicit:
        parsed = parse_flexible_date(explicit.group(1))
        if parsed:
            return parsed.isoformat()
    # fallback "today"
    if lowered in {"now", "adesso"}:
        return today.isoformat()
    return None


def enforce_deterministic_transaction_date(payload: dict[str, Any], text: str | None) -> dict[str, Any]:
    """Make user-specified transaction dates win over router-provided dates."""
    intent = str(payload.get("intent") or "").strip()
    if intent not in {"create_expense", "create_income", "create_transfer", "create_transaction_batch"}:
        return payload

    date_value = extract_relative_or_explicit_date_from_text(text or "")
    if not date_value:
        return payload

    params = payload.get("params")
    if not isinstance(params, dict):
        params = {}
    if intent == "create_transaction_batch":
        transactions = params.get("transactions")
        if isinstance(transactions, list):
            for item in transactions:
                if not isinstance(item, dict):
                    continue
                item_params = item.get("params")
                if isinstance(item_params, dict):
                    item_params["date"] = date_value
        payload["params"] = params
        return payload

    payload["params"] = {**params, "date": date_value}
    return payload


def coerce_transaction_date(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}T", raw):
        return raw[:10]
    parsed = parse_flexible_date(raw)
    if parsed is not None:
        return parsed.isoformat()
    return raw


def format_display_date(value: str | date | datetime | None) -> str:
    parsed = parse_flexible_date(value)
    if parsed is None:
        return str(value or "").strip() or "?"
    return parsed.strftime(DISPLAY_DATE_FORMAT)


def format_display_period(start: str | date | datetime, end: str | date | datetime) -> str:
    return f"{format_display_date(start)} - {format_display_date(end)}"


def find_tesseract_executable() -> str | None:
    configured = os.getenv("TESSERACT_CMD", "").strip()
    candidates = [
        configured,
        shutil.which("tesseract"),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        "/usr/bin/tesseract",
        "/usr/local/bin/tesseract",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def explicit_language_hint(text: str | None) -> str | None:
    return conversation_module.explicit_language_hint(text)


def configured_chat_language() -> str:
    return conversation_module.configured_chat_language()


def detect_language(text: str | None) -> str:
    return conversation_module.detect_language(text)


def localize(text_en: str, text_it: str | None = None, *, source_text: str | None = None) -> str:
    return conversation_module.localize(text_en, text_it, source_text=source_text)


def locale_language(source_text: str | None = None) -> str:
    return conversation_module.locale_language(source_text)


def load_locale_catalog(language: str) -> dict[str, Any]:
    conversation_module.set_locale_dir(LOCALE_DIR)
    return conversation_module.load_locale_catalog(language)


def locale_value(section: str, key: str, *, source_text: str | None = None) -> Any:
    conversation_module.set_locale_dir(LOCALE_DIR)
    return conversation_module.locale_value(section, key, source_text=source_text)


def bot_text(key: str, *, source_text: str | None = None, **kwargs: Any) -> str:
    conversation_module.set_locale_dir(LOCALE_DIR)
    return conversation_module.bot_text(key, source_text=source_text, **kwargs)


def bot_text_or_default(key: str, default_en: str, default_it: str, *, source_text: str | None = None, **kwargs: Any) -> str:
    try:
        return bot_text(key, source_text=source_text, **kwargs)
    except KeyError:
        value = localize(default_en, default_it, source_text=source_text)
        return value.format(**kwargs) if kwargs else value


def bot_list(key: str, *, source_text: str | None = None) -> list[str]:
    conversation_module.set_locale_dir(LOCALE_DIR)
    return conversation_module.bot_list(key, source_text=source_text)


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"offset": 0, "initialized": False}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"offset": 0, "initialized": False}


def save_state(state: dict[str, Any]) -> None:
    import shutil
    import tempfile
    import time

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["_version"] = 2
    state["_last_modified"] = time.time()

    # Atomic write via temp file
    with tempfile.NamedTemporaryFile(mode='w', dir=STATE_PATH.parent, delete=False, suffix='.json') as f:
        json.dump(state, f, indent=2, sort_keys=True)
        temp_path = f.name

    try:
        # Backup old state if it exists
        if STATE_PATH.exists():
            shutil.copy2(STATE_PATH, STATE_PATH.with_suffix('.json.bak'))

        # Move temp to final location (atomic on most filesystems)
        shutil.move(temp_path, STATE_PATH)
    except Exception:
        # Clean up temp file on failure
        try:
            Path(temp_path).unlink()
        except Exception:
            pass
        raise


def retry_state_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    snapshot = json.loads(json.dumps(state))
    for key in ("retry_queue", "offset", "initialized", "_version", "_last_modified", "last_live_ping_date"):
        snapshot.pop(key, None)
    return snapshot


def enqueue_offline_retry(state: dict[str, Any], *, text: str, chat_id: str, source_text: str | None = None) -> BotResponse:
    queue = state.get("retry_queue")
    if not isinstance(queue, list):
        queue = []
    now = time.time()
    queue.append(
        {
            "text": text,
            "chat_id": chat_id,
            "attempts": 0,
            "max_attempts": OFFLINE_RETRY_MAX_ATTEMPTS,
            "next_at": now + OFFLINE_RETRY_DELAY_SECONDS,
            "created_at": now,
            "state": retry_state_snapshot(state),
        }
    )
    state["retry_queue"] = queue
    return BotResponse(
        localize(
            "Firefly looks offline. I queued this request and will retry up to 3 times, once per hour.",
            "Firefly sembra offline. Ho messo questa richiesta in coda e riprovero fino a 3 volte, una volta all'ora.",
            source_text=source_text,
        )
    )


def process_due_offline_retries(service: BridgeService, bot_token: str, state: dict[str, Any]) -> None:
    queue = state.get("retry_queue")
    if not isinstance(queue, list) or not queue:
        return
    now = time.time()
    kept: list[dict[str, Any]] = []
    changed = False
    for item in queue:
        if not isinstance(item, dict):
            changed = True
            continue
        next_at = float(item.get("next_at") or 0)
        if next_at > now:
            kept.append(item)
            continue

        text = str(item.get("text") or "").strip()
        chat_id = str(item.get("chat_id") or "").strip()
        attempts = int(item.get("attempts") or 0)
        max_attempts = int(item.get("max_attempts") or OFFLINE_RETRY_MAX_ATTEMPTS)
        retry_state = item.get("state") if isinstance(item.get("state"), dict) else {}
        draft_state_keys = ("draft", "pending_action", "pending_transaction", "pending_draft_account_fix")
        retry_had_draft_state = any(key in retry_state for key in draft_state_keys)
        if not text or not chat_id or attempts >= max_attempts:
            changed = True
            continue

        try:
            response = process_message(service, text, retry_state)
        except (ConfigurationError, FireflyAPIError, ValueError, RuntimeError) as exc:
            if is_firefly_offline_error(exc):
                attempts += 1
                if attempts >= max_attempts:
                    send_message(
                        bot_token,
                        chat_id,
                        localize(
                            "Firefly is still offline, so I stopped retrying that queued request after 3 attempts.",
                            "Firefly e ancora offline, quindi ho interrotto quella richiesta in coda dopo 3 tentativi.",
                            source_text=text,
                        ),
                    )
                    changed = True
                    continue
                item["attempts"] = attempts
                item["next_at"] = now + OFFLINE_RETRY_DELAY_SECONDS
                kept.append(item)
                changed = True
                continue
            response = BotResponse(
                localize(
                    "A queued request could not be completed. Please resend it when Firefly is available.",
                    "Una richiesta in coda non e stata completata. Rimandala quando Firefly e disponibile.",
                    source_text=text,
                )
            )

        if response.photo_path:
            send_photo(bot_token, chat_id, response.photo_path, response.text)
            try:
                Path(response.photo_path).unlink(missing_ok=True)
            except OSError:
                pass
        elif response.document_path:
            send_document(bot_token, chat_id, response.document_path, response.text, response.document_filename)
            try:
                Path(response.document_path).unlink(missing_ok=True)
            except OSError:
                pass
        else:
            send_message(bot_token, chat_id, response.text)

        if retry_had_draft_state or any(key in retry_state for key in draft_state_keys):
            for key in draft_state_keys:
                if key in retry_state:
                    state[key] = retry_state[key]
                else:
                    state.pop(key, None)
        changed = True

    state["retry_queue"] = kept
    if changed:
        save_state(state)


def telegram_request(bot_token: str, method: str, *, params: dict[str, Any] | None = None, data: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    try:
        response: Response = requests.post(url, params=params, data=data, timeout=POLL_TIMEOUT_SECONDS + 5)
    except RequestException as exc:
        raise TelegramBotError(f"Telegram request failed for {method}: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise TelegramBotError(f"Telegram returned non-JSON data for {method}: {response.text[:200]}") from exc

    if response.status_code >= 400 or not payload.get("ok"):
        raise TelegramBotError(f"Telegram returned an error for {method}: {payload}")
    return payload


def send_typing_action(bot_token: str, chat_id: int) -> None:
    """Send 'typing...' action to indicate bot is processing."""
    try:
        telegram_request(bot_token, "sendChatAction", data={"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass  # Non-critical, don't disrupt flow on failure


def _strip_markdown_artifacts(text: str) -> str:
    cleaned = text.replace("**", "").replace("__", "").replace("`", "")
    while True:
        updated = ITALIC_MARKER_PATTERN.sub(r"\1", cleaned)
        if updated == cleaned:
            break
        cleaned = updated
    return cleaned


def prepare_telegram_text(text: str | None, *, limit: int) -> str:
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    value = _strip_markdown_artifacts(value)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    if not value:
        value = "No content."
    if len(value) > limit:
        value = value[: max(limit - 3, 1)].rstrip() + "..."
    return value


def send_message(bot_token: str, chat_id: str, text: str) -> None:
    safe_text = prepare_telegram_text(text, limit=TELEGRAM_TEXT_LIMIT)
    telegram_request(
        bot_token,
        "sendMessage",
        data={
            "chat_id": chat_id,
            "text": safe_text,
            "disable_web_page_preview": "true",
        },
    )


COMMAND_ALIASES: dict[str, str] = {
    "/aiuto":        "/help",
    "/comandi":      "/commands",
    "/configura":    "/setup",
    "/training":     "/train",
    "/allena":       "/train",
    "/impara":       "/train",
    "/aggiungi":     "/add",
    "/saldi":        "/balances",
    "/riepilogo":    "/summary",
    "/recenti":      "/recent",
    "/principali":   "/topcategories",
    "/reportbudget": "/budgetreport",
    "/impostabudget": "/setbudgetlimit",
    "/manutenzione": "/maintenance",
    "/cerca":        "/search",
    "/salute":       "/health",
    "/esporta":      "/backup",
    "/backupdata":   "/backup",
    "/annulla":      "/cancel",
    "/balance":      "/balances",
    "/command":      "/commands",
    "/conti":        "/accounts",
    "/account":      "/accounts",
    "/categorie":    "/categories",
    "/budget":       "/budgets",
    "/ricorrenze":   "/recurrences",
    "/grafico":      "/graph",
    "/spesa":        "/expense",
    "/entrata":      "/income",
    "/trasferimento": "/transfer",
    "/nuovacategoria": "/newcategory",
    "/nuovobudget":  "/newbudget",
    "/nuovoconto":   "/newaccount",
    "/clona":        "/clone",
    "/stop":         "/cancel",
}


def ensure_telegram_commands(bot_token: str) -> None:
    commands_en = [
        {"command": "help", "description": "Help and examples"},
        {"command": "commands", "description": "Full command list"},
        {"command": "setup", "description": "Show or reset finance profile"},
        {"command": "train", "description": "Teach account defaults and aliases"},
        {"command": "add", "description": "Interactive transaction add"},
        {"command": "balances", "description": "Show balances"},
        {"command": "health", "description": "Health check"},
        {"command": "summary", "description": "Monthly/custom summary"},
        {"command": "recent", "description": "Recent transactions"},
        {"command": "search", "description": "Search transactions by keyword"},
        {"command": "topcategories", "description": "Top spending categories"},
        {"command": "budgetreport", "description": "Budget report"},
        {"command": "backup", "description": "Send JSON backup"},
        {"command": "maintenance", "description": "Cleanup mode"},
    ]
    commands_it = [
        {"command": "aiuto", "description": "Aiuto ed esempi"},
        {"command": "comandi", "description": "Lista comandi completa"},
        {"command": "configura", "description": "Mostra o resetta il profilo"},
        {"command": "impara", "description": "Insegna conti e alias"},
        {"command": "aggiungi", "description": "Aggiunta transazione guidata"},
        {"command": "saldi", "description": "Mostra saldi"},
        {"command": "salute", "description": "Controllo stato"},
        {"command": "riepilogo", "description": "Riepilogo mese o intervallo"},
        {"command": "recenti", "description": "Transazioni recenti"},
        {"command": "cerca", "description": "Cerca transazioni per parola chiave"},
        {"command": "principali", "description": "Categorie principali per spesa"},
        {"command": "reportbudget", "description": "Report budget"},
        {"command": "grafico", "description": "Grafici saldi, spese o flusso"},
        {"command": "esporta", "description": "Invia backup JSON"},
        {"command": "manutenzione", "description": "Modalita manutenzione"},
    ]
    telegram_request(
        bot_token,
        "setMyCommands",
        data={
            "commands": json.dumps(commands_en, ensure_ascii=False),
            "scope": json.dumps({"type": "all_private_chats"}),
            "language_code": "en",
        },
    )
    telegram_request(
        bot_token,
        "setMyCommands",
        data={
            "commands": json.dumps(commands_it, ensure_ascii=False),
            "scope": json.dumps({"type": "all_private_chats"}),
            "language_code": "it",
        },
    )
    default_commands = commands_it if configured_chat_language() == "it" else commands_en
    telegram_request(
        bot_token,
        "setMyCommands",
        data={
            "commands": json.dumps(default_commands, ensure_ascii=False),
            "scope": json.dumps({"type": "all_private_chats"}),
        },
    )


def send_photo(bot_token: str, chat_id: str, photo_path: str, caption: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    safe_caption = prepare_telegram_text(caption, limit=TELEGRAM_CAPTION_LIMIT)
    try:
        with open(photo_path, "rb") as handle:
            response = requests.post(
                url,
                data={
                    "chat_id": chat_id,
                    "caption": safe_caption,
                    "disable_notification": "false",
                },
                files={"photo": handle},
                timeout=POLL_TIMEOUT_SECONDS + 10,
            )
    except OSError as exc:
        raise TelegramBotError(f"Could not open photo for Telegram upload: {exc}") from exc
    except RequestException as exc:
        raise TelegramBotError(f"Telegram sendPhoto failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise TelegramBotError(f"Telegram returned non-JSON data for sendPhoto: {response.text[:200]}") from exc

    if response.status_code >= 400 or not payload.get("ok"):
        raise TelegramBotError(f"Telegram returned an error for sendPhoto: {payload}")


def send_document(bot_token: str, chat_id: str, document_path: str, caption: str, filename: str | None = None) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    safe_caption = prepare_telegram_text(caption, limit=TELEGRAM_CAPTION_LIMIT)
    try:
        with open(document_path, "rb") as handle:
            response = requests.post(
                url,
                data={
                    "chat_id": chat_id,
                    "caption": safe_caption,
                    "disable_notification": "false",
                },
                files={"document": (filename or Path(document_path).name, handle, "application/json")},
                timeout=POLL_TIMEOUT_SECONDS + 30,
            )
    except OSError as exc:
        raise TelegramBotError(f"Could not open document for Telegram upload: {exc}") from exc
    except RequestException as exc:
        raise TelegramBotError(f"Telegram sendDocument failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise TelegramBotError(f"Telegram returned non-JSON data for sendDocument: {response.text[:200]}") from exc

    if response.status_code >= 400 or not payload.get("ok"):
        raise TelegramBotError(f"Telegram returned an error for sendDocument: {payload}")


def fetch_telegram_file_bytes(bot_token: str, file_id: str) -> tuple[bytes, str]:
    payload = telegram_request(bot_token, "getFile", params={"file_id": file_id})
    file_path = str((payload.get("result") or {}).get("file_path") or "").strip()
    if not file_path:
        raise TelegramBotError("Telegram did not return a file path for the uploaded image.")

    url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
    try:
        response = requests.get(url, timeout=POLL_TIMEOUT_SECONDS + 10)
        response.raise_for_status()
    except RequestException as exc:
        raise TelegramBotError(f"Could not download Telegram file: {exc}") from exc

    suffix = Path(file_path).suffix.casefold()
    mime_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".pdf": "application/pdf",
    }.get(suffix, "image/jpeg")
    return response.content, mime_type


def format_accounts(accounts: list[dict[str, Any]], *, source_text: str | None = None) -> str:
    if not accounts:
        return localize("No accounts found.", "Nessun conto trovato.", source_text=source_text)
    lines = [localize("Accounts:", "Conti:", source_text=source_text)]
    for account in accounts:
        name = account.get("attributes", {}).get("name") or account.get("name") or "<unnamed>"
        account_type = account.get("attributes", {}).get("type") or account.get("type") or "unknown"
        lines.append(f"- {name} [{account_type}]")
    return "\n".join(lines[:50])


def format_balances(balances: list[dict[str, Any]], *, source_text: str | None = None) -> str:
    if not balances:
        return localize("No asset balances found.", "Nessun saldo patrimoniale trovato.", source_text=source_text)

    totals: dict[str, float] = {}
    lines = [localize("Balances:", "Saldi:", source_text=source_text)]
    for account in balances:
        name = account.get("name") or "<unnamed>"
        currency = account.get("currency_code") or ""
        raw_amount = str(account.get("current_balance") or "0")
        try:
            amount_str = f"€{float(raw_amount.replace(',', '.')):.2f}"
        except (ValueError, TypeError):
            amount_str = raw_amount
        lines.append(f"- {name}: {amount_str} {currency}".rstrip())
        try:
            totals[currency] = totals.get(currency, 0.0) + float(raw_amount)
        except ValueError:
            pass

    if totals:
        lines.append("")
        lines.append(localize("Totals:", "Totali:", source_text=source_text))
        for currency, total in sorted(totals.items()):
            lines.append(f"- {total:.2f} {currency}".rstrip())
    return "\n".join(lines[:80])


def format_named_records(label: str, items: list[dict[str, Any]], *, source_text: str | None = None) -> str:
    if not items:
        return localize(f"No {label.lower()} found.", f"Nessun elemento trovato per {label.lower()}.", source_text=source_text)
    lines = [f"{label}:"]
    for item in items[:50]:
        name = item.get("attributes", {}).get("name") or item.get("name") or "<unnamed>"
        lines.append(f"- {name}")
    return "\n".join(lines)


def format_transactions(records: list[dict[str, Any]], *, title: str, source_text: str | None = None) -> str:
    if not records:
        return f"{title}\n{localize('No transactions found.', 'Nessuna transazione trovata.', source_text=source_text)}"
    lines = [title]
    for record in records[:15]:
        date = format_display_date(str(record.get("date", ""))[:10])
        try:
            amount_str = f"€{float(str(record.get('amount', 0)).replace(',', '.')):.2f}"
        except (ValueError, TypeError):
            amount_str = str(record.get("amount", ""))
        description = record.get("description") or "<no description>"
        category = record.get("category_name") or "-"
        lines.append(f"- {date} | {amount_str} | {description} | {category}")
    return "\n".join(lines)


def format_summary(payload: dict[str, Any], *, source_text: str | None = None) -> str:
    label = payload.get("label") or payload.get("month") or "<unknown>"
    summary = payload.get("summary", {})
    data = summary.get("data") if isinstance(summary, dict) else None

    lines = [localize(f"Summary for {label}:", f"Riepilogo per {label}:", source_text=source_text)]
    if isinstance(data, list) and data:
        for item in data[:20]:
            if not isinstance(item, dict):
                continue
            label = item.get("title") or item.get("name") or item.get("key") or "item"
            value = item.get("monetary_value") or item.get("value") or item.get("amount") or item.get("native_value") or "n/a"
            lines.append(f"- {label}: {value}")
        return "\n".join(lines)

    if isinstance(summary, dict) and summary:
        for key, item in list(summary.items())[:20]:
            if isinstance(item, dict):
                label = item.get("title") or item.get("name") or item.get("key") or key
                value = item.get("value_parsed") or item.get("monetary_value") or item.get("value") or item.get("amount") or "n/a"
            else:
                label = key
                value = item
            lines.append(f"- {label}: {value}")
        return "\n".join(lines)

    lines.append(json.dumps(summary, indent=2, sort_keys=True)[:3000])
    return "\n".join(lines)


def summary_metric_value(summary: dict[str, Any], metric_keyword: str) -> str | None:
    if not isinstance(summary, dict):
        return None
    for key, item in summary.items():
        if metric_keyword not in str(key).casefold():
            continue
        if isinstance(item, dict):
            return str(item.get("value_parsed") or item.get("monetary_value") or item.get("value") or item.get("amount") or "")
        return str(item)
    return None


def format_spending_total(label: str, summary: dict[str, Any], *, source_text: str | None = None) -> str:
    amount = summary_metric_value(summary, "spent")
    if not amount:
        return localize(
            f"I could not find the spending total for {label}.",
            f"Non sono riuscito a trovare il totale speso per {label}.",
            source_text=source_text,
        )
    return localize(
        f"You spent {amount} in {label}.",
        f"Hai speso {amount} in {label}.",
        source_text=source_text,
    )


def format_income_vs_spending(label: str, summary: dict[str, Any], *, source_text: str | None = None) -> str:
    earned = summary_metric_value(summary, "earned")
    spent = summary_metric_value(summary, "spent")
    if not earned and not spent:
        return localize(
            f"I could not find income and spending totals for {label}.",
            f"Non sono riuscito a trovare entrate e uscite per {label}.",
            source_text=source_text,
        )
    lines = [localize(f"Income and spending for {label}:", f"Entrate e uscite per {label}:", source_text=source_text)]
    if earned:
        lines.append(localize(f"- Earned: {earned}", f"- Entrate: {earned}", source_text=source_text))
    if spent:
        lines.append(localize(f"- Spent: {spent}", f"- Uscite: {spent}", source_text=source_text))
    return "\n".join(lines)


def format_transaction_preview(payload: dict[str, Any], *, intro: str, outro: str, source_text: str | None = None) -> str:
    tx = (payload.get("transactions") or [{}])[0]
    date_value = format_display_date(str(tx.get("date", ""))[:10])
    type_label = localized_transaction_type(tx.get("type", "transaction"), source_text=source_text)
    amount_label = format_money(tx.get("amount"))
    headline = localize(
        f"{type_label} {amount_label} on {date_value}",
        f"{type_label} {amount_label} del {date_value}",
        source_text=source_text,
    )
    return (
        f"{intro}\n"
        f"{headline}\n"
        f"{tx.get('description', 'No description')}\n"
        f"{tx.get('source_name', '-') or '-'} -> {tx.get('destination_name', '-') or '-'}\n"
        f"{outro}"
    )


def format_transaction_batch_preview(
    payloads: list[dict[str, Any]],
    *,
    intro: str,
    outro: str,
    source_text: str | None = None,
) -> str:
    lines = [intro, localize(f"{len(payloads)} transactions prepared:", f"{len(payloads)} transazioni preparate:", source_text=source_text)]
    for payload in payloads[:10]:
        tx = (payload.get("transactions") or [{}])[0]
        lines.append(
            "- "
            + " | ".join(
                [
                    format_display_date(str(tx.get("date", ""))[:10]),
                    format_money(tx.get("amount")),
                    str(tx.get("description") or "No description"),
                    str(tx.get("category_name") or "-"),
                ]
            )
        )
    lines.append(outro)
    return "\n".join(lines)


def format_duplicate_blocked(duplicate: dict[str, Any], *, source_text: str | None = None) -> str:
    return localize(
        "Blocked as a likely duplicate.\n"
        f"{format_display_date(str(duplicate.get('date', ''))[:10])} | {format_money(duplicate.get('amount'))} | {duplicate.get('description')}",
        "Bloccata come possibile duplicato.\n"
        f"{format_display_date(str(duplicate.get('date', ''))[:10])} | {format_money(duplicate.get('amount'))} | {duplicate.get('description')}",
        source_text=source_text,
    )


def _first_payload_transaction(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    transactions = payload.get("transactions")
    if isinstance(transactions, list) and transactions and isinstance(transactions[0], dict):
        return transactions[0]
    return {}


def _merged_created_transaction(result: dict[str, Any], fallback_payload: dict[str, Any] | None) -> dict[str, Any]:
    fallback_tx = _first_payload_transaction(fallback_payload)
    data = result.get("data")
    if isinstance(data, dict):
        attributes = data.get("attributes", {})
        transactions = attributes.get("transactions", [])
        if isinstance(transactions, list) and transactions and isinstance(transactions[0], dict):
            tx = dict(fallback_tx)
            tx.update({key: value for key, value in transactions[0].items() if value not in (None, "")})
            if not tx.get("description") and attributes.get("group_title"):
                tx["description"] = attributes.get("group_title")
            return tx
    return dict(fallback_tx)


def _transaction_group_id(record: dict[str, Any]) -> str:
    return str(record.get("journal_id") or record.get("transaction_id") or "").strip()


def _remember_last_committed_txn(
    state: dict[str, Any],
    created: dict[str, Any],
    fallback_payload: dict[str, Any] | None = None,
) -> None:
    tx_data = _merged_created_transaction(created, fallback_payload)
    group_id = ""
    data = created.get("data") if isinstance(created, dict) else None
    if isinstance(data, dict) and data.get("id") is not None:
        group_id = str(data.get("id")).strip()
    if not group_id:
        group_id = _transaction_group_id(tx_data)
    if not group_id:
        return
    state["last_committed_txn"] = {
        "id": group_id,
        "description": str(tx_data.get("description") or ""),
        "amount": str(tx_data.get("amount") or ""),
        "type": str(tx_data.get("type") or "").strip() or None,
    }


def _field_or_missing(value: Any, *, source_text: str | None = None) -> str:
    text = str(value or "").strip()
    if text:
        return text
    return localize("not set", "non impostato", source_text=source_text)


def format_created_transaction_result(
    result: dict[str, Any],
    *,
    fallback_payload: dict[str, Any] | None = None,
    source_text: str | None = None,
) -> str:
    success_title = localize(
        "Transaction created successfully.",
        "Transazione creata con successo.",
        source_text=source_text,
    )
    tx = _merged_created_transaction(result, fallback_payload)
    if tx:
        lines = [
            success_title,
            "",
            f"{localize('Type', 'Tipo', source_text=source_text)}: {localized_transaction_type(tx.get('type', 'transaction'), source_text=source_text)}",
            f"{localize('Amount', 'Importo', source_text=source_text)}: {format_money(tx.get('amount'))}",
            f"{localize('Date', 'Data', source_text=source_text)}: {format_display_date(str(tx.get('date', ''))[:10])}",
            f"{localize('Description', 'Descrizione', source_text=source_text)}: {_field_or_missing(tx.get('description'), source_text=source_text)}",
            f"{localize('From', 'Da', source_text=source_text)}: {_field_or_missing(tx.get('source_name'), source_text=source_text)}",
            f"{localize('To', 'A', source_text=source_text)}: {_field_or_missing(tx.get('destination_name'), source_text=source_text)}",
            f"{localize('Category', 'Categoria', source_text=source_text)}: {_field_or_missing(tx.get('category_name'), source_text=source_text)}",
            f"Budget: {_field_or_missing(tx.get('budget_name'), source_text=source_text)}",
            f"{localize('Mode', 'Modalita', source_text=source_text)}: live",
        ]
        return "\n".join(lines)

    return success_title


def format_pending_action_preview(title: str, lines: list[str], outro: str) -> str:
    return "\n".join([title, *lines, outro]).strip()


def remember_pending_action(state: dict[str, Any], *, kind: str, payload: dict[str, Any], preview: str) -> None:
    state["pending_action"] = {
        "kind": kind,
        "payload": payload,
        "preview": preview,
    }


def remember_pending_transaction(state: dict[str, Any], payload: dict[str, Any]) -> None:
    preview = format_transaction_preview(
        payload,
        intro=localize("Draft prepared. Nothing was written yet.", "Bozza preparata. Non ho ancora scritto nulla.", source_text="it" if configured_chat_language() == "it" else "en"),
        outro=localize("Say 'commit it' to write it for real.", "Scrivi 'commit it' per registrarla davvero.", source_text="it" if configured_chat_language() == "it" else "en"),
        source_text="it" if configured_chat_language() == "it" else "en",
    )
    remember_pending_action(state, kind="transaction_create", payload=payload, preview=preview)


def remember_pending_transaction_batch(state: dict[str, Any], payloads: list[dict[str, Any]], *, source_text: str | None = None) -> None:
    preview = format_transaction_batch_preview(
        payloads,
        intro=localize("Draft prepared. Nothing was written yet.", "Bozza preparata. Non ho ancora scritto nulla.", source_text=source_text),
        outro=localize("Say 'commit it' to write them for real.", "Scrivi 'commit it' per registrarle davvero.", source_text=source_text),
        source_text=source_text,
    )
    remember_pending_action(state, kind="transaction_batch_create", payload={"transactions": payloads}, preview=preview)


def clear_pending_action(state: dict[str, Any]) -> None:
    state.pop("pending_action", None)


def clear_pending_transaction(state: dict[str, Any]) -> None:
    pending = state.get("pending_action")
    if isinstance(pending, dict) and pending.get("kind") == "transaction_create":
        clear_pending_action(state)


def normalize_match_text(value: str | None) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", normalize_natural_text(value or "")).strip()


def match_choice(value: str, choices: list[str]) -> str | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.isdigit():
        index = int(cleaned) - 1
        if 0 <= index < len(choices):
            return choices[index]
    folded = normalize_match_text(cleaned)
    if not folded:
        return None
    for choice in choices:
        if normalize_match_text(choice) == folded:
            return choice
    for choice in choices:
        choice_folded = normalize_match_text(choice)
        if folded in choice_folded or choice_folded in folded:
            return choice
    return None


def unique_usable_names(names: list[str], *, limit: int) -> list[str]:
    result: list[str] = []
    for name in names:
        clean = usable_account_name(name)
        if not clean:
            continue
        result.append(clean)
        if len(result) >= limit:
            break
    return result


def account_choice_list(service: BridgeService, *, account_type: str = "all", limit: int = SETUP_ACCOUNT_CHOICE_LIMIT) -> list[str]:
    if account_type == "all":
        cache = FireflyObjectCache(service.client)
        names = cache.accounts() or summarize_name_list(service.client.list_accounts("all"), limit=limit * 2)
        return unique_usable_names(names, limit=limit)

    try:
        names = summarize_name_list(service.client.list_accounts(account_type), limit=limit * 2)
    except Exception:
        names = []
    typed_names = unique_usable_names(names, limit=limit)
    if typed_names:
        return typed_names
    return account_choice_list(service, account_type="all", limit=limit)


def setup_account_choices_for_step(service: BridgeService, step: str, *, limit: int = SETUP_ACCOUNT_CHOICE_LIMIT) -> list[str]:
    account_type_by_step = {
        "expense_source_account": "asset",
        "expense_destination_account": "expense",
        "income_source_account": "revenue",
        "income_destination_account": "asset",
        "card_payment_account": "asset",
        "cash_payment_account": "asset",
    }
    return account_choice_list(service, account_type=account_type_by_step.get(step, "all"), limit=limit)


def add_flow_account_choices(service: BridgeService, params: dict[str, Any], step: str, *, limit: int = 12) -> list[str]:
    intent = str(params.get("intent") or "").strip()
    return intent_account_choices(service, intent, step, limit=limit)


def intent_account_choices(service: BridgeService, intent: str, field: str, *, limit: int = 12) -> list[str]:
    if intent == "create_expense":
        account_type = "asset" if field == "source" else "expense"
    elif intent == "create_income":
        account_type = "revenue" if field == "source" else "asset"
    elif intent == "create_transfer":
        account_type = "asset"
    else:
        account_type = "all"
    return account_choice_list(service, account_type=account_type, limit=limit)


def usable_account_name(value: Any) -> str:
    return str(value or "").strip()


def parse_yes_no(value: str) -> bool | None:
    lowered = normalize_natural_text(value)
    if lowered in {"yes", "y", "si", "sì", "ok", "auto", "true"}:
        return True
    if lowered in {"no", "n", "skip", "false"}:
        return False
    return None


def get_finance_profile(state: dict[str, Any]) -> dict[str, Any]:
    profile = state.get("finance_profile")
    if isinstance(profile, dict):
        return dict(profile)
    return {
        "setup_complete": False,
        "ask_budget_when_missing": True,
        "auto_budget_from_history": True,
    }


def save_finance_profile(state: dict[str, Any], profile: dict[str, Any]) -> None:
    state["finance_profile"] = dict(profile)


def finance_profile_ready(state: dict[str, Any]) -> bool:
    profile = get_finance_profile(state)
    required = (
        "expense_source_account",
        "expense_destination_account",
        "income_destination_account",
    )
    return bool(profile.get("setup_complete")) and all(str(profile.get(key) or "").strip() for key in required)


def finance_profile_summary(state: dict[str, Any], *, source_text: str | None = None) -> str:
    profile = get_finance_profile(state)
    lines = [localize("Finance profile:", "Profilo finanziario:", source_text=source_text)]
    for key, label_en, label_it in (
        ("expense_source_account", "Expenses paid from", "Spese pagate da"),
        ("expense_destination_account", "Expenses recorded to", "Spese registrate verso"),
        ("income_source_account", "Income comes from", "Entrate provenienti da"),
        ("income_destination_account", "Income deposited into", "Entrate depositate su"),
    ):
        value = str(profile.get(key) or "-").strip() or "-"
        lines.append(f"- {localize(label_en, label_it, source_text=source_text)}: {value}")
    payment_accounts = profile.get("payment_method_accounts")
    if isinstance(payment_accounts, dict):
        for method, label_en, label_it in (
            ("card", "When I say card", "Quando dico carta"),
            ("cash", "When I say cash", "Quando dico contanti"),
            ("app", "When I say app", "Quando dico app"),
        ):
            value = str(payment_accounts.get(method) or "-").strip() or "-"
            lines.append(f"- {localize(label_en, label_it, source_text=source_text)}: {value}")
    lines.append(
        f"- {localize('Ask budget when missing', 'Chiedi budget se manca', source_text=source_text)}: "
        f"{'yes' if bool(profile.get('ask_budget_when_missing', True)) else 'no'}"
    )
    lines.append(
        f"- {localize('Auto budget from history', 'Budget automatico da storico', source_text=source_text)}: "
        f"{'yes' if bool(profile.get('auto_budget_from_history', True)) else 'no'}"
    )
    return "\n".join(lines)


def start_finance_setup(service: BridgeService, state: dict[str, Any], *, source_text: str | None = None) -> BotResponse:
    state.pop("add_flow", None)
    state.pop("maintenance_mode", None)
    state.pop("pending_transaction_resolution", None)
    state["profile_setup"] = {
        "step_index": 0,
        "profile": get_finance_profile(state),
    }
    return BotResponse(build_finance_setup_prompt(service, state, source_text=source_text))


def setup_overview_text(state: dict[str, Any], *, source_text: str | None = None) -> str:
    intro = localize(
        "Current setup is active." if finance_profile_ready(state) else "No complete finance profile yet.",
        "La configurazione attuale e attiva." if finance_profile_ready(state) else "Non c'e ancora un profilo finanziario completo.",
        source_text=source_text,
    )
    hint = localize(
        "Use /train to teach accounts and aliases. Use /setup reset to clear the profile.",
        "Usa /impara per insegnare conti e alias. Usa /configura reset per cancellare il profilo.",
        source_text=source_text,
    )
    return f"{intro}\n\n{finance_profile_summary(state, source_text=source_text)}\n\n{hint}"


def build_finance_setup_prompt(service: BridgeService, state: dict[str, Any], *, source_text: str | None = None) -> str:
    setup = state.get("profile_setup")
    if not isinstance(setup, dict):
        return localize("No setup is active.", "Nessuna configurazione attiva.", source_text=source_text)
    step_index = int(setup.get("step_index") or 0)
    step_index = max(0, min(step_index, len(PROFILE_SETUP_STEPS) - 1))
    step = PROFILE_SETUP_STEPS[step_index]
    account_options = setup_account_choices_for_step(service, step)

    if step in {
        "expense_source_account",
        "expense_destination_account",
        "income_source_account",
        "income_destination_account",
        "card_payment_account",
        "cash_payment_account",
    }:
        prompt_map = {
            "expense_source_account": localize(
                "Training 1/8. Which account usually pays expenses?",
                "Training 1/8. Da quale conto paghi di solito le spese?",
                source_text=source_text,
            ),
            "expense_destination_account": localize(
                "Training 2/8. Which Firefly expense account should receive expenses?",
                "Training 2/8. In quale conto spese di Firefly devo registrare le uscite?",
                source_text=source_text,
            ),
            "income_source_account": localize(
                "Training 3/8. Optional: who usually sends income? Reply 'skip' to leave empty.",
                "Training 3/8. Opzionale: da chi arrivano di solito le entrate? Rispondi 'skip' per lasciare vuoto.",
                source_text=source_text,
            ),
            "income_destination_account": localize(
                "Training 4/8. Which account receives income?",
                "Training 4/8. Su quale conto arrivano le entrate?",
                source_text=source_text,
            ),
            "card_payment_account": localize(
                "Training 5/8. When you say 'card', which account should I use? Reply 'skip' if none.",
                "Training 5/8. Quando dici 'carta', quale conto devo usare? Rispondi 'skip' se nessuno.",
                source_text=source_text,
            ),
            "cash_payment_account": localize(
                "Training 6/8. When you say 'cash/contanti', which account should I use? Reply 'skip' if none.",
                "Training 6/8. Quando dici 'contanti/cash', quale conto devo usare? Rispondi 'skip' se nessuno.",
                source_text=source_text,
            ),
        }
        lines = [prompt_map[step]]
        if account_options:
            list_labels = {
                "expense_source_account": localize("Asset accounts to choose from:", "Conti/carte da cui paghi:", source_text=source_text),
                "expense_destination_account": localize("Expense accounts to choose from:", "Conti spese in Firefly:", source_text=source_text),
                "income_source_account": localize("Revenue accounts to choose from:", "Conti entrata in Firefly:", source_text=source_text),
                "income_destination_account": localize("Asset accounts to choose from:", "Conti che ricevono denaro:", source_text=source_text),
                "card_payment_account": localize("Asset accounts to choose from:", "Conti/carte disponibili:", source_text=source_text),
                "cash_payment_account": localize("Asset accounts to choose from:", "Conti/casse disponibili:", source_text=source_text),
            }
            lines.append(list_labels.get(step, localize("Available accounts:", "Conti disponibili:", source_text=source_text)))
            for index, name in enumerate(account_options, start=1):
                lines.append(f"{index}. {name}")
        examples = {
            "expense_source_account": localize(
                "Example: for 'coffee paid by card', choose the card/checking/cash account. Do not choose the generic expense account here.",
                "Esempio: per 'caffe pagato con carta', scegli carta/conto/cassa. Non scegliere qui il conto spese generico.",
                source_text=source_text,
            ),
            "expense_destination_account": localize(
                "Example: for the same coffee, this is the merchant/expense side, often named Expenses, Out, or a shop account. This is not the card.",
                "Esempio: per lo stesso caffe, questo e il lato esercente/spesa, spesso chiamato Spese, Out o negozio. Non e la carta.",
                source_text=source_text,
            ),
            "income_source_account": localize(
                "Example: for 'salary 2500', this can be Employer or Salary. You can skip it if you do not use revenue accounts.",
                "Esempio: per 'stipendio 2500', puo essere Datore di lavoro o Stipendio. Puoi saltare se non usi conti entrata.",
                source_text=source_text,
            ),
            "income_destination_account": localize(
                "Example: salary lands in your checking account, card account, or cash account.",
                "Esempio: lo stipendio arriva sul conto corrente, carta o cassa.",
                source_text=source_text,
            ),
            "card_payment_account": localize(
                "Example: 'coffee paid by card' uses this as the source account.",
                "Esempio: 'caffe pagato con carta' usa questo come conto sorgente.",
                source_text=source_text,
            ),
            "cash_payment_account": localize(
                "Example: 'coffee cash' uses this as the source account.",
                "Esempio: 'caffe in contanti' usa questo come conto sorgente.",
                source_text=source_text,
            ),
        }
        lines.append(examples[step])
        lines.append(localize("Reply with account name or number.", "Rispondi con nome conto o numero.", source_text=source_text))
        return "\n".join(lines)

    if step == "ask_budget_when_missing":
        return localize(
            "Training 7/8. If budget is unclear, should I always ask before saving? (yes/no)\nExample: coffee could be Bar or Cibo.",
            "Training 7/8. Se il budget e incerto, devo sempre chiedere prima di salvare? (si/no)\nEsempio: un caffe puo stare in Bar o Cibo.",
            source_text=source_text,
        )

    return localize(
        "Training 8/8. Should I auto-assign budget from similar past transactions? (yes/no)\nExample: recurring supermarket expenses inherit the same budget.",
        "Training 8/8. Vuoi che assegni automaticamente il budget da transazioni passate simili? (si/no)\nEsempio: spese ricorrenti al supermercato ereditano lo stesso budget.",
        source_text=source_text,
    )


def handle_finance_setup_message(service: BridgeService, state: dict[str, Any], text: str) -> BotResponse | None:
    setup = state.get("profile_setup")
    if not isinstance(setup, dict):
        return None
    if text.strip().startswith("/"):
        return None

    if has_cancel_intent(text):
        state.pop("profile_setup", None)
        return BotResponse(localize("Setup cancelled.", "Setup annullato.", source_text=text))

    step_index = int(setup.get("step_index") or 0)
    if step_index >= len(PROFILE_SETUP_STEPS):
        state.pop("profile_setup", None)
        return BotResponse(finance_profile_summary(state, source_text=text))

    step = PROFILE_SETUP_STEPS[step_index]
    profile = dict(setup.get("profile") or get_finance_profile(state))
    account_options = setup_account_choices_for_step(service, step)
    answer = text.strip()

    if step in {
        "expense_source_account",
        "expense_destination_account",
        "income_source_account",
        "income_destination_account",
        "card_payment_account",
        "cash_payment_account",
    }:
        if step in {"income_source_account", "card_payment_account", "cash_payment_account"} and normalize_natural_text(answer) in {"skip", "salta", "none", "nessuno"}:
            if step == "income_source_account":
                profile["income_source_account"] = None
            else:
                payment_accounts = dict(profile.get("payment_method_accounts") or {})
                payment_accounts.pop("card" if step == "card_payment_account" else "cash", None)
                profile["payment_method_accounts"] = payment_accounts
        else:
            resolved = match_choice(answer, account_options)
            if not resolved:
                return BotResponse(
                    localize(
                        "I couldn't match that account. Please reply with a listed number or exact account name.",
                        "Non riesco ad associare quel conto. Rispondi con un numero in lista o con il nome esatto.",
                        source_text=text,
                    )
                )
            if step == "card_payment_account":
                payment_accounts = dict(profile.get("payment_method_accounts") or {})
                payment_accounts["card"] = resolved
                profile["payment_method_accounts"] = payment_accounts
            elif step == "cash_payment_account":
                payment_accounts = dict(profile.get("payment_method_accounts") or {})
                payment_accounts["cash"] = resolved
                profile["payment_method_accounts"] = payment_accounts
            else:
                profile[step] = resolved
    elif step == "ask_budget_when_missing":
        parsed = parse_yes_no(answer)
        if parsed is None:
            return BotResponse(localize("Please reply with yes or no.", "Rispondi con si o no.", source_text=text))
        profile["ask_budget_when_missing"] = parsed
    elif step == "auto_budget_from_history":
        parsed = parse_yes_no(answer)
        if parsed is None:
            return BotResponse(localize("Please reply with yes or no.", "Rispondi con si o no.", source_text=text))
        profile["auto_budget_from_history"] = parsed

    step_index += 1
    if step_index >= len(PROFILE_SETUP_STEPS):
        profile["setup_complete"] = True
        save_finance_profile(state, profile)
        state.pop("profile_setup", None)
        summary = finance_profile_summary(state, source_text=text)
        outro = localize(
            "Training complete. I'll use this profile for account aliases, auto-assignments, and questions when uncertain.",
            "Training completato. Usero questo profilo per alias dei conti, auto-assegnazioni e domande quando sono incerto.",
            source_text=text,
        )
        return BotResponse(f"{summary}\n\n{outro}")

    setup["step_index"] = step_index
    setup["profile"] = profile
    state["profile_setup"] = setup
    return BotResponse(build_finance_setup_prompt(service, state, source_text=text))


def cached_recent_transactions_for_autofill(service: BridgeService) -> list[dict[str, Any]]:
    global _AUTOFILL_TX_CACHE
    now = time.time()
    cached = _AUTOFILL_TX_CACHE
    if now - float(cached.get("loaded_at") or 0) < AUTOFILL_CACHE_SECONDS:
        records = cached.get("records")
        if isinstance(records, list):
            return records

    end = date.today()
    start = end - timedelta(days=180)
    try:
        records = flatten_transactions(service.client.list_transactions(start=start, end=end, limit=300))
    except Exception:
        records = []
    _AUTOFILL_TX_CACHE = {"loaded_at": now, "records": records}
    return records


def text_similarity_score(target: str, candidate: str) -> int:
    if not target or not candidate:
        return 0
    score = 0
    if target == candidate:
        score += 20
    if target in candidate or candidate in target:
        score += 8
    target_tokens = {token for token in target.split() if len(token) >= 3}
    candidate_tokens = {token for token in candidate.split() if len(token) >= 3}
    overlap = target_tokens & candidate_tokens
    score += len(overlap) * 3
    return score


def infer_from_past_transactions(
    service: BridgeService,
    *,
    transaction_kind: str,
    description: str | None,
    merchant: str | None = None,
) -> dict[str, str]:
    target = normalize_match_text(f"{description or ''} {merchant or ''}")
    if not target:
        return {}
    best_record: dict[str, Any] | None = None
    best_score = 0
    for record in cached_recent_transactions_for_autofill(service):
        if str(record.get("type") or "").strip() != transaction_kind:
            continue
        record_text = normalize_match_text(
            f"{record.get('description') or ''} {record.get('destination_name') or ''} {record.get('category_name') or ''}"
        )
        score = text_similarity_score(target, record_text)
        if score > best_score:
            best_record = record
            best_score = score
    if best_record is None or best_score < 8:
        return {}
    inferred: dict[str, str] = {}
    for field_name, record_key in (
        ("source", "source_name"),
        ("destination", "destination_name"),
        ("category", "category_name"),
        ("budget", "budget_name"),
    ):
        value = str(best_record.get(record_key) or "").strip()
        if value:
            inferred[field_name] = value
    return inferred


def apply_profile_and_history_autofill(
    service: BridgeService,
    state: dict[str, Any],
    *,
    transaction_kind: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    updated: dict[str, Any] = dict(params)
    profile = get_finance_profile(state)

    if transaction_kind == "withdrawal":
        if not str(updated.get("source") or "").strip():
            updated["source"] = profile.get("expense_source_account")
        if not str(updated.get("destination") or "").strip():
            updated["destination"] = profile.get("expense_destination_account")
    elif transaction_kind == "deposit":
        if not str(updated.get("source") or "").strip():
            updated["source"] = profile.get("income_source_account")
        if not str(updated.get("destination") or "").strip():
            updated["destination"] = profile.get("income_destination_account")

    inferred = infer_from_past_transactions(
        service,
        transaction_kind=transaction_kind,
        description=str(updated.get("description") or "").strip() or None,
        merchant=str(updated.get("merchant") or "").strip() or None,
    )
    for field in ("source", "destination", "category"):
        if not str(updated.get(field) or "").strip() and inferred.get(field):
            updated[field] = inferred[field]

    if bool(profile.get("auto_budget_from_history", True)):
        if not str(updated.get("budget") or "").strip() and inferred.get("budget"):
            updated["budget"] = inferred["budget"]
    return updated


def queue_transaction_field_resolution(
    service: BridgeService,
    state: dict[str, Any],
    *,
    payload: dict[str, Any],
    fields: list[str],
    source_text: str | None = None,
) -> BotResponse:
    intent = str(payload.get("intent") or "").strip()
    first_field = fields[0] if fields else "source"
    options = intent_account_choices(service, intent, first_field, limit=15)
    state["pending_transaction_resolution"] = {
        "payload": payload,
        "fields": fields,
        "source_text": source_text or "",
        "options": options,
    }
    return BotResponse(build_transaction_resolution_prompt(state))


def build_transaction_resolution_prompt(state: dict[str, Any]) -> str:
    pending = state.get("pending_transaction_resolution")
    if not isinstance(pending, dict):
        return "No pending transaction resolution."
    fields = list(pending.get("fields") or [])
    options = list(pending.get("options") or [])
    if not fields:
        return "No unresolved fields."
    field = fields[0]
    payload = pending.get("payload") if isinstance(pending.get("payload"), dict) else {}
    intent = str(payload.get("intent") or "")
    if intent == "create_expense" and field == "source":
        lines = ["I need the account that paid for the expense. Choose your card/checking/cash account."]
    elif intent == "create_expense" and field == "destination":
        lines = ["I need the Firefly expense-side account. This is usually Expenses, Out, or a merchant account, not your card."]
    elif intent == "create_income" and field == "source":
        lines = ["I need who sent the money. Choose a revenue account, or reply skip if you do not use one."]
    elif intent == "create_income" and field == "destination":
        lines = ["I need the account that received the money."]
    elif intent == "create_transfer" and field == "source":
        lines = ["I need the account to transfer from."]
    elif intent == "create_transfer" and field == "destination":
        lines = ["I need the account to transfer to."]
    else:
        field_label = "source account" if field == "source" else "destination account"
        lines = [f"I need the {field_label} before I can prepare the draft safely."]
    if options:
        lines.append("Available accounts:")
        for index, name in enumerate(options, start=1):
            lines.append(f"{index}. {name}")
        lines.append("Reply with account name or number.")
    return "\n".join(lines)


def handle_pending_transaction_resolution(service: BridgeService, state: dict[str, Any], text: str) -> BotResponse | None:
    pending = state.get("pending_transaction_resolution")
    if not isinstance(pending, dict):
        return None
    if text.strip().startswith("/"):
        return None

    if has_cancel_intent(text):
        state.pop("pending_transaction_resolution", None)
        return BotResponse(localize("Draft discarded.", "Bozza annullata.", source_text=text))

    payload = pending.get("payload")
    if not isinstance(payload, dict):
        state.pop("pending_transaction_resolution", None)
        return BotResponse(localize("Pending draft data is invalid. Please resend.", "La bozza in attesa non e valida. Rimanda la richiesta.", source_text=text))

    fields = list(pending.get("fields") or [])
    options = list(pending.get("options") or [])
    if not fields:
        state.pop("pending_transaction_resolution", None)
        return execute_intent(service, payload, state)

    field = fields[0]
    resolved = match_choice(text, options) if options else text.strip()
    if not resolved:
        return BotResponse(build_transaction_resolution_prompt(state))

    params = payload.get("params")
    if not isinstance(params, dict):
        params = {}
        payload["params"] = params
    params[field] = resolved
    fields = fields[1:]
    if fields:
        pending["payload"] = payload
        pending["fields"] = fields
        pending["options"] = intent_account_choices(service, str(payload.get("intent") or ""), fields[0], limit=15)
        state["pending_transaction_resolution"] = pending
        return BotResponse(build_transaction_resolution_prompt(state))

    state.pop("pending_transaction_resolution", None)
    return execute_intent(service, payload, state)


def start_add_flow(state: dict[str, Any], *, source_text: str | None = None, preset_kind: str | None = None) -> None:
    flow = {
        "step": "kind",
        "params": {"live": False},
        "source_text": source_text or "",
    }
    if preset_kind in {"create_expense", "create_income", "create_transfer"}:
        flow["params"]["intent"] = preset_kind
        flow["step"] = "amount"
    state["add_flow"] = flow


def build_add_flow_prompt(service: BridgeService, state: dict[str, Any], *, source_text: str | None = None) -> str:
    flow = state.get("add_flow")
    if not isinstance(flow, dict):
        return localize("No active add flow.", "Nessun flusso /add attivo.", source_text=source_text)
    step = str(flow.get("step") or "kind")
    params = dict(flow.get("params") or {})
    account_options = add_flow_account_choices(service, params, step, limit=10)
    cache = FireflyObjectCache(service.client)
    category_options = cache.categories()[:8]
    budget_options = cache.budgets()[:8]

    if step == "kind":
        return localize(
            "Add step 1/8. What type?\n1. expense\n2. income\n3. transfer\nExample: 1",
            "Add step 1/8. Che tipo?\n1. spesa\n2. entrata\n3. trasferimento\nEsempio: 1",
            source_text=source_text,
        )
    if step == "amount":
        return localize(
            "Add step 2/8. Amount?\nExamples: 4.50, 12,30",
            "Add step 2/8. Importo?\nEsempi: 4.50, 12,30",
            source_text=source_text,
        )
    if step == "description":
        return localize(
            "Add step 3/8. Description?\nExamples: coffee, supermarket, salary",
            "Add step 3/8. Descrizione?\nEsempi: caffe, supermercato, stipendio",
            source_text=source_text,
        )
    if step == "date":
        return localize(
            "Add step 4/8. Date? (optional)\nExamples: today, yesterday, 2 days ago, 20-04-2026, skip",
            "Add step 4/8. Data? (opzionale)\nEsempi: oggi, ieri, 2 giorni fa, 20-04-2026, skip",
            source_text=source_text,
        )
    if step == "source":
        intent = str(params.get("intent") or "")
        source_prompt = {
            "create_expense": localize(
                "Add step 5/8. Which account paid? Choose your card/checking/cash account, or 'auto'.",
                "Add step 5/8. Da quale conto hai pagato? Scegli carta/conto/cassa, oppure 'auto'.",
                source_text=source_text,
            ),
            "create_income": localize(
                "Add step 5/8. Who sent the money? Choose a revenue account, or 'auto'/'skip'.",
                "Add step 5/8. Da chi arrivano i soldi? Scegli un conto entrata, oppure 'auto'/'skip'.",
                source_text=source_text,
            ),
            "create_transfer": localize(
                "Add step 5/8. Transfer from which account?",
                "Add step 5/8. Trasferimento da quale conto?",
                source_text=source_text,
            ),
        }
        lines = [source_prompt.get(intent, localize("Add step 5/8. Source account? (or 'auto'/'skip')", "Add step 5/8. Conto sorgente? (oppure 'auto'/'skip')", source_text=source_text))]
        if account_options:
            lines.append(localize("Available choices:", "Scelte disponibili:", source_text=source_text))
            for i, name in enumerate(account_options, start=1):
                lines.append(f"{i}. {name}")
        return "\n".join(lines)
    if step == "destination":
        intent = str(params.get("intent") or "")
        destination_prompt = {
            "create_expense": localize(
                "Add step 6/8. Where should Firefly record the expense side? Usually Expenses/Out/merchant, or 'auto'.",
                "Add step 6/8. Dove registro il lato spesa in Firefly? Di solito Spese/Out/esercente, oppure 'auto'.",
                source_text=source_text,
            ),
            "create_income": localize(
                "Add step 6/8. Which account received the money?",
                "Add step 6/8. Su quale conto sono arrivati i soldi?",
                source_text=source_text,
            ),
            "create_transfer": localize(
                "Add step 6/8. Transfer to which account?",
                "Add step 6/8. Trasferimento verso quale conto?",
                source_text=source_text,
            ),
        }
        lines = [destination_prompt.get(intent, localize("Add step 6/8. Destination account? (or 'auto'/'skip')", "Add step 6/8. Conto destinazione? (oppure 'auto'/'skip')", source_text=source_text))]
        if account_options:
            lines.append(localize("Available choices:", "Scelte disponibili:", source_text=source_text))
            for i, name in enumerate(account_options, start=1):
                lines.append(f"{i}. {name}")
        return "\n".join(lines)
    if step == "category":
        lines = [localize("Add step 7/8. Category? (or 'auto'/'skip')", "Add step 7/8. Categoria? (oppure 'auto'/'skip')", source_text=source_text)]
        if category_options:
            lines.append(localize("Categories:", "Categorie:", source_text=source_text))
            for i, name in enumerate(category_options, start=1):
                lines.append(f"{i}. {name}")
        return "\n".join(lines)
    if step == "budget":
        lines = [localize("Add step 8/8. Budget? (or 'auto'/'skip')", "Add step 8/8. Budget? (oppure 'auto'/'skip')", source_text=source_text)]
        if budget_options:
            lines.append(localize("Budgets:", "Budget:", source_text=source_text))
            for i, name in enumerate(budget_options, start=1):
                lines.append(f"{i}. {name}")
        return "\n".join(lines)
    return localize("Preparing draft...", "Sto preparando la bozza...", source_text=source_text)


def _flow_skip(value: str) -> bool:
    return normalize_natural_text(value) in {"skip", "salta", "auto", "none", "nessuno"}


def _intent_from_add_type(value: str) -> str | None:
    normalized = normalize_natural_text(value)
    mapping = {
        "1": "create_expense",
        "expense": "create_expense",
        "spesa": "create_expense",
        "2": "create_income",
        "income": "create_income",
        "entrata": "create_income",
        "3": "create_transfer",
        "transfer": "create_transfer",
        "trasferimento": "create_transfer",
    }
    return mapping.get(normalized)


def handle_add_flow_message(service: BridgeService, state: dict[str, Any], text: str) -> BotResponse | None:
    flow = state.get("add_flow")
    if not isinstance(flow, dict):
        return None
    if text.strip().startswith("/"):
        return None
    if has_cancel_intent(text):
        state.pop("add_flow", None)
        return BotResponse(localize("Add flow cancelled.", "Flusso /add annullato.", source_text=text))

    step = str(flow.get("step") or "kind")
    params = dict(flow.get("params") or {})
    answer = text.strip()
    cache = FireflyObjectCache(service.client)

    if step == "kind":
        intent = _intent_from_add_type(answer)
        if not intent:
            return BotResponse(build_add_flow_prompt(service, state, source_text=text))
        params["intent"] = intent
        flow["step"] = "amount"
    elif step == "amount":
        amount = parse_amount_from_text(answer) or answer.replace(",", ".").strip()
        try:
            Decimal(str(amount))
        except Exception:
            return BotResponse(localize("Please provide a valid amount.", "Inserisci un importo valido.", source_text=text))
        params["amount"] = str(amount)
        flow["step"] = "description"
    elif step == "description":
        if not answer:
            return BotResponse(localize("Please provide a description.", "Inserisci una descrizione.", source_text=text))
        params["description"] = answer
        flow["step"] = "date"
    elif step == "date":
        if not _flow_skip(answer):
            parsed = parse_flexible_date(answer) or parse_relative_date_hint(answer)
            if parsed is None:
                return BotResponse(localize("Please use a valid date or 'skip'.", "Usa una data valida o 'skip'.", source_text=text))
            params["date"] = parsed.isoformat()
        flow["step"] = "source"
    elif step == "source":
        if not _flow_skip(answer):
            resolved = match_choice(answer, add_flow_account_choices(service, params, "source", limit=50))
            params["source"] = resolved or answer
        flow["step"] = "destination"
    elif step == "destination":
        if not _flow_skip(answer):
            resolved = match_choice(answer, add_flow_account_choices(service, params, "destination", limit=50))
            params["destination"] = resolved or answer
        flow["step"] = "category"
    elif step == "category":
        if not _flow_skip(answer):
            resolved = match_choice(answer, cache.categories())
            params["category"] = resolved or answer
        flow["step"] = "budget"
    elif step == "budget":
        if not _flow_skip(answer):
            resolved = match_choice(answer, cache.budgets())
            params["budget"] = resolved or answer
        intent = str(params.get("intent") or "").strip()
        state.pop("add_flow", None)
        if intent not in {"create_expense", "create_income", "create_transfer"}:
            return BotResponse(localize("Add flow failed: unknown intent.", "Flusso /add fallito: intent sconosciuto.", source_text=text))
        payload = {
            "intent": intent,
            "source_text": str(flow.get("source_text") or text),
            "params": {k: v for k, v in params.items() if k != "intent"},
        }
        return execute_intent(service, payload, state)

    flow["params"] = params
    state["add_flow"] = flow
    return BotResponse(build_add_flow_prompt(service, state, source_text=text))


def start_maintenance_mode(state: dict[str, Any], *, source_text: str | None = None) -> None:
    state["maintenance_mode"] = {"step": "menu", "source_text": source_text or ""}


def maintenance_menu_text(*, source_text: str | None = None) -> str:
    return localize(
        "Maintenance:\n1. Accounts  2. Categories  3. Budgets  4. Exit\nPick a type to view and delete items.",
        "Manutenzione:\n1. Conti  2. Categorie  3. Budget  4. Esci\nScegli un tipo per visualizzare ed eliminare.",
        source_text=source_text,
    )


def _maintenance_item_name(item: dict[str, Any]) -> str:
    attrs = item.get("attributes", {}) if isinstance(item, dict) else {}
    return str(attrs.get("name") or item.get("name") or "").strip() or "?"


def _maintenance_numbered_list(items: list[dict[str, Any]], label: str, *, show_type: bool = False) -> str:
    if not items:
        return f"{label}: —"
    rows: list[str] = []
    for item in items[:30]:
        attrs = item.get("attributes", {}) if isinstance(item, dict) else {}
        name = str(attrs.get("name") or item.get("name") or "").strip()
        if not name:
            continue
        if show_type:
            acct_type = str(attrs.get("type") or item.get("type") or "").strip()
            rows.append(f"{name} [{acct_type}]" if acct_type else name)
        else:
            rows.append(name)
    if not rows:
        return f"{label}: —"
    lines = [f"{label} ({len(rows)}):"]
    for i, row in enumerate(rows, 1):
        lines.append(f"  {i}. {row}")
    return "\n".join(lines)


def _resolve_multi_selection(
    answer: str, items: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse comma/space-separated numbers or names; return (matched_items, unmatched_tokens)."""
    names = summarize_name_list(items, limit=30)
    name_to_item: dict[str, dict[str, Any]] = {}
    for item in items:
        n = _maintenance_item_name(item)
        if n and n != "?":
            name_to_item[n] = item

    parts = [p.strip() for p in re.split(r"[,;]+", answer) if p.strip()]
    matched: list[dict[str, Any]] = []
    unmatched: list[str] = []
    seen_ids: set[str] = set()
    for part in parts:
        choice = match_choice(part, names)
        if choice:
            item = name_to_item.get(choice)
            if item:
                item_id = str(item.get("id") or "")
                if item_id not in seen_ids:
                    matched.append(item)
                    seen_ids.add(item_id)
        else:
            unmatched.append(part)
    return matched, unmatched


def _is_back_to_menu(text: str) -> bool:
    lowered = normalize_natural_text(text)
    return lowered in {"menu", "fine", "basta", "indietro", "back", "home", "esci", "exit"}


def _fetch_maintenance_items(service: BridgeService, item_type: str) -> list[dict[str, Any]]:
    if item_type == "account":
        return service.client.list_accounts("all")
    if item_type == "category":
        return service.client.list_categories()
    return service.client.list_budgets()


def _show_maintenance_type(service: BridgeService, item_type: str, src: str) -> BotResponse:
    items = _fetch_maintenance_items(service, item_type)
    label_map = {
        "account": localize("Accounts", "Conti", source_text=src),
        "category": localize("Categories", "Categorie", source_text=src),
        "budget": localize("Budget", "Budget", source_text=src),
    }
    label = label_map[item_type]
    list_text = _maintenance_numbered_list(items, label, show_type=(item_type == "account"))
    hint = localize(
        "Type number(s) to delete (e.g. 1, 3, 5) or name(s). 'menu' to go back.",
        "Digita numero/i da eliminare (es. 1, 3, 5) o nome/i. 'menu' per tornare.",
        source_text=src,
    )
    return BotResponse(f"{list_text}\n\n{hint}")


def handle_maintenance_message(service: BridgeService, state: dict[str, Any], text: str) -> BotResponse | None:
    mode = state.get("maintenance_mode")
    if not isinstance(mode, dict):
        return None
    if text.strip().startswith("/"):
        return None

    src = str(mode.get("source_text") or text)

    if has_cancel_intent(text):
        state.pop("maintenance_mode", None)
        return BotResponse(localize("Maintenance closed.", "Manutenzione chiusa.", source_text=src))

    step = str(mode.get("step") or "menu")
    answer = text.strip()

    # --- menu ---
    if step == "menu":
        normalized = normalize_natural_text(answer)
        type_map = {
            "1": "account", "conti": "account", "conto": "account",
            "accounts": "account", "account": "account",
            "2": "category", "categorie": "category", "categoria": "category",
            "categories": "category", "category": "category",
            "3": "budget", "budgets": "budget",
            "4": "exit", "esci": "exit", "exit": "exit",
        }
        action = type_map.get(normalized)
        if action == "exit":
            state.pop("maintenance_mode", None)
            return BotResponse(localize("Maintenance closed.", "Manutenzione chiusa.", source_text=src))
        if action in {"account", "category", "budget"}:
            mode["step"] = action
            state["maintenance_mode"] = mode
            return _show_maintenance_type(service, action, src)
        return BotResponse(maintenance_menu_text(source_text=src))

    # --- type sub-menu (list + select for deletion) ---
    if step in {"account", "category", "budget"}:
        if _is_back_to_menu(answer):
            mode["step"] = "menu"
            state["maintenance_mode"] = mode
            return BotResponse(maintenance_menu_text(source_text=src))

        items = _fetch_maintenance_items(service, step)
        matched, unmatched = _resolve_multi_selection(answer, items)

        if not matched:
            hint = localize(
                "No match found. Type number(s) (e.g. 1, 3) or name(s), or 'menu' to go back.",
                "Nessuna corrispondenza. Digita numero/i (es. 1, 3) o nome/i, o 'menu' per tornare.",
                source_text=src,
            )
            return BotResponse(hint)

        mode["pending_deletes"] = [
            {"id": str(item.get("id") or ""), "name": _maintenance_item_name(item)}
            for item in matched
        ]
        mode["step"] = f"confirm_{step}"
        state["maintenance_mode"] = mode

        pending = mode["pending_deletes"]
        items_text = "\n".join(f"  • {p['name']}" for p in pending)
        count = len(pending)
        warn = localize(
            f"Delete {'these ' + str(count) + ' items' if count > 1 else 'this item'}?\n{items_text}\n\nSay 'conferma' to confirm or 'annulla' to cancel.",
            f"Eliminare {'questi ' + str(count) + ' elementi' if count > 1 else 'questo elemento'}?\n{items_text}\n\nScrivi 'conferma' per eliminare o 'annulla' per annullare.",
            source_text=src,
        )
        return BotResponse(warn)

    # --- confirmation step ---
    if step.startswith("confirm_"):
        base_type = step[len("confirm_"):]

        normalized = normalize_natural_text(answer)
        if normalized in {"annulla", "cancel", "no", "nope"} or has_cancel_intent(text):
            mode["step"] = base_type
            mode.pop("pending_deletes", None)
            state["maintenance_mode"] = mode
            return _show_maintenance_type(service, base_type, src)

        if not (has_commit_intent(text) or normalized in {"conferma", "si", "sì", "yes", "ok", "y"}):
            pending = mode.get("pending_deletes") or []
            items_text = "\n".join(f"  • {p['name']}" for p in pending)
            return BotResponse(localize(
                f"Confirm deletion?\n{items_text}\n\n'conferma' or 'annulla'.",
                f"Confermare eliminazione?\n{items_text}\n\n'conferma' o 'annulla'.",
                source_text=src,
            ))

        pending = list(mode.get("pending_deletes") or [])
        deleted: list[str] = []
        errors: list[str] = []
        for entry in pending:
            item_id = entry["id"]
            name = entry["name"]
            try:
                if base_type == "account":
                    service.client.delete_account(item_id)
                elif base_type == "category":
                    service.client.delete_category(item_id)
                else:
                    service.client.delete_budget(item_id)
                deleted.append(name)
            except Exception as exc:
                errors.append(f"{name}: {exc}")

        mode.pop("pending_deletes", None)
        mode["step"] = base_type
        state["maintenance_mode"] = mode

        lines: list[str] = []
        if deleted:
            label = localize("Deleted", "Eliminati", source_text=src)
            lines.append(f"{label}: {', '.join(deleted)}")
        if errors:
            lines.append(localize("Errors:", "Errori:", source_text=src))
            lines.extend(f"  • {e}" for e in errors)
        lines.append("")
        updated = _show_maintenance_type(service, base_type, src)
        return BotResponse("\n".join(lines) + "\n" + updated.text)

    mode["step"] = "menu"
    state["maintenance_mode"] = mode
    return BotResponse(maintenance_menu_text(source_text=src))


def has_cancel_intent(text: str) -> bool:
    lowered = text.strip().casefold()
    return lowered in {"cancel", "annulla", "annullare", "discard", "stop"}


def has_commit_intent(text: str) -> bool:
    lowered = text.strip().casefold()
    return lowered in {
        "ok",
        "okay",
        "yes",
        "y",
        "si",
        "sì",
        "va bene",
        "commit it",
        "commit",
        "conferma",
        "confermare",
        "salva",
        "salva pure",
        "save it",
        "save it for real",
        "save for real",
        "create it",
        "create it for real",
        "do it",
        "go ahead",
    }


def has_high_value_confirmation(text: str) -> bool:
    lowered = text.casefold()
    return any(phrase in lowered for phrase in {"yes high value", "i confirm", "confirm it", "yes confirm"})


def build_draft_manager(service: BridgeService) -> DraftManager:
    behavior = (service.settings.policy or {}).get("behavior", {})
    mappings = service.settings.mappings or {}
    cache = FireflyObjectCache(service.client)
    return DraftManager(
        object_cache=cache,
        category_budget_map=dict(mappings.get("category_budget_map") or {}),
        auto_confirm_merchants=list(mappings.get("auto_confirm_merchants") or []),
        skip_budget_threshold=float(behavior.get("skip_budget_for_low_amounts", 5.0)),
        category_confirmation_mode=str(behavior.get("category_confirmation_mode", "ask")),
    )


def render_draft_session(manager: DraftManager, session: Any) -> str:
    phase = getattr(session, "phase", None)
    if phase == DraftPhase.CATEGORY_CONFIRM:
        return manager.build_category_confirm_message(session)
    if phase == DraftPhase.CATEGORY_SELECT:
        return manager.build_category_select_message(session)
    if phase == DraftPhase.BUDGET_SUGGEST:
        return manager.build_budget_suggest_message(session)
    return manager.build_review_message(session)


def ensure_category_exists(service: BridgeService, category_name: str | None) -> None:
    name = str(category_name or "").strip()
    if not name:
        return
    target = normalize_match_text(name)
    if not target:
        return
    existing = summarize_name_list(service.client.list_categories(), limit=500)
    for item in existing:
        if normalize_match_text(item) == target:
            return
    service.client.create_category(name)


def ensure_categories_for_payload(service: BridgeService, payload: dict[str, Any]) -> None:
    transactions = payload.get("transactions")
    if not isinstance(transactions, list):
        return
    for transaction in transactions:
        if not isinstance(transaction, dict):
            continue
        ensure_category_exists(service, str(transaction.get("category_name") or "").strip() or None)


def commit_pending_transaction(service: BridgeService, state: dict[str, Any], text: str) -> BotResponse:
    pending = state.get("pending_action")
    if not isinstance(pending, dict):
        return BotResponse(bot_text("no_pending_draft", source_text=text))

    kind = str(pending.get("kind") or "").strip()
    payload = pending.get("payload")

    if kind == "transaction_create":
        if not isinstance(payload, dict) or not payload.get("transactions"):
            save_draft_session(state, None)
            return BotResponse("The pending transaction draft is incomplete. Please send it again.")
        ensure_categories_for_payload(service, payload)

        try:
            result = service.commit_transaction(
                payload,
                dry_run=False,
                confirm_high_value=has_high_value_confirmation(text),
            )
        except (ConfigurationError, FireflyAPIError, ValueError, RuntimeError) as exc:
            if is_firefly_offline_error(exc):
                raise exc
            if isinstance(exc, FireflyAPIError):
                invalid_field = invalid_account_field_from_error(exc)
                if invalid_field:
                    return queue_pending_draft_account_fix(
                        service,
                        state,
                        field=invalid_field,
                        source_text=text,
                    )
            save_draft_session(state, None)
            raise exc

        if result["status"] == "duplicate_blocked":
            save_draft_session(state, None)
            return BotResponse(format_duplicate_blocked(result["duplicate"], source_text=text))

        save_draft_session(state, None)
        created = result.get("result", {})
        _remember_last_committed_txn(state, created, payload)
        return BotResponse(format_created_transaction_result(created, fallback_payload=payload, source_text=text))

    if kind == "transaction_amount_split":
        txn_id = str((payload or {}).get("txn_id") or "").strip()
        new_amount = str((payload or {}).get("new_amount") or "").strip()
        description = str((payload or {}).get("description") or "").strip()
        tx_type = str((payload or {}).get("tx_type") or "").strip()
        if not txn_id or not new_amount or not tx_type:
            clear_pending_action(state)
            return BotResponse(localize("Split action incomplete.", "Azione di divisione incompleta.", source_text=text))
        if service.client.update_transaction(int(txn_id), {"type": tx_type, "amount": new_amount}):
            clear_pending_action(state)
            state["last_committed_txn"] = {
                "id": txn_id,
                "description": description,
                "amount": new_amount,
                "type": tx_type,
            }
            return BotResponse(
                localize(
                    f"✅ Transaction updated.\n{description}: {new_amount} EUR",
                    f"✅ Transazione aggiornata.\n{description}: {new_amount} EUR",
                    source_text=text,
                )
            )
        clear_pending_action(state)
        return BotResponse(localize("❌ Could not update the transaction.", "❌ Impossibile aggiornare la transazione.", source_text=text))

    if kind == "transaction_batch_create":
        payloads = list((payload or {}).get("transactions") or []) if isinstance(payload, dict) else []
        if not payloads:
            save_draft_session(state, None)
            return BotResponse(localize("The pending draft batch is incomplete. Please send it again.", "La bozza multipla in attesa e incompleta. Rimandamela.", source_text=text))

        confirm_high_value = has_high_value_confirmation(text)
        for item in payloads:
            tx = (item.get("transactions") or [{}])[0]
            if Decimal(str(tx.get("amount") or "0")) >= service.settings.high_value_threshold and not confirm_high_value:
                return BotResponse(
                    localize(
                        f"One draft meets the high-value threshold ({service.settings.high_value_threshold}). Reply with 'yes high value' and then commit again if you want to continue.",
                        f"Una bozza supera la soglia di alto importo ({service.settings.high_value_threshold}). Rispondi con 'yes high value' e poi conferma di nuovo se vuoi continuare.",
                        source_text=text,
                    )
                )
            duplicate = service.find_duplicate(item)
            if duplicate:
                save_draft_session(state, None)
                return BotResponse(format_duplicate_blocked(duplicate, source_text=text))

        created_lines = [localize("Transactions created:", "Transazioni create:", source_text=text)]
        try:
            for item in payloads:
                ensure_categories_for_payload(service, item)
                result = service.commit_transaction(item, dry_run=False, confirm_high_value=True)
                created = result.get("result", {})
                created_lines.append(
                    format_created_transaction_result(created, fallback_payload=item, source_text=text).replace("\n", " | ")
                )
        except (ConfigurationError, FireflyAPIError, ValueError, RuntimeError) as exc:
            if is_firefly_offline_error(exc):
                raise exc
            save_draft_session(state, None)
            raise exc
        save_draft_session(state, None)
        return BotResponse("\n".join(created_lines))

    if kind == "category_create":
        name = str((payload or {}).get("name") or "").strip()
        service.client.create_category(name)
        clear_pending_action(state)
        return BotResponse(f"Category created: {name}")

    if kind == "budget_create":
        name = str((payload or {}).get("name") or "").strip()
        service.client.create_budget(name)
        clear_pending_action(state)
        return BotResponse(f"Budget created: {name}")

    if kind == "account_create":
        name = str((payload or {}).get("name") or "").strip()
        account_type = str((payload or {}).get("account_type") or "").strip()
        opening_balance = str((payload or {}).get("opening_balance") or "").strip() or None
        opening_balance_date = str((payload or {}).get("opening_balance_date") or "").strip() or None
        service.client.create_account(
            name=name,
            account_type=account_type,
            opening_balance=opening_balance,
            opening_balance_date=opening_balance_date,
        )
        clear_pending_action(state)
        return BotResponse(f"Account created: {name} [{account_type}]")

    if kind == "category_delete":
        category_id = str((payload or {}).get("category_id") or "").strip()
        name = str((payload or {}).get("name") or "").strip() or category_id
        if not category_id:
            clear_pending_action(state)
            return BotResponse("The pending category delete action is incomplete.")
        service.client.delete_category(category_id)
        clear_pending_action(state)
        return BotResponse(localize(f"Category deleted: {name}", f"Categoria eliminata: {name}", source_text=text))

    if kind == "budget_delete":
        budget_id = str((payload or {}).get("budget_id") or "").strip()
        name = str((payload or {}).get("name") or "").strip() or budget_id
        if not budget_id:
            clear_pending_action(state)
            return BotResponse("The pending budget delete action is incomplete.")
        service.client.delete_budget(budget_id)
        clear_pending_action(state)
        return BotResponse(localize(f"Budget deleted: {name}", f"Budget eliminato: {name}", source_text=text))

    if kind == "account_delete":
        account_id = str((payload or {}).get("account_id") or "").strip()
        name = str((payload or {}).get("name") or "").strip() or account_id
        if not account_id:
            clear_pending_action(state)
            return BotResponse("The pending account delete action is incomplete.")
        service.client.delete_account(account_id)
        clear_pending_action(state)
        return BotResponse(localize(f"Account deleted: {name}", f"Conto eliminato: {name}", source_text=text))

    if kind == "budget_limit_set":
        budget_id = str((payload or {}).get("budget_id") or "").strip()
        amount = str((payload or {}).get("amount") or "").strip()
        start = str((payload or {}).get("start") or "").strip()
        end = str((payload or {}).get("end") or "").strip()
        existing_limit_id = str((payload or {}).get("budget_limit_id") or "").strip()
        budget_name = str((payload or {}).get("budget_name") or "").strip() or "budget"
        if existing_limit_id:
            service.client.update_budget_limit(
                budget_limit_id=existing_limit_id,
                amount=amount,
                start=start,
                end=end,
                notes=str((payload or {}).get("notes") or "").strip() or None,
            )
        else:
            service.client.create_budget_limit(
                budget_id=budget_id,
                amount=amount,
                start=start,
                end=end,
                notes=str((payload or {}).get("notes") or "").strip() or None,
            )
        clear_pending_action(state)
        return BotResponse(f"Budget limit set.\n{budget_name}: {amount}\nPeriod: {format_display_period(start, end)}")

    if kind == "recurrence_create":
        created = service.client.create_recurrence(payload)
        clear_pending_action(state)
        data = created.get("data", {})
        attributes = data.get("attributes", {}) if isinstance(data, dict) else {}
        title = attributes.get("title") or (payload or {}).get("title") or "Recurring transaction"
        recurrence_id = data.get("id") if isinstance(data, dict) else None
        return BotResponse(f"Recurring transaction created.\n#{recurrence_id or '?'} {title}")

    if kind == "recurrence_delete":
        recurrence_id = str((payload or {}).get("recurrence_id") or "").strip()
        if not recurrence_id:
            clear_pending_action(state)
            return BotResponse("The pending recurrence delete action is incomplete.")
        service.client.delete_recurrence(recurrence_id)
        clear_pending_action(state)
        return BotResponse(f"Recurring transaction #{recurrence_id} deleted.")

    clear_pending_action(state)
    return BotResponse("The pending action could not be completed safely. Please send it again.")


def recent_window(days: int) -> tuple[date, date]:
    end = date.today()
    start = end - timedelta(days=max(days, 1) - 1)
    return start, end


def last_month_window() -> tuple[date, date]:
    first_this_month = date.today().replace(day=1)
    end = first_this_month - timedelta(days=1)
    start = end.replace(day=1)
    return start, end


def month_label_for_window(start: date, end: date) -> str:
    return format_display_period(start, end)


def month_window_from_label(month: str | None) -> tuple[date, date]:
    if month:
        start = date.fromisoformat(f"{month}-01")
    else:
        start = date.today().replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1, day=1) - timedelta(days=1)
    else:
        end = start.replace(month=start.month + 1, day=1) - timedelta(days=1)
    return start, end


MONTH_NAME_TO_NUMBER = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "gennaio": 1,
    "febbraio": 2,
    "marzo": 3,
    "aprile": 4,
    "maggio": 5,
    "giugno": 6,
    "luglio": 7,
    "agosto": 8,
    "settembre": 9,
    "ottobre": 10,
    "novembre": 11,
    "dicembre": 12,
}
MONTH_NAME_PATTERN = "|".join(sorted((re.escape(name) for name in MONTH_NAME_TO_NUMBER), key=len, reverse=True))


def month_from_name(name: str) -> int | None:
    return MONTH_NAME_TO_NUMBER.get(name.casefold().strip())


def _legacy_parse_natural_period_values(text: str) -> dict[str, str]:
    lowered = text.casefold()
    current_year = date.today().year

    explicit_range = re.search(
        r"\b(?:from|da)\s+(\d{4}-\d{2}-\d{2})\s+(?:to|al|a)\s+(\d{4}-\d{2}-\d{2})\b",
        lowered,
    )
    if explicit_range:
        return {"from": explicit_range.group(1), "to": explicit_range.group(2)}

    month_range = re.search(
        r"\b(?:from|da)\s+([a-zàèéìòù]+)\s+(?:to|al|a)\s+([a-zàèéìòù]+)(?:\s+(\d{4}))?\b",
        lowered,
    )
    if month_range:
        start_month = month_from_name(month_range.group(1))
        end_month = month_from_name(month_range.group(2))
        year = int(month_range.group(3) or current_year)
        if start_month and end_month:
            start, _ = month_window_from_label(f"{year:04d}-{start_month:02d}")
            _, end = month_window_from_label(f"{year:04d}-{end_month:02d}")
            return {"from": start.isoformat(), "to": end.isoformat()}

    specific_month = re.search(r"\b(?:for|per|in|a|nel mese di|mese di)\s+([a-zàèéìòù]+)(?:\s+(\d{4}))?\b", lowered)
    if specific_month:
        month_number = month_from_name(specific_month.group(1))
        year = int(specific_month.group(2) or current_year)
        if month_number:
            return {"month": f"{year:04d}-{month_number:02d}"}

    named_month = re.search(r"\b(?:month of|mese di|in)\s+([a-zàèéìòù]+)(?:\s+(\d{4}))?\b", lowered)
    if not named_month:
        named_month = re.search(r"\b([a-zàèéìòù]+)\s+(\d{4})\b", lowered)
    if named_month:
        month_number = month_from_name(named_month.group(1))
        year = int(named_month.group(2) or current_year)
        if month_number:
            return {"month": f"{year:04d}-{month_number:02d}"}

    if "last month" in lowered or "ultimo mese" in lowered or "scorso mese" in lowered:
        start, _ = last_month_window()
        return {"month": start.strftime("%Y-%m")}

    if "this month" in lowered or "questo mese" in lowered:
        return {"month": date.today().strftime("%Y-%m")}

    return {}


def parse_natural_period_values(text: str) -> dict[str, str]:
    lowered = re.sub(r"[^\w\s\-/.]", " ", normalize_natural_text(text))
    lowered = " ".join(lowered.split())
    current_year = date.today().year

    explicit_range = re.search(
        rf"\b(?:from|da|dal)\s+({DATE_TOKEN_PATTERN})\s+(?:to|al|ad|a|and)\s+({DATE_TOKEN_PATTERN})\b",
        lowered,
    )
    if explicit_range:
        start = parse_flexible_date(explicit_range.group(1))
        end = parse_flexible_date(explicit_range.group(2))
        if start and end:
            return {"from": start.isoformat(), "to": end.isoformat()}

    month_range = re.search(
        rf"\b(?:from|da|dal|tra)\s+({MONTH_NAME_PATTERN})(?:\s+(\d{{4}}))?\s+(?:to|al|ad|a|and)\s+({MONTH_NAME_PATTERN})(?:\s+(\d{{4}}))?\b",
        lowered,
    )
    if month_range:
        start_month = month_from_name(month_range.group(1))
        end_month = month_from_name(month_range.group(3))
        start_year = int(month_range.group(2) or current_year)
        end_year = int(month_range.group(4) or start_year)
        if start_month and end_month:
            if not month_range.group(4) and end_month < start_month:
                end_year += 1
            start, _ = month_window_from_label(f"{start_year:04d}-{start_month:02d}")
            _, end = month_window_from_label(f"{end_year:04d}-{end_month:02d}")
            return {"from": start.isoformat(), "to": end.isoformat()}

    year_range = re.search(
        r"\b(?:from|da|dal)\s+(20\d{2})\s+(?:to|al|ad|a|and)\s+(20\d{2})\b",
        lowered,
    )
    if year_range:
        start_year = int(year_range.group(1))
        end_year = int(year_range.group(2))
        if end_year < start_year:
            start_year, end_year = end_year, start_year
        return {"from": f"{start_year:04d}-01-01", "to": f"{end_year:04d}-12-31"}

    specific_month = re.search(
        rf"\b(?:for|per|in|a|during|durante|nel|nel mese di|mese di)\s+({MONTH_NAME_PATTERN})(?:\s+(\d{{4}}))?\b",
        lowered,
    )
    if specific_month:
        month_number = month_from_name(specific_month.group(1))
        year = int(specific_month.group(2) or current_year)
        if month_number:
            return {"month": f"{year:04d}-{month_number:02d}"}

    named_month = re.search(rf"\b(?:month of|mese di|in)\s+({MONTH_NAME_PATTERN})(?:\s+(\d{{4}}))?\b", lowered)
    if not named_month:
        named_month = re.search(rf"\b({MONTH_NAME_PATTERN})\s+(\d{{4}})\b", lowered)
    if named_month:
        month_number = month_from_name(named_month.group(1))
        year = int(named_month.group(2) or current_year)
        if month_number:
            return {"month": f"{year:04d}-{month_number:02d}"}

    bare_month = re.search(rf"\b({MONTH_NAME_PATTERN})\b", lowered)
    if bare_month:
        month_number = month_from_name(bare_month.group(1))
        if month_number:
            return {"month": f"{current_year:04d}-{month_number:02d}"}

    if "last month" in lowered or "ultimo mese" in lowered or "scorso mese" in lowered:
        start, _ = last_month_window()
        return {"month": start.strftime("%Y-%m")}

    if "this month" in lowered or "questo mese" in lowered:
        return {"month": date.today().strftime("%Y-%m")}

    explicit_year = re.search(r"\b(?:for|per|in|nel|during|durante)\s+(20\d{2})\b", lowered)
    if explicit_year:
        year = int(explicit_year.group(1))
        return {"from": f"{year:04d}-01-01", "to": f"{year:04d}-12-31"}

    return {}


def coerce_period_values(values: dict[str, Any] | None) -> dict[str, str]:
    raw = values if isinstance(values, dict) else {}
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        if value is None:
            continue
        normalized[str(key).strip().lower()] = str(value).strip()
    return normalized


def period_from_values(
    values: dict[str, Any] | None,
    *,
    default_days: int | None = None,
    default_last_month: bool = False,
    default_current_month: bool = False,
) -> tuple[date, date, str]:
    normalized = coerce_period_values(values)
    month = normalized.get("month") or None
    start_text = normalized.get("start_date") or normalized.get("from") or normalized.get("start") or None
    end_text = normalized.get("end_date") or normalized.get("to") or normalized.get("end") or None

    if month:
        start, end = month_window_from_label(month)
        return start, end, month_label_for_window(start, end)

    if start_text or end_text:
        if not start_text or not end_text:
            raise ValueError("Both from and to dates are required for a custom period.")
        start = parse_flexible_date(start_text)
        end = parse_flexible_date(end_text)
        if start is None or end is None:
            raise ValueError("Dates must use DD-MM-YYYY or YYYY-MM-DD.")
        if end < start:
            raise ValueError("The end date must be on or after the start date.")
        return start, end, format_display_period(start, end)

    if default_days is not None:
        days_text = normalized.get("days") or str(default_days)
        days = int(days_text)
        start, end = recent_window(days)
        return start, end, month_label_for_window(start, end)

    if default_last_month:
        start, end = last_month_window()
        return start, end, month_label_for_window(start, end)

    if default_current_month:
        start, end = month_window_from_label(None)
        return start, end, month_label_for_window(start, end)

    raise ValueError("No valid period could be determined.")


def aggregate_spending_by_category(records: list[dict[str, Any]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for record in records:
        if str(record.get("type")) != "withdrawal":
            continue
        category = str(record.get("category_name") or record.get("destination_name") or "Uncategorized")
        try:
            amount = abs(float(str(record.get("amount") or "0")))
        except ValueError:
            continue
        totals[category] = totals.get(category, 0.0) + amount
    return dict(sorted(totals.items(), key=lambda item: item[1], reverse=True))


def aggregate_cashflow_by_day(records: list[dict[str, Any]]) -> tuple[list[str], list[float], list[float]]:
    incoming: dict[str, float] = {}
    outgoing: dict[str, float] = {}
    for record in records:
        day = str(record.get("date") or "")[:10]
        if not day:
            continue
        try:
            amount = abs(float(str(record.get("amount") or "0")))
        except ValueError:
            continue
        if str(record.get("type")) == "deposit":
            incoming[day] = incoming.get(day, 0.0) + amount
        elif str(record.get("type")) == "withdrawal":
            outgoing[day] = outgoing.get(day, 0.0) + amount
    days = sorted(set(incoming) | set(outgoing))
    return days, [incoming.get(day, 0.0) for day in days], [outgoing.get(day, 0.0) for day in days]


def aggregate_spending_by_budget(records: list[dict[str, Any]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for record in records:
        if str(record.get("type")) != "withdrawal":
            continue
        budget = str(record.get("budget_name") or "No budget")
        try:
            amount = abs(float(str(record.get("amount") or "0")))
        except ValueError:
            continue
        totals[budget] = totals.get(budget, 0.0) + amount
    return dict(sorted(totals.items(), key=lambda item: item[1], reverse=True))


def format_top_spending_categories(
    records: list[dict[str, Any]],
    *,
    label: str,
    source_text: str | None = None,
    limit: int | None = 8,
) -> str:
    totals = aggregate_spending_by_category(records)
    if not totals:
        return localize(f"No spending found for {label}.", f"Nessuna spesa trovata per {label}.", source_text=source_text)

    lines = [localize(f"Top spending categories for {label}:", f"Categorie di spesa principali per {label}:", source_text=source_text)]
    total_amount = 0.0
    items = list(totals.items()) if limit is None else list(totals.items())[:limit]
    for name, value in items:
        total_amount += value
        lines.append(f"- {name}: {value:.2f}")
    lines.append(localize(f"Total outgoing tracked: {total_amount:.2f}", f"Totale uscite considerate: {total_amount:.2f}", source_text=source_text))
    return "\n".join(lines)


def format_budget_report(
    records: list[dict[str, Any]],
    *,
    label: str,
    budget_limits: dict[str, float] | None = None,
    source_text: str | None = None,
) -> str:
    totals = aggregate_spending_by_budget(records)
    limits = budget_limits or {}
    if not totals and not limits:
        return localize(
            f"No budget-linked spending found for {label}.",
            f"Nessuna spesa collegata a budget trovata per {label}.",
            source_text=source_text,
        )

    lines = [localize(f"Budget report for {label}:", f"Report budget per {label}:", source_text=source_text)]
    names = list(totals.keys())
    for name in sorted(limits):
        if name not in totals:
            names.append(name)
    for name in names[:12]:
        spent = totals.get(name, 0.0)
        limit = limits.get(name)
        spent_label = localize("spent", "spesi", source_text=source_text)
        limit_label = localize("limit", "limite", source_text=source_text)
        if limit and limit > 0:
            remaining = limit - spent
            if remaining >= 0:
                remaining_label = localize("left", "rimasti", source_text=source_text)
                lines.append(f"- {name}: {spent_label} {spent:.2f} / {limit_label} {limit:.2f} ({remaining_label}: {remaining:.2f})")
            else:
                over_label = localize("over limit", "oltre limite", source_text=source_text)
                lines.append(f"- {name}: {spent_label} {spent:.2f} / {limit_label} {limit:.2f} ({over_label}: {abs(remaining):.2f})")
        else:
            no_limit = localize("no limit", "senza limite", source_text=source_text)
            lines.append(f"- {name}: {spent_label} {spent:.2f} ({no_limit})")
    return "\n".join(lines)


def collect_budget_limits(service: BridgeService, start: date, end: date) -> dict[str, float]:
    budget_limits: dict[str, float] = {}
    for budget in service.client.list_budgets():
        b_id = str(budget.get("id") or "").strip()
        b_name = str((budget.get("attributes") or {}).get("name") or "").strip()
        if not b_id or not b_name:
            continue
        total_limit = 0.0
        for item in service.client.list_budget_limits(b_id, start=start.isoformat(), end=end.isoformat()):
            raw = (item.get("attributes") or {}).get("amount") or 0
            try:
                amount = float(raw)
            except (ValueError, TypeError):
                amount = 0.0
            if amount > 0:
                total_limit += amount
        if total_limit > 0:
            budget_limits[b_name] = total_limit
    return budget_limits


_FREQ_LABEL_IT = {"weekly": "settimanale", "monthly": "mensile", "yearly": "annuale", "daily": "giornaliera"}
_FREQ_LABEL_EN = {"weekly": "weekly", "monthly": "monthly", "yearly": "yearly", "daily": "daily"}
_TX_TYPE_LABEL_IT = {"withdrawal": "spesa", "deposit": "entrata", "transfer": "trasferimento"}
_TX_TYPE_LABEL_EN = {"withdrawal": "expense", "deposit": "income", "transfer": "transfer"}


def _recurrence_freq_label(freq: str, *, source_text: str | None = None) -> str:
    freq = (freq or "").strip().lower()
    if locale_language(source_text) == "it":
        return _FREQ_LABEL_IT.get(freq, freq)
    return _FREQ_LABEL_EN.get(freq, freq)


def _build_recurrence_suggestion_prompt(
    pending_recurrence_suggestion: dict[str, Any],
    *,
    source_text: str | None = None,
) -> str:
    cadence = str(pending_recurrence_suggestion.get("cadence") or "monthly")
    transaction_kind = str(pending_recurrence_suggestion.get("transaction_kind") or "withdrawal")
    freq_label = _recurrence_freq_label(cadence, source_text=source_text)
    return localize(
        f"This looks like a recurring {transaction_kind}: {freq_label}.\n"
        "Want me to create a recurrence too? (yes/no)",
        f"Sembra una transazione ricorrente: {freq_label}.\n"
        "Vuoi creare anche una ricorrenza? (si/no)",
        source_text=source_text,
    )


def _should_offer_recurrence_before_review(state: dict[str, Any], session: Any) -> bool:
    if not isinstance(state.get("pending_recurrence_suggestion"), dict):
        return False
    if state.get("awaiting_recurrence_answer"):
        return False
    return getattr(session, "phase", None) == DraftPhase.REVIEW


def _begin_recurrence_suggestion_prompt(
    state: dict[str, Any],
    pending_recurrence_suggestion: dict[str, Any],
    *,
    source_text: str | None = None,
) -> str:
    state["awaiting_recurrence_answer"] = True
    return _build_recurrence_suggestion_prompt(pending_recurrence_suggestion, source_text=source_text)


def _recurrence_type_label(tx_type: str, *, source_text: str | None = None) -> str:
    tx_type = (tx_type or "").strip().lower()
    if locale_language(source_text) == "it":
        return _TX_TYPE_LABEL_IT.get(tx_type, tx_type)
    return _TX_TYPE_LABEL_EN.get(tx_type, tx_type)


def format_recurrences(items: list[dict[str, Any]], *, source_text: str | None = None) -> str:
    if not items:
        return localize("No recurring transactions found.", "Nessuna transazione ricorrente trovata.", source_text=source_text)

    is_it = locale_language(source_text) == "it"
    header = localize("Recurring transactions:", "Transazioni ricorrenti:", source_text=source_text)
    lines = [header]
    total_monthly_expenses = 0.0
    total_monthly_income = 0.0

    for item in items[:25]:
        attributes = item.get("attributes", {})
        active = attributes.get("active", True)
        if not active:
            continue

        title = str(attributes.get("title") or attributes.get("description") or "<unnamed>")

        repetitions = attributes.get("repetitions") or []
        freq = "monthly"
        if isinstance(repetitions, list) and repetitions:
            rep = repetitions[0] if isinstance(repetitions[0], dict) else {}
            freq = str(rep.get("type") or "monthly").lower()
        freq_label = _recurrence_freq_label(freq, source_text=source_text)

        next_date_raw = attributes.get("next_expected_match")
        next_date = format_display_date(next_date_raw) if next_date_raw else "?"

        txs = attributes.get("transactions") or []
        tx = txs[0] if isinstance(txs, list) and txs and isinstance(txs[0], dict) else {}
        amount_raw = str(tx.get("amount") or "0")
        try:
            amount = float(amount_raw)
        except ValueError:
            amount = 0.0
        currency = str(tx.get("currency_code") or tx.get("foreign_currency_code") or "EUR")
        tx_type = str(tx.get("type") or "withdrawal").lower()
        type_label = _recurrence_type_label(tx_type, source_text=source_text)
        source_name = str(tx.get("source_name") or "").strip()
        dest_name = str(tx.get("destination_name") or "").strip()
        category = str(tx.get("category_name") or "").strip()
        budget = str(tx.get("budget_name") or "").strip()

        if freq == "monthly":
            if tx_type == "withdrawal":
                total_monthly_expenses += amount
            elif tx_type == "deposit":
                total_monthly_income += amount
        elif freq == "weekly":
            normalized = amount * 52 / 12
            if tx_type == "withdrawal":
                total_monthly_expenses += normalized
            elif tx_type == "deposit":
                total_monthly_income += normalized
        elif freq == "yearly":
            normalized = amount / 12
            if tx_type == "withdrawal":
                total_monthly_expenses += normalized
            elif tx_type == "deposit":
                total_monthly_income += normalized

        next_label = localize("next", "prossima", source_text=source_text)
        line = f"- {title}: {currency} {amount:.2f} | {type_label} | {freq_label} | {next_label} {next_date}"
        if source_name and dest_name:
            line += f"\n  {source_name} → {dest_name}"
        if category:
            cat_label = localize("cat", "cat", source_text=source_text)
            line += f" | {cat_label}: {category}"
        if budget:
            bud_label = localize("budget", "budget", source_text=source_text)
            line += f" | {bud_label}: {budget}"
        lines.append(line)

    if total_monthly_expenses > 0 or total_monthly_income > 0:
        net = total_monthly_income - total_monthly_expenses
        sign = "+" if net >= 0 else ""
        lines.append("")
        if is_it:
            lines.append(f"Totale mensile stimato:")
            if total_monthly_income > 0:
                lines.append(f"  Entrate: {total_monthly_income:.2f}")
            if total_monthly_expenses > 0:
                lines.append(f"  Uscite: {total_monthly_expenses:.2f}")
            lines.append(f"  Netto: {sign}{net:.2f}")
        else:
            lines.append("Estimated monthly total:")
            if total_monthly_income > 0:
                lines.append(f"  Income: {total_monthly_income:.2f}")
            if total_monthly_expenses > 0:
                lines.append(f"  Expenses: {total_monthly_expenses:.2f}")
            lines.append(f"  Net: {sign}{net:.2f}")

    return "\n".join(lines)


def recurrence_payload_from_params(service: BridgeService, params: dict[str, Any]) -> dict[str, Any]:
    amount = str(params.get("amount") or "").strip()
    description = str(params.get("description") or params.get("title") or "").strip()
    if not amount or not description:
        raise RuntimeError("Recurring transactions need at least amount and description.")

    cadence = str(params.get("cadence") or "monthly").strip().lower()
    if cadence not in {"weekly", "monthly", "yearly"}:
        raise RuntimeError("Recurring cadence must be weekly, monthly, or yearly.")

    source_name = str(params.get("source") or "").strip() or None
    destination_name = str(params.get("destination") or "").strip() or None
    category_name = str(params.get("category") or "").strip() or None
    budget_name = str(params.get("budget") or "").strip() or None
    merchant = str(params.get("merchant") or "").strip() or None

    tx_payload = service.build_transaction(
        transaction_kind="withdrawal",
        amount=amount,
        description=description,
        transaction_date=str(params.get("date") or "").strip() or None,
        source_name=source_name,
        destination_name=destination_name,
        category_name=category_name,
        budget_name=budget_name,
        notes=str(params.get("notes") or "").strip() or None,
        tags=[],
        merchant=merchant,
        currency_code=None,
    )
    tx = tx_payload["transactions"][0]

    if cadence == "weekly":
        moment = str(params.get("day_of_week") or "monday").strip().lower()
    elif cadence == "monthly":
        moment = str(params.get("day_of_month") or 1).strip()
    else:
        moment = str(params.get("day_of_month") or 1).strip()

    first_date = str(params.get("date") or "").strip() or tx["date"][:10]
    repeat_until = str(params.get("repeat_until") or "").strip() or None

    return {
        "title": str(params.get("title") or description).strip(),
        "first_date": first_date,
        "repeat_until": repeat_until,
        "repetitions": [
            {
                "type": cadence,
                "moment": moment,
                "skip": 0,
            }
        ],
        "transactions": [
            {
                "description": tx["description"],
                "amount": tx["amount"],
                "source_name": tx.get("source_name"),
                "destination_name": tx.get("destination_name"),
                "category_name": tx.get("category_name"),
                "budget_name": tx.get("budget_name"),
                "type": tx["type"],
            }
        ],
    }


def resolve_recurrence_id(client: FireflyClient, params: dict[str, Any]) -> str | None:
    recurrence_id = str(params.get("recurrence_id") or "").strip()
    if recurrence_id:
        return recurrence_id
    title = str(params.get("title") or params.get("description") or "").strip().casefold()
    if not title:
        return None
    for item in client.list_recurrences():
        attributes = item.get("attributes", {})
        candidate = str(attributes.get("title") or attributes.get("description") or "").strip().casefold()
        if candidate == title:
            return str(item.get("id") or "")
    return None


def resolve_budget(client: FireflyClient, name: str) -> dict[str, Any] | None:
    target = name.strip().casefold()
    if not target:
        return None
    for item in client.list_budgets():
        attributes = item.get("attributes", {})
        candidate = str(attributes.get("name") or item.get("name") or "").strip().casefold()
        if candidate == target:
            return item
    return None


def create_spending_chart(
    records: list[dict[str, Any]],
    *,
    days: int,
    label: str | None = None,
    source_text: str | None = None,
    limit: int | None = 8,
) -> tuple[str, str]:
    totals = aggregate_spending_by_category(records)
    if not totals:
        raise RuntimeError("No spending data found for the selected period.")

    top_items = list(totals.items()) if limit is None else list(totals.items())[:limit]
    labels = [name for name, _ in top_items]
    values = [value for _, value in top_items]

    tmp = tempfile.NamedTemporaryFile(prefix="firefly-spending-", suffix=".png", delete=False)
    tmp.close()

    fig, ax = plt.subplots(figsize=(9, 5.5))
    bars = ax.barh(labels[::-1], values[::-1], color="#2e8b57")
    period_label = label or localize(f"last {days} days", f"ultimi {days} giorni", source_text=source_text)
    ax.set_title(localize(f"Spending by category ({period_label})", f"Spese per categoria ({period_label})", source_text=source_text))
    ax.set_xlabel(localize("Amount", "Importo", source_text=source_text))
    for bar, value in zip(bars, values[::-1]):
        ax.text(bar.get_width(), bar.get_y() + bar.get_height() / 2, f" {value:.2f}", va="center")
    fig.tight_layout()
    fig.savefig(tmp.name, dpi=160)
    plt.close(fig)

    total = sum(values)
    caption = localize(
        f"Spending chart for {period_label}. Total outgoing: {total:.2f}",
        f"Grafico spese per {period_label}. Totale uscite: {total:.2f}",
        source_text=source_text,
    )
    return tmp.name, caption


def create_cashflow_chart(records: list[dict[str, Any]], *, days: int, label: str | None = None, source_text: str | None = None) -> tuple[str, str]:
    labels, incoming, outgoing = aggregate_cashflow_by_day(records)
    if not labels:
        raise RuntimeError("No cashflow data found for the selected period.")

    tmp = tempfile.NamedTemporaryFile(prefix="firefly-cashflow-", suffix=".png", delete=False)
    tmp.close()

    fig, ax = plt.subplots(figsize=(10, 5.5))
    ax.plot(labels, incoming, marker="o", label=localize("Incoming", "Entrate", source_text=source_text), color="#1f77b4")
    ax.plot(labels, outgoing, marker="o", label=localize("Outgoing", "Uscite", source_text=source_text), color="#d62728")
    period_label = label or localize(f"last {days} days", f"ultimi {days} giorni", source_text=source_text)
    ax.set_title(localize(f"Cashflow by day ({period_label})", f"Flusso di cassa giornaliero ({period_label})", source_text=source_text))
    ax.set_ylabel(localize("Amount", "Importo", source_text=source_text))
    ax.tick_params(axis="x", rotation=45)
    ax.legend()
    fig.tight_layout()
    fig.savefig(tmp.name, dpi=160)
    plt.close(fig)

    caption = localize(
        f"Cashflow chart for {period_label}.",
        f"Grafico del flusso di cassa per {period_label}.",
        source_text=source_text,
    )
    return tmp.name, caption


def create_balance_chart(balances: list[dict[str, Any]], *, source_text: str | None = None) -> tuple[str, str]:
    if not balances:
        raise RuntimeError("No balances available for a balance chart.")

    rows: list[tuple[str, float, str]] = []
    for account in balances:
        name = str(account.get("name") or "<unnamed>")
        currency = str(account.get("currency_code") or "")
        raw_amount = str(account.get("current_balance") or "0")
        try:
            amount = float(raw_amount)
        except ValueError:
            continue
        rows.append((name, amount, currency))

    if not rows:
        raise RuntimeError("No numeric balances available for a balance chart.")

    primary_currency = max(
        {currency: sum(abs(amount) for _, amount, cur in rows if cur == currency) for _, amount, currency in rows}.items(),
        key=lambda item: item[1],
    )[0]
    filtered = [(name, amount) for name, amount, currency in rows if currency == primary_currency]
    if not filtered:
        raise RuntimeError("Could not build a same-currency balance chart.")

    filtered.sort(key=lambda item: abs(item[1]), reverse=True)
    labels = [name for name, _ in filtered]
    values = [abs(amount) for _, amount in filtered]

    tmp = tempfile.NamedTemporaryFile(prefix="firefly-balances-", suffix=".png", delete=False)
    tmp.close()

    fig, ax = plt.subplots(figsize=(10, max(4.5, 0.7 * len(labels) + 1.5)))
    bars = ax.barh(labels[::-1], values[::-1], color="#2d6a9f")
    ax.set_title(localize(f"Account balances ({primary_currency})", f"Saldi dei conti ({primary_currency})", source_text=source_text))
    ax.set_xlabel(localize(f"Amount ({primary_currency})", f"Importo ({primary_currency})", source_text=source_text))
    total = sum(values) or 1.0
    for bar, value in zip(bars, values[::-1]):
        percentage = (value / total) * 100
        ax.text(bar.get_width(), bar.get_y() + bar.get_height() / 2, f"  {value:.2f} ({percentage:.1f}%)", va="center")
    fig.tight_layout()
    fig.savefig(tmp.name, dpi=160)
    plt.close(fig)

    caption = localize(f"Account balances in {primary_currency}.", f"Saldi dei conti in {primary_currency}.", source_text=source_text)
    return tmp.name, caption


def create_budget_chart(
    budget_limits: dict[str, float],
    budget_spent: dict[str, float],
    *,
    label: str,
    source_text: str | None = None,
) -> tuple[str, str]:
    names = sorted(set(list(budget_limits.keys()) + list(budget_spent.keys())))
    if not names:
        raise RuntimeError("No budget data found.")

    spent_vals = [budget_spent.get(n, 0.0) for n in names]
    limit_vals = [budget_limits.get(n, 0.0) for n in names]
    remaining_vals = [max(lim - sp, 0.0) for lim, sp in zip(limit_vals, spent_vals)]
    over_vals = [max(sp - lim, 0.0) for lim, sp in zip(limit_vals, spent_vals)]

    tmp = tempfile.NamedTemporaryFile(prefix="firefly-budget-", suffix=".png", delete=False)
    tmp.close()

    fig, ax = plt.subplots(figsize=(10, max(4.5, 0.7 * len(names) + 1.5)))
    bar_h = 0.3
    y = list(range(len(names)))
    y_spent = [i + bar_h for i in y]
    y_rem = [i - bar_h for i in y]

    ax.barh(y_spent[::-1], spent_vals[::-1], height=bar_h, color="#d62728",
            label=localize("Spent", "Speso", source_text=source_text))
    ax.barh(y_rem[::-1], remaining_vals[::-1], height=bar_h, color="#2ca02c",
            label=localize("Remaining", "Rimanente", source_text=source_text))
    if any(v > 0 for v in over_vals):
        ax.barh(y_spent[::-1], over_vals[::-1], height=bar_h, color="#ff7f0e",
                label=localize("Over budget", "Oltre budget", source_text=source_text))

    ax.set_yticks(y[::-1])
    ax.set_yticklabels(names[::-1])
    ax.set_title(localize(f"Budget usage ({label})", f"Utilizzo budget ({label})", source_text=source_text))
    ax.set_xlabel(localize("Amount", "Importo", source_text=source_text))
    ax.legend()
    fig.tight_layout()
    fig.savefig(tmp.name, dpi=160)
    plt.close(fig)

    summary_lines = [localize(f"Budget {label}:", f"Budget {label}:", source_text=source_text)]
    for name in names:
        sp = budget_spent.get(name, 0.0)
        lim = budget_limits.get(name, 0.0)
        if lim > 0:
            rem = lim - sp
            if rem >= 0:
                summary_lines.append(localize(
                    f"- {name}: {sp:.2f} / {lim:.2f}, {rem:.2f} remaining",
                    f"- {name}: {sp:.2f} / {lim:.2f}, rimasti {rem:.2f}",
                    source_text=source_text,
                ))
            else:
                summary_lines.append(localize(
                    f"- {name}: {sp:.2f} / {lim:.2f}, over by {abs(rem):.2f}",
                    f"- {name}: {sp:.2f} / {lim:.2f}, oltre di {abs(rem):.2f}",
                    source_text=source_text,
                ))
        else:
            summary_lines.append(f"- {name}: {sp:.2f}")
    caption = "\n".join(summary_lines)
    return tmp.name, caption


def create_recurrence_chart(
    items: list[dict[str, Any]],
    *,
    source_text: str | None = None,
) -> tuple[str, str]:
    rows: list[tuple[str, float, str]] = []
    for item in items:
        attributes = item.get("attributes", {})
        if not attributes.get("active", True):
            continue
        title = str(attributes.get("title") or attributes.get("description") or "<unnamed>")
        repetitions = attributes.get("repetitions") or []
        freq = "monthly"
        if isinstance(repetitions, list) and repetitions:
            rep = repetitions[0] if isinstance(repetitions[0], dict) else {}
            freq = str(rep.get("type") or "monthly").lower()
        txs = attributes.get("transactions") or []
        tx = txs[0] if isinstance(txs, list) and txs and isinstance(txs[0], dict) else {}
        amount_raw = str(tx.get("amount") or "0")
        try:
            amount = float(amount_raw)
        except ValueError:
            amount = 0.0
        tx_type = str(tx.get("type") or "withdrawal").lower()
        if freq == "weekly":
            amount = amount * 52 / 12
        elif freq == "yearly":
            amount = amount / 12
        rows.append((title, amount, tx_type))

    if not rows:
        raise RuntimeError("No active recurring transactions found.")

    rows.sort(key=lambda r: r[1], reverse=True)
    names = [r[0] for r in rows]
    amounts = [r[1] for r in rows]
    colors = []
    for _, _, tx_type in rows:
        if tx_type == "deposit":
            colors.append("#2ca02c")
        elif tx_type == "withdrawal":
            colors.append("#d62728")
        else:
            colors.append("#1f77b4")

    tmp = tempfile.NamedTemporaryFile(prefix="firefly-recur-", suffix=".png", delete=False)
    tmp.close()

    fig, ax = plt.subplots(figsize=(10, max(4.5, 0.5 * len(names) + 2.0)))
    bars = ax.barh(names[::-1], amounts[::-1], color=colors[::-1])
    ax.set_title(localize("Active recurring transactions (monthly equiv.)", "Ricorrenze attive (equiv. mensile)", source_text=source_text))
    ax.set_xlabel(localize("Amount / month", "Importo / mese", source_text=source_text))
    for bar, value in zip(bars, amounts[::-1]):
        ax.text(bar.get_width(), bar.get_y() + bar.get_height() / 2, f"  {value:.2f}", va="center")
    fig.tight_layout()
    fig.savefig(tmp.name, dpi=160)
    plt.close(fig)

    total_exp = sum(a for _, a, t in rows if t == "withdrawal")
    total_inc = sum(a for _, a, t in rows if t == "deposit")
    net = total_inc - total_exp
    sign = "+" if net >= 0 else ""
    caption_lines = [localize("Active recurring (monthly equiv.):", "Ricorrenze attive (equiv. mensile):", source_text=source_text)]
    if total_inc > 0:
        caption_lines.append(localize(f"Income: {total_inc:.2f}", f"Entrate: {total_inc:.2f}", source_text=source_text))
    if total_exp > 0:
        caption_lines.append(localize(f"Expenses: {total_exp:.2f}", f"Uscite: {total_exp:.2f}", source_text=source_text))
    caption_lines.append(localize(f"Net: {sign}{net:.2f}", f"Netto: {sign}{net:.2f}", source_text=source_text))
    return tmp.name, "\n".join(caption_lines)


def summarize_name_list(items: list[dict[str, Any]], *, limit: int = 25) -> list[str]:
    names: list[str] = []
    for item in items[:limit]:
        if "attributes" in item and isinstance(item["attributes"], dict):
            name = item["attributes"].get("name")
        else:
            name = item.get("name")
        if name:
            names.append(str(name))
    return names


def get_router_context(service: BridgeService) -> RouterContext:
    global _ROUTER_CONTEXT
    now = time.time()
    if _ROUTER_CONTEXT is not None and now - _ROUTER_CONTEXT.loaded_at < ROUTER_CONTEXT_CACHE_SECONDS:
        return _ROUTER_CONTEXT

    _ROUTER_CONTEXT = RouterContext(
        accounts=summarize_name_list(service.client.list_accounts("all")),
        categories=summarize_name_list(service.client.list_categories()),
        budgets=summarize_name_list(service.client.list_budgets()),
        merchant_shortcuts=sorted(service.settings.merchant_rules.keys())[:30],
        loaded_at=now,
    )
    return _ROUTER_CONTEXT


def build_router_instructions(service: BridgeService) -> str:
    context = get_router_context(service)
    preferred_language = configured_chat_language()

    return (
        "You are FireflyClaw, an intent router for a Firefly III Telegram finance bot.\n"
        "Prioritize reliability over detail.\n"
        "The user may write in Italian, English, or any other language. Understand the request regardless of language.\n"
        f"Preferred reply language for clarifications: {preferred_language}.\n"
        "Always keep clarifications in the same language as the user.\n"
        "Interpret the user's finance request and return ONLY valid JSON.\n"
        "Do not include markdown, prose, code fences, or explanations.\n"
        "Users may mention dates as DD-MM-YYYY or YYYY-MM-DD. Normalize all JSON params dates to YYYY-MM-DD.\n"
        "Preserve explicit months and date ranges exactly. Never substitute a different month or a wider window.\n"
        "Choose one intent from:\n"
        f"{sorted(INTENT_VALUES)}\n"
        "Schema:\n"
        "{"
        "\"intent\":\"...\","
        "\"confidence\":0.0,"
        "\"reply\":\"short clarification only when intent=clarify\","
        "\"params\":{"
        "\"days\":7,"
        "\"month\":\"YYYY-MM\","
        "\"start_date\":\"YYYY-MM-DD\","
        "\"end_date\":\"YYYY-MM-DD\","
        "\"query\":\"...\"," 
        "\"amount\":\"12.50\","
        "\"description\":\"...\","
        "\"source\":\"...\","
        "\"destination\":\"...\","
        "\"category\":\"...\","
        "\"budget\":\"...\"," 
        "\"budget_limit\":\"300.00\"," 
        "\"merchant\":\"...\"," 
        "\"date\":\"YYYY-MM-DD\"," 
        "\"title\":\"...\"," 
        "\"name\":\"...\"," 
        "\"account_type\":\"asset|cash|expense|revenue\"," 
        "\"recurrence_id\":\"...\"," 
        "\"cadence\":\"monthly|weekly|yearly\"," 
        "\"interval\":1," 
        "\"repeat_until\":\"YYYY-MM-DD\"," 
        "\"day_of_month\":1," 
        "\"day_of_week\":\"monday\"," 
        "\"with_graph\":false," 
        "\"all_categories\":false," 
        "\"notes\":\"...\"," 
        "\"live\":false," 
        "\"yes_high_value\":false"
        "}"
        "}\n"
        "Intent-specific required params:\n"
        "- create_expense: amount (required), description (required), source (asset account), destination (expense account), category, budget, date, merchant\n"
        "- create_income: amount (required), description (required), source (revenue account), destination (asset account), category, date\n"
        "- create_transfer: amount (required), description (required), source (asset account, required), destination (asset account, required), date\n"
        "- create_recurrence: cadence (monthly|weekly|yearly|daily, required), amount (required), title (required), description, source, destination, category, budget, first_date (YYYY-MM-DD)\n"
        "- delete_recurrence: recurrence_id (required) or title (required, for name-based lookup)\n"
        "- set_budget_limit: budget_name (required), amount (required), month (YYYY-MM, required)\n"
        "- create_account: name (required), account_type (asset|cash|expense|revenue, required)\n"
        "- create_category: name (required)\n"
        "- create_budget: name (required), amount (optional)\n"
        "- get_recent: days or start_date+end_date, query (optional keyword)\n"
        "- top_spending_categories: month or start_date+end_date, with_graph (bool), all_categories (bool)\n"
        "- compare_periods: left_period (object with month or start_date+end_date), right_period (same), metric (summary|income_vs_spending|spending_total|top_spending_categories)\n"
        "ATM/cash withdrawal = create_expense (it is a Firefly withdrawal type, not a separate intent).\n"
        "A transfer without explicit source account: use create_transfer but leave source empty; the bot will ask for it.\n"
        "Rules:\n"
        "- Use clarify if the user intent is ambiguous or a write request is missing key details.\n"
        "- reply should be in the same language as the user when intent=clarify.\n"
        "- Prefer broad correctness over detailed guesses.\n"
        "- Do not hallucinate merchant names, item lists, tax details, account names, or dates.\n"
        "- If the user asks what you can do, use help.\n"
        "- If the user asks how much money they have, use get_balances.\n"
        "- If the user asks how much they spent in a period, use get_spending_total.\n"
        "- If the user asks how much they earned and spent, or asks for income vs outgoing totals, use get_income_vs_spending.\n"
        "- If the user asks for a graph/chart of balances/net worth/composition, use graph_balances.\n"
        "- If the user asks for a graph/chart of spending/expenses, use graph_spending.\n"
        "- If the user asks for incoming vs outgoing or cashflow, use graph_cashflow.\n"
        "- If the user asks for a graph/chart of budget usage, budget limits, or how much budget is left, use graph_budget.\n"
        "- If the user asks for a graph/chart of recurring transactions or recurring amounts, use graph_recurrences.\n"
        "- If the user asks where they spent the most last month, top categories, or a graph by category, use top_spending_categories.\n"
        "- If the user asks about budgets, budget usage, or budget charts, use graph_budget for a visual chart or budget_report for a text summary.\n"
        "- For reports, transaction searches, and graphs, preserve explicit time windows from the user. Use month for full-month requests and start_date/end_date for custom ranges.\n"
        "- If the user asks to set, raise, lower, or adjust a budget amount/limit for a month, use set_budget_limit.\n"
        "- If the user asks to list recurring transactions, their amounts, or a summary of recurrences, use list_recurrences.\n"
        "- If the user asks to add a recurring transaction, use create_recurrence.\n"
        "- If the user asks to delete or remove a recurring transaction, use delete_recurrence.\n"
        "- If the user asks to add a new category, use create_category.\n"
        "- If the user asks to add a new budget, use create_budget.\n"
        "- If the user asks to add a new account, debit card, wallet, or cash account, use create_account.\n"
        "- For 'add transaction' messages, infer expense/income/transfer if possible; otherwise clarify.\n"
        "- For transfer-like language such as moving money from cash to cards/accounts, use create_transfer.\n"
        "- For new category names, preserve the user's language. Italian category names are allowed and preferred if the user writes in Italian.\n"
        "- For create_expense/create_income/create_transfer, keep description short and generic.\n"
        "- For categories, prefer exact names from Known category names. Use broad existing categories when possible.\n"
        "- If confidence is low, simplify category/description instead of adding detail.\n"
        "- Keep writes conservative. live should default to false unless the user explicitly says to commit/write/save for real.\n"
        "- Prefer exact names from the provided lists when possible.\n"
        f"Known account names: {context.accounts}\n"
        f"Known category names: {context.categories}\n"
        f"Known budget names: {context.budgets}\n"
        f"Known merchant shortcuts: {context.merchant_shortcuts}\n"
    )


def extract_json_object(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if not raw:
        raise ValueError("Router returned empty output.")

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"Router did not return JSON: {raw[:400]}")
    return json.loads(raw[start : end + 1])


def reset_router_session(state: dict[str, Any]) -> None:
    state["router_session_epoch"] = int(state.get("router_session_epoch", 0) or 0) + 1
    state.pop("router_previous_response_id", None)
    state["router_consecutive_failures"] = 0


def perform_router_request(service: BridgeService, text: str, state: dict[str, Any]) -> dict[str, Any]:
    instructions = build_router_instructions(service)
    raw_text = call_ai_text(instructions, text, max_tokens=600)
    payload = extract_json_object(raw_text)
    state["router_consecutive_failures"] = 0
    return payload


# Note: this bot defines its own router loop around ai_router.call_ai_text.
def run_picoclaw_router(service: BridgeService, text: str, state: dict[str, Any]) -> dict[str, Any] | None:
    for attempt in range(2):
        try:
            payload = perform_router_request(service, text, state)
        except (AIRouterGatewayError, ValueError, json.JSONDecodeError):
            state["router_consecutive_failures"] = int(state.get("router_consecutive_failures", 0) or 0) + 1
            if attempt == 0:
                reset_router_session(state)
                continue
            return None

        intent = str(payload.get("intent", "")).strip()
        if intent not in INTENT_VALUES:
            state["router_consecutive_failures"] = int(state.get("router_consecutive_failures", 0) or 0) + 1
            if attempt == 0:
                reset_router_session(state)
                continue
            return None
        return payload
    return None


def run_receipt_ocr(image_bytes: bytes, *, mime_type: str) -> str | None:
    executable = find_tesseract_executable()
    if not executable:
        return None

    suffix = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }.get(mime_type, ".jpg")

    tmp = tempfile.NamedTemporaryFile(prefix="firefly-receipt-", suffix=suffix, delete=False)
    try:
        tmp.write(image_bytes)
        tmp.close()

        commands = (
            [executable, tmp.name, "stdout", "-l", "ita+eng", "--psm", "6"],
            [executable, tmp.name, "stdout", "-l", "eng", "--psm", "6"],
        )
        for command in commands:
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=20,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            text = clip_text(completed.stdout, limit=4000)
            if text:
                return text
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    return None


def use_pdfapihub_ocr_provider() -> bool:
    raw = os.getenv("FIREFLY_PDF_OCR_PROVIDER_ENABLED", "true").strip().casefold()
    return raw not in {"0", "false", "no", "off"}


def external_pdf_ocr_plugin_command() -> str:
    # Optional compatibility hook for custom OCR wrappers.
    # Example: "my-ocr-cli --file {file} --mime {mime}"
    return os.getenv("FIREFLY_PDF_OCR_PLUGIN_CMD", "").strip()


def run_external_pdf_ocr_plugin(image_bytes: bytes, *, mime_type: str, file_name: str | None = None) -> str | None:
    command = external_pdf_ocr_plugin_command()
    if not command:
        return None

    suffix = ".pdf" if mime_type == "application/pdf" else Path(file_name or "receipt.jpg").suffix or ".jpg"
    tmp = tempfile.NamedTemporaryFile(prefix="firefly-plugin-ocr-", suffix=suffix, delete=False)
    try:
        tmp.write(image_bytes)
        tmp.close()
        try:
            raw_parts = shlex.split(command)
        except ValueError:
            return None
        if not raw_parts:
            return None
        parts: list[str] = []
        has_file_placeholder = False
        substitutions = {
            "{file}": tmp.name,
            "{mime}": mime_type,
            "{name}": file_name or Path(tmp.name).name,
        }
        for raw_part in raw_parts:
            part = raw_part
            for placeholder, replacement in substitutions.items():
                if placeholder in part:
                    part = part.replace(placeholder, replacement)
                    if placeholder == "{file}":
                        has_file_placeholder = True
            parts.append(part)
        executable = shutil.which(parts[0])
        if not executable:
            return None
        parts[0] = executable
        if not has_file_placeholder:
            parts.append(tmp.name)
        try:
            completed = subprocess.run(
                parts,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=45,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        raw = (completed.stdout or "").strip()
        if not raw:
            return None
        if raw.startswith("{") or raw.startswith("["):
            try:
                payload = json.loads(raw)
            except ValueError:
                return normalize_ai_ocr_text(raw)
            return extract_text_from_pdfapihub_payload(payload)
        return normalize_ai_ocr_text(raw)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def pdfapihub_api_key() -> str:
    return (
        os.getenv("PDFAPIHUB_API_KEY", "").strip()
        or os.getenv("FIREFLY_PDFAPIHUB_API_KEY", "").strip()
    )


def pdfapihub_base_url() -> str:
    return os.getenv("PDFAPIHUB_BASE_URL", "https://pdfapihub.com/api").strip().rstrip("/")


def extract_text_from_pdfapihub_payload(payload: Any) -> str | None:
    candidates: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                candidates.append(cleaned)
            return
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if isinstance(value, dict):
            for key in ("text", "ocr_text", "extracted_text", "content", "markdown", "raw_text"):
                raw = value.get(key)
                if isinstance(raw, str) and raw.strip():
                    candidates.append(raw.strip())
            for key in ("result", "data", "pages", "items", "blocks"):
                if key in value:
                    walk(value.get(key))

    walk(payload)
    if not candidates:
        return None
    best = max(candidates, key=len)
    return normalize_ai_ocr_text(best)


def run_pdfapihub_ocr(image_bytes: bytes, *, mime_type: str, file_name: str | None = None) -> str | None:
    if not use_pdfapihub_ocr_provider():
        return None
    plugin_text = run_external_pdf_ocr_plugin(image_bytes, mime_type=mime_type, file_name=file_name)
    if plugin_text:
        return plugin_text
    api_key = pdfapihub_api_key()
    if not api_key:
        return None

    is_pdf = mime_type == "application/pdf" or str(file_name or "").casefold().endswith(".pdf")
    endpoint = "/v1/pdf/ocr/parse" if is_pdf else "/v1/image/ocr/parse"
    url = f"{pdfapihub_base_url()}{endpoint}"
    headers = {"CLIENT-API-KEY": api_key}
    effective_name = file_name or ("receipt.pdf" if is_pdf else "receipt.jpg")
    files = {"file": (effective_name, image_bytes, mime_type)}
    data = {
        "lang": os.getenv("FIREFLY_PDF_OCR_LANG", "ita+eng"),
    }

    try:
        response = requests.post(url, headers=headers, files=files, data=data, timeout=35)
    except RequestException:
        return None

    if response.status_code >= 400:
        return None
    try:
        payload = response.json()
    except ValueError:
        return normalize_ai_ocr_text(response.text)
    return extract_text_from_pdfapihub_payload(payload)


def use_ai_ocr() -> bool:
    raw = os.getenv("FIREFLY_RECEIPT_AI_OCR", "true").strip().casefold()
    return raw not in {"0", "false", "no", "off"}


def normalize_ai_ocr_text(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", text, count=1).rstrip()
        if text.endswith("```"):
            text = text[:-3].rstrip()
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = clip_text(text, limit=4000)
    if not text:
        return None
    if not re.search(r"[0-9A-Za-z]", text):
        return None
    return text


def ocr_text_score(text: str | None) -> int:
    if not text:
        return 0
    normalized = normalize_natural_text(text)
    amount_matches = len(re.findall(r"\d+(?:[.,]\d{2})", text))
    keyword_hits = 0
    for keyword in ("totale", "total", "eur", "euro", "pagamento", "payment", "ricevuta", "receipt", "pos", "importo"):
        if keyword in normalized:
            keyword_hits += 1
    length_score = min(len(text) // 50, 20)
    return (amount_matches * 10) + (keyword_hits * 4) + length_score


def select_best_ocr_text(*candidates: str | None) -> str | None:
    best_text: str | None = None
    best_score = -1
    for candidate in candidates:
        normalized = normalize_ai_ocr_text(candidate)
        if not normalized:
            continue
        score = ocr_text_score(normalized)
        if score > best_score or (score == best_score and best_text is not None and len(normalized) > len(best_text)):
            best_text = normalized
            best_score = score
    return best_text


def run_receipt_ai_ocr(image_bytes: bytes, *, mime_type: str) -> str | None:
    if not use_ai_ocr():
        return None
    instructions = (
        "You are an OCR transcriber for finance receipts and bank screenshots.\n"
        "Return only plain text that is visibly readable from the image.\n"
        "Do not summarize or explain.\n"
        "Preserve amounts, dates, merchants, and currency tokens exactly.\n"
        "If text is unreadable, return an empty string."
    )
    try:
        extracted = call_ai_vision(
            instructions,
            "Transcribe this image as plain text lines.",
            image_bytes,
            mime_type=mime_type,
            max_tokens=1200,
        )
    except (AIRouterGatewayError, ValueError):
        return None
    return normalize_ai_ocr_text(extracted)


def parse_receipt_date(value: str | None) -> str | None:
    parsed = parse_flexible_date(value)
    return parsed.isoformat() if parsed else None


def titlecase_merchant(value: str | None) -> str | None:
    cleaned = clean_free_text_slot(value)
    if not cleaned:
        return None
    folded = normalize_natural_text(cleaned)
    if "in's mercato" in folded or "ins mercato" in folded or "supermercato in" in folded:
        return "Supermercato In's"
    tokens: list[str] = []
    for token in cleaned.split():
        if token.isupper() and len(token) <= 4:
            tokens.append(token)
        else:
            tokens.append(token[:1].upper() + token[1:])
    return " ".join(tokens)


def _build_receipt_candidate(
    *,
    source: str,
    amount: str,
    merchant: str | None,
    transaction_kind: str,
    transaction_date: str | None,
    source_hint: str | None,
    note_snippet: str | None = None,
) -> dict[str, Any]:
    topic_text = note_snippet or source
    description = infer_receipt_description(topic_text, merchant, transaction_kind=transaction_kind)
    notes = []
    if source_hint:
        notes.append(f"Source hint: {source_hint}")
    if note_snippet:
        notes.append(f"Extracted text: {clip_text(note_snippet, limit=220)}")
    return {
        "intent": transaction_kind,
        "amount": amount.replace(",", "."),
        "merchant": merchant,
        "description": description,
        "date": transaction_date,
        "source_hint": source_hint,
        "topic_hint_text": topic_text,
        "notes": " | ".join(part for part in notes if part) or None,
    }


def _extract_single_receipt_candidate(text: str | None, *, caption: str | None = None) -> dict[str, Any] | None:
    source = "\n".join(part for part in [caption, text] if part).strip()
    if not source:
        return None

    normalized = normalize_natural_text(source)
    lines = [clip_text(line, limit=120) for line in source.splitlines()]
    compact_lines = [line for line in (clean_free_text_slot(line) for line in lines) if line]
    if not compact_lines:
        compact_lines = [clip_text(source, limit=120) or source]

    provider_match = detect_receipt_source_hint(source)

    merchant = None
    amount = None
    transaction_kind = "create_expense"

    revolut_match = re.search(r"revolut\s+([^\n]+?)\s+(\d+(?:[.,]\d{1,2})?)\s*(?:eur|euro)?", normalized, re.IGNORECASE)
    if revolut_match:
        merchant = titlecase_merchant(revolut_match.group(1))
        amount = revolut_match.group(2).replace(",", ".")

    if amount is None:
        bper_match = re.search(r"pagamento pos di\s+(\d+(?:[.,]\d{1,2})?)\s*(?:eur|euro)\s+presso\s+(.+?)(?:\s+tocca|\s*$)", normalized, re.IGNORECASE)
        if bper_match:
            amount = bper_match.group(1).replace(",", ".")
            merchant = titlecase_merchant(bper_match.group(2))

    if amount is None:
        for keyword_pattern in (
            r"totale complessivo\s*(?:eur|euro)?\s*(\d+(?:[.,]\d{1,2})?)",
            r"importo pagato\s*(?:eur|euro)?\s*(\d+(?:[.,]\d{1,2})?)",
            r"importo\s*(?:eur|euro)?\s*(\d+(?:[.,]\d{1,2})?)",
            r"pagamento elettronico\s*(\d+(?:[.,]\d{1,2})?)",
            r"pagamento contante\s*(\d+(?:[.,]\d{1,2})?)",
        ):
            match = re.search(keyword_pattern, normalized, re.IGNORECASE)
            if match:
                amount = match.group(1).replace(",", ".")
                break

    if merchant is None:
        merchant_match = re.search(r"presso\s+(.+?)(?:\s+tocca|\s*$)", normalized, re.IGNORECASE)
        if merchant_match:
            merchant = titlecase_merchant(merchant_match.group(1))

    if merchant is None and "supermercato in" in normalized:
        merchant = "Supermercato In's"

    if merchant is None:
        for line in compact_lines:
            folded = normalize_natural_text(line)
            if any(keyword in folded for keyword in {"supermercato", "mercato", "coop", "carrefour", "esselunga", "argenta"}):
                merchant = titlecase_merchant(line)
                break
            if folded in IGNORED_RECEIPT_MERCHANT_LINES:
                continue
            if len(folded) >= 4 and any(char.isalpha() for char in folded):
                merchant = titlecase_merchant(line)
                break

    date_match = re.search(r"\b(\d{2}[./]\d{2}[./]\d{2,4})\b", normalized)
    transaction_date = parse_receipt_date(date_match.group(1)) if date_match else None

    if any(keyword in normalized for keyword in {"accredito", "bonifico ricevuto", "salary", "stipendio", "received payment"}):
        transaction_kind = "create_income"

    if amount is None:
        generic_amounts = re.findall(r"\b(\d+(?:[.,]\d{1,2})?)\s*(?:eur|euro)\b", normalized, re.IGNORECASE)
        if generic_amounts:
            amount = generic_amounts[-1].replace(",", ".")

    if amount is None:
        return None

    return _build_receipt_candidate(
        source=source,
        amount=amount,
        merchant=merchant,
        transaction_kind=transaction_kind,
        transaction_date=transaction_date,
        source_hint=provider_match,
        note_snippet=source,
    )


_RECEIPT_INCOME_KEYWORDS = frozenset({
    "accredito", "bonifico ricevuto", "salary", "stipendio", "received payment",
    "entrata", "ricevuto", "pagamento ricevuto", "hai ricevuto", "ricevuta",
    "bonifico in entrata", "accredito bonifico", "credited",
})


def extract_receipt_candidates(text: str | None, *, caption: str | None = None) -> list[dict[str, Any]]:
    source = "\n".join(part for part in [caption, text] if part).strip()
    if not source:
        return []

    normalized_source = normalize_natural_text(source)
    default_kind = "create_income" if any(kw in normalized_source for kw in _RECEIPT_INCOME_KEYWORDS) else "create_expense"

    transaction_date = None
    date_match = re.search(r"\b(\d{2}[./]\d{2}[./]\d{2,4})\b", source)
    if date_match:
        transaction_date = parse_receipt_date(date_match.group(1))

    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def append_candidate(*, amount: str, merchant: str | None, source_hint: str | None, snippet: str, transaction_kind: str | None = None) -> None:
        clean_amount = amount.replace(",", ".")
        clean_merchant = titlecase_merchant(merchant) if merchant else None
        key = (
            clean_amount,
            normalize_natural_text(clean_merchant or ""),
            normalize_natural_text(source_hint or ""),
        )
        if key in seen:
            return
        seen.add(key)
        kind = transaction_kind if transaction_kind is not None else default_kind
        candidates.append(
            _build_receipt_candidate(
                source=source,
                amount=clean_amount,
                merchant=clean_merchant,
                transaction_kind=kind,
                transaction_date=transaction_date,
                source_hint=source_hint,
                note_snippet=snippet,
            )
        )

    # 1. POS payment blocks (BPER / standard Italian bank notifications)
    for match in re.finditer(
        r"pagamento\s+pos(?:\s+di)?\s+(\d+(?:[.,]\d{1,2})?)\s*(?:eur|euro|€)?\s+(?:presso|at)\s+([^\n]+)",
        source,
        re.IGNORECASE,
    ):
        snippet = match.group(0)
        append_candidate(
            amount=match.group(1),
            merchant=match.group(2).strip(" ."),
            source_hint=detect_receipt_source_hint(snippet) or detect_receipt_source_hint(source),
            snippet=snippet,
            transaction_kind="create_expense",
        )

    # 2. Generic bank notification: "€X,XX [presso|at|-] Merchant" or "Merchant €X,XX"
    for match in re.finditer(
        r"(?:€|EUR)\s*(\d+(?:[.,]\d{1,2})?)(?:\s*[-–]\s*|\s+(?:presso|at)\s+)([A-Za-zÀ-ÿ][^\n]{2,60})",
        source,
        re.IGNORECASE,
    ):
        snippet = match.group(0)
        append_candidate(
            amount=match.group(1),
            merchant=match.group(2).strip(" .-"),
            source_hint=detect_receipt_source_hint(source),
            snippet=snippet,
        )

    # 3. Revolut blocks
    revolut_blocks = re.finditer(
        r"revolut[\s:,-]*([^\n€]{2,80}?)\s+(\d+(?:[.,]\d{1,2})?)\s*(?:eur|euro|€)",
        source,
        re.IGNORECASE,
    )
    for match in revolut_blocks:
        snippet = match.group(0)
        append_candidate(
            amount=match.group(2),
            merchant=match.group(1).strip(" ."),
            source_hint="Revolut",
            snippet=snippet,
        )

    # 4. Generic EUR amount lines (amount followed by EUR/€, OR € followed by amount)
    if not candidates:
        lines = [line.strip() for line in source.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            amount_match = re.search(
                r"(\d+(?:[.,]\d{1,2})?)\s*(?:eur|euro|€)\b|(?:€|eur(?:o)?)\s*(\d+(?:[.,]\d{1,2})?)",
                line,
                re.IGNORECASE,
            )
            if not amount_match:
                continue
            raw_amount = amount_match.group(1) or amount_match.group(2)
            if not raw_amount:
                continue
            nearby = "\n".join(lines[max(index - 2, 0) : min(index + 2, len(lines))])
            source_hint = detect_receipt_source_hint(nearby) or detect_receipt_source_hint(source)
            merchant = None
            for candidate_line in reversed(lines[max(index - 2, 0) : index + 1]):
                folded = normalize_natural_text(candidate_line)
                if folded in IGNORED_RECEIPT_MERCHANT_LINES:
                    continue
                if any(char.isalpha() for char in candidate_line) and "eur" not in folded:
                    merchant = candidate_line
                    break
            if merchant and source_hint and normalize_natural_text(merchant) == normalize_natural_text(source_hint):
                merchant = lines[index - 1] if index > 0 else merchant
            if merchant:
                append_candidate(amount=raw_amount, merchant=merchant, source_hint=source_hint, snippet=nearby)

    if candidates:
        return candidates

    single = _extract_single_receipt_candidate(text, caption=caption)
    return [single] if single else []


def extract_receipt_candidate(text: str | None, *, caption: str | None = None) -> dict[str, Any] | None:
    candidates = extract_receipt_candidates(text, caption=caption)
    return candidates[0] if candidates else None


def resolve_receipt_source_account(service: BridgeService, hint: str | None) -> tuple[str | None, list[str]]:
    names = [str(account.get("name") or "").strip() for account in service.account_balances()]
    account_names = [name for name in names if name]
    if not hint:
        return None, account_names

    hint_folded = normalize_natural_text(hint)
    exact = [name for name in account_names if normalize_natural_text(name) == hint_folded]
    if exact:
        return exact[0], account_names

    fuzzy = [
        name for name in account_names
        if hint_folded in normalize_natural_text(name) or normalize_natural_text(name) in hint_folded
    ]
    if len(fuzzy) == 1:
        return fuzzy[0], account_names
    return None, account_names


def build_receipt_account_clarification(candidate: dict[str, Any], account_names: list[str], *, source_text: str | None = None) -> str:
    amount = str(candidate.get("amount") or "").strip()
    merchant = str(candidate.get("merchant") or candidate.get("description") or "that payment").strip()
    source_hint = str(candidate.get("source_hint") or "the screenshot").strip()
    account_list = ", ".join(account_names[:5]) or "your main account"
    return localize(
        f"I read a payment of {amount} from {merchant}, but I am not sure which account to use for the debit ({source_hint}). Which account should I use? Available accounts: {account_list}.",
        f"Ho letto un pagamento di {amount} da {merchant}, ma non sono sicuro di quale conto usare per l'addebito ({source_hint}). Quale conto devo usare? Conti disponibili: {account_list}.",
        source_text=source_text,
    )


def build_receipt_fallback_payload(service: BridgeService, *, caption: str | None = None, extracted_text: str | None = None) -> dict[str, Any] | None:
    candidates = extract_receipt_candidates(extracted_text, caption=caption)
    if not candidates:
        return None

    source_text = caption or extracted_text
    items: list[dict[str, Any]] = []
    for candidate in candidates:
        source_account, _ = resolve_receipt_source_account(service, str(candidate.get("source_hint") or "").strip() or None)
        params: dict[str, Any] = {
            "amount": candidate["amount"],
            "description": candidate["description"],
            "merchant": candidate.get("merchant"),
            "date": candidate.get("date"),
            "notes": candidate.get("notes"),
            "category": infer_receipt_category(
                service,
                str(candidate.get("topic_hint_text") or source_text or ""),
                str(candidate.get("merchant") or ""),
                transaction_kind=str(candidate.get("intent") or "create_expense"),
            ),
            "live": False,
        }
        if source_account:
            params["source"] = source_account
        items.append(
            {
                "intent": candidate["intent"],
                "params": {key: value for key, value in params.items() if value not in {None, ""}},
            }
        )

    if len(items) > 1:
        return {
            "intent": "create_transaction_batch",
            "confidence": 0.82,
            "reply": "",
            "source_text": source_text,
            "params": {"transactions": items, "live": False},
        }

    return {
        "intent": items[0]["intent"],
        "confidence": 0.78,
        "reply": "",
        "source_text": source_text,
        "params": items[0]["params"],
    }


def run_receipt_router(
    service: BridgeService,
    image_bytes: bytes,
    *,
    mime_type: str,
    caption: str | None = None,
    extracted_text: str | None = None,
) -> dict[str, Any] | None:
    context = get_router_context(service)
    instructions = (
        "You are FireflyClaw extracting a receipt or financial document for a Firefly III finance bot.\n"
        "Prioritize reliability over detail.\n"
        "The user may write in any language. Return ONLY valid JSON.\n"
        "If this is a receipt, infer the most likely Firefly action and use one of the supported intents.\n"
        f"Supported intents: {sorted(INTENT_VALUES)}\n"
        "Prioritize create_expense for purchase receipts unless evidence suggests income or transfer.\n"
        "Use clarify if key facts are uncertain; ask the minimum safe follow-up in the same language as the user.\n"
        "Use only details clearly visible in the image or OCR text.\n"
        "Count how many distinct transactions are visible and include it as visible_transaction_count (integer >= 1).\n"
        "Do not hallucinate unreadable merchant names, item lines, or dates.\n"
        "Keep params.description short and generic.\n"
        "Prefer broad existing categories from Known category names when possible.\n"
        "Preserve category names in the user's language when proposing a new category.\n"
        "You may receive OCR text extracted from the image. Use it as supporting evidence, but prefer the image if there is a conflict.\n"
        f"Known account names: {context.accounts}\n"
        f"Known category names: {context.categories}\n"
        f"Known budget names: {context.budgets}\n"
        f"Known merchant shortcuts: {context.merchant_shortcuts}\n"
        "Schema matches the normal router, including params.amount, params.description, params.merchant, params.category, params.date, params.start_date, params.end_date, params.live, and reply."
    )
    parts = [caption or "Analyze this receipt and prepare the safest Firefly action."]
    if extracted_text:
        parts.append(f"OCR text:\n{extracted_text}")
    user_text = "\n\n".join(part for part in parts if part)

    try:
        raw_text = call_ai_vision(instructions, user_text, image_bytes, mime_type=mime_type, max_tokens=1200)
        parsed = extract_json_object(raw_text)
    except (AIRouterGatewayError, ValueError, json.JSONDecodeError):
        return None

    intent = str(parsed.get("intent", "")).strip()
    if intent not in INTENT_VALUES:
        return None
    return parsed


def extract_visible_transaction_count(payload: dict[str, Any] | None) -> int | None:
    if not isinstance(payload, dict):
        return None
    candidates = (
        payload.get("visible_transaction_count"),
        payload.get("visible_transactions"),
        payload.get("transaction_count"),
        payload.get("count"),
    )
    for raw in candidates:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    params = payload.get("params")
    if isinstance(params, dict):
        for key in ("visible_transaction_count", "visible_transactions", "transaction_count"):
            try:
                value = int(params.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
    return None


def fallback_payload_transaction_count(payload: dict[str, Any] | None) -> int:
    if not isinstance(payload, dict):
        return 0
    intent = str(payload.get("intent") or "").strip()
    params = payload.get("params")
    if intent == "create_transaction_batch" and isinstance(params, dict):
        transactions = params.get("transactions")
        if isinstance(transactions, list):
            return len([item for item in transactions if isinstance(item, dict)])
    if intent in {"create_expense", "create_income", "create_transfer"}:
        return 1
    return 0


def help_text(source_text: str | None = None) -> str:
    lines = [
        bot_text("help_title", source_text=source_text),
        bot_text("help_intro", source_text=source_text),
        bot_text("help_commands_hint", source_text=source_text),
        bot_text("help_periods", source_text=source_text),
        "",
        bot_text("help_natural_title", source_text=source_text),
    ]
    lines.extend(f"- {example}" for example in bot_list("help_natural_examples", source_text=source_text))
    lines.extend(
        [
            "",
            bot_text("help_write_safety", source_text=source_text),
            bot_text("help_high_value", source_text=source_text),
        ]
    )
    return "\n".join(lines)


def commands_text(source_text: str | None = None) -> str:
    lines = [
        bot_text("commands_title", source_text=source_text),
        bot_text("commands_intro", source_text=source_text),
        bot_text("commands_periods", source_text=source_text),
        "",
    ]
    lines.extend(bot_list("command_lines", source_text=source_text))
    lines.extend(["", bot_text("commands_natural_hint", source_text=source_text)])
    return "\n".join(lines)


def _safe_backup_collect(backup: dict[str, Any], key: str, loader: Any) -> None:
    try:
        backup["data"][key] = loader()
    except Exception as exc:
        backup["errors"][key] = str(exc)


def _export_all_transactions(client: FireflyClient) -> list[dict[str, Any]]:
    try:
        return client.export_collection("transactions", params={"limit": 100})
    except FireflyAPIError:
        return client.export_collection(
            "transactions",
            params={"start": "1970-01-01", "end": "2100-12-31", "limit": 100},
        )


def create_firefly_backup_document(service: BridgeService) -> tuple[str, str, dict[str, Any]]:
    exported_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    backup: dict[str, Any] = {
        "format": "firefly-picoclaw-companion-backup/v1",
        "exported_at": exported_at,
        "source": "telegram /backup",
        "data": {},
        "errors": {},
    }

    _safe_backup_collect(backup, "about", service.client.health)
    _safe_backup_collect(backup, "transactions", lambda: _export_all_transactions(service.client))
    _safe_backup_collect(backup, "accounts", lambda: service.client.list_accounts("all"))
    _safe_backup_collect(backup, "categories", service.client.list_categories)
    _safe_backup_collect(backup, "budgets", service.client.list_budgets)
    _safe_backup_collect(backup, "recurrences", service.client.list_recurrences)
    for key, path in (
        ("bills", "bills"),
        ("piggy_banks", "piggy-banks"),
        ("tags", "tags"),
        ("rules", "rules"),
    ):
        _safe_backup_collect(backup, key, lambda path=path: service.client.export_collection(path, params={"limit": 100}))

    fd, document_path = tempfile.mkstemp(prefix="firefly-backup-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(backup, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    filename = f"firefly-backup-{datetime.utcnow().strftime('%Y%m%d-%H%M%SZ')}.json"
    return document_path, filename, backup


def parse_kv_args(raw: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    if not raw.strip():
        return parsed
    for token in shlex.split(raw):
        if "=" not in token:
            raise ValueError(f"Expected key=value argument, got: {token}")
        key, value = token.split("=", 1)
        parsed[key.strip().lower()] = value.strip()
    return parsed


def as_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on", "live"}


def parse_amount_from_text(text: str) -> str | None:
    return _parse_amount_from_text(text)


def format_money(value: Any, *, currency: str = "EUR ") -> str:
    raw = str(value or "").strip()
    if not raw:
        return f"{currency}?"
    try:
        amount = Decimal(raw.replace(",", "."))
    except Exception:
        return f"{currency}{raw}"
    return f"{currency}{amount.quantize(Decimal('0.01'))}"


def localized_transaction_type(value: Any, *, source_text: str | None = None) -> str:
    transaction_type = str(value or "").strip()
    labels = {
        "withdrawal": ("Expense", "Spesa"),
        "deposit": ("Income", "Entrata"),
        "transfer": ("Transfer", "Trasferimento"),
    }
    en, it = labels.get(transaction_type, (transaction_type or "Transaction", transaction_type or "Transazione"))
    return localize(en, it, source_text=source_text)


def contains_any(text: str, keywords: set[str] | tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def clean_free_text_slot(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = " ".join(value.rstrip(" .,!?:;").split())
    if not cleaned:
        return None
    payment_words = r"cash|contanti|contante|card|carta|bancomat|debit(?:o)?|credit(?:o)?|visa|mastercard|paypal|revolut|wise"
    cleaned = re.sub(rf"\b(?:paid|made|pagato|pagata|pagati|pagate|pagando)\s+(?:with|con)\s+(?:{payment_words})\b.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(rf"\b(?:with|con|in)\s+(?:{payment_words})\b.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(?:today|oggi|yesterday|ieri|tomorrow|domani)\b.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(?:on|il|del|della)\s+\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = " ".join(cleaned.strip(" .,!?:;").split())
    return cleaned or None


def clean_transaction_description(description: str, *, source_text: str | None = None, merchant: str | None = None) -> str:
    cleaned = clean_free_text_slot(description)
    if cleaned:
        return cleaned
    merchant_clean = clean_free_text_slot(merchant)
    if merchant_clean:
        return merchant_clean
    source_clean = clean_free_text_slot(source_text)
    return source_clean or description.strip()


def unique_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        folded = normalize_natural_text(value)
        if not folded or folded in seen:
            continue
        seen.add(folded)
        output.append(value)
    return output


def detect_receipt_source_hint(text: str | None) -> str | None:
    normalized = normalize_natural_text(text)
    for keyword, label in RECEIPT_SOURCE_HINTS.items():
        if keyword in normalized:
            return label
    return None


def list_known_category_names(service: BridgeService | None) -> list[str]:
    names: list[str] = []
    if service is not None:
        client = getattr(service, "client", None)
        if client is not None and hasattr(client, "list_categories"):
            try:
                category_records = client.list_categories()
            except Exception:
                category_records = []
            for record in category_records:
                name = str(record.get("attributes", {}).get("name") or record.get("name") or "").strip()
                if name:
                    names.append(name)
        settings = getattr(service, "settings", None)
        if settings is not None and hasattr(settings, "category_aliases"):
            names.extend(str(value) for value in settings.category_aliases.values())
    names.extend(DEFAULT_CATEGORY_NAMES)
    return unique_preserving_order(names)


def choose_existing_name(candidates: tuple[str, ...] | list[str], existing_names: list[str]) -> str | None:
    normalized = {normalize_natural_text(name): name for name in existing_names if str(name).strip()}
    for candidate in candidates:
        key = normalize_natural_text(candidate)
        if key in normalized:
            return normalized[key]
    for candidate in candidates:
        key = normalize_natural_text(candidate)
        for existing_key, existing_name in normalized.items():
            if key and (key in existing_key or existing_key in key):
                return existing_name
    return None


def infer_receipt_topic(source: str | None, merchant: str | None) -> dict[str, Any] | None:
    haystack = normalize_natural_text("\n".join(part for part in [merchant, source] if part))
    for rule in RECEIPT_TOPIC_RULES:
        if any(keyword in haystack for keyword in rule["keywords"]):
            return rule
    return None


def infer_receipt_description(source: str | None, merchant: str | None, *, transaction_kind: str) -> str:
    topic = infer_receipt_topic(source, merchant)
    language = locale_language(source or merchant)
    if transaction_kind == "create_income":
        return "Stipendio" if language == "it" else "Salary"
    if topic:
        return str(topic["description_it"] if language == "it" else topic["description_en"])
    if transaction_kind == "create_transfer":
        return "Bonifico" if language == "it" else "Transfer"
    return "Spesa" if language == "it" else "Expense"


def infer_receipt_category(service: BridgeService | None, source: str | None, merchant: str | None, *, transaction_kind: str) -> str | None:
    existing_names = list_known_category_names(service)
    if transaction_kind == "create_income":
        return choose_existing_name(("Stipendio", "Salary"), existing_names)
    topic = infer_receipt_topic(source, merchant)
    if topic:
        resolved = choose_existing_name(topic["categories"], existing_names)
        if resolved:
            return resolved
    fallback_candidates = ("Acquisti", "Spesa", "Groceries", "Cibo")
    if transaction_kind == "create_expense":
        return choose_existing_name(fallback_candidates, existing_names)
    return None


def extract_recent_query(text: str) -> str | None:
    lowered = normalize_natural_text(text)
    patterns = (
        r"\bmovimenti\s+(.+?)(?:\s+(?:dal|da|from)\s+\d{4}-\d{2}-\d{2}\b|$)",
        r"\btransactions?\s+(.+?)(?:\s+(?:from|between)\s+\d{4}-\d{2}-\d{2}\b|$)",
        r"\b(?:recent|recenti)\s+transactions?\s+(.+?)(?:\s+(?:from|between)\s+\d{4}-\d{2}-\d{2}\b|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if not match:
            continue
        query = re.sub(r"^(?:di|dei|delle|del|dello|della|the|for|su|sui|sulle|i|gli|le|il|la)\s+", "", match.group(1))
        query = clean_free_text_slot(query)
        if query:
            return query
    return None


def build_receipt_intro_reply(source_text: str | None = None) -> str:
    return localize(
        "Send the receipt photo or the bank screenshot and I will prepare a draft. If the account or the action is not clear enough, I will ask before saving anything.",
        "Mandami pure la foto della ricevuta o lo screenshot della banca e preparo una bozza. Se non e chiaro quale conto usare o che tipo di movimento registrare, te lo chiedo prima di salvare qualsiasi cosa.",
        source_text=source_text,
    )


def build_guided_transaction_prompt(text: str) -> str | None:
    lowered = text.casefold()
    if not any(keyword in lowered for keyword in {"transaction", "expense", "income", "transfer", "spent", "pay", "paid", "received"}):
        return None

    amount = parse_amount_from_text(text)
    if any(keyword in lowered for keyword in {"spent", "expense", "pay ", "paid", "bought"}):
        kind = "expense"
    elif any(keyword in lowered for keyword in {"income", "salary", "received", "deposit"}):
        kind = "income"
    elif "transfer" in lowered:
        kind = "transfer"
    else:
        kind = "transaction"

    if kind == "transaction":
        return localize(
            "I can add that, but I still need to know whether it is an expense, income, or transfer.\n"
            "Examples:\n"
            "- expense 10 for lunch\n"
            "- income 2500 salary\n"
            "- transfer 100 from Main Checking to Savings",
            "Posso aggiungerla, ma devo ancora capire se si tratta di una spesa, di un'entrata o di un trasferimento.\n"
            "Esempi:\n"
            "- spesa 10 per pranzo\n"
            "- entrata 2500 stipendio\n"
            "- trasferisci 100 da Conto Principale a Risparmi",
            source_text=text,
        )

    if not amount:
        return localize(
            f"I understood this as a possible {kind}, but I could not find the amount. Please include a number.",
            f"L'ho interpretata come una possibile {kind}, ma non ho trovato l'importo. Inserisci una cifra.",
            source_text=text,
        )

    return localize(
        f"I understood this as a possible {kind} of {amount}.\n"
        "To stay safe, send a bit more detail in one sentence, for example:\n"
        f"- {kind} {amount} for lunch at coop\n"
        f"- {kind} {amount} yesterday category groceries\n"
        "I will prepare a dry-run first.",
        f"L'ho interpretata come una possibile {kind} da {amount}.\n"
        "Per sicurezza, mandami un po' piu di dettaglio in una sola frase, per esempio:\n"
        f"- {kind} {amount} per pranzo da coop\n"
        f"- {kind} {amount} ieri categoria spesa\n"
        "Prima preparo sempre una simulazione.",
        source_text=text,
    )


def has_explicit_period_request(text: str) -> bool:
    lowered = normalize_natural_text(text)
    if re.search(rf"\b(?:\d{{4}}-\d{{2}}(?:-\d{{2}})?|{DATE_TOKEN_PATTERN})\b", lowered):
        return True
    if any(marker in lowered for marker in {" from ", " to ", " between ", " dal ", " al ", " ad ", " tra ", " fino al ", " until "}):
        return True
    month_markers = {
        "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december",
        "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
    }
    if any(marker in lowered for marker in month_markers):
        return True
    return bool(re.search(r"\b20\d{2}\b", lowered))


def enforce_deterministic_period(payload: dict[str, Any], text: str) -> dict[str, Any]:
    """Override AI period values with deterministic parsing when user period is explicit."""
    if not has_explicit_period_request(text):
        return payload
    deterministic = parse_natural_period_values(text)
    if not deterministic:
        return payload
    params = payload.get("params")
    if not isinstance(params, dict):
        params = {}
    stripped = {
        key: value
        for key, value in params.items()
        if key not in {"month", "from", "to", "start", "end", "start_date", "end_date"}
    }
    payload["params"] = {**stripped, **deterministic}
    payload.setdefault("source_text", text)
    return payload


def parse_compare_period_params(text: str) -> tuple[dict[str, str], dict[str, str]] | None:
    lowered = normalize_natural_text(text)
    if not re.search(r"\b(?:vs|versus)\b", lowered):
        return None
    parts = re.split(r"\b(?:vs|versus)\b", lowered, maxsplit=1)
    if len(parts) != 2:
        return None
    left_text = parts[0].strip()
    right_text = parts[1].strip()
    if not left_text or not right_text:
        return None
    left_period = parse_natural_period_values(left_text)
    right_period = parse_natural_period_values(right_text)
    if not left_period or not right_period:
        return None
    return left_period, right_period


def parse_natural_intent_payload(text: str) -> dict[str, Any] | None:
    payload = _parse_natural_intent_payload(text)
    if isinstance(payload, dict) and payload.get("intent") == "clarify" and not payload.get("reply"):
        payload["reply"] = build_receipt_intro_reply(text)
    return payload


def interpret_natural_command(text: str) -> str | None:
    lowered = normalize_natural_text(text)
    if not lowered:
        return None
    graph_words = {"graph", "chart", "grafico", "grafica"}
    balance_words = {"money i have", "money", "balances", "balance", "saldo", "saldi", "soldi", "net worth", "composition"}
    category_words = {"category", "categories", "categoria", "categorie"}
    spending_words = {"spending", "expenses", "expense", "groceries", "spesa", "spese"}
    cashflow_words = {"cashflow", "income vs", "incoming", "outgoing", "entrate", "uscite", "flusso di cassa"}
    show_words = {"show", "show me", "list", "list all", "mostra", "fammi vedere", "elenca"}
    top_category_words = {"top", "most", "used", "usate", "usati", "speso", "spent", "piu"}
    explicit_period = has_explicit_period_request(lowered)
    if (
        "how much money" in lowered
        or "how much moeny" in lowered
        or "quanti soldi" in lowered
        or "quanto ho" in lowered
        or lowered in {"balance", "balances", "saldo", "saldi"}
    ) and not any(keyword in lowered for keyword in {"speso", "spent", "expense", "spesa"}):
        return "/balances"
    if "what can you do" in lowered or "cosa sai fare" in lowered or lowered in {"help", "aiuto"}:
        return "/help"
    if lowered in {"command", "commands", "command list", "show commands", "comando", "comandi", "mostra comandi"}:
        return "/commands"
    if lowered.startswith("cerca ") or lowered.startswith("search "):
        return f"/{lowered}"
    if lowered.startswith("trova "):
        query = lowered[6:].strip()
        return f"/search {query}" if query else None
    if lowered.startswith("find "):
        query = lowered[5:].strip()
        return f"/search {query}" if query else None
    if "show accounts" in lowered or "show me the accounts" in lowered or "mostra conti" in lowered or lowered in {"accounts", "conti"}:
        return "/accounts"
    if lowered in {"categories", "categorie"}:
        return "/categories"
    if contains_any(lowered, category_words) and contains_any(lowered, show_words):
        return "/categories"
    if "show budgets" in lowered or "mostra budget" in lowered or lowered in {"budgets", "budget"}:
        return "/budgets"
    if ("summary" in lowered or "riepilogo" in lowered) and "graph" not in lowered and "graf" not in lowered:
        if explicit_period:
            return None
        return "/summary"
    if lowered.startswith("recent"):
        if explicit_period:
            return None
        return f"/{lowered}"
    if contains_any(lowered, category_words) and contains_any(lowered, top_category_words):
        if explicit_period:
            return None
        if contains_any(lowered, graph_words):
            return "/topcategories graph"
        return "/topcategories"
    if explicit_period and contains_any(lowered, category_words):
        return None
    budget_words = {"budget", "budget rimasto", "limite budget"}
    recurrence_words = {"ricorrenz", "recurrenc", "ricorrenti", "ricorrente", "recurring"}
    if contains_any(lowered, graph_words):
        if explicit_period:
            return None
        if contains_any(lowered, category_words):
            return "/topcategories graph"
        if contains_any(lowered, balance_words):
            return "/graph balances 30"
        if contains_any(lowered, spending_words):
            return "/graph spending 30"
        if contains_any(lowered, cashflow_words):
            return "/graph cashflow 30"
        if contains_any(lowered, budget_words):
            return "/graph budget"
        if any(w in lowered for w in recurrence_words):
            return "/graph recurrences"
        return "/graph balances 30"
    return None


def infer_account_from_payment_method(
    service: BridgeService,
    description: str,
    locale: str = "en",
    state: dict[str, Any] | None = None,
) -> str | None:
    """Try to infer account from payment method keywords in description.

    Returns account name or None if no good match found.
    """
    payment_method = extract_payment_method(description, locale=locale)
    if not payment_method:
        return None

    if state is not None:
        profile = get_finance_profile(state)
        payment_accounts = profile.get("payment_method_accounts")
        if isinstance(payment_accounts, dict):
            configured = str(payment_accounts.get(payment_method) or "").strip()
            if configured:
                return configured

    # Map payment methods to account type patterns
    type_patterns = {
        "card": ["credit card", "debit card", "card", "visa", "mastercard"],
        "cash": ["cash", "wallet", "asset", "checking"],
        "transfer": ["asset", "bank"],
        "app": ["app", "digital", "paypal", "revolut"],
    }

    patterns = type_patterns.get(payment_method, [])
    if not patterns:
        return None

    accounts = service.client.list_accounts("all")
    for account in accounts:
        attrs = account.get("attributes") or {}
        account_type = str(attrs.get("type") or "").strip().lower()
        account_name = str(attrs.get("name") or "").strip()
        account_name_folded = normalize_natural_text(account_name)
        if not account_name:
            continue
        for pattern in patterns:
            pattern_folded = normalize_natural_text(pattern)
            if pattern.lower() in account_type or pattern_folded in account_name_folded:
                return account_name

    return None


def configured_payment_account_for_text(
    state: dict[str, Any],
    text: str,
    *,
    locale: str = "en",
) -> str | None:
    payment_method = extract_payment_method(text, locale=locale)
    if not payment_method:
        return None
    profile = get_finance_profile(state)
    payment_accounts = profile.get("payment_method_accounts")
    if not isinstance(payment_accounts, dict):
        return None
    configured = str(payment_accounts.get(payment_method) or "").strip()
    return configured or None


def invalid_account_field_from_error(exc: BaseException) -> str | None:
    message = str(exc).casefold()
    if "could not find a valid source account" in message or "transactions.0.source_" in message:
        return "source"
    if "could not find a valid destination account" in message or "transactions.0.destination_" in message:
        return "destination"
    return None


def is_firefly_offline_error(exc: BaseException) -> bool:
    if not isinstance(exc, FireflyAPIError):
        return False
    message = str(exc).casefold()
    return (
        "returned http 500" in message
        or "returned http 502" in message
        or "returned http 503" in message
        or "returned http 504" in message
        or "request to firefly iii failed" in message
        or "php_network_getaddresses" in message
        or "getaddrinfo" in message
        or "connection refused" in message
        or "connection timed out" in message
    )


def trained_account_for_failed_field(
    payload: dict[str, Any],
    field: str,
    state: dict[str, Any],
    *,
    source_text: str | None = None,
) -> str | None:
    tx = _first_payload_transaction(payload)
    transaction_type = str(tx.get("type") or "").strip()
    profile = get_finance_profile(state)
    combined_text = " ".join(
        part
        for part in [
            source_text or "",
            str(tx.get("description") or ""),
            str(tx.get("notes") or ""),
        ]
        if part
    )
    payment_account = configured_payment_account_for_text(
        state,
        combined_text,
        locale=locale_language(source_text),
    )

    if transaction_type == "withdrawal":
        if field == "source":
            return payment_account or str(profile.get("expense_source_account") or "").strip() or None
        if field == "destination":
            return str(profile.get("expense_destination_account") or "").strip() or None
    if transaction_type == "deposit":
        if field == "source":
            return str(profile.get("income_source_account") or "").strip() or None
        if field == "destination":
            return payment_account or str(profile.get("income_destination_account") or "").strip() or None
    return None


def update_pending_transaction_account(
    state: dict[str, Any],
    *,
    field: str,
    account_name: str,
) -> dict[str, Any] | None:
    key = "source_name" if field == "source" else "destination_name"
    pending = state.get("pending_action")
    payload = pending.get("payload") if isinstance(pending, dict) else None
    if not isinstance(payload, dict):
        payload = state.get("pending_transaction")
    if not isinstance(payload, dict):
        return None

    tx = _first_payload_transaction(payload)
    if not tx:
        return None
    tx[key] = account_name

    session = load_draft_session(state)
    if session is not None and session.drafts:
        draft = session.drafts[0]
        if field == "source":
            draft.source_name = account_name
        else:
            draft.destination_name = account_name
        draft.payload = payload
        draft.sync_payload()
        session.phase = DraftPhase.REVIEW
        save_draft_session(state, session)
    else:
        state["pending_transaction"] = payload
        if isinstance(pending, dict):
            pending["payload"] = payload
            state["pending_action"] = pending
    return payload


def queue_pending_draft_account_fix(
    service: BridgeService,
    state: dict[str, Any],
    *,
    field: str,
    source_text: str | None = None,
) -> BotResponse:
    pending = state.get("pending_action")
    payload = pending.get("payload") if isinstance(pending, dict) else state.get("pending_transaction")
    payload = payload if isinstance(payload, dict) else {}
    trained_account = trained_account_for_failed_field(payload, field, state, source_text=source_text)
    if trained_account:
        updated = update_pending_transaction_account(state, field=field, account_name=trained_account)
        if updated:
            session = load_draft_session(state)
            manager = build_draft_manager(service)
            review = render_draft_session(manager, session) if session is not None else format_transaction_preview(
                updated,
                intro=localize("Draft updated.", "Bozza aggiornata.", source_text=source_text),
                outro=localize("Say 'confirm' to save it.", "Scrivi 'conferma' per salvarla.", source_text=source_text),
                source_text=source_text,
            )
            label = localize("source", "sorgente", source_text=source_text) if field == "source" else localize("destination", "destinazione", source_text=source_text)
            return BotResponse(
                localize(
                    f"I switched the {label} account to your trained account: {trained_account}. Confirm again when ready.",
                    f"Ho corretto il conto {label} usando quello scelto in /train: {trained_account}. Conferma di nuovo quando vuoi.",
                    source_text=source_text,
                )
                + "\n\n"
                + review
            )

    tx = _first_payload_transaction(payload)
    intent = {
        "withdrawal": "create_expense",
        "deposit": "create_income",
        "transfer": "create_transfer",
    }.get(str(tx.get("type") or "").strip(), "create_expense")
    options = intent_account_choices(service, intent, field, limit=15)
    state["pending_draft_account_fix"] = {
        "field": field,
        "options": options,
        "source_text": source_text or "",
    }
    return BotResponse(build_draft_account_fix_prompt(state))


def build_draft_account_fix_prompt(state: dict[str, Any]) -> str:
    pending = state.get("pending_draft_account_fix")
    if not isinstance(pending, dict):
        return "No account correction is pending."
    field = str(pending.get("field") or "source")
    source_text = str(pending.get("source_text") or "")
    options = list(pending.get("options") or [])
    if field == "source":
        lines = [
            localize(
                "That source account is not available in Firefly. Choose the account that paid.",
                "Quel conto sorgente non e disponibile in Firefly. Scegli il conto che ha pagato.",
                source_text=source_text,
            )
        ]
    else:
        lines = [
            localize(
                "That destination account is not available in Firefly. Choose the account to use.",
                "Quel conto destinazione non e disponibile in Firefly. Scegli il conto da usare.",
                source_text=source_text,
            )
        ]
    if options:
        lines.append(localize("Available accounts:", "Conti disponibili:", source_text=source_text))
        for index, name in enumerate(options, start=1):
            lines.append(f"{index}. {name}")
        lines.append(localize("Reply with account name or number.", "Rispondi con nome conto o numero.", source_text=source_text))
    return "\n".join(lines)


def handle_pending_draft_account_fix(service: BridgeService, state: dict[str, Any], text: str) -> BotResponse | None:
    pending = state.get("pending_draft_account_fix")
    if not isinstance(pending, dict):
        return None
    if text.strip().startswith("/"):
        return None
    if has_cancel_intent(text):
        state.pop("pending_draft_account_fix", None)
        return BotResponse(localize("Draft discarded.", "Bozza annullata.", source_text=text))

    field = str(pending.get("field") or "source")
    options = list(pending.get("options") or [])
    resolved = match_choice(text, options) if options else text.strip()
    if not resolved:
        return BotResponse(build_draft_account_fix_prompt(state))
    updated = update_pending_transaction_account(state, field=field, account_name=resolved)
    state.pop("pending_draft_account_fix", None)
    if not updated:
        return BotResponse(localize("The pending draft is no longer available. Please send it again.", "La bozza in attesa non e piu disponibile. Rimandamela.", source_text=text))
    session = load_draft_session(state)
    manager = build_draft_manager(service)
    review = render_draft_session(manager, session) if session is not None else format_transaction_preview(
        updated,
        intro=localize("Draft updated.", "Bozza aggiornata.", source_text=text),
        outro=localize("Say 'confirm' to save it.", "Scrivi 'conferma' per salvarla.", source_text=text),
        source_text=text,
    )
    return BotResponse(review)


def execute_intent(service: BridgeService, payload: dict[str, Any], state: dict[str, Any]) -> BotResponse:
    intent = str(payload.get("intent", "")).strip()
    params = payload.get("params", {})
    source_text = str(payload.get("source_text") or "").strip() or None
    if not isinstance(params, dict):
        params = {}
    payload["params"] = params
    payload = enforce_deterministic_transaction_date(payload, source_text)
    params = payload.get("params", {})
    if not isinstance(params, dict):
        params = {}

    if intent == "help":
        return BotResponse(help_text(source_text))

    if intent == "clarify":
        reply = str(payload.get("reply") or "").strip()
        if reply:
            return BotResponse(reply)
        return BotResponse(bot_text("need_more_detail", source_text=source_text))

    if intent == "get_balances":
        return BotResponse(format_balances(service.account_balances(), source_text=source_text))

    if intent == "get_spending_total":
        start, end, label = period_from_values(params, default_current_month=True)
        summary = service.client.summary_basic(start=start, end=end)
        return BotResponse(format_spending_total(label, summary, source_text=source_text))

    if intent == "get_income_vs_spending":
        start, end, label = period_from_values(params, default_current_month=True)
        summary = service.client.summary_basic(start=start, end=end)
        return BotResponse(format_income_vs_spending(label, summary, source_text=source_text))

    if intent == "list_accounts":
        return BotResponse(format_accounts(service.client.list_accounts("all"), source_text=source_text))

    if intent == "list_categories":
        return BotResponse(format_named_records(localize("Categories", "Categorie", source_text=source_text), service.client.list_categories(), source_text=source_text))

    if intent == "list_budgets":
        return BotResponse(format_named_records(localize("Budgets", "Budget", source_text=source_text), service.client.list_budgets(), source_text=source_text))

    if intent == "get_summary":
        start, end, label = period_from_values(params, default_current_month=True)
        return BotResponse(
            format_summary(
                {
                    "label": label,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "summary": service.client.summary_basic(start=start, end=end),
                },
                source_text=source_text,
            )
        )

    if intent == "get_recent":
        start, end, label = period_from_values(params, default_days=int(params.get("days") or 7))
        query = str(params.get("query") or "").strip() or None
        records = flatten_transactions(service.client.list_transactions(start=start, end=end, limit=100))
        if query:
            needle = normalize_natural_text(query)
            records = [
                record for record in records
                if needle in normalize_natural_text(" ".join(
                    [
                        str(record.get("description", "")),
                        str(record.get("source_name", "")),
                        str(record.get("destination_name", "")),
                        str(record.get("category_name", "")),
                        str(record.get("budget_name", "")),
                    ]
                ))
            ]
        return BotResponse(format_transactions(records[:15], title=localize(f"Recent transactions ({label}):", f"Transazioni recenti ({label}):", source_text=source_text), source_text=source_text))

    if intent == "search_transactions":
        query = str(params.get("query") or "").strip()
        end_dt = date.today()
        start_dt = end_dt - timedelta(days=90)
        records = flatten_transactions(service.client.list_transactions(start=start_dt, end=end_dt, limit=200))
        if query:
            needle = normalize_natural_text(query)
            records = [
                r for r in records
                if needle in normalize_natural_text(" ".join([
                    str(r.get("description", "")),
                    str(r.get("source_name", "")),
                    str(r.get("destination_name", "")),
                    str(r.get("category_name", "")),
                ]))
            ]
        if not records:
            return BotResponse(bot_text("search_no_results", source_text=source_text, query=query))
        return BotResponse(format_transactions(
            records[:20],
            title=bot_text("search_results_title", source_text=source_text, query=query),
            source_text=source_text,
        ))

    if intent == "compare_periods":
        left_values = params.get("left_period")
        right_values = params.get("right_period")
        if not isinstance(left_values, dict) or not isinstance(right_values, dict):
            return BotResponse(localize(
                "I need two explicit periods to compare.",
                "Mi servono due periodi espliciti da confrontare.",
                source_text=source_text,
            ))
        metric = str(params.get("metric") or "summary").strip().lower()
        left_start, left_end, left_label = period_from_values(left_values, default_current_month=True)
        right_start, right_end, right_label = period_from_values(right_values, default_current_month=True)

        lines = [localize("Comparison:", "Confronto:", source_text=source_text)]
        if metric == "income_vs_spending":
            left_summary = service.client.summary_basic(start=left_start, end=left_end)
            right_summary = service.client.summary_basic(start=right_start, end=right_end)
            lines.extend(
                [
                    f"- {left_label}: {localize('income', 'entrate', source_text=source_text)} {summary_metric_value(left_summary, 'income') or '?'} | "
                    f"{localize('spending', 'spese', source_text=source_text)} {summary_metric_value(left_summary, 'spend') or '?'}",
                    f"- {right_label}: {localize('income', 'entrate', source_text=source_text)} {summary_metric_value(right_summary, 'income') or '?'} | "
                    f"{localize('spending', 'spese', source_text=source_text)} {summary_metric_value(right_summary, 'spend') or '?'}",
                ]
            )
            return BotResponse("\n".join(lines))

        if metric == "spending_total":
            left_summary = service.client.summary_basic(start=left_start, end=left_end)
            right_summary = service.client.summary_basic(start=right_start, end=right_end)
            lines.extend(
                [
                    f"- {left_label}: {summary_metric_value(left_summary, 'spend') or '?'}",
                    f"- {right_label}: {summary_metric_value(right_summary, 'spend') or '?'}",
                ]
            )
            return BotResponse("\n".join(lines))

        if metric == "top_spending_categories":
            left_records = flatten_transactions(service.client.list_transactions(start=left_start, end=left_end, limit=300))
            right_records = flatten_transactions(service.client.list_transactions(start=right_start, end=right_end, limit=300))
            left_totals = aggregate_spending_by_category(left_records)
            right_totals = aggregate_spending_by_category(right_records)
            left_top = max(left_totals.items(), key=lambda item: item[1]) if left_totals else None
            right_top = max(right_totals.items(), key=lambda item: item[1]) if right_totals else None
            lines.extend(
                [
                    f"- {left_label}: {left_top[0]} ({left_top[1]:.2f})" if left_top else f"- {left_label}: {localize('no spending data', 'nessun dato di spesa', source_text=source_text)}",
                    f"- {right_label}: {right_top[0]} ({right_top[1]:.2f})" if right_top else f"- {right_label}: {localize('no spending data', 'nessun dato di spesa', source_text=source_text)}",
                ]
            )
            return BotResponse("\n".join(lines))

        left_summary = service.client.summary_basic(start=left_start, end=left_end)
        right_summary = service.client.summary_basic(start=right_start, end=right_end)
        lines.extend(
            [
                f"- {left_label}: {localize('income', 'entrate', source_text=source_text)} {summary_metric_value(left_summary, 'income') or '?'} | "
                f"{localize('spending', 'spese', source_text=source_text)} {summary_metric_value(left_summary, 'spend') or '?'}",
                f"- {right_label}: {localize('income', 'entrate', source_text=source_text)} {summary_metric_value(right_summary, 'income') or '?'} | "
                f"{localize('spending', 'spese', source_text=source_text)} {summary_metric_value(right_summary, 'spend') or '?'}",
            ]
        )
        return BotResponse("\n".join(lines))

    if intent == "top_spending_categories":
        start, end, label = period_from_values(params, default_current_month=True)
        records = flatten_transactions(service.client.list_transactions(start=start, end=end, limit=300))
        all_categories = as_bool(str(params.get("all_categories") or ""), default=False)
        text = format_top_spending_categories(records, label=label, source_text=source_text, limit=None if all_categories else 8)
        if as_bool(str(params.get("with_graph") or ""), default=False):
            photo_path, caption = create_spending_chart(
                records,
                days=max((end - start).days + 1, 1),
                label=label,
                source_text=source_text,
                limit=None if all_categories else 8,
            )
            return BotResponse(f"{text}\n\n{caption}", photo_path=photo_path)
        return BotResponse(text)

    if intent == "budget_report":
        start, end, label = period_from_values(params, default_current_month=True)
        records = flatten_transactions(service.client.list_transactions(start=start, end=end, limit=300))
        budget_limits: dict[str, float] = {}
        try:
            budget_limits = collect_budget_limits(service, start, end)
        except Exception:
            pass
        return BotResponse(format_budget_report(records, label=label, budget_limits=budget_limits, source_text=source_text))

    if intent == "set_budget_limit":
        budget_name = str(params.get("budget") or params.get("name") or "").strip()
        amount = str(params.get("budget_limit") or params.get("amount") or "").strip()
        month = str(params.get("month") or "").strip() or None
        if not budget_name or not amount:
            reply = str(payload.get("reply") or "").strip()
            return BotResponse(reply or "I need the budget name and the new amount to prepare that safely.")
        budget = resolve_budget(service.client, budget_name)
        if not budget:
            return BotResponse(f"I could not find a budget named '{budget_name}'. Create it first or tell me the exact budget name.")
        budget_id = str(budget.get("id") or "").strip()
        start, end = month_window_from_label(month)
        existing_limit_id = None
        try:
            existing_limits = service.client.list_budget_limits(budget_id, start=start.isoformat(), end=end.isoformat())
        except FireflyAPIError:
            existing_limits = []
        for item in existing_limits:
            existing_limit_id = str(item.get("id") or "").strip() or existing_limit_id
            if existing_limit_id:
                break
        preview = format_pending_action_preview(
            "Budget limit draft prepared.",
            [
                f"Budget: {budget_name}",
                f"Amount: {amount}",
                f"Period: {format_display_period(start, end)}",
                f"Action: {'update existing limit' if existing_limit_id else 'create new limit'}",
            ],
            "Say 'commit it' if you want me to apply this budget limit.",
        )
        remember_pending_action(
            state,
            kind="budget_limit_set",
            payload={
                "budget_id": budget_id,
                "budget_name": budget_name,
                "amount": amount,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "budget_limit_id": existing_limit_id,
                "notes": str(params.get("notes") or "").strip() or None,
            },
            preview=preview,
        )
        return BotResponse(preview)

    if intent == "list_recurrences":
        return BotResponse(format_recurrences(service.client.list_recurrences()))

    if intent == "create_recurrence":
        recurrence_payload = recurrence_payload_from_params(service, params)
        title = recurrence_payload.get("title") or "Recurring transaction"
        preview = format_pending_action_preview(
            "Recurring transaction draft prepared.",
            [
                f"Title: {title}",
                f"Starts: {recurrence_payload.get('first_date')}",
                f"Repeats: {json.dumps(recurrence_payload.get('repetitions', []), ensure_ascii=False)}",
            ],
            "Say 'commit it' if you want me to create this recurring transaction.",
        )
        remember_pending_action(state, kind="recurrence_create", payload=recurrence_payload, preview=preview)
        return BotResponse(preview)

    if intent == "delete_recurrence":
        recurrence_id = resolve_recurrence_id(service.client, params)
        if not recurrence_id:
            reply = str(payload.get("reply") or "").strip()
            return BotResponse(reply or "I need the recurring transaction name or id to delete it safely.")
        preview = format_pending_action_preview(
            "Recurring transaction delete draft prepared.",
            [f"Recurrence id: {recurrence_id}"],
            "Say 'commit it' if you want me to delete it.",
        )
        remember_pending_action(
            state,
            kind="recurrence_delete",
            payload={"recurrence_id": recurrence_id},
            preview=preview,
        )
        return BotResponse(preview)

    if intent == "create_category":
        name = str(params.get("name") or params.get("category") or "").strip()
        if not name:
            reply = str(payload.get("reply") or "").strip()
            return BotResponse(reply or "I need the new category name.")
        preview = format_pending_action_preview(
            "Category draft prepared.",
            [f"Category name: {name}"],
            "Say 'commit it' if you want me to create it.",
        )
        remember_pending_action(state, kind="category_create", payload={"name": name}, preview=preview)
        return BotResponse(preview)

    if intent == "create_budget":
        name = str(params.get("name") or params.get("budget") or "").strip()
        if not name:
            reply = str(payload.get("reply") or "").strip()
            return BotResponse(reply or "I need the new budget name.")
        preview = format_pending_action_preview(
            "Budget draft prepared.",
            [f"Budget name: {name}"],
            "Say 'commit it' if you want me to create it.",
        )
        remember_pending_action(state, kind="budget_create", payload={"name": name}, preview=preview)
        return BotResponse(preview)

    if intent == "create_account":
        name = str(params.get("name") or "").strip()
        account_type = str(params.get("account_type") or "").strip().lower()
        if not name or not account_type:
            reply = str(payload.get("reply") or "").strip()
            return BotResponse(reply or "I need at least the new account name and account type.")
        preview = format_pending_action_preview(
            "Account draft prepared.",
            [
                f"Account name: {name}",
                f"Account type: {account_type}",
                f"Opening balance: {str(params.get('amount') or params.get('opening_balance') or 'not set')}",
            ],
            "Say 'commit it' if you want me to create it.",
        )
        remember_pending_action(
            state,
            kind="account_create",
            payload={
                "name": name,
                "account_type": account_type,
                "opening_balance": str(params.get("amount") or params.get("opening_balance") or "").strip() or None,
                "opening_balance_date": str(params.get("date") or "").strip() or None,
            },
            preview=preview,
        )
        return BotResponse(preview)

    if intent in {"graph_balances", "graph_spending", "graph_cashflow"}:
        if intent == "graph_balances":
            photo_path, caption = create_balance_chart(service.account_balances(), source_text=source_text)
            return BotResponse(caption, photo_path=photo_path)
        start, end, label = period_from_values(params, default_days=int(params.get("days") or 30))
        records = flatten_transactions(service.client.list_transactions(start=start, end=end, limit=100))
        if intent == "graph_spending":
            photo_path, caption = create_spending_chart(records, days=max((end - start).days + 1, 1), label=label, source_text=source_text)
            return BotResponse(caption, photo_path=photo_path)
        photo_path, caption = create_cashflow_chart(records, days=max((end - start).days + 1, 1), label=label, source_text=source_text)
        return BotResponse(caption, photo_path=photo_path)

    if intent == "graph_budget":
        start, end, label = period_from_values(params, default_current_month=True)
        records = flatten_transactions(service.client.list_transactions(start=start, end=end, limit=300))
        budget_spent = aggregate_spending_by_budget(records)
        try:
            budget_limits_map = collect_budget_limits(service, start, end)
        except Exception:
            budget_limits_map = {}
        photo_path, caption = create_budget_chart(budget_limits_map, budget_spent, label=label, source_text=source_text)
        return BotResponse(caption, photo_path=photo_path)

    if intent == "graph_recurrences":
        items = service.client.list_recurrences()
        photo_path, caption = create_recurrence_chart(items, source_text=source_text)
        return BotResponse(caption, photo_path=photo_path)

    if intent in {"create_expense", "create_income", "create_transfer"}:
        transaction_kind = {
            "create_expense": "withdrawal",
            "create_income": "deposit",
            "create_transfer": "transfer",
        }[intent]
        params = apply_profile_and_history_autofill(
            service,
            state,
            transaction_kind=transaction_kind,
            params=params,
        )
        amount = str(params.get("amount") or "").strip()
        description = str(params.get("description") or "").strip()
        if not amount or not description:
            if amount and not description:
                state["pending_description_input"] = {
                    "payload_params": params,
                    "source_text": source_text,
                    "intent": intent,
                }
                return BotResponse(localize(
                    "Got it. What's the description for this transaction?",
                    "Capito. Come descrivo questa transazione?",
                    source_text=source_text,
                ))
            reply = str(payload.get("reply") or "").strip()
            return BotResponse(reply or "I understood this as a write request, but I still need at least amount and description.")

        defaults = service.settings.mappings.get("defaults", {})
        rules = service.resolve_merchant_rule(str(params.get("merchant") or "").strip() or None)
        inferred_account = infer_account_from_payment_method(
            service,
            " ".join(part for part in [source_text or "", description] if part),
            locale=locale_language(source_text),
            state=state,
        )
        trained_payment_account = configured_payment_account_for_text(
            state,
            " ".join(part for part in [source_text or "", description] if part),
            locale=locale_language(source_text),
        )
        if transaction_kind == "withdrawal":
            if trained_payment_account:
                params["source"] = trained_payment_account
            elif inferred_account and not str(params.get("source") or "").strip():
                params["source"] = inferred_account
        if transaction_kind == "deposit":
            if trained_payment_account:
                params["destination"] = trained_payment_account
            elif inferred_account and not str(params.get("destination") or "").strip():
                params["destination"] = inferred_account

        missing_fields: list[str] = []
        if transaction_kind == "withdrawal":
            effective_source = usable_account_name(params.get("source") or defaults.get("expense_source_account"))
            effective_destination = usable_account_name(params.get("destination") or rules.get("destination_account") or defaults.get("expense_destination_account"))
            if effective_source:
                params["source"] = effective_source
            else:
                params.pop("source", None)
            if effective_destination:
                params["destination"] = effective_destination
            else:
                params.pop("destination", None)
            if not effective_source:
                missing_fields.append("source")
            if not effective_destination:
                missing_fields.append("destination")
        elif transaction_kind == "deposit":
            effective_source = usable_account_name(params.get("source") or rules.get("source_account") or defaults.get("income_source_account"))
            effective_destination = usable_account_name(params.get("destination") or defaults.get("income_destination_account"))
            if effective_source:
                params["source"] = effective_source
            else:
                params.pop("source", None)
            if effective_destination:
                params["destination"] = effective_destination
            else:
                params.pop("destination", None)
            if not effective_source:
                missing_fields.append("source")
            if not effective_destination:
                missing_fields.append("destination")
        else:
            if not usable_account_name(params.get("source")):
                missing_fields.append("source")
            if not usable_account_name(params.get("destination")):
                missing_fields.append("destination")
        if missing_fields:
            queued_payload = {
                "intent": intent,
                "source_text": source_text,
                "params": params,
            }
            return queue_transaction_field_resolution(
                service,
                state,
                payload=queued_payload,
                fields=missing_fields,
                source_text=source_text,
            )

        description = clean_transaction_description(
            description,
            source_text=source_text,
            merchant=str(params.get("merchant") or "").strip() or None,
        )
        params["description"] = description

        # Try fuzzy-match account names for transfers
        if transaction_kind == "transfer":
            try:
                account_names = [
                    str(a.get("attributes", {}).get("name", "")).strip()
                    for a in service.client.list_accounts("all") if a.get("attributes", {}).get("name")
                ]
                if str(params.get("source") or "").strip():
                    matched = fuzzy_match_category(params["source"], account_names, threshold=0.6)
                    if matched:
                        params["source"] = matched
                if str(params.get("destination") or "").strip():
                    matched = fuzzy_match_category(params["destination"], account_names, threshold=0.6)
                    if matched:
                        params["destination"] = matched
            except Exception:
                pass

        # Try fuzzy-match category if not explicitly set
        if not str(params.get("category") or "").strip():
            try:
                available_categories = [
                    str(cat.get("attributes", {}).get("name") or "").strip()
                    for cat in service.client.list_categories()
                ]
                auto_category = fuzzy_match_category(description, available_categories)
                if auto_category:
                    params["category"] = auto_category
            except Exception:
                pass

        payload_tx = service.build_transaction(
            transaction_kind=transaction_kind,
            amount=amount,
            description=description,
            transaction_date=coerce_transaction_date(params.get("date")),
            source_name=str(params.get("source") or "").strip() or None,
            destination_name=str(params.get("destination") or "").strip() or None,
            category_name=str(params.get("category") or "").strip() or None,
            budget_name=str(params.get("budget") or "").strip() or None,
            notes=str(params.get("notes") or "").strip() or None,
            tags=[],
            merchant=str(params.get("merchant") or "").strip() or None,
            currency_code=None,
        )
        result = service.commit_transaction(
            payload_tx,
            dry_run=True,
            confirm_high_value=False,
        )
        if result["status"] == "duplicate_blocked":
            return BotResponse(format_duplicate_blocked(result["duplicate"], source_text=source_text))
        manager = build_draft_manager(service)
        session = manager.create_session(
            [payload_tx],
            original_text=source_text or "",
            language=locale_language(source_text),
        )
        save_draft_session(state, session)

        response_text = render_draft_session(manager, session)

        # Detect recurrence pattern and suggest creating one
        recurrence_type = extract_recurrence(source_text or "")
        if recurrence_type and transaction_kind in {"withdrawal", "deposit"}:
            state["pending_recurrence_suggestion"] = {
                "cadence": recurrence_type,
                "amount": amount,
                "description": description,
                "transaction_kind": transaction_kind,
                "source": str(params.get("source") or "").strip() or None,
                "destination": str(params.get("destination") or "").strip() or None,
                "category": str(params.get("category") or "").strip() or None,
                "budget": str(params.get("budget") or "").strip() or None,
                "date": coerce_transaction_date(params.get("date")),
            }
        if _should_offer_recurrence_before_review(state, session):
            return BotResponse(
                _begin_recurrence_suggestion_prompt(
                    state,
                    state["pending_recurrence_suggestion"],
                    source_text=source_text,
                )
            )
        return BotResponse(response_text)

    if intent == "create_transaction_batch":
        raw_transactions = list(params.get("transactions") or [])
        payloads: list[dict[str, Any]] = []
        for item in raw_transactions:
            if not isinstance(item, dict):
                continue
            item_intent = str(item.get("intent") or "create_expense").strip()
            item_params = item.get("params", {})
            if not isinstance(item_params, dict):
                continue
            transaction_kind = {
                "create_expense": "withdrawal",
                "create_income": "deposit",
                "create_transfer": "transfer",
            }.get(item_intent)
            if transaction_kind is None:
                continue
            item_params = apply_profile_and_history_autofill(
                service,
                state,
                transaction_kind=transaction_kind,
                params=item_params,
            )
            amount = str(item_params.get("amount") or "").strip()
            description = str(item_params.get("description") or "").strip()
            if not amount or not description:
                continue
            defaults = service.settings.mappings.get("defaults", {})
            rules = service.resolve_merchant_rule(str(item_params.get("merchant") or "").strip() or None)
            inferred_account = infer_account_from_payment_method(
                service,
                " ".join(part for part in [source_text or "", description] if part),
                locale=locale_language(source_text),
                state=state,
            )
            if transaction_kind == "withdrawal" and inferred_account and not str(item_params.get("source") or "").strip():
                item_params["source"] = inferred_account
            if transaction_kind == "deposit" and inferred_account and not str(item_params.get("destination") or "").strip():
                item_params["destination"] = inferred_account

            if transaction_kind == "withdrawal":
                effective_source = usable_account_name(item_params.get("source") or defaults.get("expense_source_account"))
                effective_destination = usable_account_name(item_params.get("destination") or rules.get("destination_account") or defaults.get("expense_destination_account"))
                if effective_source:
                    item_params["source"] = effective_source
                else:
                    item_params.pop("source", None)
                if effective_destination:
                    item_params["destination"] = effective_destination
                else:
                    item_params.pop("destination", None)
            elif transaction_kind == "deposit":
                effective_source = usable_account_name(item_params.get("source") or rules.get("source_account") or defaults.get("income_source_account"))
                effective_destination = usable_account_name(item_params.get("destination") or defaults.get("income_destination_account"))
                if effective_source:
                    item_params["source"] = effective_source
                else:
                    item_params.pop("source", None)
                if effective_destination:
                    item_params["destination"] = effective_destination
                else:
                    item_params.pop("destination", None)
            else:
                effective_source = usable_account_name(item_params.get("source"))
                effective_destination = usable_account_name(item_params.get("destination"))

            if not effective_source or not effective_destination:
                return BotResponse(
                    localize(
                        "I need source and destination accounts for at least one transaction in the batch. Send them explicitly if defaults/history cannot resolve them.",
                        "Mi servono conto sorgente e destinazione per almeno una transazione nel batch. Invia i conti esplicitamente se default/storico non riescono a risolverli.",
                        source_text=source_text,
                    )
                )
            # Try fuzzy-match category if not explicitly set
            description = clean_transaction_description(
                description,
                source_text=source_text,
                merchant=str(item_params.get("merchant") or "").strip() or None,
            )
            item_params["description"] = description
            if not str(item_params.get("category") or "").strip():
                try:
                    available_categories = [
                        str(cat.get("attributes", {}).get("name") or "").strip()
                        for cat in service.client.list_categories()
                    ]
                    auto_category = fuzzy_match_category(description, available_categories)
                    if auto_category:
                        item_params["category"] = auto_category
                except Exception:
                    pass
            payloads.append(
                service.build_transaction(
                    transaction_kind=transaction_kind,
                    amount=amount,
                    description=description,
                    transaction_date=coerce_transaction_date(item_params.get("date")),
                    source_name=str(item_params.get("source") or "").strip() or None,
                    destination_name=str(item_params.get("destination") or "").strip() or None,
                    category_name=str(item_params.get("category") or "").strip() or None,
                    budget_name=str(item_params.get("budget") or "").strip() or None,
                    notes=str(item_params.get("notes") or "").strip() or None,
                    tags=[],
                    merchant=str(item_params.get("merchant") or "").strip() or None,
                    currency_code=None,
                )
            )
        if not payloads:
            return BotResponse(localize("I could not prepare safe drafts from that screenshot yet.", "Non sono ancora riuscito a preparare bozze sicure da quello screenshot.", source_text=source_text))
        for payload_tx in payloads:
            duplicate = service.find_duplicate(payload_tx)
            if duplicate:
                return BotResponse(format_duplicate_blocked(duplicate, source_text=source_text))
        manager = build_draft_manager(service)
        session = manager.create_session(
            payloads,
            original_text=source_text or "",
            language=locale_language(source_text),
        )
        save_draft_session(state, session)
        return BotResponse(render_draft_session(manager, session))

    return BotResponse("I could not map that request to a supported Firefly action yet.")


# ---------------------------------------------------------------------------
# Recent transaction picker / clone / split
# ---------------------------------------------------------------------------

_RECENT_TXN_PICK_LIMIT = 10
_PICKER_EMOJI_NUMBERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
_LAST_TXN_HINT_PATTERN = re.compile(r"l['\u2019]?\s*ultima|ultima\s+transazione|last(?:est)?\s+transaction", re.IGNORECASE)
_CLONE_REQUEST_PATTERN = re.compile(
    r"\b(?:clone|clona|clonare|duplica|duplicare|copia|duplicate)\b",
    re.IGNORECASE,
)


def fetch_recent_transactions_for_picker(
    service: BridgeService,
    *,
    days: int = 90,
    limit: int = _RECENT_TXN_PICK_LIMIT,
) -> list[dict[str, Any]]:
    try:
        records = flatten_transactions(
            service.client.list_transactions(
                start=date.today() - timedelta(days=days),
                end=date.today(),
                limit=100,
            )
        )
    except Exception:
        return []
    records.sort(
        key=lambda item: (
            str(item.get("date") or ""),
            str(_transaction_group_id(item)),
        ),
        reverse=True,
    )
    return records[:limit]


def format_recent_transaction_picker_lines(
    records: list[dict[str, Any]],
    *,
    source_text: str | None = None,
    intro: str | None = None,
) -> list[str]:
    lines: list[str] = []
    if intro:
        lines.append(intro)
    else:
        lines.append(
            localize(
                "Choose a transaction by number:",
                "Scegli una transazione con il numero:",
                source_text=source_text,
            )
        )
    for index, record in enumerate(records, 1):
        emoji = _PICKER_EMOJI_NUMBERS[index - 1] if index - 1 < len(_PICKER_EMOJI_NUMBERS) else f"{index}."
        description = str(record.get("description") or "—").strip()[:40]
        amount = format_money(record.get("amount"))
        date_label = format_display_date(str(record.get("date") or "")[:10])
        category = str(record.get("category_name") or "").strip()
        route = " → ".join(
            part
            for part in [
                str(record.get("source_name") or "").strip(),
                str(record.get("destination_name") or "").strip(),
            ]
            if part
        )
        details = " | ".join(
            part
            for part in [
                date_label,
                description,
                amount,
                category,
                route,
            ]
            if part
        )
        lines.append(f"{emoji} {details}")
    lines.append(
        localize(
            "Reply with the number, or /cancel to stop.",
            "Rispondi con il numero, oppure /cancel per annullare.",
            source_text=source_text,
        )
    )
    return lines


def start_clone_transaction_flow(
    service: BridgeService,
    state: dict[str, Any],
    *,
    source_text: str | None = None,
) -> BotResponse:
    records = fetch_recent_transactions_for_picker(service)
    if not records:
        return BotResponse(
            localize(
                "No recent transactions found to clone.",
                "Nessuna transazione recente da clonare.",
                source_text=source_text,
            )
        )
    state["pending_clone_selection"] = {"records": records}
    return BotResponse("\n".join(format_recent_transaction_picker_lines(records, source_text=source_text)))


def duplicate_transaction_payload(
    service: BridgeService,
    record: dict[str, Any],
    *,
    target_date: date,
) -> dict[str, Any]:
    transaction_kind = str(record.get("type") or "withdrawal").strip()
    return service.build_transaction(
        transaction_kind=transaction_kind,
        amount=str(record.get("amount") or "0"),
        description=str(record.get("description") or ""),
        transaction_date=target_date.isoformat(),
        source_name=record.get("source_name"),
        destination_name=record.get("destination_name"),
        category_name=record.get("category_name"),
        budget_name=record.get("budget_name"),
        notes=record.get("notes"),
        tags=None,
        merchant=None,
        currency_code=None,
    )


def handle_pending_clone_selection(
    service: BridgeService,
    state: dict[str, Any],
    text: str,
) -> BotResponse | None:
    pending_clone = state.get("pending_clone_selection")
    if not isinstance(pending_clone, dict) or text.strip().startswith("/"):
        return None

    if has_cancel_intent(text):
        state.pop("pending_clone_selection", None)
        return BotResponse(localize("Clone cancelled.", "Clonazione annullata.", source_text=text))

    records = list(pending_clone.get("records") or [])
    if not records:
        state.pop("pending_clone_selection", None)
        return BotResponse(
            localize(
                "The clone list expired. Try again.",
                "La lista per la clonazione e scaduta. Riprova.",
                source_text=text,
            )
        )

    choice = match_choice(text, [str(index) for index in range(1, len(records) + 1)])
    if choice is None:
        return BotResponse("\n".join(format_recent_transaction_picker_lines(records, source_text=text)))

    record = records[int(choice) - 1]
    state.pop("pending_clone_selection", None)
    payload = duplicate_transaction_payload(service, record, target_date=date.today())
    try:
        result = service.commit_transaction(payload, dry_run=False, confirm_high_value=True)
    except (ConfigurationError, FireflyAPIError, ValueError, RuntimeError) as exc:
        if is_firefly_offline_error(exc):
            raise
        return BotResponse(
            localize(
                f"Could not clone the transaction: {exc}",
                f"Impossibile clonare la transazione: {exc}",
                source_text=text,
            )
        )

    if result["status"] == "duplicate_blocked":
        return BotResponse(format_duplicate_blocked(result["duplicate"], source_text=text))

    created = result.get("result", {})
    _remember_last_committed_txn(state, created, payload)
    return BotResponse(format_created_transaction_result(created, fallback_payload=payload, source_text=text))


def handle_clone_transaction_intent(
    service: BridgeService,
    text: str,
    state: dict[str, Any],
) -> BotResponse | None:
    if not _CLONE_REQUEST_PATTERN.search(text):
        return None
    return start_clone_transaction_flow(service, state, source_text=text)


# ---------------------------------------------------------------------------
# Split transaction intent
# ---------------------------------------------------------------------------

_SPLIT_PATTERN = re.compile(
    r"\b(?:divid[ia](?:la|lo|le)?|split(?:\s+it)?|divide(?:\s+it)?)\b"
    r"(?:\s+[\w']+){0,8}?\s+(?:per|by|in)\s+(\d+(?:[.,]\d+)?)\b",
    re.IGNORECASE,
)
_SPLIT_HINT_STOPWORDS = {
    "per",
    "by",
    "in",
    "la",
    "il",
    "lo",
    "le",
    "i",
    "gli",
    "una",
    "uno",
    "un",
    "the",
    "that",
    "this",
    "quella",
    "quello",
    "questa",
    "questo",
    "transazione",
    "transazioni",
    "transaction",
    "transactions",
    "spesa",
    "spese",
    "expense",
    "expenses",
    "movimento",
    "movimenti",
    "dividi",
    "dividila",
    "dividila",
    "split",
    "divide",
    "ultima",
    "ultimo",
    "last",
    "latest",
}


def _split_transaction_hint(text: str) -> str | None:
    lowered = normalize_natural_text(text)
    lowered = _SPLIT_PATTERN.sub(" ", lowered)
    lowered = _LAST_TXN_HINT_PATTERN.sub(" ", lowered)
    lowered = re.sub(r"\b(?:per|by|in)\s+\d+(?:[.,]\d+)?\b", " ", lowered)
    tokens = [token for token in lowered.split() if token and token not in _SPLIT_HINT_STOPWORDS and len(token) >= 3]
    if not tokens:
        return None
    return " ".join(tokens)


def _record_from_last_committed(last: dict[str, Any]) -> dict[str, Any]:
    return {
        "journal_id": str(last.get("id") or "").strip(),
        "description": str(last.get("description") or "—"),
        "amount": str(last.get("amount") or ""),
        "type": str(last.get("type") or "").strip() or None,
    }


def _records_matching_split_hint(records: list[dict[str, Any]], hint: str) -> list[dict[str, Any]]:
    target = normalize_match_text(hint)
    if not target:
        return []
    target_tokens = [token for token in target.split() if len(token) >= 3]
    matches: list[dict[str, Any]] = []
    for record in records:
        haystack = normalize_match_text(
            " ".join(
                part
                for part in [
                    str(record.get("description") or ""),
                    str(record.get("category_name") or ""),
                    str(record.get("source_name") or ""),
                    str(record.get("destination_name") or ""),
                ]
                if part
            )
        )
        if not haystack:
            continue
        if target in haystack or any(token in haystack for token in target_tokens):
            matches.append(record)
    return matches


def _queue_split_action_for_record(
    state: dict[str, Any],
    record: dict[str, Any],
    *,
    divisor: Decimal,
    source_text: str | None = None,
) -> BotResponse:
    txn_id = _transaction_group_id(record)
    if not txn_id:
        return BotResponse(
            localize(
                "I couldn't retrieve the transaction ID.",
                "Non sono riuscito a ottenere l'ID della transazione.",
                source_text=source_text,
            )
        )
    description = str(record.get("description") or "—")
    tx_type = str(record.get("type") or "").strip()
    try:
        old_amount = Decimal(str(record.get("amount") or "0"))
    except Exception:
        old_amount = None
    if old_amount is None or old_amount <= 0 or not tx_type:
        return BotResponse(
            localize(
                "I couldn't read the transaction amount.",
                "Non sono riuscito a leggere l'importo della transazione.",
                source_text=source_text,
            )
        )

    new_amount = (old_amount / divisor).quantize(Decimal("0.01"))
    preview = format_pending_action_preview(
        localize(
            f"Transaction: {description}",
            f"Transazione: {description}",
            source_text=source_text,
        ),
        [
            localize(
                f"Current amount: {old_amount:.2f}€ → new amount: {new_amount:.2f}€",
                f"Importo attuale: {old_amount:.2f}€ → nuovo importo: {new_amount:.2f}€",
                source_text=source_text,
            )
        ],
        localize("Confirm? (yes / no)", "Confermo? (si / no)", source_text=source_text),
    )
    remember_pending_action(
        state,
        kind="transaction_amount_split",
        payload={
            "txn_id": txn_id,
            "description": description,
            "old_amount": str(old_amount),
            "new_amount": str(new_amount),
            "tx_type": tx_type,
        },
        preview=preview,
    )
    return BotResponse(preview)


def _start_split_selection_flow(
    state: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    divisor: Decimal,
    source_text: str | None = None,
    intro: str | None = None,
) -> BotResponse:
    state["pending_split_selection"] = {
        "records": records,
        "divisor": str(divisor),
    }
    return BotResponse(
        "\n".join(
            format_recent_transaction_picker_lines(
                records,
                source_text=source_text,
                intro=intro,
            )
        )
    )


def _start_split_latest_confirm_flow(
    state: dict[str, Any],
    latest: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    divisor: Decimal,
    source_text: str | None = None,
) -> BotResponse:
    description = str(latest.get("description") or "—")
    amount = format_money(latest.get("amount"))
    state["pending_split_latest_confirm"] = {
        "record": latest,
        "records": records,
        "divisor": str(divisor),
    }
    return BotResponse(
        localize(
            f"Latest Firefly transaction: {description} | {amount}.\nSplit that one? (yes / no)",
            f"Ultima transazione in Firefly: {description} | {amount}.\nLa divido? (si / no)",
            source_text=source_text,
        )
    )


def handle_pending_split_latest_confirm(
    service: BridgeService,
    state: dict[str, Any],
    text: str,
) -> BotResponse | None:
    pending = state.get("pending_split_latest_confirm")
    if not isinstance(pending, dict) or text.strip().startswith("/"):
        return None

    if has_cancel_intent(text):
        state.pop("pending_split_latest_confirm", None)
        return BotResponse(localize("Split cancelled.", "Divisione annullata.", source_text=text))

    try:
        divisor = Decimal(str(pending.get("divisor") or "0").replace(",", "."))
    except Exception:
        divisor = Decimal("0")
    if divisor <= 0:
        state.pop("pending_split_latest_confirm", None)
        return BotResponse(localize("Split action incomplete.", "Azione di divisione incompleta.", source_text=text))

    if has_commit_intent(text):
        record = pending.get("record")
        state.pop("pending_split_latest_confirm", None)
        if not isinstance(record, dict):
            return BotResponse(localize("Split action incomplete.", "Azione di divisione incompleta.", source_text=text))
        return _queue_split_action_for_record(state, record, divisor=divisor, source_text=text)

    lowered = normalize_natural_text(text)
    if lowered in {"no", "n", "nope", "non"}:
        records = list(pending.get("records") or [])
        state.pop("pending_split_latest_confirm", None)
        if not records:
            records = fetch_recent_transactions_for_picker(service)
        if not records:
            return BotResponse(
                localize(
                    "I couldn't find a recent transaction to split.",
                    "Non ho trovato transazioni recenti da dividere.",
                    source_text=text,
                )
            )
        return _start_split_selection_flow(
            state,
            records,
            divisor=divisor,
            source_text=text,
            intro=localize(
                "Choose which transaction to split:",
                "Scegli quale transazione dividere:",
                source_text=text,
            ),
        )

    return BotResponse(
        localize(
            "Reply yes to split the latest transaction, or no to choose from the list.",
            "Rispondi si per dividere l'ultima transazione, oppure no per scegliere dalla lista.",
            source_text=text,
        )
    )


def handle_pending_split_selection(
    service: BridgeService,
    state: dict[str, Any],
    text: str,
) -> BotResponse | None:
    pending = state.get("pending_split_selection")
    if not isinstance(pending, dict) or text.strip().startswith("/"):
        return None

    if has_cancel_intent(text):
        state.pop("pending_split_selection", None)
        return BotResponse(localize("Split cancelled.", "Divisione annullata.", source_text=text))

    records = list(pending.get("records") or [])
    if not records:
        state.pop("pending_split_selection", None)
        return BotResponse(
            localize(
                "The split list expired. Try again.",
                "La lista per la divisione e scaduta. Riprova.",
                source_text=text,
            )
        )

    try:
        divisor = Decimal(str(pending.get("divisor") or "0").replace(",", "."))
    except Exception:
        divisor = Decimal("0")
    if divisor <= 0:
        state.pop("pending_split_selection", None)
        return BotResponse(localize("Split action incomplete.", "Azione di divisione incompleta.", source_text=text))

    choice = match_choice(text, [str(index) for index in range(1, len(records) + 1)])
    if choice is None:
        return BotResponse(
            "\n".join(
                format_recent_transaction_picker_lines(
                    records,
                    source_text=text,
                    intro=localize(
                        "Choose which transaction to split:",
                        "Scegli quale transazione dividere:",
                        source_text=text,
                    ),
                )
            )
        )

    record = records[int(choice) - 1]
    state.pop("pending_split_selection", None)
    return _queue_split_action_for_record(state, record, divisor=divisor, source_text=text)


def handle_split_transaction_intent(
    service: BridgeService,
    text: str,
    state: dict[str, Any],
) -> BotResponse | None:
    """Detect 'dividi per N' / 'split by N' and queue a pending_action to halve the amount."""
    m = _SPLIT_PATTERN.search(text.casefold())
    if not m:
        return None

    try:
        divisor = Decimal(m.group(1).replace(",", "."))
        if divisor <= 0:
            return None
    except Exception:
        return None

    records = fetch_recent_transactions_for_picker(service)
    if not records:
        return BotResponse(
            localize(
                "I couldn't find a recent transaction to split.",
                "Non ho trovato transazioni recenti da dividere.",
                source_text=text,
            )
        )

    wants_firefly_latest = bool(_LAST_TXN_HINT_PATTERN.search(text))
    if wants_firefly_latest:
        return _queue_split_action_for_record(state, records[0], divisor=divisor, source_text=text)

    hint = _split_transaction_hint(text)
    if hint:
        matches = _records_matching_split_hint(records, hint)
        latest_id = _transaction_group_id(records[0])
        if len(matches) == 1 and _transaction_group_id(matches[0]) == latest_id:
            return _queue_split_action_for_record(state, matches[0], divisor=divisor, source_text=text)
        if matches:
            picker_records = matches if len(matches) > 1 else records
            intro = localize(
                "Choose which transaction to split:",
                "Scegli quale transazione dividere:",
                source_text=text,
            )
            if len(matches) > 1:
                intro = localize(
                    f"I found {len(matches)} matching transactions. Choose which one to split:",
                    f"Ho trovato {len(matches)} transazioni corrispondenti. Scegli quale dividere:",
                    source_text=text,
                )
            return _start_split_selection_flow(
                state,
                picker_records,
                divisor=divisor,
                source_text=text,
                intro=intro,
            )
        return _start_split_selection_flow(
            state,
            records,
            divisor=divisor,
            source_text=text,
            intro=localize(
                "I couldn't match that transaction. Choose which one to split:",
                "Non ho trovato quella transazione. Scegli quale dividere:",
                source_text=text,
            ),
        )

    last = state.get("last_committed_txn")
    if isinstance(last, dict) and last.get("id") and last.get("amount"):
        rec = _record_from_last_committed(last)
        latest_id = _transaction_group_id(records[0])
        if str(rec.get("journal_id") or "") == latest_id:
            return _queue_split_action_for_record(state, rec, divisor=divisor, source_text=text)
        return _start_split_latest_confirm_flow(state, records[0], records, divisor=divisor, source_text=text)

    return _start_split_latest_confirm_flow(state, records[0], records, divisor=divisor, source_text=text)


def parse_direct_write_sentence(service: BridgeService, text: str, state: dict[str, Any]) -> BotResponse | None:
    """Regex-based fallback for explicit transaction sentences when the AI router is unavailable.

    Requires unambiguous transaction intent signals AND a currency-anchored amount.
    Returns None whenever the intent or amount is unclear so the caller can show a
    generic help message rather than silently creating a junk draft.
    """
    lowered = text.casefold().strip()
    if not lowered:
        return None

    transaction_kind: str | None = None
    # Explicit command verbs only — "spesa" alone is too ambiguous in Italian.
    # Single words use \b word-boundary; multi-word phrases use substring search.
    expense_patterns = [
        r"\bexpense\b", r"\bspent\b", r"\bpaid\b", r"\bbought\b",
        r"\bpagato\b", r"\bcomprato\b", r"ho\s+speso\b", r"ho\s+pagato\b",
        r"\badd\s+(?:an?\s+)?expense\b", r"\baggiungi\s+(?:una?\s+)?spesa\b",
        r"\baggiungi\b(?=.*(?:€|eur(?:o)?))",
        r"\bwithdraw\b", r"\bwithdrew\b", r"\bwithdrawal\b",
        r"\bprelievo\b", r"\bprelevo\b", r"\bho\s+prelevato\b", r"\bprelevat\b",
        r"\batm\b",
    ]
    income_patterns = [
        r"\bincome\b", r"\bsalary\b", r"\breceived\b", r"\bdeposit\b",
        r"\bstipendio\b", r"\bricevuto\b",
        r"\badd\s+(?:an?\s+)?income\b", r"\baggiungi\s+(?:una?\s+)?entrata\b",
    ]
    transfer_patterns = [
        r"\btransfer\b", r"\bmove\b", r"sposta\s+denaro", r"\btrasferisci\b",
        r"\badd\s+(?:a\s+)?transfer\b", r"\baggiungi\s+(?:un\s+)?trasferimento\b",
    ]
    if any(re.search(p, lowered) for p in expense_patterns):
        transaction_kind = "withdrawal"
    elif any(re.search(p, lowered) for p in income_patterns):
        transaction_kind = "deposit"
    elif any(re.search(p, lowered) for p in transfer_patterns):
        transaction_kind = "transfer"

    if transaction_kind is None:
        return None

    # Require a currency-anchored amount to avoid matching random numbers in non-transaction text.
    currency_amount_match = re.search(
        r'(\d+(?:[.,]\d{1,2})?)\s*(?:€|eur(?:o)?|usd|\$)|(?:€|eur(?:o)?|usd|\$)\s*(\d+(?:[.,]\d{1,2})?)',
        text,
        re.IGNORECASE,
    )
    if not currency_amount_match:
        if transaction_kind == "deposit":
            salary_signals = {"salary", "stipendio", "paycheck", "payslip", "busta paga", "wages"}
            if any(signal in lowered for signal in salary_signals):
                currency_amount_match = re.search(
                    r'(\d+(?:[.,]\d{1,2})?)',
                    text,
                    re.IGNORECASE,
                )
        if not currency_amount_match:
            return None
    raw_amount = (currency_amount_match.group(1) or currency_amount_match.group(2) or "").replace(",", ".")
    if not raw_amount:
        return None

    date_value = extract_relative_or_explicit_date_from_text(text)

    source = None
    destination = None
    category = None
    merchant = None
    description_hint = None

    if any(keyword in lowered for keyword in {"cash", "contanti", "in cash"}):
        source = "Cash" if transaction_kind == "withdrawal" else source
    if transaction_kind == "transfer":
        move_match = re.search(r'\bfrom\s+([a-zA-Z][a-zA-Z0-9 _-]{1,60})\s+\bto\s+([a-zA-Z][a-zA-Z0-9 _-]{1,60})', text, re.IGNORECASE)
        if not move_match:
            move_match = re.search(r'\bda\s+([a-zA-ZÀ-ÿ][a-zA-ZÀ-ÿ0-9 _-]{1,60})\s+(?:a|al|alla|nel)\s+([a-zA-ZÀ-ÿ][a-zA-ZÀ-ÿ0-9 _-]{1,60})', text, re.IGNORECASE)
        if move_match:
            source = move_match.group(1).strip().rstrip(".")
            destination = move_match.group(2).strip().rstrip(".")

    description_match = re.search(r'\b(?:for|per)\s+([a-zA-ZÀ-ÿ][a-zA-ZÀ-ÿ0-9 _-]{1,60})', text, re.IGNORECASE)
    if description_match:
        description_hint = clean_free_text_slot(description_match.group(1))
    if not description_hint:
        di_match = re.search(r'\bdi\s+([^\d.,;:!?]+)', text, re.IGNORECASE)
        if di_match:
            description_hint = clean_free_text_slot(di_match.group(1))

    at_match = re.search(r'\b(?:at|da|presso)\s+([a-zA-ZÀ-ÿ][a-zA-ZÀ-ÿ0-9 _-]{1,60})', text, re.IGNORECASE)
    if at_match:
        merchant = clean_free_text_slot(at_match.group(1))

    category_match = re.search(r'\b(?:category|categoria)\s+([a-zA-ZÀ-ÿ][a-zA-ZÀ-ÿ0-9 _-]{1,60})', text, re.IGNORECASE)
    if category_match:
        category = clean_free_text_slot(category_match.group(1))

    # Transfers: synthesize description from source/destination if available
    if transaction_kind == "transfer" and source and destination and not description_hint:
        description_hint = f"{source} → {destination}"

    description = description_hint or merchant
    if not description and transaction_kind == "deposit":
        if any(signal in lowered for signal in {"salary", "paycheck", "payslip", "wages"}):
            description = "Salary"
        elif any(signal in lowered for signal in {"stipendio", "busta paga"}):
            description = "Stipendio"
    if not description and transaction_kind == "withdrawal":
        if any(signal in lowered for signal in {"withdraw", "withdrew", "withdrawal", "atm", "prelievo", "prelevo", "prelevat", "bancomat"}):
            description = "Prelievo ATM" if locale_language(text) == "it" else "ATM withdrawal"
    if not description:
        # No meaningful description extractable — bail out; caller will show help text.
        return None

    payload = {
        "intent": {
            "withdrawal": "create_expense",
            "deposit": "create_income",
            "transfer": "create_transfer",
        }[transaction_kind],
        "source_text": text,
        "params": {
            "amount": raw_amount,
            "description": description,
            "date": date_value,
            "source": source,
            "destination": destination,
            "category": category,
            "budget": None,
            "notes": None,
            "merchant": merchant,
            "live": False,
        },
    }
    try:
        return execute_intent(service, payload, state)
    except FireflyAPIError as exc:
        if is_firefly_offline_error(exc):
            raise
        return BotResponse(
            localize(
                f"I got part of it, but I still need a safer mapping. {exc}",
                f"Ho capito una parte della richiesta, ma mi serve un'associazione piu sicura. {exc}",
                source_text=text,
            )
        )
    except RuntimeError as exc:
        return BotResponse(
            localize(
                f"I got part of it, but I still need a safer mapping. {exc}",
                f"Ho capito una parte della richiesta, ma mi serve un'associazione piu sicura. {exc}",
                source_text=text,
            )
        )


def process_receipt_message(service: BridgeService, bot_token: str, message: dict[str, Any], state: dict[str, Any]) -> BotResponse:
    photos = message.get("photo") or []
    if not isinstance(photos, list) or not photos:
        return BotResponse(localize("I could not find a usable image in that message.", "Non ho trovato un'immagine utilizzabile in quel messaggio.", source_text=str(message.get("caption") or "")))

    photo = photos[-1]
    file_id = str(photo.get("file_id") or "").strip()
    if not file_id:
        return BotResponse(localize("I could not access that image.", "Non riesco ad accedere a quell'immagine.", source_text=str(message.get("caption") or "")))

    image_bytes, mime_type = fetch_telegram_file_bytes(bot_token, file_id)
    caption = str(message.get("caption") or "").strip() or None
    pdfapihub_ocr_text = run_pdfapihub_ocr(image_bytes, mime_type=mime_type, file_name=str(message.get("file_name") or "").strip() or None)
    tesseract_text = run_receipt_ocr(image_bytes, mime_type=mime_type)
    ai_ocr_text = run_receipt_ai_ocr(image_bytes, mime_type=mime_type)
    extracted_text = pdfapihub_ocr_text or select_best_ocr_text(ai_ocr_text, tesseract_text)
    fallback_payload = build_receipt_fallback_payload(service, caption=caption, extracted_text=extracted_text)
    payload = run_receipt_router(service, image_bytes, mime_type=mime_type, caption=caption, extracted_text=extracted_text)
    ai_visible_count = extract_visible_transaction_count(payload)
    visible_count = count_visible_transactions(extracted_text or "", ai_visible_count)
    visible_count = max(visible_count, fallback_payload_transaction_count(fallback_payload))
    state["last_receipt_context"] = {
        "caption": caption or "",
        "extracted_text": extracted_text or "",
        "ocr_text_pdfapihub": pdfapihub_ocr_text or "",
        "ocr_text_tesseract": tesseract_text or "",
        "ocr_text_ai": ai_ocr_text or "",
        "visible_count": visible_count,
    }

    if visible_count > 1 and fallback_payload is not None:
        if str(fallback_payload.get("intent") or "").strip() != "create_transaction_batch":
            rebuilt = build_receipt_fallback_payload(service, caption=caption, extracted_text=extracted_text)
            if rebuilt is not None and str(rebuilt.get("intent") or "").strip() == "create_transaction_batch":
                fallback_payload = rebuilt

    if fallback_payload is not None and str(fallback_payload.get("intent") or "").strip() == "create_transaction_batch":
        payload = fallback_payload
    elif payload is None or str(payload.get("intent") or "").strip() == "clarify":
        if fallback_payload is not None and str(fallback_payload.get("intent") or "").strip() != "clarify":
            payload = fallback_payload
        elif payload is None and fallback_payload is not None:
            payload = fallback_payload
    if payload is None:
        locale_src = caption or (extracted_text[:200] if extracted_text else None)
        # Best-effort: try to extract at least an amount and create a partial draft
        if extracted_text or caption:
            raw_text = extracted_text or caption or ""
            amt_match = re.search(
                r"(?:€|eur(?:o)?)\s*(\d+(?:[.,]\d{1,2})?)|(\d+(?:[.,]\d{1,2})?)\s*(?:€|eur(?:o)?)",
                raw_text,
                re.IGNORECASE,
            )
            if amt_match:
                raw_amount = (amt_match.group(1) or amt_match.group(2) or "").replace(",", ".")
                if raw_amount:
                    partial_payload = {
                        "intent": "create_expense",
                        "confidence": 0.3,
                        "source_text": locale_src,
                        "params": {
                            "amount": raw_amount,
                            "description": localize("Payment", "Pagamento", source_text=locale_src),
                            "date": None,
                            "live": False,
                        },
                    }
                    try:
                        resp = execute_intent(service, partial_payload, state)
                        prefix = localize(
                            "⚠️ I could read the amount but not all details — please check and correct this draft:",
                            "⚠️ Ho letto l'importo ma non tutti i dettagli — controlla e correggi questa bozza:",
                            source_text=locale_src,
                        )
                        return BotResponse(f"{prefix}\n\n{resp.text}")
                    except Exception:
                        pass
            # No amount either — ask specifically
            return BotResponse(localize(
                "I could see text but couldn't find an amount. What was the total?",
                "Ho visto del testo ma non sono riuscito a leggere l'importo. Qual era il totale?",
                source_text=locale_src,
            ))
        return BotResponse(bot_text("receipt_unreadable", source_text=locale_src))
    response = execute_intent(service, payload, state)
    if visible_count > 1:
        counting_line = bot_text("receipt_counting", source_text=caption or extracted_text, count=visible_count)
        if response.text:
            return BotResponse(f"{counting_line}\n\n{response.text}", photo_path=response.photo_path)
        return BotResponse(counting_line, photo_path=response.photo_path)
    return response


def process_document_message(service: BridgeService, bot_token: str, message: dict[str, Any], state: dict[str, Any]) -> BotResponse:
    document = message.get("document") or {}
    if not isinstance(document, dict):
        return BotResponse(localize("I could not find a usable document in that message.", "Non ho trovato un documento utilizzabile in quel messaggio.", source_text=str(message.get("caption") or "")))

    file_id = str(document.get("file_id") or "").strip()
    if not file_id:
        return BotResponse(localize("I could not access that document.", "Non riesco ad accedere a quel documento.", source_text=str(message.get("caption") or "")))

    file_name = str(document.get("file_name") or "").strip() or "document"
    doc_mime = str(document.get("mime_type") or "").strip()
    file_bytes, guessed_mime = fetch_telegram_file_bytes(bot_token, file_id)
    mime_type = doc_mime or guessed_mime
    caption = str(message.get("caption") or "").strip() or None

    if mime_type.startswith("image/"):
        photo_message = dict(message)
        photo_message["photo"] = [{"file_id": file_id}]
        photo_message["file_name"] = file_name
        return process_receipt_message(service, bot_token, photo_message, state)

    if mime_type != "application/pdf" and not file_name.casefold().endswith(".pdf"):
        return BotResponse(
            localize(
                "I can process receipt images or PDF documents. Send a photo/screenshot or a PDF.",
                "Posso elaborare immagini ricevuta o documenti PDF. Invia una foto/screenshot o un PDF.",
                source_text=caption,
            )
        )

    pdfapihub_text = run_pdfapihub_ocr(file_bytes, mime_type="application/pdf", file_name=file_name)
    extracted_text = pdfapihub_text
    if not extracted_text:
        return BotResponse(
            localize(
                "I could not read text from this PDF yet. Check PDFAPIHub key/config or send a screenshot.",
                "Non sono ancora riuscito a leggere il testo di questo PDF. Controlla la chiave/config PDFAPIHub o invia uno screenshot.",
                source_text=caption,
            )
        )

    fallback_payload = build_receipt_fallback_payload(service, caption=caption, extracted_text=extracted_text)
    if fallback_payload is None:
        return BotResponse(bot_text("receipt_unreadable", source_text=caption or extracted_text))

    visible_count = max(
        count_visible_transactions(extracted_text or "", None),
        fallback_payload_transaction_count(fallback_payload),
    )
    state["last_receipt_context"] = {
        "caption": caption or "",
        "extracted_text": extracted_text or "",
        "ocr_text_pdfapihub": pdfapihub_text or "",
        "visible_count": visible_count,
    }
    response = execute_intent(service, fallback_payload, state)
    if visible_count > 1:
        counting_line = bot_text("receipt_counting", source_text=caption or extracted_text, count=visible_count)
        if response.text:
            return BotResponse(f"{counting_line}\n\n{response.text}", photo_path=response.photo_path)
        return BotResponse(counting_line, photo_path=response.photo_path)
    return response


def process_message(service: BridgeService, text: str, state: dict[str, Any]) -> BotResponse:
    # Handle edit mode
    edit_mode = state.get("edit_mode")
    if isinstance(edit_mode, dict):
        if has_cancel_intent(text):
            state.pop("edit_mode", None)
            return BotResponse(localize("Edit cancelled.", "Modifica annullata.", source_text=text))

        step = str(edit_mode.get("step") or "").strip()
        if step == "choose_field":
            field_choice = normalize_natural_text(text).strip()
            if field_choice not in {"amount", "date", "category"}:
                return BotResponse(
                    localize(
                        "Choose one: amount, date, or category",
                        "Scegli uno: amount, date, o category",
                        source_text=text,
                    )
                )
            edit_mode["field"] = field_choice
            edit_mode["step"] = "input_value"
            return BotResponse(
                localize(
                    f"Enter new {field_choice}:",
                    f"Inserisci nuovo {field_choice}:",
                    source_text=text,
                )
            )
        elif step == "input_value":
            txn_id = edit_mode.get("txn_id")
            field = edit_mode.get("field")
            txn_data = edit_mode.get("txn_data", {})
            new_value = text.strip()

            update_payload = {}
            if field == "amount":
                try:
                    update_payload["amount"] = str(float(new_value))
                except ValueError:
                    return BotResponse(localize("Invalid amount.", "Importo non valido.", source_text=text))
            elif field == "date":
                parsed_date = parse_flexible_date(new_value)
                if not parsed_date:
                    return BotResponse(localize("Invalid date.", "Data non valida.", source_text=text))
                update_payload["date"] = parsed_date.isoformat()
            elif field == "category":
                update_payload["category_name"] = new_value

            if service.client.update_transaction(txn_id, update_payload):
                state.pop("edit_mode", None)
                old_value = txn_data.get(field, "?")
                return BotResponse(
                    localize(
                        f"✅ Updated {field}: {old_value} → {new_value}",
                        f"✅ {field} aggiornato: {old_value} → {new_value}",
                        source_text=text,
                    )
                )
            state.pop("edit_mode", None)
            return BotResponse(
                localize(
                    f"❌ Could not update {field}",
                    f"❌ Impossibile aggiornare {field}",
                    source_text=text,
                )
            )

    # Handle pending recurrence suggestion (yes/no after draft with recurrence keywords)
    pending_recurrence_suggestion = state.get("pending_recurrence_suggestion")
    if isinstance(pending_recurrence_suggestion, dict) and not text.strip().startswith("/"):
        draft_session = load_draft_session(state)
        if draft_session is not None and draft_session.is_active:
            phase = getattr(draft_session, "phase", None)
            if phase in {DraftPhase.CATEGORY_CONFIRM, DraftPhase.CATEGORY_SELECT, DraftPhase.BUDGET_SUGGEST}:
                pending_recurrence_suggestion = None
            elif phase == DraftPhase.REVIEW and not state.get("awaiting_recurrence_answer"):
                pending_recurrence_suggestion = None
    if isinstance(pending_recurrence_suggestion, dict) and not text.strip().startswith("/"):
        lowered_answer = normalize_natural_text(text)
        if has_cancel_intent(text) or any(w in lowered_answer for w in {"no", "nope", "nein", "non"}):
            state.pop("pending_recurrence_suggestion", None)
            state.pop("awaiting_recurrence_answer", None)
            review_text = ""
            draft_session = load_draft_session(state)
            if draft_session is not None and draft_session.is_active and getattr(draft_session, "phase", None) == DraftPhase.REVIEW:
                review_text = build_draft_manager(service).build_review_message(draft_session)
            response_text = localize("Got it, no recurrence created.", "Ok, nessuna ricorrenza creata.", source_text=text)
            if review_text:
                response_text = f"{response_text}\n\n{review_text}"
            return BotResponse(response_text)
        if any(w in lowered_answer for w in {"yes", "si", "sì", "ok", "sure", "vai", "crea", "create"}):
            state.pop("pending_recurrence_suggestion", None)
            state.pop("awaiting_recurrence_answer", None)
            cadence = str(pending_recurrence_suggestion.get("cadence") or "monthly")
            rec_params = {
                "amount": str(pending_recurrence_suggestion.get("amount") or ""),
                "description": str(pending_recurrence_suggestion.get("description") or ""),
                "title": str(pending_recurrence_suggestion.get("description") or ""),
                "cadence": cadence,
                "source": pending_recurrence_suggestion.get("source"),
                "destination": pending_recurrence_suggestion.get("destination"),
                "category": pending_recurrence_suggestion.get("category"),
                "budget": pending_recurrence_suggestion.get("budget"),
                "date": pending_recurrence_suggestion.get("date"),
            }
            try:
                recurrence_payload = recurrence_payload_from_params(service, rec_params)
            except RuntimeError as exc:
                return BotResponse(str(exc))
            title = recurrence_payload.get("title") or "Recurring transaction"
            freq_label = _recurrence_freq_label(cadence, source_text=text)
            preview = format_pending_action_preview(
                localize("Recurrence draft prepared.", "Bozza ricorrenza preparata.", source_text=text),
                [
                    localize(f"Name: {title}", f"Nome: {title}", source_text=text),
                    localize(f"Frequency: {freq_label}", f"Frequenza: {freq_label}", source_text=text),
                    localize(f"Starts: {recurrence_payload.get('first_date', '?')}", f"Inizio: {recurrence_payload.get('first_date', '?')}", source_text=text),
                    localize(f"Amount: {rec_params['amount']}", f"Importo: {rec_params['amount']}", source_text=text),
                ],
                localize(
                    "Say 'commit it' to create this recurrence.",
                    "Scrivi 'conferma' per crearla.",
                    source_text=text,
                ),
            )
            remember_pending_action(state, kind="recurrence_create", payload=recurrence_payload, preview=preview)
            return BotResponse(preview)

    # Handle "make it recurring" / "rendila ricorrente" on active draft
    if not text.strip().startswith("/"):
        recur_from_draft_words = {
            "rendila ricorrente", "rendi ricorrente", "rendilo ricorrente",
            "make it recurring", "make this recurring", "make recurrent",
            "questa si ripete", "questa spesa si ripete", "si ripete",
        }
        lowered_msg = normalize_natural_text(text)
        if any(phrase in lowered_msg for phrase in recur_from_draft_words) or (
            any(w in lowered_msg for w in {"ricorrente", "ricorrenza", "recurring", "recurrence"})
            and extract_recurrence(text)
        ):
            pending_action = state.get("pending_action")
            draft_session = load_draft_session(state)
            tx_data: dict[str, Any] | None = None
            if isinstance(pending_action, dict) and pending_action.get("kind") == "transaction_create":
                payload_tx = pending_action.get("payload", {})
                tx_list = payload_tx.get("transactions") or []
                if tx_list and isinstance(tx_list[0], dict):
                    tx_data = tx_list[0]
            elif draft_session is not None and draft_session.is_active:
                session_txs = getattr(draft_session, "transactions", None) or []
                if session_txs and isinstance(session_txs[0], dict):
                    tx_data = session_txs[0]
            if tx_data:
                cadence = extract_recurrence(text) or "monthly"
                rec_params = {
                    "amount": str(tx_data.get("amount") or ""),
                    "description": str(tx_data.get("description") or ""),
                    "title": str(tx_data.get("description") or ""),
                    "cadence": cadence,
                    "source": tx_data.get("source_name"),
                    "destination": tx_data.get("destination_name"),
                    "category": tx_data.get("category_name"),
                    "budget": tx_data.get("budget_name"),
                    "date": str(tx_data.get("date") or "")[:10] or None,
                }
                try:
                    recurrence_payload = recurrence_payload_from_params(service, rec_params)
                except RuntimeError as exc:
                    return BotResponse(str(exc))
                freq_label = _recurrence_freq_label(cadence, source_text=text)
                title = recurrence_payload.get("title") or "Recurring transaction"
                preview = format_pending_action_preview(
                    localize("Recurrence draft prepared.", "Bozza ricorrenza preparata.", source_text=text),
                    [
                        localize(f"Name: {title}", f"Nome: {title}", source_text=text),
                        localize(f"Frequency: {freq_label}", f"Frequenza: {freq_label}", source_text=text),
                        localize(f"Starts: {recurrence_payload.get('first_date', '?')}", f"Inizio: {recurrence_payload.get('first_date', '?')}", source_text=text),
                        localize(f"Amount: {rec_params['amount']}", f"Importo: {rec_params['amount']}", source_text=text),
                    ],
                    localize(
                        "Say 'commit it' to create this recurrence.",
                        "Scrivi 'conferma' per crearla.",
                        source_text=text,
                    ),
                )
                remember_pending_action(state, kind="recurrence_create", payload=recurrence_payload, preview=preview)
                return BotResponse(preview)

    # Handle pending description input
    pending_desc = state.get("pending_description_input")
    if pending_desc and not text.startswith("/"):
        description = text.strip()
        if description:
            state.pop("pending_description_input", None)
            pending_desc["payload_params"]["description"] = description
            payload = {
                "intent": pending_desc["intent"],
                "source_text": pending_desc["source_text"],
                "params": pending_desc["payload_params"],
            }
            return execute_intent(service, payload, state)
        return BotResponse(localize("Please provide a description.", "Per favore fornisci una descrizione.", source_text=text))

    add_response = handle_add_flow_message(service, state, text)
    if add_response is not None:
        return add_response

    setup_response = handle_finance_setup_message(service, state, text)
    if setup_response is not None:
        return setup_response

    maintenance_response = handle_maintenance_message(service, state, text)
    if maintenance_response is not None:
        return maintenance_response

    draft_account_fix_response = handle_pending_draft_account_fix(service, state, text)
    if draft_account_fix_response is not None:
        return draft_account_fix_response

    resolution_response = handle_pending_transaction_resolution(service, state, text)
    if resolution_response is not None:
        return resolution_response

    if not text.strip().startswith("/"):
        draft_session = load_draft_session(state)
        if has_commit_intent(text) and (draft_session is None or getattr(draft_session, "phase", None) == DraftPhase.REVIEW):
            return commit_pending_transaction(service, state, text)

        clone_response = handle_pending_clone_selection(service, state, text)
        if clone_response is not None:
            return clone_response

        split_latest_response = handle_pending_split_latest_confirm(service, state, text)
        if split_latest_response is not None:
            return split_latest_response

        split_selection_response = handle_pending_split_selection(service, state, text)
        if split_selection_response is not None:
            return split_selection_response

        split_response = handle_split_transaction_intent(service, text, state)
        if split_response is not None:
            return split_response

        if draft_session is not None and draft_session.is_active:
            manager = build_draft_manager(service)
            if has_cancel_intent(text):
                manager.mark_discarded(draft_session)
                save_draft_session(state, None)
                return BotResponse(bot_text("draft_discarded", source_text=text))

            if manager.is_correction(text):
                corrected = manager.apply_correction(draft_session, text)
                if corrected == "__LIST_ACCOUNTS__":
                    accounts = service.client.list_accounts("all")
                    account_list = format_accounts(accounts, source_text=text)
                    hint = localize(
                        "Say 'change from X' to change source or 'change to X' to change destination.",
                        "Scrivi 'cambia da X' per cambiare sorgente o 'cambia a X' per cambiare destinazione.",
                        source_text=text,
                    )
                    return BotResponse(f"{account_list}\n\n{hint}")
                if corrected == "__REREAD__":
                    last_receipt = state.get("last_receipt_context")
                    if isinstance(last_receipt, dict):
                        caption = str(last_receipt.get("caption") or "").strip() or None
                        extracted_text = str(last_receipt.get("extracted_text") or "").strip() or None
                        payload = build_receipt_fallback_payload(
                            service,
                            caption=caption,
                            extracted_text=extracted_text,
                        )
                        if payload is not None:
                            payload.setdefault("source_text", text)
                            reread_response = execute_intent(service, payload, state)
                            visible_count = int(last_receipt.get("visible_count") or 0)
                            if visible_count > 1:
                                counting_line = bot_text(
                                    "receipt_counting",
                                    source_text=caption or extracted_text or text,
                                    count=visible_count,
                                )
                                if reread_response.text:
                                    return BotResponse(
                                        f"{counting_line}\n\n{reread_response.text}",
                                        photo_path=reread_response.photo_path,
                                    )
                                return BotResponse(counting_line, photo_path=reread_response.photo_path)
                            return reread_response
                    corrected = bot_text("receipt_unreadable", source_text=text)
                save_draft_session(state, draft_session)
                return BotResponse(corrected)

            advanced = manager.advance(draft_session, text)
            save_draft_session(state, draft_session)
            if _should_offer_recurrence_before_review(state, draft_session):
                return BotResponse(
                    _begin_recurrence_suggestion_prompt(
                        state,
                        state["pending_recurrence_suggestion"],
                        source_text=text,
                    )
                )
            return BotResponse(advanced or manager.build_review_message(draft_session))

        clone_response = handle_clone_transaction_intent(service, text, state)
        if clone_response is not None:
            return clone_response

        direct_write = parse_direct_write_sentence(service, text, state)
        if direct_write is not None:
            return direct_write

        natural_payload = parse_natural_intent_payload(text)
        if natural_payload is not None:
            try:
                return execute_intent(service, natural_payload, state)
            except (ConfigurationError, FireflyAPIError, ValueError, RuntimeError) as exc:
                if is_firefly_offline_error(exc):
                    raise
                return BotResponse(f"Request failed: {exc}")

        shortcut = interpret_natural_command(text)
        if shortcut is not None:
            command_text = shortcut
        else:
            ai_payload = None
            if router_health.check():
                ai_payload = run_picoclaw_router(service, text, state)
            if ai_payload is None and not router_health.check():
                natural_payload = parse_natural_intent_payload(text)
                if natural_payload is not None:
                    ai_payload = natural_payload
                else:
                    success, result, missing = parse_deterministic_with_fallback(text)
                    if missing:
                        return BotResponse(
                            localize(
                                "I still need the amount in the same sentence. Example: add an expense of 12.50 for lunch.",
                                "Mi serve ancora l'importo nella stessa frase. Esempio: aggiungi una spesa di 12,50 per pranzo.",
                                source_text=text,
                            )
                        )
                    return BotResponse(localize(
                        "⚠️  Router down. Try /help or /list_accounts",
                        "⚠️  Router non disponibile. Prova /help o /lista_conti",
                        source_text=text
                    ))
            if ai_payload is not None:
                ai_payload.setdefault("source_text", text)
                ai_payload = enforce_deterministic_period(ai_payload, text)
                try:
                    return execute_intent(service, ai_payload, state)
                except (ConfigurationError, FireflyAPIError, ValueError, RuntimeError) as exc:
                    if is_firefly_offline_error(exc):
                        raise
                    return BotResponse(f"Request failed: {exc}")
            command_text = text.strip()
    else:
        command_text = text.strip()

    if not command_text:
        return BotResponse(help_text(text))

    if not command_text.startswith("/"):
        guided_prompt = build_guided_transaction_prompt(text)
        if guided_prompt:
            return BotResponse(guided_prompt)
        return BotResponse(
            localize(
                "I did not understand that yet.\n\nTry something like:\n- how much money do i have\n- make a graph of my balances\n- add an expense of 12.50 for lunch",
                "Non l'ho ancora capito bene.\n\nProva con qualcosa del tipo:\n- quanti soldi ho\n- fammi un grafico dei miei saldi\n- aggiungi una spesa di 12,50 per pranzo",
                source_text=text,
            )
        )

    parts = command_text.split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower()
    command = COMMAND_ALIASES.get(command, command)
    tail = parts[1] if len(parts) > 1 else ""

    if command == "/start":
        profile = get_finance_profile(state)
        payment_accounts = profile.get("payment_method_accounts")
        has_card_alias = isinstance(payment_accounts, dict) and bool(str(payment_accounts.get("card") or "").strip())
        if not finance_profile_ready(state) or not has_card_alias:
            return start_finance_setup(service, state, source_text=text)
        return BotResponse(help_text(text))

    if command == "/help":
        return BotResponse(help_text(text))

    if command in {"/command", "/commands"}:
        return BotResponse(commands_text(text))

    if command == "/cancel":
        state.pop("add_flow", None)
        state.pop("maintenance_mode", None)
        state.pop("profile_setup", None)
        state.pop("pending_description_input", None)
        state.pop("pending_draft_account_fix", None)
        state.pop("pending_transaction_resolution", None)
        state.pop("pending_recurrence_suggestion", None)
        state.pop("awaiting_recurrence_answer", None)
        state.pop("pending_clone_selection", None)
        state.pop("pending_split_latest_confirm", None)
        state.pop("pending_split_selection", None)
        state.pop("retry_queue", None)
        draft_session = load_draft_session(state)
        if draft_session is not None:
            manager = build_draft_manager(service)
            manager.mark_discarded(draft_session)
            save_draft_session(state, None)
        return BotResponse(localize("Cancelled.", "Annullato.", source_text=text))

    if command == "/undo":
        manager = build_draft_manager(service)
        snapshot = manager.undo()
        if snapshot:
            draft = snapshot.draft
            return BotResponse(
                localize(
                    f"✅ Restored: {draft.description} €{draft.amount}",
                    f"✅ Ripristinato: {draft.description} €{draft.amount}",
                    source_text=text
                )
            )
        return BotResponse(localize("❌ No undo history", "❌ Nessuna cronologia di annullamento", source_text=text))

    if command == "/add":
        preset = _intent_from_add_type(tail.strip()) if tail.strip() else None
        start_add_flow(state, source_text=text, preset_kind=preset)
        return BotResponse(build_add_flow_prompt(service, state, source_text=text))

    if command == "/maintenance":
        action = normalize_natural_text(tail)
        if action in {"off", "exit", "stop"}:
            state.pop("maintenance_mode", None)
            return BotResponse(localize("Maintenance mode closed.", "Modalita manutenzione chiusa.", source_text=text))
        start_maintenance_mode(state, source_text=text)
        return BotResponse(maintenance_menu_text(source_text=text))

    if command == "/setup":
        state.pop("maintenance_mode", None)
        state.pop("add_flow", None)
        action = normalize_natural_text(tail)
        if action == "status":
            return BotResponse(setup_overview_text(state, source_text=text))
        if action in {"reset", "restart"}:
            state.pop("finance_profile", None)
            state.pop("profile_setup", None)
            return BotResponse(
                localize(
                    "Finance profile reset. Use /train to teach accounts again.",
                    "Profilo finanziario cancellato. Usa /impara per insegnare di nuovo i conti.",
                    source_text=text,
                )
            )
        if action in {"start", "train", "impara", "allena"}:
            return start_finance_setup(service, state, source_text=text)
        return BotResponse(setup_overview_text(state, source_text=text))

    if command == "/train":
        return start_finance_setup(service, state, source_text=text)

    if command == "/health":
        return BotResponse(build_health_message(service, source_text=text))

    if command == "/backup":
        document_path, filename, backup = create_firefly_backup_document(service)
        counts = {
            key: len(value)
            for key, value in backup.get("data", {}).items()
            if isinstance(value, list)
        }
        transaction_count = counts.get("transactions", 0)
        error_count = len(backup.get("errors", {}))
        size_kb = max(1, int(os.path.getsize(document_path) / 1024))
        caption = localize(
            f"Backup JSON ready: {transaction_count} transactions, {len(counts)} collections, {size_kb} KB. Errors: {error_count}.",
            f"Backup JSON pronto: {transaction_count} transazioni, {len(counts)} collezioni, {size_kb} KB. Errori: {error_count}.",
            source_text=text,
        )
        return BotResponse(caption, document_path=document_path, document_filename=filename)

    if command == "/delete":
        if not tail.strip():
            try:
                recent_txns = flatten_transactions(service.client.list_transactions(start=date.today() - timedelta(days=30), end=date.today(), limit=10))
                if not recent_txns:
                    return BotResponse(localize("No recent transactions found.", "Nessuna transazione recente trovata.", source_text=text))
                lines = [localize("Recent transactions (last 30 days):", "Transazioni recenti (ultimi 30 giorni):", source_text=text)]
                emoji_nums = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
                for i, txn in enumerate(recent_txns[:10], 1):
                    txn_id = str(txn.get("transaction_journal_id", "?")).strip()
                    desc = str(txn.get("description", "?")).strip()[:40]
                    amount = str(txn.get("amount", "?")).strip()
                    emoji = emoji_nums[i - 1] if i - 1 < len(emoji_nums) else f"{i}."
                    lines.append(f"{emoji} [{txn_id}] {desc} €{amount}")
                lines.append(localize("Use: /delete <id>", "Usa: /delete <id>", source_text=text))
                return BotResponse("\n".join(lines))
            except Exception as exc:
                return BotResponse(f"Error fetching transactions: {exc}")
        try:
            txn_id = int(tail.strip().split()[0])
            if service.client.delete_transaction(txn_id):
                return BotResponse(localize(f"✅ Deleted transaction {txn_id}", f"✅ Transazione {txn_id} eliminata", source_text=text))
            return BotResponse(localize(f"❌ Could not delete transaction {txn_id}", f"❌ Impossibile eliminare transazione {txn_id}", source_text=text))
        except ValueError:
            return BotResponse(localize("Invalid transaction ID. Use: /delete <id>", "ID transazione non valido. Usa: /delete <id>", source_text=text))
        except Exception as exc:
            return BotResponse(f"Error: {exc}")

    if command == "/clone":
        if tail.strip():
            return BotResponse(localize("Usage: /clone", "Uso: /clona", source_text=text))
        return start_clone_transaction_flow(service, state, source_text=text)

    if command == "/edit":
        if not tail.strip():
            return BotResponse(localize("Usage: /edit <transaction_id>", "Uso: /edit <id_transazione>", source_text=text))
        try:
            txn_id = int(tail.strip().split()[0])
            txns = service.client.list_transactions(start=date.today() - timedelta(days=365), end=date.today() + timedelta(days=1), limit=1000)
            txn_data = None
            for t in txns:
                if str(t.get("transaction_journal_id", "")).strip() == str(txn_id):
                    txn_data = t
                    break
            if not txn_data:
                return BotResponse(localize(f"Transaction {txn_id} not found.", f"Transazione {txn_id} non trovata.", source_text=text))

            desc = str(txn_data.get("description", "?")).strip()
            amount = str(txn_data.get("amount", "?")).strip()
            date_str = str(txn_data.get("date", "?")).strip()
            category = str(txn_data.get("category_name", "?")).strip()

            state["edit_mode"] = {
                "txn_id": txn_id,
                "txn_data": txn_data,
                "step": "choose_field",
            }

            lines = [
                localize("Current transaction:", "Transazione attuale:", source_text=text),
                f"  {desc} | €{amount}",
                f"  {date_str} | {category}",
                "",
                localize("What to edit?", "Cosa modificare?", source_text=text),
                "1️⃣ amount",
                "2️⃣ date",
                "3️⃣ category",
                "❌ cancel",
            ]
            return BotResponse("\n".join(lines))
        except ValueError:
            return BotResponse(localize("Invalid transaction ID.", "ID transazione non valido.", source_text=text))
        except Exception as exc:
            return BotResponse(f"Error: {exc}")

    if command in {"/balances", "/balance"}:
        return BotResponse(format_balances(service.account_balances(), source_text=text))

    if command == "/accounts":
        return BotResponse(format_accounts(service.client.list_accounts("all"), source_text=text))

    if command == "/categories":
        return BotResponse(format_named_records(localize("Categories", "Categorie", source_text=text), service.client.list_categories(), source_text=text))

    if command == "/budgets":
        return BotResponse(format_named_records(localize("Budgets", "Budget", source_text=text), service.client.list_budgets(), source_text=text))

    if command == "/summary":
        values = parse_kv_args(tail) if "=" in tail else {"month": tail.strip()} if tail.strip() else {}
        start, end, label = period_from_values(values, default_current_month=True)
        return BotResponse(
            format_summary(
                {
                    "label": label,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "summary": service.client.summary_basic(start=start, end=end),
                },
                source_text=text,
            )
        )

    if command == "/recent":
        values: dict[str, str] = {}
        if "=" in tail:
            values = parse_kv_args(tail)
        elif tail.strip():
            tokens = tail.split(maxsplit=1)
            if tokens[0].isdigit():
                values["days"] = tokens[0]
                if len(tokens) > 1:
                    values["query"] = tokens[1]
            else:
                values["query"] = tail.strip()
        start, end, label = period_from_values(values, default_days=int(values.get("days") or 7))
        query = values.get("query") or None
        records = flatten_transactions(service.client.list_transactions(start=start, end=end, limit=100))
        if query:
            needle = normalize_natural_text(query)
            records = [
                record for record in records
                if needle in normalize_natural_text(" ".join(
                    [
                        str(record.get("description", "")),
                        str(record.get("source_name", "")),
                        str(record.get("destination_name", "")),
                        str(record.get("category_name", "")),
                        str(record.get("budget_name", "")),
                    ]
                ))
            ]
        return BotResponse(format_transactions(records[:15], title=localize(f"Recent transactions ({label}):", f"Transazioni recenti ({label}):", source_text=text), source_text=text))

    if command == "/search":
        query = tail.strip() if tail else ""
        if not query:
            return BotResponse(localize(
                "Usage: /search <keyword>  — searches transactions in the last 90 days.",
                "Uso: /cerca <parola chiave>  — cerca tra le transazioni degli ultimi 90 giorni.",
                source_text=text,
            ))
        end = date.today()
        start = end - timedelta(days=90)
        records = flatten_transactions(service.client.list_transactions(start=start, end=end, limit=200))
        needle = normalize_natural_text(query)
        records = [
            r for r in records
            if needle in normalize_natural_text(" ".join([
                str(r.get("description", "")),
                str(r.get("source_name", "")),
                str(r.get("destination_name", "")),
                str(r.get("category_name", "")),
                str(r.get("budget_name", "")),
            ]))
        ]
        if not records:
            return BotResponse(bot_text("search_no_results", source_text=text, query=query))
        return BotResponse(format_transactions(
            records[:20],
            title=bot_text("search_results_title", source_text=text, query=query),
            source_text=text,
        ))

    if command == "/recurrences":
        return BotResponse(format_recurrences(service.client.list_recurrences(), source_text=text))

    if command == "/topcategories":
        tokens = tail.split()
        with_graph = any(token.casefold() in {"graph", "chart", "grafico", "grafica"} for token in tokens)
        kv_tokens = [token for token in tokens if "=" in token]
        values = parse_kv_args(" ".join(kv_tokens)) if kv_tokens else {}
        if "month" not in values:
            month_token = next((token for token in tokens if re.fullmatch(r"\d{4}-\d{2}", token)), None)
            if month_token:
                values["month"] = month_token
        start, end, label = period_from_values(values, default_current_month=True)
        records = flatten_transactions(service.client.list_transactions(start=start, end=end, limit=300))
        all_categories = any(token.casefold() in {"all", "tutte", "tutti"} for token in tokens) or as_bool(values.get("all_categories"), default=False)
        text_body = format_top_spending_categories(records, label=label, source_text=text, limit=None if all_categories else 8)
        if with_graph:
            photo_path, caption = create_spending_chart(
                records,
                days=max((end - start).days + 1, 1),
                label=label,
                source_text=text,
                limit=None if all_categories else 8,
            )
            return BotResponse(f"{text_body}\n\n{caption}", photo_path=photo_path)
        return BotResponse(text_body)

    if command == "/budgetreport":
        values = parse_kv_args(tail) if "=" in tail else {"month": tail.strip()} if tail.strip() else {}
        start, end, label = period_from_values(values, default_current_month=True)
        records = flatten_transactions(service.client.list_transactions(start=start, end=end, limit=300))
        budget_limits: dict[str, float] = {}
        try:
            budget_limits = collect_budget_limits(service, start, end)
        except Exception:
            pass
        return BotResponse(format_budget_report(records, label=label, budget_limits=budget_limits, source_text=text))

    if command == "/setbudgetlimit":
        values = parse_kv_args(tail)
        budget_name = values.get("budget") or values.get("name")
        amount = values.get("amount")
        month = values.get("month")
        if not budget_name or not amount:
            return BotResponse("Missing required fields. Example: /setbudgetlimit budget=\"Groceries\" amount=350")
        budget = resolve_budget(service.client, budget_name)
        if not budget:
            return BotResponse(f"I could not find a budget named '{budget_name}'.")
        budget_id = str(budget.get("id") or "").strip()
        start, end = month_window_from_label(month)
        existing_limit_id = None
        try:
            existing_limits = service.client.list_budget_limits(budget_id, start=start.isoformat(), end=end.isoformat())
        except FireflyAPIError:
            existing_limits = []
        for item in existing_limits:
            existing_limit_id = str(item.get("id") or "").strip() or existing_limit_id
            if existing_limit_id:
                break
        preview = format_pending_action_preview(
            "Budget limit draft prepared.",
            [
                f"Budget: {budget_name}",
                f"Amount: {amount}",
                f"Period: {format_display_period(start, end)}",
                f"Action: {'update existing limit' if existing_limit_id else 'create new limit'}",
            ],
            "Say 'commit it' if you want me to apply this budget limit.",
        )
        remember_pending_action(
            state,
            kind="budget_limit_set",
            payload={
                "budget_id": budget_id,
                "budget_name": budget_name,
                "amount": amount,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "budget_limit_id": existing_limit_id,
                "notes": values.get("notes"),
            },
            preview=preview,
        )
        return BotResponse(preview)

    if command == "/graph":
        tokens = tail.split()
        if not tokens:
            return BotResponse(
                localize(
                    "Usage: /graph <type> [period]\n"
                    "Types: balances, spending, cashflow, budget, recurrences\n"
                    "Example: /graph budget month=2026-05",
                    "Uso: /graph <tipo> [periodo]\n"
                    "Tipi: saldi, spese, cashflow, budget, ricorrenze\n"
                    "Esempio: /graph budget month=2026-05",
                    source_text=text,
                )
            )
        graph_kind = tokens[0].casefold()
        values = parse_kv_args(" ".join(tokens[1:])) if any("=" in token for token in tokens[1:]) else {}
        if "days" not in values and len(tokens) > 1 and tokens[1].isdigit():
            values["days"] = tokens[1]
        if graph_kind in {"spending", "category", "categories", "categoria", "categorie", "spese"}:
            start, end, _ = period_from_values(values, default_days=int(values.get("days") or 30))
            records = flatten_transactions(service.client.list_transactions(start=start, end=end, limit=100))
            chart_label = month_label_for_window(start, end)
            all_categories = any(token.casefold() in {"all", "tutte", "tutti"} for token in tokens) or as_bool(values.get("all_categories"), default=False)
            photo_path, caption = create_spending_chart(
                records,
                days=max((end - start).days + 1, 1),
                label=chart_label,
                source_text=text,
                limit=None if all_categories else 8,
            )
            return BotResponse(caption, photo_path=photo_path)
        if graph_kind == "cashflow":
            start, end, _ = period_from_values(values, default_days=int(values.get("days") or 30))
            records = flatten_transactions(service.client.list_transactions(start=start, end=end, limit=100))
            chart_label = month_label_for_window(start, end)
            photo_path, caption = create_cashflow_chart(records, days=max((end - start).days + 1, 1), label=chart_label, source_text=text)
            return BotResponse(caption, photo_path=photo_path)
        if graph_kind in {"balances", "balance", "saldi"}:
            photo_path, caption = create_balance_chart(service.account_balances(), source_text=text)
            return BotResponse(caption, photo_path=photo_path)
        if graph_kind == "budget":
            start, end, label = period_from_values(values, default_current_month=True)
            records = flatten_transactions(service.client.list_transactions(start=start, end=end, limit=300))
            budget_spent = aggregate_spending_by_budget(records)
            try:
                budget_limits_map = collect_budget_limits(service, start, end)
            except Exception:
                budget_limits_map = {}
            photo_path, caption = create_budget_chart(budget_limits_map, budget_spent, label=label, source_text=text)
            return BotResponse(caption, photo_path=photo_path)
        if graph_kind in {"recurrences", "ricorrenze", "recurring", "ricorrenti"}:
            items = service.client.list_recurrences()
            photo_path, caption = create_recurrence_chart(items, source_text=text)
            return BotResponse(caption, photo_path=photo_path)
        return BotResponse(localize(
            "Unsupported graph type. Use: balances, spending, cashflow, budget, recurrences.",
            "Tipo di grafico non supportato. Usa: saldi, spese, cashflow, budget, ricorrenze.",
            source_text=text,
        ))

    if command in {"/expense", "/income", "/transfer"}:
        values = parse_kv_args(tail)
        amount = values.get("amount")
        description = values.get("description")
        if not amount or not description:
            return BotResponse(
                "Missing required fields.\n"
                f"Example: {command} amount=12.50 description=\"Lunch\" merchant=coop"
            )
        transaction_kind = {
            "/expense": "withdrawal",
            "/income": "deposit",
            "/transfer": "transfer",
        }[command]
        payload = service.build_transaction(
            transaction_kind=transaction_kind,
            amount=amount,
            description=description,
            transaction_date=values.get("date"),
            source_name=values.get("source"),
            destination_name=values.get("destination"),
            category_name=values.get("category"),
            budget_name=values.get("budget"),
            notes=values.get("notes"),
            tags=[values["tag"]] if "tag" in values else [],
            merchant=values.get("merchant"),
            currency_code=values.get("currency"),
        )
        live = as_bool(values.get("live"), default=False)
        result = service.commit_transaction(
            payload,
            dry_run=not live,
            confirm_high_value=as_bool(values.get("yes_high_value"), default=False),
        )
        if result["status"] == "dry_run":
            remember_pending_transaction(state, payload)
            return BotResponse(
                format_transaction_preview(
                    payload,
                    intro="Dry-run only. Nothing was written.",
                    outro="Say 'commit it' or send the same request with live=yes to create it for real.",
                    source_text=text,
                )
            )
        if result["status"] == "duplicate_blocked":
            return BotResponse(format_duplicate_blocked(result["duplicate"], source_text=text))
        created = result.get("result", {})
        clear_pending_transaction(state)
        _remember_last_committed_txn(state, created, payload)
        return BotResponse(format_created_transaction_result(created, fallback_payload=payload, source_text=text))

    if command == "/newcategory":
        values = parse_kv_args(tail)
        name = values.get("name")
        if not name:
            return BotResponse("Missing required field. Example: /newcategory name=\"Bar\"")
        preview = format_pending_action_preview(
            "Category draft prepared.",
            [f"Category name: {name}"],
            "Say 'commit it' if you want me to create it.",
        )
        remember_pending_action(state, kind="category_create", payload={"name": name}, preview=preview)
        return BotResponse(preview)

    if command == "/newbudget":
        values = parse_kv_args(tail)
        name = values.get("name")
        if not name:
            return BotResponse("Missing required field. Example: /newbudget name=\"Vacation\"")
        preview = format_pending_action_preview(
            "Budget draft prepared.",
            [f"Budget name: {name}"],
            "Say 'commit it' if you want me to create it.",
        )
        remember_pending_action(state, kind="budget_create", payload={"name": name}, preview=preview)
        return BotResponse(preview)

    if command == "/newaccount":
        values = parse_kv_args(tail)
        name = values.get("name")
        account_type = values.get("type")
        if not name or not account_type:
            return BotResponse("Missing required fields. Example: /newaccount name=\"Wallet\" type=cash opening_balance=50")
        preview = format_pending_action_preview(
            "Account draft prepared.",
            [
                f"Account name: {name}",
                f"Account type: {account_type}",
                f"Opening balance: {values.get('opening_balance', 'not set')}",
            ],
            "Say 'commit it' if you want me to create it.",
        )
        remember_pending_action(
            state,
            kind="account_create",
            payload={
                "name": name,
                "account_type": account_type,
                "opening_balance": values.get("opening_balance"),
                "opening_balance_date": values.get("opening_balance_date") or values.get("date"),
            },
            preview=preview,
        )
        return BotResponse(preview)

    return BotResponse(bot_text("unsupported_command", source_text=text) + "\n\n" + commands_text(text))


def boot_state_validator(state: dict) -> int:
    """Validate state and clean orphaned drafts. Returns count of cleaned drafts."""
    import time
    if "drafts" not in state:
        state["drafts"] = {}
    cleaned = 0
    now = time.time()
    keys_to_delete = []

    for user_id_str, draft_data in state["drafts"].items():
        created_at = draft_data.get("created_at", 0)
        phase = draft_data.get("phase", "UNKNOWN")

        # If older than 24 hours and not committed, mark for deletion
        if (now - created_at) > 86400 and phase != "COMMITTED":
            keys_to_delete.append(user_id_str)
            cleaned += 1

    for key in keys_to_delete:
        del state["drafts"][key]
        log.info(f"Cleaned orphaned draft for user {key}")

    return cleaned


def build_health_message(service: BridgeService, *, source_text: str | None = None, prefix_key: str | None = None) -> str:
    selected_prefix = prefix_key
    try:
        payload = service.client.health()
        version = payload.get("data", {}).get("version") or payload.get("version") or "unknown"
        body = localize(
            f"Firefly bridge is healthy.\nVersion: {version}",
            f"Il bridge Firefly e sano.\nVersione: {version}",
            source_text=source_text,
        )
    except Exception as exc:
        if prefix_key == "startup_ok":
            selected_prefix = "startup_warn"
        body = localize(
            f"Firefly bridge health check failed: {exc}",
            f"Controllo salute Firefly bridge fallito: {exc}",
            source_text=source_text,
        )
    prefix = ""
    if selected_prefix == "live_ping_ok":
        prefix = bot_text_or_default("live_ping_ok", "Scheduled live check:", "Controllo programmato:", source_text=source_text)
    elif selected_prefix == "startup_ok":
        prefix = bot_text_or_default(
            "startup_ok",
            "Firefly bridge is healthy. Bot ready. /help shows what you can ask.",
            "Firefly bridge sano. Bot pronto. /help mostra cosa puoi chiedermi.",
            source_text=source_text,
        )
    elif selected_prefix == "startup_warn":
        prefix = bot_text_or_default(
            "startup_warn",
            "Firefly bridge started with a warning. /help shows what you can ask.",
            "Firefly bridge avviato con un avviso. /help mostra cosa puoi chiedermi.",
            source_text=source_text,
        )
    elif selected_prefix:
        prefix = bot_text_or_default(selected_prefix, "", "", source_text=source_text)
    return f"{prefix}\n{body}".strip()


def parse_live_ping_time(value: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", value.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def maybe_send_live_ping(bot_token: str, chat_id: str, service: BridgeService, state: dict[str, Any]) -> None:
    if not LIVE_PING_ENABLED:
        return
    scheduled = parse_live_ping_time(LIVE_PING_TIME)
    if scheduled is None:
        return
    now = datetime.now()
    hour, minute = scheduled
    if (now.hour, now.minute) < (hour, minute):
        return
    today_key = now.date().isoformat()
    if state.get("last_live_ping_date") == today_key:
        return
    send_message(bot_token, chat_id, build_health_message(service, source_text=configured_chat_language(), prefix_key="live_ping_ok"))
    state["last_live_ping_date"] = today_key
    save_state(state)


def main() -> int:
    bot_token = os.getenv("FIREFLY_TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    owner_id = os.getenv("FIREFLY_TELEGRAM_OWNER_ID", os.getenv("TELEGRAM_OWNER_ID", "")).strip()
    target_id = os.getenv("FIREFLY_TELEGRAM_TARGET_ID", os.getenv("TELEGRAM_TARGET_ID", owner_id)).strip() or owner_id
    if not bot_token or not owner_id:
        print("telegram_firefly_bot: TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_ID are required.", file=sys.stderr)
        return 0

    logger = configure_logging(False)
    settings = BridgeSettings.from_env()
    client = FireflyClient(settings=settings, logger=logger)
    service = BridgeService(client=client, settings=settings, logger=logger)
    try:
        ensure_telegram_commands(bot_token)
    except TelegramBotError as exc:
        print(f"telegram_firefly_bot: could not register bot commands: {exc}", file=sys.stderr)
    state = load_state()
    cleaned_count = boot_state_validator(state)
    if cleaned_count > 0:
        log.info(f"Cleaned {cleaned_count} orphaned drafts on startup")
        save_state(state)
    offset = int(state.get("offset", 0))
    initialized = bool(state.get("initialized", False))
    if STARTUP_HEALTHCHECK_ENABLED:
        try:
            send_message(bot_token, target_id, build_health_message(service, source_text=configured_chat_language(), prefix_key="startup_ok"))
        except TelegramBotError as exc:
            print(f"telegram_firefly_bot: startup health notification failed: {exc}", file=sys.stderr)

    while True:
        try:
            maybe_send_live_ping(bot_token, target_id, service, state)
            process_due_offline_retries(service, bot_token, state)
            payload = telegram_request(
                bot_token,
                "getUpdates",
                params={
                    "offset": offset,
                    "timeout": POLL_TIMEOUT_SECONDS,
                    "allowed_updates": json.dumps(["message"]),
                },
            )
            if not initialized:
                # On the very first run, we flush pending messages.
                results = payload.get("result", [])
                if results:
                    offset = max(int(update.get("update_id", 0)) + 1 for update in results)
                initialized = True
                state["offset"] = offset
                state["initialized"] = True
                save_state(state)
                continue
            for update in payload.get("result", []):
                update_id = int(update.get("update_id", 0))
                offset = max(offset, update_id + 1)
                state["offset"] = offset
                state["initialized"] = True
                save_state(state)

                message = update.get("message") or {}
                chat = message.get("chat") or {}
                sender = message.get("from") or {}
                text = message.get("text") or message.get("caption") or ""
                has_photo = bool(message.get("photo"))
                has_document = bool(message.get("document"))

                if not text and not has_photo and not has_document:
                    continue
                if str(sender.get("id", "")) != owner_id:
                    continue
                if chat.get("type") not in ALLOWED_PRIVATE_CHAT_TYPES:
                    continue

                # Show "typing..." action for slow operations
                try:
                    send_typing_action(bot_token, int(chat.get("id", 0)))
                except Exception:
                    pass

                try:
                    if has_photo:
                        response = process_receipt_message(service, bot_token, message, state)
                    elif has_document:
                        response = process_document_message(service, bot_token, message, state)
                    else:
                        response = process_message(service, text, state)
                except (ConfigurationError, FireflyAPIError, ValueError, RuntimeError) as exc:
                    if is_firefly_offline_error(exc) and text:
                        response = enqueue_offline_retry(
                            state,
                            text=text,
                            chat_id=str(chat.get("id")),
                            source_text=text,
                        )
                    else:
                        response = BotResponse(f"Request failed: {exc}")

                if response.photo_path:
                    send_photo(bot_token, str(chat.get("id")), response.photo_path, response.text)
                    try:
                        Path(response.photo_path).unlink(missing_ok=True)
                    except OSError:
                        pass
                elif response.document_path:
                    send_document(
                        bot_token,
                        str(chat.get("id")),
                        response.document_path,
                        response.text,
                        response.document_filename,
                    )
                    try:
                        Path(response.document_path).unlink(missing_ok=True)
                    except OSError:
                        pass
                else:
                    send_message(bot_token, str(chat.get("id")), response.text)
                save_state(state)

        except TelegramBotError as exc:
            print(f"telegram_firefly_bot: {exc}", file=sys.stderr)
            time.sleep(5)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:  # pragma: no cover - defensive long-running process
            print(f"telegram_firefly_bot unexpected error: {exc}", file=sys.stderr)
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
