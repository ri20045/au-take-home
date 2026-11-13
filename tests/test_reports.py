"""Tests for status derivation using the Assignment A SQLite schema."""

import csv
import datetime as dt
import json

import pytest

from solution.pipeline import db, reports


@pytest.fixture
def db_conn(tmp_path, motor_schema):
    conn = db.init_db(str(tmp_path / "test.db"), motor_schema)
    yield conn
    conn.close()


def _insert_lead(conn, lead_id, phone_hash, created_at):
    """Insert a test lead using the schema-driven database columns."""
    conn.execute(
        """
        INSERT INTO leads (
            lead_id, created_at, workflow_id, batch_id, phone_hash,
            phone_last4, customer_name_hash, policy_number, policy_expiry,
            premium_amount, preferred_language
        ) VALUES (?, ?, 'motor_renewal', 'batch_test', ?, '1234',
                  '', 'MOT/TEST', NULL, NULL, 'en')
        """,
        (lead_id, created_at, phone_hash),
    )


def _insert_call(conn, call_id, lead_id, phone_hash):
    """Create the call-to-lead phone mapping."""
    conn.execute(
        """
        INSERT INTO calls (workflow_id, call_id, phone_hash)
        VALUES ('motor_renewal', ?, ?)
        """,
        (call_id, phone_hash),
    )


