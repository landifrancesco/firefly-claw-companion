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


def contains_any(text: str, words: set[str] | tuple[str, ...]) -> bool:
    """Check if any target phrase exists in normalized text."""
    return any(word in text for word in words)


def parse_amount_from_text(text: str) -> str | None:
    match = re.search(r'(\d+(?:[.,]\d{1,2})?)\s*(?:â‚¬|eur|euro|usd|\$)?', text, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).replace(",", ".")


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
    budget_words = {"budget", "budget rimasto", "limite budget", "budget limit"}
    recurrence_words = {"ricorrenza", "ricorrenze", "recurrence", "recurrences", "ricorrenti", "ricorrente", "recurring"}

    params = parse_natural_period_values(lowered)
    wants_all_categories = contains_any(lowered, all_words) and contains_any(lowered, category_words)

    if contains_any(lowered, receipt_words) and contains_any(lowered, send_words):
        return {"intent": "clarify", "confidence": 0.98, "reply": "", "source_text": text, "params": {}}

    if contains_any(lowered, graph_words) and contains_any(lowered, budget_words):
        return {"intent": "graph_budget", "confidence": 0.9, "reply": "", "source_text": text, "params": {**params}}

    if contains_any(lowered, graph_words) and contains_any(lowered, recurrence_words):
        return {"intent": "graph_recurrences", "confidence": 0.9, "reply": "", "source_text": text, "params": {}}

    if contains_any(lowered, recurrence_words) and any(
        w in lowered for w in {"mostra", "lista", "list", "show", "elenca", "vedi", "fammi vedere", "quali"}
    ):
        return {"intent": "list_recurrences", "confidence": 0.88, "reply": "", "source_text": text, "params": {}}

    create_recurrence_words = {
        "add recurring", "create recurring", "schedule recurring",
        "aggiungi ricorrente", "crea ricorrenza", "imposta ricorrenza",
        "add a recurring", "add a monthly recurring", "add monthly", "add weekly", "add yearly",
        "crea una ricorrenza",
        "aggiungi una spesa ricorrente", "aggiungi un'entrata ricorrente",
    }
    delete_recurrence_words = {
        "delete recurring", "remove recurring", "cancel recurring",
        "elimina ricorrenza", "rimuovi ricorrenza", "cancella ricorrenza",
    }
    if any(phrase in lowered for phrase in create_recurrence_words):
        cadence = extract_recurrence(lowered)
        amount = parse_amount_from_text(lowered)
        if cadence and amount:
            income_signals = {"income", "salary", "stipendio", "entrata", "guadagno"}
            tx_kind = "deposit" if any(signal in lowered for signal in income_signals) else "withdrawal"
            description_match = re.search(r'\b(?:for|per)\s+([^\W\d_][\w _-]{1,40})', lowered)
            description = clean_free_text_slot(description_match.group(1)) if description_match else None
            return {
                "intent": "create_recurrence",
                "confidence": 0.85,
                "reply": "",
                "source_text": text,
                "params": {
                    "cadence": cadence,
                    "amount": str(amount),
                    "description": description or "",
                    "title": description or "",
                    "transaction_kind": tx_kind,
                    **params,
                },
            }

    if any(phrase in lowered for phrase in delete_recurrence_words):
        pass

    comparison_periods = parse_compare_period_params(text)
    if comparison_periods is not None:
        left_period, right_period = comparison_periods
        metric = "summary"
        if contains_any(lowered, category_words):
            metric = "top_spending_categories"
        elif contains_any(lowered, income_words) and any(word in lowered for word in {"spes", "spend", "uscite", "outgoing"}):
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

    if contains_any(lowered, income_words) and any(word in lowered for word in {"spes", "spend", "uscite", "outgoing", "entrate", "income", "earned", "guadagn"}):
        return {"intent": "get_income_vs_spending", "confidence": 0.95, "reply": "", "source_text": text, "params": {**params}}

    if contains_any(lowered, category_words) and contains_any(lowered, top_words):
        return {
            "intent": "top_spending_categories",
            "confidence": 0.95,
            "reply": "",
            "source_text": text,
            "params": {**params, "with_graph": contains_any(lowered, graph_words), "all_categories": wants_all_categories},
        }

    if params and contains_any(lowered, category_words):
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

    if contains_any(lowered, spending_words) and not any(word in lowered for word in {"add expense", "expense ", "spesa ", "paid ", "pagato", "comprato", "bought"}):
        return {"intent": "get_spending_total", "confidence": 0.9, "reply": "", "source_text": text, "params": {**params}}

    if contains_any(lowered, summary_words):
        return {"intent": "get_summary", "confidence": 0.8, "reply": "", "source_text": text, "params": {**params}}

    if contains_any(lowered, recent_words):
        query = extract_recent_query(text)
        if query or params or "recent" in lowered or "recenti" in lowered:
            payload_params = {**params}
            if query:
                payload_params["query"] = query
            return {"intent": "get_recent", "confidence": 0.86, "reply": "", "source_text": text, "params": payload_params}

    # --- Budget set detection ---
    budget_set_words = {
        "set", "imposta", "fissa", "aggiorna", "limit", "limite", "cap",
        "increase", "raise", "bump",
        "lower", "reduce", "decrease", "cut",
        "aumenta", "aumentare", "alza", "abbassa", "riduci", "diminuisci",
    }
    if contains_any(lowered, budget_set_words) and "budget" in lowered:
        amount = parse_amount_from_text(lowered)
        budget_name_match = re.search(r'budget\s+["\']?([^\W\d_][\w _-]{1,40}?)["\']?\s+(?:a|to|at|=|\s)', lowered)
        budget_name = clean_free_text_slot(budget_name_match.group(1)) if budget_name_match else None
        if amount:
            return {
                "intent": "set_budget_limit",
                "confidence": 0.82,
                "reply": "",
                "source_text": text,
                "params": {**params, "budget_name": budget_name, "amount": amount},
            }

    # --- List commands ---
    list_trigger_words = {"lista", "list", "mostra", "elenca", "show", "fammi vedere", "quali sono", "dimmi", "i miei", "my"}
    budget_list_words = {"budget", "budgets"}
    account_list_words = {"conto", "conti", "account", "accounts"}

    if contains_any(lowered, list_trigger_words) and contains_any(lowered, budget_list_words):
        return {"intent": "list_budgets", "confidence": 0.88, "reply": "", "source_text": text, "params": {}}

    if contains_any(lowered, list_trigger_words) and contains_any(lowered, account_list_words):
        return {"intent": "list_accounts", "confidence": 0.88, "reply": "", "source_text": text, "params": {}}

    if contains_any(lowered, list_trigger_words) and contains_any(lowered, category_words):
        return {"intent": "list_categories", "confidence": 0.88, "reply": "", "source_text": text, "params": {}}

    # --- Transfer detection ---
    transfer_words = {"transfer", "trasferimento", "trasferisci", "trasferire", "move", "sposta", "gira", "manda"}
    from_to_en = re.search(r'\bfrom\s+([^\W\d_][\w _-]{1,50}?)\s+to\s+([^\W\d_][\w _-]{1,50})(?:\s|$)', lowered)
    from_to_it = re.search(r'\bda\s+([^\W\d_][\w _-]{1,50}?)\s+(?:a|al|alla|nel)\s+([^\W\d_][\w _-]{1,50})(?:\s|$)', lowered)
    from_to = from_to_en or from_to_it
    if contains_any(lowered, transfer_words) and from_to:
        src = clean_free_text_slot(from_to.group(1)) or from_to.group(1).strip()
        dst = clean_free_text_slot(from_to.group(2)) or from_to.group(2).strip()
        return {
            "intent": "create_transfer",
            "confidence": 0.88,
            "reply": "",
            "source_text": text,
            "params": {
                "amount": parse_amount_from_text(lowered),
                "source": src,
                "destination": dst,
                "description": f"{src} â†’ {dst}",
            },
        }
    if contains_any(lowered, transfer_words):
        amount = parse_amount_from_text(lowered)
        if amount:
            source_match = re.search(r'\bfrom\s+([^\W\d_][\w _-]{1,50})(?:\s|$)', lowered) or re.search(
                r'\bda\s+([^\W\d_][\w _-]{1,50})(?:\s|$)',
                lowered,
            )
            destination_match = re.search(r'\bto\s+([^\W\d_][\w _-]{1,50})(?:\s|$)', lowered) or re.search(
                r'\b(?:a|al|alla|nel)\s+([^\W\d_][\w _-]{1,50})(?:\s|$)',
                lowered,
            )
            source = (clean_free_text_slot(source_match.group(1)) or source_match.group(1).strip()) if source_match else None
            destination = (clean_free_text_slot(destination_match.group(1)) or destination_match.group(1).strip()) if destination_match else None
            if source and destination:
                description = f"{source} -> {destination}"
            elif source:
                description = f"Transfer from {source}"
            elif destination:
                description = f"Transfer to {destination}"
            else:
                description = "Transfer"
            return {
                "intent": "create_transfer",
                "confidence": 0.82,
                "reply": "",
                "source_text": text,
                "params": {
                    "amount": amount,
                    "source": source or "",
                    "destination": destination or "",
                    "description": description,
                },
            }

    # --- Budget report detection ---
    budget_report_words = {"budgetreport", "reportbudget", "report budget", "budget report"}
    if any(phrase in lowered for phrase in budget_report_words):
        return {
            "intent": "budget_report",
            "confidence": 0.85,
            "reply": "",
            "source_text": text,
            "params": {**params},
        }

    # --- Search / find transaction detection ---
    find_words = {"trova", "cerca", "find", "look for"}
    if any(word in lowered for word in find_words):
        search_match = re.search(r'\b(?:trova|cerca|find|look\s+for)\s+(.+)', lowered)
        query = clean_free_text_slot(search_match.group(1)) if search_match else None
        if query:
            return {
                "intent": "search_transactions",
                "confidence": 0.80,
                "reply": "",
                "source_text": text,
                "params": {"query": query, **params},
            }

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

    if any(w in lowered for w in ["daily", "ogni giorno", "day", "al giorno", "giornaliero", "giornaliera"]):
        return "daily"
    elif any(w in lowered for w in ["weekly", "ogni settimana", "week", "a settimana", "settimanale"]):
        return "weekly"
    elif any(w in lowered for w in ["monthly", "ogni mese", "every month", "month", "al mese", "mensile"]):
        return "monthly"
    elif any(w in lowered for w in ["yearly", "ogni anno", "year", "annual", "annuale", "annualmente", "annuale"]):
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
