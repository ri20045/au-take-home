"""Generate Assignment A CSV and JSON reports from the SQLite ledger."""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence
from zoneinfo import ZoneInfo

from .schema import WorkflowSchema, load_schema

_IST = ZoneInfo("Asia/Kolkata")
_DISPOSITIONS_PATH = (
    Path(__file__).resolve().parents[2]
    / "assignment-a-data"
    / "schemas"
    / "dispositions.csv"
)

_BATCH_COLUMNS = (
    "workflow_id",
    "batch_id",
    "total_leads",
    "yet_to_reach_out",
    "in_progress_telephony_failed",
    "in_progress_dialed",
    "completed",
)
_DAILY_COLUMNS = (
    "workflow_id",
    "date",
    "total_calls",
    "connected_calls",
    "connectivity_rate",
    "success_leads",
)
_LEAD_COLUMNS = (
    "workflow_id",
    "external_ref",
    "batch_id",
    "phone_masked",
    "bucket",
    "total_calls",
    "connected_calls",
    "last_call_at",
    "current_disposition",
    "ever_success",
)
_REJECTION_COLUMNS = ("workflow_id", "batch_id", "row_number", "reason")
_QUALITY_INPUT_KEYS = (
    "lead_rows_total",
    "duplicate_lead_rows_merged",
    "field_warnings",
    "events_total",
    "malformed_events",
    "duplicate_events_dropped",
)


class EventRow(NamedTuple):
    """A reportable event linked to a lead."""

    event_id: str
    call_id: str
    event_type: str
    event_timestamp: str
    disposition: Optional[str]


class LeadStatus(NamedTuple):
    """Derived disposition and bucket for a lead."""

    lead_id: Optional[str]
    phone_hash: str
    effective_disposition: Optional[str]
    bucket: str


@dataclass(frozen=True)
class _CallFacts:
    """Call-level facts derived from linked events."""

    workflow_id: str
    phone_hash: str
    call_id: str
    initiated_at: str
    connected: bool
    telephony_failed: bool
    disposition: Optional[str]
    is_success: bool
    is_terminal: bool


@dataclass(frozen=True)
class _LeadFacts:
    """Stored lead fields plus facts needed by the report writers."""

    workflow_id: str
    phone_hash: str
    lead_id: Optional[str]
    batch_id: str
    phone_last4: str
    external_ref: str
    bucket: str
    calls: tuple[_CallFacts, ...]


def _read_catalogue() -> Dict[str, Dict[str, tuple[bool, bool]]]:
    """Read each workflow's disposition terminal and success flags."""
    catalogue: Dict[str, Dict[str, tuple[bool, bool]]] = {}
    with _DISPOSITIONS_PATH.open(newline="", encoding="utf-8") as file:
        for row in csv.DictReader(file):
            catalogue.setdefault(row["workflow_id"], {})[row["code"]] = (
                row["is_terminal"].strip().lower() == "true",
                row["is_success"].strip().lower() == "true",
            )
    return catalogue


def _parse_timestamp(value: str) -> datetime:
    """Parse a stored event time and return an aware IST datetime."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_IST)
    return parsed.astimezone(_IST)


def _effective_event(events: Sequence[EventRow]) -> Optional[EventRow]:
    """Select the latest override if present, otherwise the latest set event."""
    overrides = [
        event for event in events if event.event_type == "disposition.override"
    ]
    candidates = overrides or [
        event for event in events if event.event_type == "disposition.set"
    ]
    return max(
        candidates,
        key=lambda event: (_parse_timestamp(event.event_timestamp), event.event_id),
        default=None,
    )


def effective_disposition(events: Sequence[EventRow]) -> Optional[str]:
    """Return a call's effective disposition code."""
    event = _effective_event(events)
    return event.disposition if event else None


