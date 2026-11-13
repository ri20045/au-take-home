import json
import sqlite3
import pytest

from solution.pipeline.schema import load_schema
from solution.pipeline.leads import ingest_lead_file
from solution.pipeline.db import init_db, insert_leads
from solution.pipeline.events import ingest_events_file

TEST_SECRET = "test-secret-key"

@pytest.fixture(autouse=True)
def set_secret(monkeypatch):
    monkeypatch.setenv("AGY_PII_KEY", TEST_SECRET)

@pytest.fixture
def motor_schema():
    return load_schema("motor_renewal")

def write_events_jsonl(tmp_path, lines):
    path = tmp_path / "events.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
    return str(path)

def test_event_ingestion_basic(tmp_path, motor_schema):
    # Lead CSV
    csv_path = tmp_path / "batch.csv"
    csv_path.write_text(
        "customer_name,phone,policy_number,preferred_language\n"
        "Alice,+91 79328 96340,MOT/2026/001,\n",
        encoding="utf-8",
    )
    leads, _, _ = ingest_lead_file(str(csv_path), motor_schema)
    db_file = tmp_path / "test.db"
    conn = init_db(str(db_file), motor_schema)
    insert_leads(conn, leads, motor_schema)

    # Events JSONL
    event_lines = [
        json.dumps({
            "event_id": "evt_1",
            "event_type": "call.initiated",
            "workflow_id": "motor_renewal",
            "call_id": "call_1",
            "occurred_at": "2026-10-05T13:20:21Z",
            "phone": "+91 79328 96340",
            "payload": {},
        }),
        json.dumps({
            "event_id": "evt_2",
            "event_type": "disposition.set",
            "workflow_id": "motor_renewal",
            "call_id": "call_1",
            "occurred_at": "2026-10-05T13:25:00Z",
            "phone": "+91 79328 96340",
            "payload": {"disposition_code": "renewed"},
        }),
    ]
    ev_path = write_events_jsonl(tmp_path, event_lines)
    counters = ingest_events_file(conn, ev_path, motor_schema)
    assert counters["malformed"] == 0
    assert counters["duplicate"] == 0
    assert counters["orphan"] == 0
    assert counters["linked"] == 2

def test_event_ingestion_edge_cases(tmp_path, motor_schema):
    db_file = tmp_path / "test2.db"
    conn = init_db(str(db_file), motor_schema)

    event_lines = [
        "{invalid json",  # malformed
        json.dumps({
            "event_id": "evt_dup",
            "event_type": "call.initiated",
            "workflow_id": "motor_renewal",
            "call_id": "call_x",
            "occurred_at": "2026-10-05T13:20:21Z",
            "phone": "+91 99999 99999",
            "payload": {},
        }),
        json.dumps({
            "event_id": "evt_dup",
            "event_type": "call.initiated",
            "workflow_id": "motor_renewal",
            "call_id": "call_y",
            "occurred_at": "2026-10-05T13:30:21Z",
            "phone": "+91 99999 99999",
            "payload": {},
        }),
    ]
    ev_path = write_events_jsonl(tmp_path, event_lines)
    counters = ingest_events_file(conn, ev_path, motor_schema)
    assert counters["malformed"] == 1
    assert counters["duplicate"] == 1
    # No leads present, so first valid event is orphan
    assert counters["orphan"] == 1
    assert counters["linked"] == 0

def test_disposition_override_links_via_call(tmp_path, motor_schema):
    # Insert a lead
    csv_path = tmp_path / "batch.csv"
    csv_path.write_text(
        "customer_name,phone,policy_number,preferred_language\n"
        "Bob,+91 66470 90063,MOT/2026/002,\n",
        encoding="utf-8",
    )
    leads, _, _ = ingest_lead_file(str(csv_path), motor_schema)
    db_file = tmp_path / "test3.db"
    conn = init_db(str(db_file), motor_schema)
    insert_leads(conn, leads, motor_schema)

    # First a call.initiated event (creates call mapping)
    init_event = json.dumps({
        "event_id": "e1",
        "event_type": "call.initiated",
        "workflow_id": "motor_renewal",
        "call_id": "call_z",
        "occurred_at": "2026-10-05T13:20:21Z",
        "phone": "+91 66470 90063",
        "payload": {},
    })
    # Then a disposition.override without phone, linked via call_id
    override_event = json.dumps({
        "event_id": "e2",
        "event_type": "disposition.override",
        "workflow_id": "motor_renewal",
        "call_id": "call_z",
        "occurred_at": "2026-10-05T13:30:00Z",
        "payload": {"disposition_code": "renewed", "overridden_by": "supervisor", "reason": "manual"},
    })
    ev_path = write_events_jsonl(tmp_path, [init_event, override_event])
    counters = ingest_events_file(conn, ev_path, motor_schema)
    assert counters["linked"] == 2
    # Verify override stored with phone_hash (inherited from call mapping)
    cur = conn.cursor()
    cur.execute("SELECT phone_hash FROM events WHERE event_id = ?;", ("e2",))
    row = cur.fetchone()
    assert row is not None and row[0] is not None


