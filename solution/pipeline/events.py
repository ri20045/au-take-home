"""Event ingestion, deduplication, and linking to leads.

The module works with the SQLite connection created by :mod:`solution.pipeline.db`.
It provides:

* ``ensure_event_schema(conn)`` – creates the ``calls`` and ``events`` tables.
* ``ingest_events_file(conn, path, schema)`` – reads a JSON‑Lines file, skips
  malformed lines, de-duplicates by ``event_id``, validates dispositions,
  stores immutable events, and links eligible events to leads.
* Helper functions used in the implementation are kept private (prefixed with
  ``_``) and are unit‑tested.
"""

import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, Tuple, List
from zoneinfo import ZoneInfo

from .cleaners import clean_phone
from .db import hash_pii, get_lead_by_phone, init_db, link_pending_events
from .schema import WorkflowSchema

_IST = ZoneInfo("Asia/Kolkata")
_DISPOSITIONS_PATH = (
    Path(__file__).resolve().parents[2]
    / "assignment-a-data"
    / "schemas"
    / "dispositions.csv"
)

# ---------------------------------------------------------------------------
# Table creation
# ---------------------------------------------------------------------------


def ensure_event_schema(conn: sqlite3.Connection) -> None:
    """Ensure the shared lead, call, and append-only event tables exist."""
    init_db(conn)

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _parse_line(line: str) -> Tuple[Dict | None, str | None]:
    """Parse JSONL and reject invalid JSON or records with no event ID."""
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None, "malformed"
    event_id = data.get("event_id") if isinstance(data, dict) else None
    if not isinstance(event_id, str) or not event_id.strip():
        return None, "malformed"
    return data, None


def _normalize_timestamp(value: str) -> str:
    """Convert UTC, offset, or naive-IST timestamps to an ISO IST value."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_IST)
    return parsed.astimezone(_IST).isoformat()


def _load_dispositions() -> Dict[str, set[str]]:
    """Load disposition codes grouped by workflow from the assignment catalogue."""
    codes: Dict[str, set[str]] = {}
    with _DISPOSITIONS_PATH.open(newline="", encoding="utf-8") as file:
        for row in csv.DictReader(file):
            codes.setdefault(row["workflow_id"], set()).add(row["code"])
    return codes


def _known_disposition(ev: Dict, dispositions: Dict[str, set[str]]) -> bool:
    """Return whether a disposition event's code is in its workflow catalogue."""
    if not str(ev.get("event_type", "")).startswith("disposition."):
        return True

    payload = ev.get("payload")
    code = payload.get("disposition_code") if isinstance(payload, dict) else None
    return code in dispositions.get(ev.get("workflow_id"), set())


def _store_event(
    conn: sqlite3.Connection,
    ev: Dict,
    phone_hash: str | None,
    reportable: bool = True,
) -> None:
    """Insert the event row into the ``events`` table.

    ``ev`` is the original parsed dictionary; ``phone_hash`` may be ``None``.
    The payload is stored as a JSON string for later processing.
    """
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO events (
            event_id, workflow_id, event_type, call_id, occurred_at,
            phone_hash, payload, is_reportable
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            ev["event_id"],
            ev["workflow_id"],
            ev["event_type"],
            ev["call_id"],
            _normalize_timestamp(ev["occurred_at"]),
            phone_hash,
            json.dumps(ev.get("payload", {})),
            int(reportable),
        ),
    )


def _store_call(conn: sqlite3.Connection, call_id: str, workflow_id: str, phone_hash: str) -> None:
    """Insert a call-to-lead bridge without replacing an earlier mapping.

    This is kept even when the phone is not a known lead yet, so a later
    ``disposition.override`` can still inherit the call's phone hash.
    """
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO calls (call_id, workflow_id, phone_hash) "
        "VALUES (?, ?, ?) ON CONFLICT (workflow_id, call_id) DO NOTHING;",
        (call_id, workflow_id, phone_hash),
    )


def _is_event_linked(conn: sqlite3.Connection, ev: Dict, schema: WorkflowSchema) -> Tuple[bool, str | None]:
    """Determine whether *ev* can be linked to a lead.

    Returns ``(linked, phone_hash)`` where ``linked`` is ``True`` when a matching
    lead exists. ``phone_hash`` is ``None`` for events without a phone.
    """
    event_type = ev.get("event_type")
    workflow_id = ev.get("workflow_id")
    # Most events carry a phone number.
    if event_type != "disposition.override":
        raw_phone = ev.get("phone", "")
        cleaned = clean_phone(str(raw_phone))
        if cleaned is not None:
            phone_hash = hash_pii(cleaned)
            lead = get_lead_by_phone(conn, workflow_id, cleaned)
            if lead is not None:
                return True, phone_hash
            return False, phone_hash
        return False, None
    # disposition.override – link via call_id only.
    call_id = ev.get("call_id")
    cur = conn.cursor()
    cur.execute(
        "SELECT phone_hash FROM calls WHERE call_id = ? AND workflow_id = ?;",
        (call_id, workflow_id),
    )
    row = cur.fetchone()
    if row:
        cur.execute(
            "SELECT 1 FROM leads WHERE workflow_id = ? AND phone_hash = ?",
            (workflow_id, row[0]),
        )
        return cur.fetchone() is not None, row[0]
    return False, None

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ingest_events_file(conn: sqlite3.Connection, path: str, schema: WorkflowSchema) -> Dict[str, int]:
    """Ingest a JSONL file of events.

    The function updates the database and returns a dictionary with counters:

    * ``malformed`` – lines that are not valid JSON or miss ``event_id``.
    * ``duplicate`` – events whose ``event_id`` already exists in the DB.
    * ``orphan`` – events that could not be linked to a lead at ingestion time.
    * ``linked`` – events that were successfully linked.
    * ``unknown_disposition`` – disposition events whose code is not catalogued.
    """
    ensure_event_schema(conn)
    counters = {
        "malformed": 0,
        "duplicate": 0,
        "orphan": 0,
        "linked": 0,
        "unknown_disposition": 0,
    }
    dispositions = _load_dispositions()
    cur = conn.cursor()
    with conn:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ev, err = _parse_line(line)
                if err:
                    counters["malformed"] += 1
                    continue
                cur.execute("SELECT 1 FROM events WHERE event_id = ?;", (ev["event_id"],))
                if cur.fetchone():
                    counters["duplicate"] += 1
                    continue
                linked, phone_hash = _is_event_linked(conn, ev, schema)
                reportable = _known_disposition(ev, dispositions)
                if not linked:
                    counters["orphan"] += 1
                if not reportable:
                    counters["unknown_disposition"] += 1

                _store_event(conn, ev, phone_hash, reportable)
                if ev.get("event_type") != "disposition.override" and phone_hash is not None:
                    _store_call(conn, ev["call_id"], ev["workflow_id"], phone_hash)

                if linked:
                    counters["linked"] += 1
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO event_links (
                            event_id, workflow_id, phone_hash
                        ) VALUES (?, ?, ?)
                        """,
                        (ev["event_id"], ev["workflow_id"], phone_hash),
                    )

                link_pending_events(conn, ev["workflow_id"])
    return counters
