"""Date and period parsing for the Firefly companion bot.

All date/period logic lives here. This module is fully deterministic —
no AI involvement. It handles Italian and English date phrases,
ISO-format dates, European-format dates, named months, ranges, and
relative periods.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any


DISPLAY_DATE_FORMAT = "%d-%m-%Y"
DATE_TOKEN_PATTERN = r"(?:\d{4}[-/.]\d{2}[-/.]\d{2}|\d{2}[-/.]\d{2}[-/.]\d{2,4})"

MONTH_NAME_TO_NUMBER = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
    "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
    "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}
MONTH_NAME_PATTERN = "|".join(
    sorted((re.escape(name) for name in MONTH_NAME_TO_NUMBER), key=len, reverse=True)
)

RELATIVE_PERIOD_PATTERN = re.compile(
    r"\b(?:ultim[io]|last)\s+(\d+)\s+(?:mes[ei]|months?)\b", re.IGNORECASE
)
SINCE_YEAR_START_PATTERN = re.compile(
    r"\b(?:dall inizio dell anno|since (?:the )?(?:start|beginning) of (?:the )?year|da inizio anno)\b",
    re.IGNORECASE,
)


def parse_flexible_date(value: str | date | datetime | None) -> date | None:
    """Parse a date from flexible string formats."""
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

    for fmt in (
        "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
        "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
        "%d/%m/%y", "%d.%m.%y", "%d-%m-%y",
    ):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def parse_date_safe(date_str: str) -> tuple[date | None, str]:
    """Parse date and detect ambiguity.

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


def format_display_date(value: str | date | datetime | None) -> str:
    """Format a date for user display as DD-MM-YYYY."""
    parsed = parse_flexible_date(value)
    if parsed is None:
        return str(value or "").strip() or "?"
    return parsed.strftime(DISPLAY_DATE_FORMAT)


def format_display_period(start: str | date | datetime, end: str | date | datetime) -> str:
    """Format a date range for user display."""
    return f"{format_display_date(start)} - {format_display_date(end)}"


def month_from_name(name: str) -> int | None:
    """Convert an Italian or English month name to a month number (1-12)."""
    return MONTH_NAME_TO_NUMBER.get(name.casefold().strip())


def month_window_from_label(month: str | None) -> tuple[date, date]:
    """Return (first_day, last_day) for a YYYY-MM label or current month if None."""
    if month:
        start = date.fromisoformat(f"{month}-01")
    else:
        start = date.today().replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1, day=1) - timedelta(days=1)
    else:
        end = start.replace(month=start.month + 1, day=1) - timedelta(days=1)
    return start, end


def recent_window(days: int) -> tuple[date, date]:
    """Return a (start, end) tuple for the last N days."""
    end = date.today()
    start = end - timedelta(days=max(days, 1) - 1)
    return start, end


def last_month_window() -> tuple[date, date]:
    """Return (first_day, last_day) for the previous calendar month."""
    first_this_month = date.today().replace(day=1)
    end = first_this_month - timedelta(days=1)
    start = end.replace(day=1)
    return start, end


def month_label_for_window(start: date, end: date) -> str:
    """Produce a display label for a date window."""
    return format_display_period(start, end)


def _normalize_for_period_parsing(text: str) -> str:
    """Normalize text for period parsing — strip punctuation, collapse whitespace."""
    lowered = re.sub(r"[^\w\s\-/.]", " ", text)
    return " ".join(lowered.split())


