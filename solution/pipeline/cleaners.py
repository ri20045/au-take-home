"""Utility functions for cleaning and normalising raw input values."""

import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional, Sequence


def _strip_non_digits(value: str) -> str:
    """Return *value* with every non‑digit character removed.

    The function uses a regular expression to keep only ``0-9`` characters.
    It is deliberately tiny so that the main cleaning logic stays easy to
    read.
    """
    return re.sub(r"[^0-9]", "", value)


def clean_phone(raw_phone: str) -> Optional[str]:
    """Normalise an Indian mobile phone number.

    The assignment defines the following steps:

    1. Remove **all** non‑digit characters.
    2. If the resulting string has **12** digits and starts with ``91``
       (the country code), drop the leading ``91``.
    3. If it has **11** digits and starts with ``0`` (a local leading zero),
       drop the leading ``0``.
    4. The remaining string must be **exactly 10** digits and the first
       digit must be one of ``6, 7, 8, 9``.
    5. Return the canonical representation ``+91XXXXXXXXXX``.
    6. Return ``None`` if any rule is violated.

    Parameters
    ----------
    raw_phone:
        The phone number as it appears in the source CSV – it may contain
        spaces, parentheses, hyphens, a ``+`` sign, or a currency prefix.

    Returns
    -------
    Optional[str]
        The normalised phone number in ``+91XXXXXXXXXX`` format, or ``None``
        when the input is not a valid Indian mobile number.
    """
    # 1. Keep only digits.
    digits = _strip_non_digits(raw_phone)

    # 2. Strip a leading country code ``91`` when we have 12 digits.
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]

    # 3. Strip a leading zero when we have 11 digits.
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    # After the two possible reductions we must have exactly 10 digits.
    if len(digits) != 10:
        return None

    # 4. The first digit must be 6‑9.
    if digits[0] not in {"6", "7", "8", "9"}:
        return None

    # 5. Return the canonical format.
    return f"+91{digits}"


def clean_string(raw_value: str) -> Optional[str]:
    """Trim a string, returning ``None`` when it is empty."""
    value = raw_value.strip()
    return value or None


def clean_date(raw_value: str) -> Optional[str]:
    """Normalize a supported date to ``YYYY-MM-DD``.

    Accepted formats are ``YYYY-MM-DD``, ``DD/MM/YYYY``, ``DD-MM-YYYY``,
    and ``DD-Mon-YYYY``. Invalid or empty values return ``None``.
    """
    value = raw_value.strip()
    if not value:
        return None

    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value)
    if match:
        year, month, day = map(int, match.groups())
    else:
        match = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", value)
        if match:
            day, month, year = map(int, match.groups())
        else:
            match = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", value)
            if match:
                day, month, year = map(int, match.groups())
            else:
                match = re.fullmatch(r"(\d{2})-([A-Za-z]{3})-(\d{4})", value)
                if not match:
                    return None
                day_text, month_text, year_text = match.groups()
                month_names = {
                    "jan": 1,
                    "feb": 2,
                    "mar": 3,
                    "apr": 4,
                    "may": 5,
                    "jun": 6,
                    "jul": 7,
                    "aug": 8,
                    "sep": 9,
                    "oct": 10,
                    "nov": 11,
                    "dec": 12,
                }
                month = month_names.get(month_text.lower(), 0)
                day, year = int(day_text), int(year_text)

    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def clean_decimal(raw_value: str) -> Optional[str]:
    """Normalize a non-negative decimal to two places.

    A supported currency prefix (``₹``, ``Rs``, ``Rs.``, or ``INR``) and
    conventionally grouped thousands separators are removed before parsing.
    """
    value = raw_value.strip()
    if not value:
        return None

    value = re.sub(r"^(?:₹|rs\.?|inr)\s*", "", value, count=1, flags=re.IGNORECASE)
    number_pattern = r"-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?"
    if not re.fullmatch(number_pattern, value):
        return None

    try:
        amount = Decimal(value.replace(",", ""))
        if amount < 0:
            return None
        rounded = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None

    return format(rounded, ".2f")


def clean_enum(raw_value: str, allowed_values: Sequence[str]) -> Optional[str]:
    """Match an enum value case-insensitively and return its lowercase form."""
    value = raw_value.strip()
    if not value:
        return None

    allowed_lowercase = {allowed.lower() for allowed in allowed_values}
    normalized = value.lower()
    return normalized if normalized in allowed_lowercase else None
