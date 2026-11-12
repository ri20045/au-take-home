"""Utility functions for cleaning and normalising raw input values.

Only the phone‑cleaner is needed for the first task, but the module is
structured so that future cleaners (date, decimal, enum, …) can be added
without changing the public API.
"""

import re
from typing import Optional


def _strip_non_digits(value: str) -> str:
    """Return *value* with every non‑digit character removed.

    The function uses a regular expression to keep only ``0-9`` characters.
    It is deliberately tiny so that the main cleaning logic stays easy to
    read.
    """
    return re.sub(r"\D", "", value)


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