def _insert_event(
    conn,
    event_id,
    call_id,
    event_type,
    timestamp,
    disposition=None,
):
    """Append a test event and its lead link."""
    phone_hash = conn.execute(
        "SELECT phone_hash FROM calls WHERE workflow_id = ? AND call_id = ?",
        ("motor_renewal", call_id),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO events (
            event_id, workflow_id, event_type, call_id, occurred_at,
            phone_hash, payload, is_reportable
        ) VALUES (?, 'motor_renewal', ?, ?, ?, ?, ?, 1)
        """,
        (
            event_id,
            event_type,
            call_id,
            timestamp,
            phone_hash,
            json.dumps(
                {"disposition_code": disposition} if disposition else {}
            ),
        ),
    )
    conn.execute(
        """
        INSERT INTO event_links (event_id, workflow_id, phone_hash)
        VALUES (?, 'motor_renewal', ?)
        """,
        (event_id, phone_hash),
    )


def test_effective_disposition_and_buckets(db_conn):
    """A terminal result wins; failed-only calls and untouched leads bucket correctly."""
    now = dt.datetime.now(dt.timezone.utc)
    now_iso = now.isoformat().replace("+00:00", "Z")
    older_iso = (now - dt.timedelta(days=2)).isoformat().replace("+00:00", "Z")

    _insert_lead(db_conn, "lead_A", "hash_A", now_iso)
    _insert_lead(db_conn, "lead_B", "hash_B", now_iso)
    _insert_lead(db_conn, "lead_C", "hash_C", now_iso)
    _insert_lead(db_conn, "lead_D", "hash_D", now_iso)

    _insert_call(db_conn, "call_A1", "lead_A", "hash_A")
    _insert_call(db_conn, "call_B1", "lead_B", "hash_B")
    _insert_call(db_conn, "call_C1", "lead_C", "hash_C")

    _insert_event(db_conn, "ev_A0", "call_A1", "call.initiated", now_iso)
    _insert_event(
        db_conn, "ev_A1", "call_A1", "disposition.set", now_iso, "renewed"
    )

    _insert_event(db_conn, "ev_B0", "call_B1", "call.initiated", now_iso)
    _insert_event(
        db_conn, "ev_B1", "call_B1", "disposition.set", now_iso, "not_interested"
    )
    _insert_event(
        db_conn,
        "ev_B2",
        "call_B1",
        "disposition.override",
        now_iso,
        "renewed",
    )

    _insert_event(
        db_conn, "ev_C0", "call_C1", "call.initiated", older_iso
    )
    _insert_event(
        db_conn,
        "ev_C1",
        "call_C1",
        "call.telephony_failed",
        older_iso,
    )

    statuses = reports.compute_lead_status(db_conn)
    status_by_lead = {status.lead_id: status for status in statuses}

    assert status_by_lead["lead_A"].effective_disposition == "renewed"
    assert status_by_lead["lead_A"].bucket == "completed"
    assert status_by_lead["lead_B"].effective_disposition == "renewed"
    assert status_by_lead["lead_B"].bucket == "completed"
    assert status_by_lead["lead_C"].effective_disposition is None
    assert status_by_lead["lead_C"].bucket == "in_progress_telephony_failed"
    assert status_by_lead["lead_D"].effective_disposition is None
    assert status_by_lead["lead_D"].bucket == "yet_to_reach_out"


def test_idempotent_status_computation(db_conn):
    """Computing statuses repeatedly leaves the ledger and results unchanged."""
    now_iso = (
        dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    )
    _insert_lead(db_conn, "lead_X", "hash_X", now_iso)
    _insert_call(db_conn, "call_X1", "lead_X", "hash_X")
    _insert_event(db_conn, "ev_X0", "call_X1", "call.initiated", now_iso)
    _insert_event(
        db_conn, "ev_X1", "call_X1", "disposition.set", now_iso, "renewed"
    )

    first = reports.compute_lead_status(db_conn)
    second = reports.compute_lead_status(db_conn)

    assert first == second


def test_write_reports_matches_assignment_output_contract(
    db_conn, tmp_path, motor_schema
):
    """Report files have the required headers, derived values, and quality totals."""
    timestamp = "2026-10-05T13:20:21Z"
    _insert_lead(db_conn, "lead_report", "hash_report", timestamp)
    _insert_call(db_conn, "call_report", "lead_report", "hash_report")
    _insert_event(
        db_conn, "event_init", "call_report", "call.initiated", timestamp
    )
    _insert_event(
        db_conn, "event_connected", "call_report", "call.connected", timestamp
    )
    _insert_event(
        db_conn,
        "event_disposition",
        "call_report",
        "disposition.set",
        timestamp,
        "renewed",
    )
    rejections = [
        ("motor_renewal", "batch_rejected", 2, "invalid_phone"),
    ]
    quality = {
        "lead_rows_total": 2,
        "duplicate_lead_rows_merged": 0,
        "field_warnings": 0,
        "events_total": 3,
        "malformed_events": 0,
        "duplicate_events_dropped": 0,
    }

    reports.write_reports(
        db_conn,
        tmp_path,
        {"motor_renewal": motor_schema},
        rejections,
        quality,
    )

    expected_headers = {
        "batch_summary.csv": (
            "workflow_id,batch_id,total_leads,yet_to_reach_out,"
            "in_progress_telephony_failed,in_progress_dialed,completed"
        ),
        "daily_overview.csv": (
            "workflow_id,date,total_calls,connected_calls,connectivity_rate,"
            "success_leads"
        ),
        "lead_report.csv": (
            "workflow_id,external_ref,batch_id,phone_masked,bucket,total_calls,"
            "connected_calls,last_call_at,current_disposition,ever_success"
        ),
        "rejections.csv": "workflow_id,batch_id,row_number,reason",
    }
    for filename, header in expected_headers.items():
        actual_header = (tmp_path / filename).read_text(
            encoding="utf-8"
        ).splitlines()[0]
        assert actual_header == header

    with (tmp_path / "daily_overview.csv").open(
        newline="", encoding="utf-8"
    ) as file:
        daily = next(csv.DictReader(file))
    assert daily == {
        "workflow_id": "motor_renewal",
        "date": "2026-10-05",
        "total_calls": "1",
        "connected_calls": "1",
        "connectivity_rate": "1.0000",
        "success_leads": "1",
    }

    with (tmp_path / "lead_report.csv").open(
        newline="", encoding="utf-8"
    ) as file:
        lead = next(csv.DictReader(file))
    assert lead["external_ref"] == "MOT/TEST"
    assert lead["phone_masked"] == "+91******1234"
    assert lead["current_disposition"] == "renewed"
    assert lead["ever_success"] == "true"

    quality_report = json.loads(
        (tmp_path / "data_quality.json").read_text(encoding="utf-8")
    )
    assert quality_report == {
        "lead_rows_total": 2,
        "lead_rows_rejected": 1,
        "rejections_by_reason": {"invalid_phone": 1},
        "duplicate_lead_rows_merged": 0,
        "field_warnings": 0,
        "events_total": 3,
        "malformed_events": 0,
        "duplicate_events_dropped": 0,
        "orphan_events": 0,
        "unknown_disposition_events": 0,
    }


def test_write_reports_rejects_inconsistent_input_quality(
    db_conn, tmp_path, motor_schema
):
    """Inconsistent ingestion counts fail before writing partial reports."""
    quality = {
        "lead_rows_total": 0,
        "duplicate_lead_rows_merged": 0,
        "field_warnings": 0,
        "events_total": 1,
        "malformed_events": 0,
        "duplicate_events_dropped": 0,
    }

    with pytest.raises(ValueError, match="events_total"):
        reports.write_reports(
            db_conn,
            tmp_path,
            {"motor_renewal": motor_schema},
            [],
            quality,
        )

    report_files = {
        "batch_summary.csv",
        "daily_overview.csv",
        "lead_report.csv",
        "rejections.csv",
        "data_quality.json",
    }
    assert not any((tmp_path / filename).exists() for filename in report_files)
