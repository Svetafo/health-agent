"""Tests for src/health/intake.py — pure logic, no DB or network."""

import pytest
from datetime import date, datetime
from decimal import Decimal

from src.health.intake import (
    normalize_decimal,
    normalize_int,
    normalize_sleep_min,
    parse_date,
    parse_datetime,
)


# ---------------------------------------------------------------------------
# normalize_decimal
# ---------------------------------------------------------------------------

class TestNormalizeDecimal:
    def test_none_returns_none(self):
        assert normalize_decimal(None) is None

    def test_empty_string_returns_none(self):
        assert normalize_decimal("") is None

    def test_whitespace_returns_none(self):
        assert normalize_decimal("   ") is None

    def test_integer(self):
        assert normalize_decimal(42) == Decimal("42")

    def test_float(self):
        assert normalize_decimal(3.14) == Decimal("3.14")

    def test_string_dot(self):
        assert normalize_decimal("3.14") == Decimal("3.14")

    def test_string_comma(self):
        """iPhone Russian locale: comma as decimal separator."""
        assert normalize_decimal("3,14") == Decimal("3.14")

    def test_string_comma_integer(self):
        assert normalize_decimal("100,0") == Decimal("100.0")

    def test_invalid_string_returns_none(self):
        assert normalize_decimal("abc") is None

    def test_zero(self):
        assert normalize_decimal(0) == Decimal("0")

    def test_string_with_spaces(self):
        assert normalize_decimal("  42  ") == Decimal("42")


# ---------------------------------------------------------------------------
# normalize_int
# ---------------------------------------------------------------------------

class TestNormalizeInt:
    def test_none_returns_none(self):
        assert normalize_int(None) is None

    def test_float_truncated(self):
        assert normalize_int("3.9") == 3

    def test_integer_string(self):
        assert normalize_int("100") == 100

    def test_comma_decimal(self):
        assert normalize_int("7,5") == 7


# ---------------------------------------------------------------------------
# normalize_sleep_min
# ---------------------------------------------------------------------------

class TestNormalizeSleepMin:
    def test_none_returns_none(self):
        assert normalize_sleep_min(None) is None

    def test_minutes_unchanged(self):
        """Values <= 1440 — already minutes."""
        assert normalize_sleep_min(480) == 480  # 8 hours

    def test_seconds_converted(self):
        """Values > 1440 — seconds, divide by 60."""
        assert normalize_sleep_min(28800) == 480  # 8 hours = 28800 sec

    def test_boundary_1440(self):
        """1440 minutes = exactly 24 hours — boundary, no conversion."""
        assert normalize_sleep_min(1440) == 1440

    def test_boundary_1441_converts(self):
        """1441 is already seconds."""
        assert normalize_sleep_min(1441) == 24  # 1441 // 60

    def test_string_seconds(self):
        assert normalize_sleep_min("3600") == 60  # 1 hour

    def test_zero(self):
        assert normalize_sleep_min(0) == 0

    def test_small_value(self):
        assert normalize_sleep_min(90) == 90  # 90 minutes


# ---------------------------------------------------------------------------
# parse_date
# ---------------------------------------------------------------------------

class TestParseDate:
    def test_iso_format(self):
        assert parse_date("2026-02-25") == date(2026, 2, 25)

    def test_russian_format(self):
        assert parse_date("07.03.2026") == date(2026, 3, 7)

    def test_us_format(self):
        assert parse_date("03/07/2026") == date(2026, 3, 7)

    def test_dict_shortcuts_format(self):
        """iOS Shortcuts wraps date: {'': '2026-02-25'}."""
        assert parse_date({"": "2026-02-25"}) == date(2026, 2, 25)

    def test_dict_any_key(self):
        assert parse_date({"date": "2026-02-25"}) == date(2026, 2, 25)

    def test_date_with_time_comma(self):
        """iOS sometimes sends '07.03.2026, 12:00' — take only the date part."""
        assert parse_date("07.03.2026, 12:00") == date(2026, 3, 7)

    def test_iso_with_time_comma(self):
        assert parse_date("2026-03-07, 12:00") == date(2026, 3, 7)

    def test_none_returns_today(self):
        result = parse_date(None)
        assert result == datetime.now().date()

    def test_empty_string_returns_today(self):
        result = parse_date("")
        assert result == datetime.now().date()

    def test_empty_dict_returns_today(self):
        result = parse_date({})
        assert result == datetime.now().date()

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_date("not-a-date")


# ---------------------------------------------------------------------------
# parse_datetime
# ---------------------------------------------------------------------------

class TestParseDatetime:
    def test_iso_with_timezone_colon(self):
        result = parse_datetime("2026-03-05T23:15:00+03:00")
        assert result == datetime(2026, 3, 5, 23, 15, 0)
        assert result.tzinfo is None  # naive

    def test_iso_with_timezone_no_colon(self):
        """Python 3.11 does not handle +0300 without colon."""
        result = parse_datetime("2026-03-05T23:15:00+0300")
        assert result == datetime(2026, 3, 5, 23, 15, 0)

    def test_iso_without_timezone(self):
        result = parse_datetime("2026-03-05T23:15:00")
        assert result == datetime(2026, 3, 5, 23, 15, 0)

    def test_none_returns_none(self):
        assert parse_datetime(None) is None

    def test_empty_returns_none(self):
        assert parse_datetime("") is None
