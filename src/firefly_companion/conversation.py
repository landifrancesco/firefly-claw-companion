"""Conversation context, language detection, and localization.

This module provides a single ``ConversationContext`` that carries the
detected language through an entire request lifecycle so that all
response-building code can produce correctly-localized text without
ad-hoc ``source_text`` threading.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Text normalization
# ---------------------------------------------------------------------------

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
    ("ottobre ", "ottobre "),
)


def ascii_fold(value: str) -> str:
    """Remove diacritical marks from text."""
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def normalize_natural_text(text: str | None) -> str:
    """Normalize user text for matching: casefold, strip accents, fix typos."""
    normalized = ascii_fold((text or "").replace("\u2019", "'").replace("`", "'")).casefold().strip()
    # Collapse 3+ consecutive identical chars to 2 (handles typos like "agggiungi" → "aggiungi")
    normalized = re.sub(r"(.)\1{2,}", r"\1\1", normalized)
    for source, target in COMMON_TEXT_NORMALIZATIONS:
        normalized = normalized.replace(source, target)
    return " ".join(normalized.split())


def clip_text(value: str | None, limit: int = 280) -> str | None:
    """Truncate long text for display."""
    compact = " ".join((value or "").split())
    if not compact:
        return None
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def clean_free_text_slot(value: str | None) -> str | None:
    """Normalize free-text slots extracted from natural phrases.

    Removes trailing punctuation, collapses whitespace, and strips
    payment-mode suffixes like "in cash"/"contanti" that are not part
    of the semantic query.
    """
    if not value:
        return None
    cleaned = " ".join(value.rstrip(" .,!?:;").split())
    if not cleaned:
        return None
    cleaned = re.sub(r"\b(?:paid|made)\s+with\s+(?:cash|contanti)\b.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(?:in\s*cash|cash|contanti|i\s*n\s*cash)\b.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = " ".join(cleaned.strip(" .,!?:;").split())
    return cleaned or None


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

_ITALIAN_MARKERS = (
    "quanto", "quanti", "soldi", "spesa", "spese", "conto", "conti", "categoria",
    "categorie", "budget", "ricorren", "mostra", "fammi", "aggiungi", "crea", "saldo",
    "saldi", "caff", "pagat", "contanti", "entrate", "uscite", "guadagn",
    "supermercato", "mercato",
)


def explicit_language_hint(text: str | None) -> str | None:
    """Check for a literal language hint string (e.g. 'it', 'en')."""
    hint = (text or "").strip().casefold()
    if hint in {"it", "italian", "italiano", "lang:it", "locale:it"}:
        return "it"
    if hint in {"en", "english", "inglese", "lang:en", "locale:en"}:
        return "en"
    return None


def configured_chat_language() -> str:
    """Read the configured language from the environment. Returns 'auto', 'en', or 'it'."""
    value = os.getenv("FIREFLY_CHAT_LANGUAGE", "auto").strip().lower()
    return value if value in {"auto", "en", "it"} else "auto"


def detect_language(text: str | None) -> str:
    """Detect language from text content. Returns 'it' or 'en'."""
    configured = configured_chat_language()
    if configured in {"en", "it"}:
        return configured
    hinted = explicit_language_hint(text)
    if hinted:
        return hinted
    lowered = (text or "").casefold()
    if any(marker in lowered for marker in _ITALIAN_MARKERS):
        return "it"
    return "en"


def locale_language(source_text: str | None = None) -> str:
    """Determine the locale language for response generation."""
    configured = configured_chat_language()
    if configured in {"en", "it"}:
        return configured
    hinted = explicit_language_hint(source_text)
    if hinted:
        return hinted
    return detect_language(source_text)


# ---------------------------------------------------------------------------
# Locale catalog
# ---------------------------------------------------------------------------

# Default locale dir; overridden in tests or when workspace path changes.
_LOCALE_DIR: Path | None = None


def _default_locale_dir() -> Path:
    workspace = os.getenv("OPENCLAW_WORKSPACE", "")
    if workspace:
        candidate = Path(workspace) / "i18n"
        if candidate.exists():
            return candidate
    # Fallback to workspace directory relative to project root
    project_root = Path(__file__).resolve().parent.parent.parent
    return project_root / "workspace" / "i18n"


def get_locale_dir() -> Path:
    """Return the locale directory, auto-detecting if not explicitly set."""
    global _LOCALE_DIR
    if _LOCALE_DIR is None:
        _LOCALE_DIR = _default_locale_dir()
    return _LOCALE_DIR


def set_locale_dir(path: Path) -> None:
    """Override the locale directory (for testing)."""
    global _LOCALE_DIR
    _LOCALE_DIR = path


def load_locale_catalog(language: str) -> dict[str, Any]:
    """Load a locale catalog JSON file for the given language."""
    locale_dir = get_locale_dir()
    preferred = locale_dir / f"telegram_bot.{language}.json"
    fallback = locale_dir / "telegram_bot.en.json"
    for path in (preferred, fallback):
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    raise RuntimeError(f"Telegram bot locale files are missing in {locale_dir}.")


def locale_value(section: str, key: str, *, source_text: str | None = None) -> Any:
    """Look up a locale value by section and key."""
    language = locale_language(source_text)
    localized = load_locale_catalog(language)
    fallback_catalog = load_locale_catalog("en")
    if key in localized.get(section, {}):
        return localized[section][key]
    return fallback_catalog.get(section, {}).get(key)


def bot_text(key: str, *, source_text: str | None = None, **kwargs: Any) -> str:
    """Fetch a localized string from the locale catalog and format it."""
    value = locale_value("strings", key, source_text=source_text)
    if not isinstance(value, str):
        raise KeyError(f"Missing string locale key: {key}")
    return value.format(**kwargs)


def bot_list(key: str, *, source_text: str | None = None) -> list[str]:
    """Fetch a localized list from the locale catalog."""
    value = locale_value("lists", key, source_text=source_text)
    if not isinstance(value, list):
        raise KeyError(f"Missing list locale key: {key}")
    return [str(item) for item in value]


# ---------------------------------------------------------------------------
# Localize helper (backward-compatible)
# ---------------------------------------------------------------------------

def localize(text_en: str, text_it: str, *, source_text: str | None = None) -> str:
    """Return the Italian or English string based on detected language."""
    return text_it if locale_language(source_text) == "it" else text_en


# ---------------------------------------------------------------------------
# ConversationContext — the structured replacement for source_text threading
# ---------------------------------------------------------------------------

@dataclass
class ConversationContext:
    """Carries language and original text through an entire request lifecycle.

    This replaces the ad-hoc ``source_text`` parameter that was
    previously threaded through every function.
    """

    original_text: str = ""
    language: str = field(default="")

    def __post_init__(self) -> None:
        if not self.language:
            self.language = detect_language(self.original_text)

    def localized(self, *, en: str, it: str) -> str:
        """Return the text matching this context's language."""
        return it if self.language == "it" else en

    def bot_text(self, key: str, **kwargs: Any) -> str:
        """Fetch a locale-catalog string using this context's language."""
        return bot_text(key, source_text=self.original_text, **kwargs)