def test_event_ledger_rejects_updates_and_deletes(tmp_path, motor_schema):
    conn = init_db(str(tmp_path / "append_only.db"), motor_schema)
    event = {
        "event_id": "immutable_event",
        "event_type": "call.initiated",
        "workflow_id": "motor_renewal",
        "call_id": "call_immutable",
        "occurred_at": "2026-10-05T13:20:21Z",
        "payload": {},
    }
    from solution.pipeline.events import _store_event

    _store_event(conn, event, None, reportable=True)

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE events SET payload = ? WHERE event_id = ?",
            ('{"changed": true}', "immutable_event"),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM events WHERE event_id = ?", ("immutable_event",))

    assert conn.execute(
        "SELECT COUNT(*) FROM event_links WHERE event_id = ?", ("immutable_event",)
    ).fetchone() == (0,)


def test_unknown_disposition_is_retained_but_not_reportable(tmp_path, motor_schema):
    conn = init_db(str(tmp_path / "unknown_disposition.db"), motor_schema)
    event = {
        "event_id": "unknown_disposition",
        "event_type": "disposition.set",
        "workflow_id": "motor_renewal",
        "call_id": "call_unknown",
        "occurred_at": "2026-10-05T13:20:21Z",
        "phone": "+91 66470 90063",
        "payload": {"disposition_code": "not_in_catalogue"},
    }
    event_path = write_events_jsonl(tmp_path, [json.dumps(event)])

    counters = ingest_events_file(conn, event_path, motor_schema)

    assert counters["unknown_disposition"] == 1
    assert conn.execute(
        "SELECT is_reportable FROM events WHERE event_id = ?",
        ("unknown_disposition",),
    ).fetchone() == (0,)
    assert conn.execute(
        "SELECT COUNT(*) FROM event_links WHERE event_id = ?",
        ("unknown_disposition",),
    ).fetchone() == (0,)


def test_orphan_event_links_when_lead_arrives_later(tmp_path, motor_schema):
    from solution.pipeline.db import insert_leads

    conn = init_db(str(tmp_path / "late_lead.db"), motor_schema)
    event = {
        "event_id": "early_event",
        "event_type": "call.initiated",
        "workflow_id": "motor_renewal",
        "call_id": "call_early",
        "occurred_at": "2026-10-05 10:20:21",
        "phone": "+91 66470 90063",
        "payload": {},
    }
    event_path = write_events_jsonl(tmp_path, [json.dumps(event)])

    counters = ingest_events_file(conn, event_path, motor_schema)
    assert counters["orphan"] == 1
    assert conn.execute(
        "SELECT occurred_at FROM events WHERE event_id = ?", ("early_event",)
    ).fetchone() == ("2026-10-05T10:20:21+05:30",)

    csv_path = tmp_path / "later_batch.csv"
    csv_path.write_text(
        "customer_name,phone,policy_number\n"
        "Alice,+91 66470 90063,MOT/001\n",
        encoding="utf-8",
    )
    leads, _, _ = ingest_lead_file(str(csv_path), motor_schema)
    insert_leads(conn, leads, motor_schema)

    assert conn.execute(
        "SELECT phone_hash FROM event_links WHERE event_id = ?", ("early_event",)
    ).fetchone() is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_id = ?", ("early_event",)
    ).fetchone() == (1,)


@pytest.mark.parametrize(
    "timestamp,expected",
    [
        ("2026-10-05T13:20:21Z", "2026-10-05T18:50:21+05:30"),
        ("2026-10-05T13:20:21+05:30", "2026-10-05T13:20:21+05:30"),
        ("2026-10-05 10:20:21", "2026-10-05T10:20:21+05:30"),
    ],
)
def test_event_timestamps_are_normalized_to_ist(timestamp, expected):
    from solution.pipeline.events import _normalize_timestamp

    assert _normalize_timestamp(timestamp) == expected
