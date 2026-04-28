"""Tests for the date_parser module."""
from __future__ import annotations

import unittest
from datetime import date

from firefly_companion.date_parser import (
    format_display_date,
    format_display_period,
    has_explicit_period_request,
    last_month_window,
    month_from_name,
    month_window_from_label,
    parse_flexible_date,
    parse_natural_period_values,
    period_from_values,
    recent_window,
)


class ParseFlexibleDateTest(unittest.TestCase):
    def test_iso_format(self) -> None:
        self.assertEqual(parse_flexible_date("2026-04-15"), date(2026, 4, 15))

    def test_european_format(self) -> None:
        self.assertEqual(parse_flexible_date("15-04-2026"), date(2026, 4, 15))

    def test_european_slash(self) -> None:
        self.assertEqual(parse_flexible_date("15/04/2026"), date(2026, 4, 15))

    def test_short_year(self) -> None:
        self.assertEqual(parse_flexible_date("15/04/26"), date(2026, 4, 15))

    def test_datetime_with_time(self) -> None:
        self.assertEqual(parse_flexible_date("2026-04-15T12:00:00+00:00"), date(2026, 4, 15))

    def test_none_returns_none(self) -> None:
        self.assertIsNone(parse_flexible_date(None))

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(parse_flexible_date(""))

    def test_date_object_passthrough(self) -> None:
        d = date(2026, 4, 15)
        self.assertEqual(parse_flexible_date(d), d)


class MonthHelperTest(unittest.TestCase):
    def test_italian_month_names(self) -> None:
        self.assertEqual(month_from_name("gennaio"), 1)
        self.assertEqual(month_from_name("Febbraio"), 2)
        self.assertEqual(month_from_name("marzo"), 3)
        self.assertEqual(month_from_name("dicembre"), 12)

    def test_english_month_names(self) -> None:
        self.assertEqual(month_from_name("january"), 1)
        self.assertEqual(month_from_name("December"), 12)

    def test_unknown_returns_none(self) -> None:
        self.assertIsNone(month_from_name("notamonth"))

    def test_month_window(self) -> None:
        start, end = month_window_from_label("2026-02")
        self.assertEqual(start, date(2026, 2, 1))
        self.assertEqual(end, date(2026, 2, 28))

    def test_month_window_december(self) -> None:
        start, end = month_window_from_label("2026-12")
        self.assertEqual(start, date(2026, 12, 1))
        self.assertEqual(end, date(2026, 12, 31))