def parse_natural_period_values(text: str) -> dict[str, str]:
    """Parse time period from natural language text.

    Returns a dict that may contain:
    - ``{"month": "YYYY-MM"}`` for single-month requests
    - ``{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}`` for date ranges
    - ``{}`` if no period could be determined
    """
    from firefly_companion.conversation import normalize_natural_text

    lowered = _normalize_for_period_parsing(normalize_natural_text(text))
    current_year = date.today().year

    # 1. Explicit ISO or European date range: "dal 2026-03-01 al 2026-03-31"
    explicit_range = re.search(
        rf"\b(?:from|da|dal)\s+({DATE_TOKEN_PATTERN})\s+(?:to|al|ad|a|and)\s+({DATE_TOKEN_PATTERN})\b",
        lowered,
    )
    if explicit_range:
        start = parse_flexible_date(explicit_range.group(1))
        end = parse_flexible_date(explicit_range.group(2))
        if start and end:
            return {"from": start.isoformat(), "to": end.isoformat()}

    # 2. Named month range: "da gennaio a marzo 2026"
    month_range = re.search(
        rf"\b(?:from|da|dal|tra)\s+({MONTH_NAME_PATTERN})(?:\s+(\d{{4}}))?(?:\s+(?:to|al|ad|a|and|e)\s+|\s+vs\s+)({MONTH_NAME_PATTERN})(?:\s+(\d{{4}}))?",
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

    # 3. Year range: "dal 2025 al 2026"
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

    # 4. Specific month with preposition: "per febbraio 2026", "in marzo", "a aprile 2026"
    specific_month = re.search(
        rf"\b(?:for|per|in|a|during|durante|nel|nel mese di|mese di)\s+({MONTH_NAME_PATTERN})(?:\s+(\d{{4}}))?",
        lowered,
    )
    if specific_month:
        month_number = month_from_name(specific_month.group(1))
        year = int(specific_month.group(2) or current_year)
        if month_number:
            return {"month": f"{year:04d}-{month_number:02d}"}

    # 5. "month of X" / "mese di X" / standalone "Month YYYY"
    named_month = re.search(
        rf"\b(?:month of|mese di|in)\s+({MONTH_NAME_PATTERN})(?:\s+(\d{{4}}))?", lowered
    )
    if not named_month:
        named_month = re.search(rf"\b({MONTH_NAME_PATTERN})\s+(\d{{4}})\b", lowered)
    if named_month:
        month_number = month_from_name(named_month.group(1))
        year = int(named_month.group(2) or current_year)
        if month_number:
            return {"month": f"{year:04d}-{month_number:02d}"}

    # 6. Bare month name: "febbraio" (assume current year)
    bare_month = re.search(rf"\b({MONTH_NAME_PATTERN})\b", lowered)
    if bare_month:
        month_number = month_from_name(bare_month.group(1))
        if month_number:
            return {"month": f"{current_year:04d}-{month_number:02d}"}

    # 7. Relative: "last month" / "this month"
    if "last month" in lowered or "ultimo mese" in lowered or "scorso mese" in lowered:
        start, _ = last_month_window()
        return {"month": start.strftime("%Y-%m")}

    if "this month" in lowered or "questo mese" in lowered:
        return {"month": date.today().strftime("%Y-%m")}

    # 8. Relative N months: "ultimi 3 mesi" / "last 3 months"
    relative_match = RELATIVE_PERIOD_PATTERN.search(lowered)
    if relative_match:
        n_months = int(relative_match.group(1))
        today = date.today()
        end_date = today
        start_month = today.month - n_months
        start_year = today.year
        while start_month <= 0:
            start_month += 12
            start_year -= 1
        start_date = date(start_year, start_month, 1)
        return {"from": start_date.isoformat(), "to": end_date.isoformat()}

    # 9. "dall'inizio dell'anno" / "since the start of the year"
    if SINCE_YEAR_START_PATTERN.search(lowered):
        return {"from": f"{current_year}-01-01", "to": date.today().isoformat()}

    # 10. Full year: "nel 2026" / "in 2026"
    explicit_year = re.search(r"\b(?:for|per|in|nel|during|durante)\s+(20\d{2})\b", lowered)
    if explicit_year:
        year = int(explicit_year.group(1))
        return {"from": f"{year:04d}-01-01", "to": f"{year:04d}-12-31"}

    return {}


def coerce_period_values(values: dict[str, Any] | None) -> dict[str, str]:
    """Normalize a period dict, stripping whitespace and dropping None values."""
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
    """Resolve a period from a dict of values.

    Returns ``(start, end, display_label)``.
    """
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


def has_explicit_period_request(text: str) -> bool:
    """Check whether user text contains an explicit date/period reference."""
    from firefly_companion.conversation import normalize_natural_text

    lowered = normalize_natural_text(text)
    if re.search(rf"\b(?:\d{{4}}-\d{{2}}(?:-\d{{2}})?|{DATE_TOKEN_PATTERN})\b", lowered):
        return True
    if any(marker in lowered for marker in {
        " from ", " to ", " between ", " dal ", " al ", " ad ",
        " tra ", " fino al ", " until ",
    }):
        return True
    month_markers = set(MONTH_NAME_TO_NUMBER.keys())
    if any(marker in lowered for marker in month_markers):
        return True
    return bool(re.search(r"\b20\d{2}\b", lowered))
