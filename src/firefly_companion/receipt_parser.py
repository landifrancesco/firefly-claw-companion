"""Receipt and screenshot parsing for the Firefly companion bot.

Handles OCR text extraction, multi-transaction counting, field
extraction with priority ranking, and amount validation. This module
is fully deterministic — AI vision is handled separately in ai_router.py.
"""
from __future__ import annotations

import os
import re
import shutil
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from firefly_companion.conversation import clip_text, normalize_natural_text

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


# ---------------------------------------------------------------------------
# Transaction counting (the critical missing step)
# ---------------------------------------------------------------------------

def count_visible_transactions(ocr_text: str, ai_vision_count: int | None = None) -> int:
    """Determine how many distinct transactions are visible in an OCR dump.

    This is the first step before field extraction. It prevents the
    system from merging multiple transactions into one.

    Rules:
    1. Count "Pagamento POS" occurrences
    2. Count distinct EUR/€ amount lines with different surrounding context
    3. Count distinct date stamps
    4. Use max(heuristic_count, ai_vision_count or 1)
    5. Never return 0; minimum is 1 if any financial content detected
    """
    if not ocr_text or not ocr_text.strip():
        return 0

    # Count POS payment blocks
    pos_count = len(re.findall(r"pagamento\s+pos", ocr_text, re.IGNORECASE))

    # Count distinct amount lines
    amount_lines = re.findall(
        r"(\d+[.,]\d{2})\s*(?:eur|euro|€)",
        ocr_text,
        re.IGNORECASE,
    )
    # Deduplicate amounts that appear on consecutive lines (same transaction)
    unique_amounts = list(dict.fromkeys(amount_lines))  # preserve order, remove dupes

    # Count Revolut blocks
    revolut_count = len(re.findall(
        r"revolut[\s:,-]*[^\n€]{2,80}?\s+\d+(?:[.,]\d{1,2})?\s*(?:eur|euro|€)",
        ocr_text,
        re.IGNORECASE,
    ))

    heuristic = max(pos_count, len(unique_amounts), revolut_count)

    # Consider AI vision count
    if ai_vision_count and ai_vision_count > heuristic:
        return ai_vision_count

    return max(heuristic, 1) if _has_financial_content(ocr_text) else 0


def _has_financial_content(text: str) -> bool:
    """Check if text contains any financial indicators."""
    lowered = text.casefold()
    indicators = {
        "eur", "euro", "€", "pagamento", "totale", "importo",
        "amount", "payment", "total", "receipt", "ricevuta",
    }
    return any(ind in lowered for ind in indicators)


# ---------------------------------------------------------------------------
# Amount validation
# ---------------------------------------------------------------------------

def validate_amount(amount_str: str | None) -> str | None:
    """Validate and normalize an extracted amount.

    Returns the normalized amount string, or None if invalid.
    Rejects 0.00, negative values, and unparseable strings.
    """
    if not amount_str:
        return None
    cleaned = amount_str.strip().replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if value <= 0:
        return None
    # Cap sanity check: amounts over 100,000 are suspicious from OCR
    if value > 100_000:
        return None
    from decimal import ROUND_HALF_UP
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Field extraction helpers
# ---------------------------------------------------------------------------

def detect_receipt_source_hint(text: str) -> str | None:
    """Detect a bank/provider name from receipt text."""
    lowered = normalize_natural_text(text)
    for keyword, display_name in RECEIPT_SOURCE_HINTS.items():
        if keyword in lowered:
            return display_name
    return None


def titlecase_merchant(name: str | None) -> str | None:
    """Clean and title-case a merchant name."""
    if not name:
        return None
    cleaned = re.sub(r"\s+", " ", name).strip(" .")
    if not cleaned:
        return None
    if cleaned.isupper() and len(cleaned) > 3:
        return cleaned.title()
    return cleaned


def parse_receipt_date(date_str: str) -> str | None:
    """Parse a date from receipt text and return ISO format."""
    from firefly_companion.date_parser import parse_flexible_date
    parsed = parse_flexible_date(date_str)
    return parsed.isoformat() if parsed else None


def infer_receipt_topic(text: str) -> dict[str, Any] | None:
    """Match receipt text against topic rules to infer category and description."""
    lowered = normalize_natural_text(text)
    for rule in RECEIPT_TOPIC_RULES:
        if any(kw in lowered for kw in rule["keywords"]):
            return rule
    return None


