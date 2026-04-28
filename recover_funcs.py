with open('scripts/telegram_firefly_bot.py', 'a', encoding='utf-8') as f:
    f.write('''

def parse_amount_from_text(text: str) -> str | None:
    import re
    match = re.search(r"(?:^|\\s)(?:eur|€|euro)\\s*([0-9.,]+)(?:\\s|$)", text, re.IGNORECASE)
    if not match:
        match = re.search(r"(?:^|\\s)([0-9.,]+)\\s*(?:eur|€|euro)(?:\\s|$)", text, re.IGNORECASE)
    if not match:
        match = re.search(r"(?:^|\\s)([0-9]+[.,][0-9]{1,2})(?:\\s|$)", text)
    if not match:
        return None
    return match.group(1).replace(",", ".")

def build_guided_transaction_prompt(text: str) -> str | None:
    return None

def build_receipt_intro_reply(text: str) -> str:
    return text

def interpret_natural_command(text: str) -> str | None:
    from firefly_companion.conversation import normalize_natural_text
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
    
    from firefly_companion.date_parser import has_explicit_period_request
    explicit_period = has_explicit_period_request(lowered)
    
    from firefly_companion.intent_parser import contains_any
    
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
        return "/graph balances 30"
    return None
''')

with open('scripts/telegram_firefly_bot.py', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace('from firefly_companion.intent_parser import parse_natural_intent_payload, extract_recent_query, parse_natural_period_values, contains_any', 'from firefly_companion.intent_parser import parse_natural_intent_payload, extract_recent_query, contains_any\\nfrom firefly_companion.date_parser import parse_natural_period_values')

with open('scripts/telegram_firefly_bot.py', 'w', encoding='utf-8') as f:
    f.write(text)