class ParseNaturalPeriodValuesTest(unittest.TestCase):
    """Comprehensive tests for Italian+English natural date parsing."""

    def test_explicit_iso_range(self) -> None:
        params = parse_natural_period_values("dal 2026-03-01 al 2026-03-31")
        self.assertEqual(params["from"], "2026-03-01")
        self.assertEqual(params["to"], "2026-03-31")

    def test_explicit_european_range(self) -> None:
        params = parse_natural_period_values("from 01/03/2026 to 31/03/2026")
        self.assertEqual(params["from"], "2026-03-01")
        self.assertEqual(params["to"], "2026-03-31")

    def test_named_month_with_year(self) -> None:
        params = parse_natural_period_values("febbraio 2026")
        self.assertEqual(params["month"], "2026-02")

    def test_per_month_year(self) -> None:
        params = parse_natural_period_values("per febbraio 2026")
        self.assertEqual(params["month"], "2026-02")

    def test_in_month_year(self) -> None:
        params = parse_natural_period_values("in marzo 2026")
        self.assertEqual(params["month"], "2026-03")

    def test_a_month_year(self) -> None:
        params = parse_natural_period_values("a aprile 2026")
        self.assertEqual(params["month"], "2026-04")

    def test_nel_mese_di(self) -> None:
        params = parse_natural_period_values("nel mese di gennaio 2026")
        self.assertEqual(params["month"], "2026-01")

    def test_month_range_da_a(self) -> None:
        params = parse_natural_period_values("da gennaio a marzo 2026")
        self.assertEqual(params["from"], "2026-01-01")
        self.assertEqual(params["to"], "2026-03-31")

    def test_month_range_da_ad(self) -> None:
        params = parse_natural_period_values("da gennaio ad aprile 2026")
        self.assertEqual(params["from"], "2026-01-01")
        self.assertEqual(params["to"], "2026-04-30")

    def test_full_year_nel(self) -> None:
        params = parse_natural_period_values("nel 2026")
        self.assertEqual(params["from"], "2026-01-01")
        self.assertEqual(params["to"], "2026-12-31")

    def test_full_year_in(self) -> None:
        params = parse_natural_period_values("in 2026")
        self.assertEqual(params["from"], "2026-01-01")
        self.assertEqual(params["to"], "2026-12-31")

    def test_this_month_it(self) -> None:
        params = parse_natural_period_values("questo mese")
        self.assertEqual(params["month"], date.today().strftime("%Y-%m"))

    def test_this_month_en(self) -> None:
        params = parse_natural_period_values("this month")
        self.assertEqual(params["month"], date.today().strftime("%Y-%m"))

    def test_last_month_it(self) -> None:
        params = parse_natural_period_values("ultimo mese")
        start, _ = last_month_window()
        self.assertEqual(params["month"], start.strftime("%Y-%m"))

    def test_last_n_months(self) -> None:
        params = parse_natural_period_values("ultimi 3 mesi")
        self.assertIn("from", params)
        self.assertIn("to", params)

    def test_last_n_months_en(self) -> None:
        params = parse_natural_period_values("last 6 months")
        self.assertIn("from", params)
        self.assertIn("to", params)

    def test_since_year_start_it(self) -> None:
        params = parse_natural_period_values("dall'inizio dell'anno")
        year = date.today().year
        self.assertEqual(params["from"], f"{year}-01-01")
        self.assertEqual(params["to"], date.today().isoformat())

    def test_since_year_start_en(self) -> None:
        params = parse_natural_period_values("since the start of the year")
        year = date.today().year
        self.assertEqual(params["from"], f"{year}-01-01")

    def test_no_period_returns_empty(self) -> None:
        params = parse_natural_period_values("hello world")
        self.assertEqual(params, {})

    def test_month_range_cross_year(self) -> None:
        params = parse_natural_period_values("from november 2025 to february 2026")
        self.assertEqual(params["from"], "2025-11-01")
        self.assertEqual(params["to"], "2026-02-28")


class PeriodFromValuesTest(unittest.TestCase):
    def test_month_key(self) -> None:
        start, end, label = period_from_values({"month": "2026-03"})
        self.assertEqual(start, date(2026, 3, 1))
        self.assertEqual(end, date(2026, 3, 31))

    def test_from_to_keys(self) -> None:
        start, end, label = period_from_values({"from": "2026-01-01", "to": "2026-04-30"})
        self.assertEqual(start, date(2026, 1, 1))
        self.assertEqual(end, date(2026, 4, 30))

    def test_default_days(self) -> None:
        start, end, _ = period_from_values({}, default_days=7)
        self.assertEqual((end - start).days, 6)


class DisplayFormattingTest(unittest.TestCase):
    def test_display_date(self) -> None:
        self.assertEqual(format_display_date("2026-04-15"), "15-04-2026")

    def test_display_period(self) -> None:
        result = format_display_period("2026-04-01", "2026-04-15")
        self.assertEqual(result, "01-04-2026 - 15-04-2026")


class HasExplicitPeriodTest(unittest.TestCase):
    def test_iso_date(self) -> None:
        self.assertTrue(has_explicit_period_request("show me data from 2026-04-01 to 2026-04-15"))

    def test_month_name(self) -> None:
        self.assertTrue(has_explicit_period_request("spending in febbraio"))

    def test_no_period(self) -> None:
        self.assertFalse(has_explicit_period_request("show me my balance"))


if __name__ == "__main__":
    unittest.main()