def infer_receipt_category(
    text: str,
    existing_categories: list[str] | None = None,
    *,
    language: str = "it",
) -> str | None:
    """Infer a category from receipt text, preferring existing Firefly categories."""
    topic = infer_receipt_topic(text)
    if not topic:
        return None

    candidate_names = list(topic.get("categories", ()))
    if existing_categories:
        for candidate in candidate_names:
            for existing in existing_categories:
                if candidate.casefold() == existing.casefold():
                    return existing
    return candidate_names[0] if candidate_names else None


def infer_receipt_description(text: str, *, language: str = "it") -> str:
    """Infer a description from receipt text using topic rules."""
    topic = infer_receipt_topic(text)
    if not topic:
        return "Pagamento" if language == "it" else "Payment"
    key = "description_it" if language == "it" else "description_en"
    return str(topic.get(key) or topic.get("description_en") or "Payment")


# ---------------------------------------------------------------------------
# Tesseract OCR
# ---------------------------------------------------------------------------

def find_tesseract_executable() -> str | None:
    """Locate the Tesseract binary."""
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


def run_receipt_ocr(image_bytes: bytes, *, mime_type: str = "image/jpeg") -> str | None:
    """Run Tesseract OCR on receipt image bytes.

    Returns extracted text or None if OCR fails.
    """
    import tempfile

    tesseract = find_tesseract_executable()
    if not tesseract:
        return None

    try:
        import subprocess

        ext = ".png" if "png" in mime_type else ".jpg"
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(image_bytes)
            tmp_path = tmp.name

        try:
            result = subprocess.run(
                [tesseract, tmp_path, "-", "-l", "ita+eng", "--psm", "6"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        finally:
            Path(tmp_path).unlink(missing_ok=True)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Candidate extraction (from OCR text)
# ---------------------------------------------------------------------------

def _build_receipt_candidate(
    *,
    source: str,
    amount: str,
    merchant: str | None,
    transaction_kind: str = "create_expense",
    transaction_date: str | None = None,
    source_hint: str | None = None,
    note_snippet: str | None = None,
) -> dict[str, Any]:
    """Build a structured receipt candidate dict."""
    topic = infer_receipt_topic(f"{merchant or ''} {source or ''}")
    topic_text = f"{merchant or ''} {source}"
    description = ""
    if topic:
        description = topic.get("description_it") or topic.get("description_en") or ""
    if not description and merchant:
        description = merchant

    notes: list[str] = []
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


def extract_receipt_candidates(text: str | None, *, caption: str | None = None) -> list[dict[str, Any]]:
    """Extract transaction candidates from OCR text.

    Returns a list of candidate dicts, each with:
    intent, amount, merchant, description, date, source_hint, notes
    """
    source = "\n".join(part for part in [caption, text] if part).strip()
    if not source:
        return []

    transaction_date = None
    date_match = re.search(r"\b(\d{2}[./]\d{2}[./]\d{2,4})\b", source)
    if date_match:
        transaction_date = parse_receipt_date(date_match.group(1))

    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def append_candidate(
        *, amount: str, merchant: str | None, source_hint: str | None,
        snippet: str, transaction_kind: str = "create_expense",
    ) -> None:
        clean_amount = validate_amount(amount)
        if not clean_amount:
            return
        clean_merchant = titlecase_merchant(merchant)
        key = (
            clean_amount,
            normalize_natural_text(clean_merchant or ""),
            normalize_natural_text(source_hint or ""),
        )
        if key in seen:
            return
        seen.add(key)
        candidates.append(
            _build_receipt_candidate(
                source=source,
                amount=clean_amount,
                merchant=clean_merchant,
                transaction_kind=transaction_kind,
                transaction_date=transaction_date,
                source_hint=source_hint,
                note_snippet=snippet,
            )
        )

    # 1. POS payment blocks (strongest signal)
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
        )

    # 2. Revolut blocks
    for match in re.finditer(
        r"revolut[\s:,-]*([^\n€]{2,80}?)\s+(\d+(?:[.,]\d{1,2})?)\s*(?:eur|euro|€)",
        source,
        re.IGNORECASE,
    ):
        snippet = match.group(0)
        append_candidate(
            amount=match.group(2),
            merchant=match.group(1).strip(" ."),
            source_hint="Revolut",
            snippet=snippet,
        )

    # 3. Generic EUR amount lines (weaker)
    if not candidates:
        normalized = normalize_natural_text(source)
        lines = [line.strip() for line in source.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            amount_match = re.search(r"(\d+(?:[.,]\d{1,2})?)\s*(?:eur|euro|€)\b", line, re.IGNORECASE)
            if not amount_match:
                continue
            nearby = "\n".join(lines[max(index - 2, 0): min(index + 2, len(lines))])
            source_hint = detect_receipt_source_hint(nearby) or detect_receipt_source_hint(source)
            merchant = None
            for candidate_line in reversed(lines[max(index - 2, 0): index + 1]):
                folded = normalize_natural_text(candidate_line)
                if folded in IGNORED_RECEIPT_MERCHANT_LINES:
                    continue
                if any(char.isalpha() for char in candidate_line) and "eur" not in folded:
                    merchant = candidate_line
                    break
            if merchant and source_hint and normalize_natural_text(merchant) == normalize_natural_text(source_hint):
                merchant = lines[index - 1] if index > 0 else merchant
            if merchant:
                append_candidate(
                    amount=amount_match.group(1),
                    merchant=merchant,
                    source_hint=source_hint,
                    snippet=nearby,
                )

    # 4. Fallback: single receipt extraction
    if not candidates:
        single = _extract_single_receipt_candidate(text, caption=caption)
        if single:
            candidates.append(single)

    return candidates


def _extract_single_receipt_candidate(text: str | None, *, caption: str | None = None) -> dict[str, Any] | None:
    """Extract a single transaction from a receipt (fallback path)."""
    source = "\n".join(part for part in [caption, text] if part).strip()
    if not source:
        return None

    normalized = normalize_natural_text(source)
    lines = [clip_text(line, limit=120) for line in source.splitlines()]
    compact_lines = [line for line in lines if line and line.strip()]

    provider_match = detect_receipt_source_hint(source)

    merchant = None
    amount = None
    transaction_kind = "create_expense"

    # Revolut format
    revolut_match = re.search(
        r"revolut\s+([^\n]+?)\s+(\d+(?:[.,]\d{1,2})?)\s*(?:eur|euro)?",
        normalized, re.IGNORECASE,
    )
    if revolut_match:
        merchant = titlecase_merchant(revolut_match.group(1))
        amount = revolut_match.group(2).replace(",", ".")

    # BPER POS format
    if amount is None:
        bper_match = re.search(
            r"pagamento pos di\s+(\d+(?:[.,]\d{1,2})?)\s*(?:eur|euro)\s+presso\s+(.+?)(?:\s+tocca|\s*$)",
            normalized, re.IGNORECASE,
        )
        if bper_match:
            amount = bper_match.group(1).replace(",", ".")
            merchant = titlecase_merchant(bper_match.group(2))

    # Receipt total patterns (priority ordered)
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

    # Merchant from "presso"
    if merchant is None:
        merchant_match = re.search(r"presso\s+(.+?)(?:\s+tocca|\s*$)", normalized, re.IGNORECASE)
        if merchant_match:
            merchant = titlecase_merchant(merchant_match.group(1))

    if merchant is None and "supermercato in" in normalized:
        merchant = "Supermercato In's"

    # Merchant from context lines
    if merchant is None:
        for line in compact_lines:
            folded = normalize_natural_text(line)
            if any(kw in folded for kw in {"supermercato", "mercato", "coop", "carrefour", "esselunga", "argenta"}):
                merchant = titlecase_merchant(line)
                break
            if folded in IGNORED_RECEIPT_MERCHANT_LINES:
                continue
            if len(folded) >= 4 and any(char.isalpha() for char in folded):
                merchant = titlecase_merchant(line)
                break

    # Date
    date_match = re.search(r"\b(\d{2}[./]\d{2}[./]\d{2,4})\b", normalized)
    transaction_date = parse_receipt_date(date_match.group(1)) if date_match else None

    # Income detection
    if any(kw in normalized for kw in {"accredito", "bonifico ricevuto", "salary", "stipendio", "received payment"}):
        transaction_kind = "create_income"

    # Generic EUR amount fallback
    if amount is None:
        generic_amounts = re.findall(r"\b(\d+(?:[.,]\d{1,2})?)\s*(?:eur|euro)\b", normalized, re.IGNORECASE)
        if generic_amounts:
            amount = generic_amounts[-1].replace(",", ".")

    # Validate amount
    amount = validate_amount(amount)
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


def extract_receipt_candidate(text: str | None, *, caption: str | None = None) -> dict[str, Any] | None:
    """Extract a single receipt candidate (convenience wrapper)."""
    candidates = extract_receipt_candidates(text, caption=caption)
    return candidates[0] if candidates else None
