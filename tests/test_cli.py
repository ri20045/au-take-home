"""Tests for end-to-end command-line ingestion and reporting."""

import csv
import json

from solution.pipeline.cli import generate_reports, main


def _write_drop(drop, batch_name, lead_rows, event_lines):
    """Create one small lead-and-event drop for CLI tests."""
    lead_dir = drop / "leads" / "motor_renewal"
    event_dir = drop / "events"
    lead_dir.mkdir(parents=True)
    event_dir.mkdir()
    with (lead_dir / f"{batch_name}.csv").open(
        "w", newline="", encoding="utf-8"
    ) as file:
        writer = csv.writer(file, lineterminator="\n")
        writer.writerow(["customer_name", "phone", "policy_number"])
        writer.writerows(lead_rows)
    (event_dir / "events.jsonl").write_text(
        "".join(f"{line}\n" for line in event_lines),
        encoding="utf-8",
    )


def test_cli_ingests_multiple_drops_idempotently_and_generates_reports(
    tmp_path, monkeypatch, capsys
):
    """Drop counters persist across runs and repeated commands do not inflate them."""
    monkeypatch.setenv("AGY_PII_KEY", "cli-test-key")
    drop_1 = tmp_path / "drop_1"
    drop_2 = tmp_path / "drop_2"
    event = {
        "event_id": "event_once",
        "event_type": "call.initiated",
        "workflow_id": "motor_renewal",
        "call_id": "call_once",
        "occurred_at": "2026-10-05T13:20:21Z",
        "phone": "+91 79328 96340",
        "payload": {},
    }
    event_line = json.dumps(event)
    _write_drop(
        drop_1,
        "batch_001",
        [
            ["Alice", "+91 79328 96340", "MOT/001"],
            ["Bad phone", "123", "MOT/002"],
        ],
        [event_line, "{not valid json"],
    )
    _write_drop(
        drop_2,
        "batch_002",
        [["Alice Updated", "+91 79328 96340", "MOT/002"]],
        [event_line],
    )
    database = tmp_path / "pipeline.sqlite3"

    assert main(["--db", str(database), "ingest", str(drop_1)]) == 0
    assert main(["--db", str(database), "ingest", str(drop_2)]) == 0
    assert main(["--db", str(database), "ingest", str(drop_1)]) == 0
    assert "already ingested" in capsys.readouterr().out

    reports_dir = tmp_path / "reports"
    assert main(
        ["--db", str(database), "report", "--out", str(reports_dir)]
    ) == 0

    assert {
        path.name for path in reports_dir.iterdir()
    } == {
        "batch_summary.csv",
        "daily_overview.csv",
        "lead_report.csv",
        "rejections.csv",
        "data_quality.json",
    }
    quality = json.loads(
        (reports_dir / "data_quality.json").read_text(encoding="utf-8")
    )
    assert quality == {
        "lead_rows_total": 3,
        "lead_rows_rejected": 1,
        "rejections_by_reason": {"invalid_phone": 1},
        "duplicate_lead_rows_merged": 1,
        "field_warnings": 0,
        "events_total": 3,
        "malformed_events": 1,
        "duplicate_events_dropped": 1,
        "orphan_events": 0,
        "unknown_disposition_events": 0,
    }
    with (reports_dir / "lead_report.csv").open(
        newline="", encoding="utf-8"
    ) as file:
        lead = next(csv.DictReader(file))
    assert lead["external_ref"] == "MOT/002"
    assert lead["batch_id"] == "batch_001"
    assert lead["total_calls"] == "1"

    first_report = {
        path.name: path.read_bytes() for path in reports_dir.iterdir()
    }
    generate_reports(database, reports_dir)
    assert first_report == {
        path.name: path.read_bytes() for path in reports_dir.iterdir()
    }
