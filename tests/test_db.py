import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from solution.pipeline.schema import load_schema
from solution.pipeline.leads import ingest_lead_file
from solution.pipeline.db import init_db, insert_leads, get_lead_by_phone, hash_pii

# Use a deterministic secret for tests
TEST_SECRET = "test-secret-key"

@pytest.fixture(autouse=True)
def set_secret(monkeypatch):
    monkeypatch.setenv("AGY_PII_KEY", TEST_SECRET)

@pytest.fixture
def motor_schema():
    return load_schema("motor_renewal")

def write_simple_csv(tmp_path, rows):
    header = ["customer_name", "phone", "policy_number", "preferred_language"]
    path = tmp_path / "batch_001.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        f.write(",".join(header) + "\n")
        for r in rows:
            f.write(",".join(r) + "\n")
    return str(path)

def test_db_insert_and_fetch(tmp_path, motor_schema):
    csv_path = write_simple_csv(
        tmp_path,
        [
            ["Alice", "+91 79328 96340", "MOT/2026/001", ""],
        ],
    )
    leads, _ = ingest_lead_file(csv_path, motor_schema)
    db_file = tmp_path / "test.db"
    conn = init_db(str(db_file), motor_schema)
    insert_leads(conn, leads, motor_schema)

    # Retrieve directly via helper
    lead = get_lead_by_phone(conn, "motor_renewal", "+917932896340")
    assert lead is not None
    # Verify stored columns exist
    assert lead["workflow_id"] == "motor_renewal"
    assert lead["batch_id"] == "batch_001"
    # Phone is stored as hash, not raw value
    assert lead["phone_hash"] == hash_pii("+917932896340")
    # PII field customer_name should be stored as hash as well
    assert lead["customer_name_hash"] == hash_pii("Alice")
    # Non‑PII field preferred_language (default applied) should be stored as plain text
    assert lead["preferred_language"] == "en"

def test_idempotent_insert(tmp_path, motor_schema):
    csv_path = write_simple_csv(
        tmp_path,
        [
            ["Bob", "+91 66470 90063", "MOT/2026/002", "hi"],
        ],
    )
    leads, _ = ingest_lead_file(csv_path, motor_schema)
    db_file = tmp_path / "test2.db"
    conn = init_db(str(db_file), motor_schema)
    insert_leads(conn, leads, motor_schema)
    # Insert the same leads again – should not raise and should keep same values
    insert_leads(conn, leads, motor_schema)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM leads")
    count = cur.fetchone()[0]
    assert count == 1
