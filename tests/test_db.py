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
    leads, _, _ = ingest_lead_file(csv_path, motor_schema)
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
    leads, _, _ = ingest_lead_file(csv_path, motor_schema)
    db_file = tmp_path / "test2.db"
    conn = init_db(str(db_file), motor_schema)
    insert_leads(conn, leads, motor_schema)
    # Insert the same leads again – should not raise and should keep same values
    insert_leads(conn, leads, motor_schema)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM leads")
    count = cur.fetchone()[0]
    assert count == 1


def test_reingest_preserves_original_batch_and_existing_values(
    tmp_path, motor_schema
):
    conn = init_db(str(tmp_path / "merge_existing.db"), motor_schema)
    phone = "+917932896340"
    first_lead = {
        ("motor_renewal", phone): {
            "workflow_id": "motor_renewal",
            "batch_id": "batch_first",
            "phone": phone,
            "customer_name": "Alice",
            "policy_number": "MOT/001",
            "policy_expiry": "2026-12-01",
            "premium_amount": "12000.00",
            "preferred_language": "en",
        }
    }
    later_lead = {
        ("motor_renewal", phone): {
            "workflow_id": "motor_renewal",
            "batch_id": "batch_later",
            "phone": phone,
            "customer_name": None,
            "policy_number": "MOT/002",
            "policy_expiry": None,
            "premium_amount": None,
            "preferred_language": None,
        }
    }

    insert_leads(conn, first_lead, motor_schema)
    insert_leads(conn, later_lead, motor_schema)
    stored = get_lead_by_phone(conn, "motor_renewal", phone)

    assert stored["batch_id"] == "batch_first"
    assert stored["customer_name_hash"] == hash_pii("Alice")
    assert stored["policy_number"] == "MOT/002"
    assert stored["policy_expiry"] == "2026-12-01"
    assert stored["premium_amount"] == "12000.00"
    assert stored["preferred_language"] == "en"


def test_stored_lead_pii_is_not_plaintext(tmp_path, motor_schema):
    conn = init_db(str(tmp_path / "pii.db"), motor_schema)
    raw_name = "Customer Private Name"
    raw_phone = "+917932896340"
    leads = {
        ("motor_renewal", raw_phone): {
            "workflow_id": "motor_renewal",
            "batch_id": "batch_pii",
            "phone": raw_phone,
            "customer_name": raw_name,
            "policy_number": "MOT/PII",
            "policy_expiry": None,
            "premium_amount": None,
            "preferred_language": "en",
        }
    }

    insert_leads(conn, leads, motor_schema)

    for table_name, in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'"
    ):
        columns = [
            row[1] for row in conn.execute(f'PRAGMA table_info("{table_name}")')
        ]
        for row in conn.execute(f'SELECT * FROM "{table_name}"'):
            stored_values = " ".join(str(value) for value in row if value is not None)
            assert raw_name not in stored_values
            assert raw_phone not in stored_values
            assert "7932896340" not in stored_values


def test_hash_pii_requires_secret(monkeypatch):
    monkeypatch.delenv("AGY_PII_KEY", raising=False)

    with pytest.raises(RuntimeError, match="AGY_PII_KEY"):
        hash_pii("sensitive value")


def test_multiple_workflow_schemas_share_database(tmp_path, motor_schema):
    feedback_schema = load_schema("policy_feedback")
    conn = init_db(str(tmp_path / "multi_workflow.db"), motor_schema)
    init_db(conn, feedback_schema)

    motor_leads = {
        ("motor_renewal", "+917932896340"): {
            "workflow_id": "motor_renewal",
            "batch_id": "motor_batch",
            "phone": "+917932896340",
            "customer_name": "Alice",
            "policy_number": "MOT/001",
            "policy_expiry": "2026-12-01",
            "premium_amount": "100.00",
            "preferred_language": "en",
        }
    }
    feedback_leads = {
        ("policy_feedback", "+918196106462"): {
            "workflow_id": "policy_feedback",
            "batch_id": "feedback_batch",
            "phone": "+918196106462",
            "customer_name": "Bob",
            "order_id": "ORD/001",
            "product": "Device cover",
            "order_date": "2026-11-01",
            "preferred_language": "hi",
        }
    }

    insert_leads(conn, motor_leads, motor_schema)
    insert_leads(conn, feedback_leads, feedback_schema)

    assert get_lead_by_phone(
        conn, "motor_renewal", "+917932896340"
    )["policy_number"] == "MOT/001"
    assert get_lead_by_phone(
        conn, "policy_feedback", "+918196106462"
    )["order_id"] == "ORD/001"


def test_insert_leads_rolls_back_all_rows_on_error(tmp_path, motor_schema, monkeypatch):
    import solution.pipeline.db as db_module

    conn = init_db(str(tmp_path / "transaction.db"), motor_schema)
    leads = {
        ("motor_renewal", "+917932896340"): {
            "workflow_id": "motor_renewal",
            "batch_id": "batch_001",
            "phone": "+917932896340",
            "customer_name": "Alice",
            "policy_number": "MOT/001",
            "policy_expiry": None,
            "premium_amount": None,
            "preferred_language": "en",
        },
        ("motor_renewal", "+918196106462"): {
            "workflow_id": "motor_renewal",
            "batch_id": "batch_001",
            "phone": "+918196106462",
            "customer_name": "Bob",
            "policy_number": "MOT/002",
            "policy_expiry": None,
            "premium_amount": None,
            "preferred_language": "en",
        },
    }
    original_hash_pii = db_module.hash_pii
    hash_calls = 0

    def fail_on_second_hash(value):
        nonlocal hash_calls
        hash_calls += 1
        if hash_calls == 2:
            raise RuntimeError("simulated insert failure")
        return original_hash_pii(value)

    monkeypatch.setattr(db_module, "hash_pii", fail_on_second_hash)

    with pytest.raises(RuntimeError, match="simulated insert failure"):
        insert_leads(conn, leads, motor_schema)

    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
