import csv
import pathlib
from typing import List

import pytest

from solution.pipeline.schema import load_schema
from solution.pipeline.leads import ingest_lead_file, LeadKey

@pytest.fixture
def motor_schema():
    return load_schema("motor_renewal")

def write_csv(tmp_path, filename: str, header: List[str], rows: List[List[str]]):
    path = tmp_path / filename
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    return str(path)

def test_ingest_valid_and_duplicate(tmp_path, motor_schema):
    # Two rows: same phone (duplicate), second provides a missing language.
    header = ["customer_name", "phone", "policy_number", "policy_expiry", "premium_amount", "preferred_language"]
    rows = [
        ["Alice", "+91 79328 96340", "MOT/2026/001", "2026-12-10", "₹10,000", ""],
        ["Alice B", "+91 79328 96340", "MOT/2026/001", "10-12-2026", "10000", "hi"],
    ]
    csv_path = write_csv(tmp_path, "batch_001.csv", header, rows)
    leads, rejs = ingest_lead_file(csv_path, motor_schema)
    # No rejections
    assert rejs == []
    # One lead expected
    key: LeadKey = ("motor_renewal", "+917932896340")
    assert key in leads
    lead = leads[key]
    # Values from second row should have overwritten first where not null
    assert lead["customer_name"] == "Alice B"
    assert lead["policy_expiry"] == "10-12-2026"
    assert lead["premium_amount"] == "10000"
    # Language from second row (hi) kept, not default en
    assert lead["preferred_language"] == "hi"
    # Batch id should be from first file
    assert lead["batch_id"] == "batch_001"

def test_missing_required_field(tmp_path, motor_schema):
    header = ["customer_name", "phone", "policy_number"]
    rows = [["Bob", "", "MOT/2026/002"]]  # empty phone (required)
    csv_path = write_csv(tmp_path, "batch_002.csv", header, rows)
    leads, rejs = ingest_lead_file(csv_path, motor_schema)
    assert leads == {}
    assert rejs == [(1, "missing_required:phone")]

def test_invalid_phone(tmp_path, motor_schema):
    header = ["customer_name", "phone", "policy_number"]
    rows = [["Carol", "12345", "MOT/2026/003"]]
    csv_path = write_csv(tmp_path, "batch_003.csv", header, rows)
    leads, rejs = ingest_lead_file(csv_path, motor_schema)
    assert leads == {}
    assert rejs == [(1, "invalid_phone")]

def test_default_enum_applied(tmp_path, motor_schema):
    header = ["customer_name", "phone", "policy_number"]
    rows = [["Dave", "+91 66470 90063", "MOT/2026/004"]]  # no language column
    csv_path = write_csv(tmp_path, "batch_004.csv", header, rows)
    leads, rejs = ingest_lead_file(csv_path, motor_schema)
    assert rejs == []
    key: LeadKey = ("motor_renewal", "+916647090063")
    lead = leads[key]
    # Default language should be "en"
    assert lead["preferred_language"] == "en"
