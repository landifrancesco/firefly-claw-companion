"""Natural language intent parsing.

Extracts structured intents from user text using deterministic rules.
Provides the first tier of natural language understanding before
falling back to the AI router.
"""
from __future__ import annotations

import re
from typing import Any

from firefly_companion.conversation import clean_free_text_slot, normalize_natural_text
from firefly_companion.date_parser import parse_natural_period_values


def contains_any(text: str, words: set[str]) -> bool:
    """Check if any of the target words exist in the text as full words or prefixes."""
    for word in words:
        if re.search(rf"\b{word}", text):
            return True
    return False


def extract_recent_query(text: str) -> str | None:
    """Extract the search query parameter from a 'recent transactions' request."""
    lowered = normalize_natural_text(text)
    date_token = r"(?:\d{4}-\d{2}-\d{2}|\d{2}[./]\d{2}[./]\d{2,4})"
    patterns = (
        rf"\bmovimenti\s+(.+?)(?:\s+(?:dal|da|from)\s+{date_token}|$)",
        rf"\bmostrami\s+(?:i\s+)?movimenti\s+(.+?)(?:\s+(?:dal|da|from)\s+{date_token}|$)",
        rf"\btransactions?\s+(.+?)(?:\s+(?:from|between)\s+{date_token}|$)",
        rf"\b(?:recent|recenti)\s+(?:transactions?|movimenti)\s+(.+?)(?:\s+(?:from|between|dal|da)\s+{date_token}|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if not match:
            continue
        raw_query = match.group(1).strip()
        query = re.sub(r"^(?:di|dei|delle|del|dello|della|the|for|su|sui|sulle|i|gli|le|il|la)\s+", "", raw_query)
        query = re.sub(rf"\s+(?:dal|da|from|between)\s+{date_token}.*$", "", query)
        query = clean_free_text_slot(query) or ""
        if query:
            return query
    return None


def parse_compare_period_params(text: str) -> tuple[dict[str, str], dict[str, str]] | None:
    lowered = normalize_natural_text(text)
    if not re.search(r"\b(?:vs|versus)\b", lowered):
        return None
    parts = re.split(r"\b(?:vs|versus)\b", lowered, maxsplit=1)
    if len(parts) != 2:
        return None
    left_period = parse_natural_period_values(parts[0].strip())
    right_period = parse_natural_period_values(parts[1].strip())
    if not left_period or not right_period:
        return None
    return left_period, right_period


def parse_natural_intent_payload(text: str) -> dict[str, Any] | None:
    """Parse a natural language query into a structured bot command payload.

    This function attempts to deterministically classify the user's intent.
    If it succeeds, it returns a dict with `intent` and `params`.
    If it fails, it returns None, and the router will try the AI model.
    """
    lowered = normalize_natural_text(text)
    if not lowered:
        return None

    graph_words = {"graph", "chart", "grafico", "grafica"}
    category_words = {"category", "categories", "categoria", "categorie"}
    top_words = {"top", "most", "used", "usate", "usati", "speso", "spent", "piu"}
    balance_words = {"money i have", "balances", "balance", "saldo", "saldi", "net worth", "composition"}
    spending_words = {"how much did i spend", "spent", "spending", "expenses", "expense", "quanto ho speso", "speso"}
    income_words = {"earned", "income", "entrate", "entrata", "guadagnato", "guadagnati", "guadagno"}
    spending_side_words = {"spes", "spend", "uscite", "outgoing", "speso"}
    cashflow_words = {"cashflow", "income vs", "incoming", "outgoing", "flusso di cassa"}
    summary_words = {"summary", "riepilogo", "report", "resoconto"}
    all_words = {"all", "tutte", "tutti"}
    recent_words = {"recent", "recenti", "movimento", "movimenti", "transaction", "transactions", "transazione", "transazioni"}
    receipt_words = {"receipt", "ricevuta", "scontrino", "bank screenshot", "screenshot banca", "foto ricevuta", "foto scontrino"}
    send_words = {"send", "sending", "ti mando", "mando", "invio", "inoltro"}
    show_words = {"mostra", "mostrami", "fammi vedere", "show", "show me", "elenca", "list"}
    list_trigger_words = {"lista", "list", "mostra", "elenca", "show", "fammi vedere", "quali sono", "dimmi", "cosa sono", "i miei", "my"}

    params = parse_natural_period_values(lowered)
    wants_all_categories = contains_any(lowered, all_words) and contains_any(lowered, category_words)

    # --- Receipt preamble (highest priority) ---
    if contains_any(lowered, receipt_words) and contains_any(lowered, send_words):
        return {"intent": "clarify", "confidence": 0.98, "reply": "", "source_text": text, "params": {}}

    comparison_periods = parse_compare_period_params(text)
    if comparison_periods is not None:
        left_period, right_period = comparison_periods
        metric = "summary"
        has_income_cmp = contains_any(lowered, income_words) or "entrate" in lowered
        has_spending_cmp = any(word in lowered for word in spending_side_words) or "uscite" in lowered
        if contains_any(lowered, category_words):
            metric = "top_spending_categories"
        elif has_income_cmp and has_spending_cmp:
            metric = "income_vs_spending"
        elif contains_any(lowered, spending_words):
            metric = "spending_total"
        return {
            "intent": "compare_periods",
            "confidence": 0.88,
            "reply": "",
            "source_text": text,
            "params": {
                "metric": metric,
                "left_period": left_period,
                "right_period": right_period,
            },
        }

    # --- Income vs Spending ---
    has_income_signal = contains_any(lowered, income_words) or "entrate" in lowered
    has_spending_signal = any(word in lowered for word in spending_side_words) or "uscite" in lowered
    if has_income_signal and has_spending_signal:
        return {"intent": "get_income_vs_spending", "confidence": 0.95, "reply": "", "source_text": text, "params": {**params}}

    if contains_any(lowered, cashflow_words) and not contains_any(lowered, graph_words):
        if "entrate" in lowered and "uscite" in lowered:
            return {"intent": "get_income_vs_spending", "confidence": 0.93, "reply": "", "source_text": text, "params": {**params}}

    # --- Recent queries (before category checks) ---
    if contains_any(lowered, recent_words):
        query = extract_recent_query(text)
        if query or params or "recent" in lowered or "recenti" in lowered:
            payload_params = {**params}
            if query:
                payload_params["query"] = query
            return {"intent": "get_recent", "confidence": 0.86, "reply": "", "source_text": text, "params": payload_params}

    # --- Category queries ---
    if contains_any(lowered, category_words) and contains_any(lowered, top_words):
        return {
            "intent": "top_spending_categories",
            "confidence": 0.95,
            "reply": "",
            "source_text": text,
            "params": {**params, "with_graph": contains_any(lowered, graph_words), "all_categories": wants_all_categories},
        }

    # "categorie di marzo 2026"
    if contains_any(lowered, category_words) and params:
        return {
            "intent": "top_spending_categories",
            "confidence": 0.88,
            "reply": "",
            "source_text": text,
            "params": {**params, "with_graph": contains_any(lowered, graph_words), "all_categories": wants_all_categories},
        }

    if contains_any(lowered, graph_words) and contains_any(lowered, category_words):
        return {
            "intent": "top_spending_categories",
            "confidence": 0.9,
            "reply": "",
            "source_text": text,
            "params": {**params, "with_graph": True, "all_categories": wants_all_categories},
        }

    # --- Graph queries ---
    if contains_any(lowered, graph_words) and contains_any(lowered, balance_words):
        return {"intent": "graph_balances", "confidence": 0.9, "reply": "", "source_text": text, "params": {**params}}

    if contains_any(lowered, graph_words) and contains_any(lowered, cashflow_words):
        return {"intent": "graph_cashflow", "confidence": 0.9, "reply": "", "source_text": text, "params": {**params}}

    if contains_any(lowered, graph_words) and ("spes" in lowered or "spend" in lowered):
        return {"intent": "graph_spending", "confidence": 0.9, "reply": "", "source_text": text, "params": {**params}}

    # --- Spending total ---
    if contains_any(lowered, spending_words) and not any(word in lowered for word in {"add expense", "expense ", "spesa ", "paid ", "pagato", "comprato", "bought"}):
        return {"intent": "get_spending_total", "confidence": 0.9, "reply": "", "source_text": text, "params": {**params}}

    # --- Summary ---
    if contains_any(lowered, summary_words):
        return {"intent": "get_summary", "confidence": 0.8, "reply": "", "source_text": text, "params": {**params}}

    # --- List commands ---
    budget_list_words = {"budget", "budgets"}
    account_list_words = {"conto", "conti", "account", "accounts"}
    category_list_words = category_words

    if contains_any(lowered, list_trigger_words) and contains_any(lowered, budget_list_words):
        return {"intent": "list_budgets", "confidence": 0.88, "reply": "", "source_text": text, "params": {}}

    if contains_any(lowered, list_trigger_words) and contains_any(lowered, account_list_words):
        return {"intent": "list_accounts", "confidence": 0.88, "reply": "", "source_text": text, "params": {}}

    if contains_any(lowered, list_trigger_words) and contains_any(lowered, category_list_words):
        return {"intent": "list_categories", "confidence": 0.88, "reply": "", "source_text": text, "params": {}}

    return None


def extract_amount(text: str) -> float | None:
    """Extract amount from text. Returns float or None."""
    lowered = normalize_natural_text(text)
    # Match patterns like "5", "12.50", "€10", "10 euro"
    match = re.search(r"(\d+(?:[.,]\d{2})?)\s*(?:€|euro|euros)?", lowered)
    if match:
        try:
            amount_str = match.group(1).replace(",", ".")
            return float(amount_str)
        except ValueError:
            return None
    return None


def extract_date(text: str) -> str | None:
    """Extract date from text. Returns date string or None."""
    # Uses existing date parser
    from firefly_companion.date_parser import parse_flexible_date
    result = parse_flexible_date(text)
    return str(result) if result else None


def extract_payment_method(text: str, locale: str = "en") -> str | None:
    """Extract payment method from text.

    Returns method (card, cash, transfer, app) or None.
    """
    from firefly_companion.conversation import PAYMENT_METHOD_KEYWORDS

    text_lower = normalize_natural_text(text)
    keywords = PAYMENT_METHOD_KEYWORDS.get(locale, {})

    for method, words in keywords.items():
        if any(word in text_lower for word in words):
            return method

    return None


def extract_recurrence(text: str) -> str | None:
    """Extract recurrence pattern from text.

    Returns recurrence type (daily, weekly, monthly, yearly) or None.
    """
    lowered = normalize_natural_text(text)

    if any(w in lowered for w in ["daily", "ogni giorno", "day", "al giorno"]):
        return "daily"
    elif any(w in lowered for w in ["weekly", "ogni settimana", "week", "a settimana"]):
        return "weekly"
    elif any(w in lowered for w in ["monthly", "ogni mese", "every month", "month", "al mese"]):
        return "monthly"
    elif any(w in lowered for w in ["yearly", "ogni anno", "year", "annual", "annuale", "annualmente"]):
        return "yearly"

    return None


def fuzzy_match_category(description: str, categories: list[str], threshold: float = 0.6) -> str | None:
    """Fuzzy-match description to a category name.

    Uses SequenceMatcher to find best match above threshold.
    Returns category name or None.
    """
    from difflib import SequenceMatcher

    desc_lower = description.casefold().strip()
    if not desc_lower:
        return None

    best_match = None
    best_score = 0.0

    for category in categories:
        cat_lower = category.casefold().strip()
        if not cat_lower:
            continue

        # Exact substring = 100%
        if desc_lower in cat_lower or cat_lower in desc_lower:
            return category

        # Fuzzy match
        score = SequenceMatcher(None, desc_lower, cat_lower).ratio()
        if score > best_score:
            best_score = score
            best_match = category

    return best_match if best_score >= threshold else None


def parse_deterministic_with_fallback(text: str) -> tuple[bool, dict[str, Any] | None, str | None]:
    """Parse text and detect what's missing.

    Returns (success, result, missing_field).
    - If success=True, either result is not None (parse succeeded) or missing_field is set
    - If success=False, parsing failed and we need user input on missing_field
    """
    result = parse_natural_intent_payload(text)
    if result is not None:
        return True, result, None

    # Check what's missing
    if not extract_amount(text):
        return False, None, "amount"
    return False, None, None
