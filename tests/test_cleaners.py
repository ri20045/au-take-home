import pytest
from solution.pipeline.cleaners import clean_phone

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
