import pytest
from solution.pipeline.cleaners import (
    clean_date,
    clean_decimal,
    clean_enum,
    clean_phone,
    clean_string,
)

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+91 79328 96340", "+917932896340"),
        ("(+91) 8196106462", "+918196106462"),
        ("06892038806", "+916892038806"),
        ("91-94758-33920", "+919475833920"),
        ("5546220212", None),  # invalid: starts with 5
        ("", None),
        ("+91-12345", None),  # too short
        ("+91 1234567890123", None),  # too long after stripping

        ("0 9123456789", "+919123456789"),
    ],
)
def test_clean_phone(raw, expected):
    assert clean_phone(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  Renewal customer  ", "Renewal customer"),
        ("", None),
        (" \t ", None),
    ],
)
def test_clean_string(raw, expected):
    assert clean_string(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-12-01", "2026-12-01"),
        ("01/12/2026", "2026-12-01"),
        ("01-12-2026", "2026-12-01"),
        ("01-Dec-2026", "2026-12-01"),
        ("01-dEC-2026", "2026-12-01"),
        ("31/02/2026", None),
        ("2026-13-01", None),
        ("12/01/2026", "2026-01-12"),
        ("", None),
    ],
)
def test_clean_date(raw, expected):
    assert clean_date(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("₹10,000", "10000.00"),
        ("Rs. 1,234.5", "1234.50"),
        ("rs100.25", "100.25"),
        ("INR 0", "0.00"),
        ("12.345", "12.35"),
        ("12.344", "12.34"),
        ("-1", None),
        ("1,00", None),
        ("₹", None),
        ("", None),
    ],
)
def test_clean_decimal(raw, expected):
    assert clean_decimal(raw) == expected


@pytest.mark.parametrize(
    "raw,allowed,expected",
    [
        (" HI ", ["en", "hi", "ta"], "hi"),
        ("EN", ["en", "hi", "ta"], "en"),
        ("fr", ["en", "hi", "ta"], None),
        ("", ["en", "hi", "ta"], None),
    ],
)
def test_clean_enum(raw, allowed, expected):
    assert clean_enum(raw, allowed) == expected