# ---------------------------------------------------------------------------
# Clarification helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Payment method keywords
# ---------------------------------------------------------------------------

PAYMENT_METHOD_KEYWORDS = {
    "it": {
        "card": ["carta", "credito", "debit", "bancomat"],
        "cash": ["contanti", "contante", "soldi", "cash"],
        "transfer": ["bonifico", "trasferimento"],
        "app": ["paypal", "revolut", "wise", "app"],
    },
    "en": {
        "card": ["card", "credit", "debit", "visa", "mastercard"],
        "cash": ["cash", "money", "soldi"],
        "transfer": ["transfer", "wire", "bonifico"],
        "app": ["paypal", "revolut", "wise", "app", "venmo"],
    },
}


def build_clarification_prompt(missing_field: str, *, source_text: str | None = None) -> tuple[str, list[str]]:
    """Build a clarification prompt for missing transaction field.

    Returns (prompt_text, button_options).
    """
    language = locale_language(source_text)

    if missing_field == "amount":
        prompt = localize("💰 How much?", "💰 Quanto?", source_text=source_text)
        options = ["€5", "€10", "€20", "€50", "€100"]
    elif missing_field == "account":
        prompt = localize("💳 Which account?", "💳 Quale conto?", source_text=source_text)
        options = ["Card", "Cash", "Savings"]
    elif missing_field == "category":
        prompt = localize("📁 Which category?", "📁 Quale categoria?", source_text=source_text)
        options = ["Groceries", "Transport", "Coffee"]
    elif missing_field == "date":
        prompt = localize("📅 What date?", "📅 Quale data?", source_text=source_text)
        options = ["Today", "Yesterday", "This week"]
    else:
        prompt = localize("❓ What do you mean?", "❓ Cosa intendi?", source_text=source_text)
        options = ["/help", "/add", "/cancel"]

    return prompt, options