def _load_report_facts(
    conn: sqlite3.Connection, schemas: Dict[str, WorkflowSchema]
) -> List[_LeadFacts]:
    """Load leads and derive linked call facts from the append-only ledger."""
    catalogue = _read_catalogue()
    schema_fields = {
        workflow_id: next(
            (field.name for field in schema.fields if field.reference), None
        )
        for workflow_id, schema in schemas.items()
    }

    cursor = conn.execute(
        """
        SELECT workflow_id, phone_hash, lead_id, batch_id, phone_last4
        FROM leads
        ORDER BY workflow_id, phone_hash
        """
    )
    lead_rows = cursor.fetchall()

    events_by_lead_call: Dict[tuple[str, str, str], List[EventRow]] = defaultdict(list)
    cursor = conn.execute(
        """
        SELECT l.workflow_id, l.phone_hash, e.event_id, e.call_id,
               e.event_type, e.occurred_at, e.payload
        FROM event_links AS l
        JOIN events AS e ON e.event_id = l.event_id
        WHERE e.is_reportable = 1
        """
    )
    for (
        workflow_id,
        phone_hash,
        event_id,
        call_id,
        event_type,
        occurred_at,
        payload,
    ) in cursor:
        payload_value = json.loads(payload)
        disposition = (
            payload_value.get("disposition_code")
            if isinstance(payload_value, dict)
            else None
        )
        events_by_lead_call[(workflow_id, phone_hash, call_id)].append(
            EventRow(event_id, call_id, event_type, occurred_at, disposition)
        )

    calls_by_lead: Dict[tuple[str, str], Dict[str, List[EventRow]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for (workflow_id, phone_hash, call_id), events in events_by_lead_call.items():
        calls_by_lead[(workflow_id, phone_hash)][call_id] = events

    facts: List[_LeadFacts] = []
    for workflow_id, phone_hash, lead_id, batch_id, phone_last4 in lead_rows:
        ref_field = schema_fields.get(workflow_id)
        if ref_field is None:
            raise ValueError(
                f"No reference field configured for workflow {workflow_id}"
            )
        ref_column = '"' + ref_field.replace('"', '""') + '"'
        reference = conn.execute(
            f"SELECT {ref_column} FROM leads "
            "WHERE workflow_id = ? AND phone_hash = ?",
            (workflow_id, phone_hash),
        ).fetchone()[0]

        calls: List[_CallFacts] = []
        lead_calls = calls_by_lead.get((workflow_id, phone_hash), {})
        for call_id, events in lead_calls.items():
            initiated = [
                event for event in events if event.event_type == "call.initiated"
            ]
            if not initiated:
                continue
            initiated_event = min(
                initiated,
                key=lambda event: (
                    _parse_timestamp(event.event_timestamp),
                    event.event_id,
                ),
            )
            disposition_event = _effective_event(events)
            disposition = disposition_event.disposition if disposition_event else None
            terminal, success = catalogue.get(workflow_id, {}).get(
                disposition, (False, False)
            )
            calls.append(
                _CallFacts(
                    workflow_id=workflow_id,
                    phone_hash=phone_hash,
                    call_id=call_id,
                    initiated_at=_parse_timestamp(
                        initiated_event.event_timestamp
                    ).isoformat(),
                    connected=any(
                        event.event_type == "call.connected" for event in events
                    ),
                    telephony_failed=any(
                        event.event_type == "call.telephony_failed"
                        for event in events
                    ),
                    disposition=disposition,
                    is_success=success,
                    is_terminal=terminal,
                )
            )

        calls.sort(key=lambda call: (call.initiated_at, call.call_id))
        if not calls:
            bucket = "yet_to_reach_out"
        elif any(call.is_terminal for call in calls):
            bucket = "completed"
        elif all(call.telephony_failed for call in calls):
            bucket = "in_progress_telephony_failed"
        else:
            bucket = "in_progress_dialed"

        facts.append(
            _LeadFacts(
                workflow_id=workflow_id,
                phone_hash=phone_hash,
                lead_id=lead_id,
                batch_id=batch_id,
                phone_last4=phone_last4 or "",
                external_ref=reference or "",
                bucket=bucket,
                calls=tuple(calls),
            )
        )
    return facts


def compute_lead_status(conn: sqlite3.Connection) -> List[LeadStatus]:
    """Compute each lead's current disposition and bucket."""
    schemas = {
        "motor_renewal": load_schema("motor_renewal"),
        "policy_feedback": load_schema("policy_feedback"),
    }
    facts = _load_report_facts(conn, schemas)
    statuses = []
    for lead in facts:
        calls_with_disposition = [
            call for call in lead.calls if call.disposition is not None
        ]
        latest_disposition = max(
            calls_with_disposition,
            key=lambda call: (call.initiated_at, call.call_id),
            default=None,
        )
        statuses.append(
            LeadStatus(
                lead.lead_id,
                lead.phone_hash,
                latest_disposition.disposition if latest_disposition else None,
                lead.bucket,
            )
        )
    return statuses


def _write_csv(path: Path, columns: Sequence[str], rows: Iterable[dict]) -> None:
    """Write rows to a UTF-8 CSV with the exact requested column order."""
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _rejection_row(value) -> dict:
    """Convert accepted rejection tuple shapes to the report's named fields."""
    if isinstance(value, dict):
        return {
            "workflow_id": value["workflow_id"],
            "batch_id": value["batch_id"],
            "row_number": value["row_number"],
            "reason": value["reason"],
        }
    if len(value) == 4:
        workflow_id, batch_id, row_number, reason = value
        return {
            "workflow_id": workflow_id,
            "batch_id": batch_id,
            "row_number": row_number,
            "reason": reason,
        }
    raise ValueError("Rejections must include workflow, batch, row, and reason")


def write_reports(
    conn: sqlite3.Connection,
    output_dir: str | Path,
    schemas: Dict[str, WorkflowSchema],
    rejections: Sequence,
    quality: Mapping[str, int],
) -> None:
    """Write all five reports using the Assignment A output schemas.

    ``quality`` must supply input-row counts that cannot be reconstructed from
    SQLite. The function checks those totals against the database and derives
    rejection, orphan, and unknown-disposition counts from the final state.
    """
    missing_quality = [key for key in _QUALITY_INPUT_KEYS if key not in quality]
    if missing_quality:
        raise ValueError(
            "Missing required ingestion quality counters: "
            + ", ".join(missing_quality)
        )
    for key in _QUALITY_INPUT_KEYS:
        value = quality[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"Quality counter {key} must be a non-negative integer")

    leads = _load_report_facts(conn, schemas)
    rejection_rows = sorted(
        (_rejection_row(value) for value in rejections),
        key=lambda row: (
            row["workflow_id"],
            row["batch_id"],
            row["row_number"],
        ),
    )
    event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    if quality["lead_rows_total"] != (
        len(leads) + quality["duplicate_lead_rows_merged"] + len(rejection_rows)
    ):
        raise ValueError(
            "lead_rows_total does not match leads, duplicates, and rejections"
        )
    if quality["events_total"] != (
        event_count + quality["malformed_events"] + quality["duplicate_events_dropped"]
    ):
        raise ValueError(
            "events_total does not match stored, malformed, and duplicate events"
        )

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    buckets_by_batch: Dict[tuple[str, str], dict] = {}
    for lead in leads:
        key = (lead.workflow_id, lead.batch_id)
        summary = buckets_by_batch.setdefault(
            key,
            {
                column: 0
                for column in _BATCH_COLUMNS[2:]
            },
        )
        summary["total_leads"] += 1
        summary[lead.bucket] += 1
    for rejection in rejections:
        rejected = _rejection_row(rejection)
        buckets_by_batch.setdefault(
            (rejected["workflow_id"], rejected["batch_id"]),
            {column: 0 for column in _BATCH_COLUMNS[2:]},
        )
    batch_rows = [
        {"workflow_id": workflow, "batch_id": batch, **counts}
        for (workflow, batch), counts in sorted(buckets_by_batch.items())
    ]
    _write_csv(out / "batch_summary.csv", _BATCH_COLUMNS, batch_rows)

    daily_calls: Dict[tuple[str, str], List[_CallFacts]] = defaultdict(list)
    for lead in leads:
        for call in lead.calls:
            call_date = _parse_timestamp(call.initiated_at).date().isoformat()
            daily_calls[(lead.workflow_id, call_date)].append(call)
    daily_rows = []
    for (workflow_id, report_date), calls in sorted(daily_calls.items()):
        total = len(calls)
        connected = sum(call.connected for call in calls)
        successful_leads = {
            call.phone_hash
            for call in calls
            if call.is_success
        }
        daily_rows.append(
            {
                "workflow_id": workflow_id,
                "date": report_date,
                "total_calls": total,
                "connected_calls": connected,
                "connectivity_rate": f"{connected / total:.4f}",
                "success_leads": len(successful_leads),
            }
        )
    _write_csv(out / "daily_overview.csv", _DAILY_COLUMNS, daily_rows)

    lead_rows = []
    for lead in sorted(leads, key=lambda item: (item.workflow_id, item.external_ref)):
        calls = lead.calls
        calls_with_disposition = [
            call for call in calls if call.disposition is not None
        ]
        latest_disposition_call = max(
            calls_with_disposition,
            key=lambda call: (call.initiated_at, call.call_id),
            default=None,
        )
        latest_call = max(
            calls, key=lambda call: (call.initiated_at, call.call_id), default=None
        )
        lead_rows.append(
            {
                "workflow_id": lead.workflow_id,
                "external_ref": lead.external_ref,
                "batch_id": lead.batch_id,
                "phone_masked": f"+91******{lead.phone_last4}",
                "bucket": lead.bucket,
                "total_calls": len(calls),
                "connected_calls": sum(call.connected for call in calls),
                "last_call_at": latest_call.initiated_at if latest_call else "",
                "current_disposition": (
                    latest_disposition_call.disposition
                    if latest_disposition_call
                    else ""
                ),
                "ever_success": str(any(call.is_success for call in calls)).lower(),
            }
        )
    _write_csv(out / "lead_report.csv", _LEAD_COLUMNS, lead_rows)

    _write_csv(out / "rejections.csv", _REJECTION_COLUMNS, rejection_rows)

    reason_counts: Dict[str, int] = defaultdict(int)
    for row in rejection_rows:
        reason_counts[row["reason"]] += 1

    quality_report = {
        "lead_rows_total": quality["lead_rows_total"],
        "lead_rows_rejected": len(rejection_rows),
        "rejections_by_reason": dict(sorted(reason_counts.items())),
        "duplicate_lead_rows_merged": quality["duplicate_lead_rows_merged"],
        "field_warnings": quality["field_warnings"],
        "events_total": quality["events_total"],
        "malformed_events": quality["malformed_events"],
        "duplicate_events_dropped": quality["duplicate_events_dropped"],
        "orphan_events": conn.execute(
            """
            SELECT COUNT(*)
            FROM events AS e
            WHERE NOT EXISTS (
                SELECT 1 FROM event_links AS l WHERE l.event_id = e.event_id
            )
            """
        ).fetchone()[0],
        "unknown_disposition_events": conn.execute(
            "SELECT COUNT(*) FROM events WHERE is_reportable = 0"
        ).fetchone()[0],
    }

    with (out / "data_quality.json").open("w", encoding="utf-8") as file:
        json.dump(quality_report, file, indent=2, sort_keys=False)
        file.write("\n")
