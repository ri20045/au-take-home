"""Command-line orchestration for the Assignment A data pipeline."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Dict, Optional, Sequence

from .db import hash_pii, init_db, insert_leads
from .events import ingest_events_file
from .leads import ingest_lead_drops
from .reports import write_reports
from .schema import SCHEMAS_DIR, WorkflowSchema, load_schema

_DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "assignment_a.sqlite3"
_QUALITY_COLUMNS = (
    "lead_rows_total",
    "duplicate_lead_rows_merged",
    "field_warnings",
    "events_total",
    "malformed_events",
    "duplicate_events_dropped",
)


def _load_schemas() -> Dict[str, WorkflowSchema]:
    """Load all workflow schemas provided with Assignment A."""
    return {
        path.stem: load_schema(path.stem)
        for path in sorted(SCHEMAS_DIR.glob("*.json"))
    }


def _ensure_run_tables(conn: sqlite3.Connection) -> None:
    """Create tables for completed-drop quality totals and rejected rows."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pipeline_drops (
            drop_path TEXT PRIMARY KEY,
            lead_rows_total INTEGER NOT NULL,
            duplicate_lead_rows_merged INTEGER NOT NULL,
            field_warnings INTEGER NOT NULL,
            events_total INTEGER NOT NULL,
            malformed_events INTEGER NOT NULL,
            duplicate_events_dropped INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pipeline_rejections (
            drop_path TEXT NOT NULL,
            workflow_id TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            row_number INTEGER NOT NULL,
            reason TEXT NOT NULL,
            PRIMARY KEY (drop_path, workflow_id, batch_id, row_number),
            FOREIGN KEY (drop_path) REFERENCES pipeline_drops (drop_path)
        );
        """
    )


def ingest_drop(drop_path: str | Path, db_path: str | Path) -> bool:
    """Ingest one drop once, storing its durable quality counts.

    Returns ``True`` when this call processes a new drop and ``False`` when
    the same resolved drop path has already been ingested.
    """
    drop = Path(drop_path).resolve()
    leads_dir = drop / "leads"
    events_dir = drop / "events"
    if not drop.is_dir():
        raise FileNotFoundError(f"Drop directory not found: {drop}")
    if not leads_dir.is_dir():
        raise FileNotFoundError(f"Lead directory not found: {leads_dir}")
    if events_dir.exists() and not events_dir.is_dir():
        raise NotADirectoryError(f"Events path is not a directory: {events_dir}")

    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file)
    try:
        schemas = _load_schemas()
        if not schemas:
            raise FileNotFoundError(f"No workflow schemas found in {SCHEMAS_DIR}")
        init_db(conn)
        for schema in schemas.values():
            init_db(conn, schema)
        _ensure_run_tables(conn)

        if conn.execute(
            "SELECT 1 FROM pipeline_drops WHERE drop_path = ?", (str(drop),)
        ).fetchone():
            return False

        leads, rejections, warnings, in_drop_duplicates = ingest_lead_drops(
            [str(drop)]
        )
        stored_identities = {
            (workflow_id, phone_hash)
            for workflow_id, phone_hash in conn.execute(
                "SELECT workflow_id, phone_hash FROM leads"
            )
        }
        duplicates = in_drop_duplicates + sum(
            (workflow_id, hash_pii(lead["phone"])) in stored_identities
            for (workflow_id, _), lead in leads.items()
        )
        for workflow_id, schema in schemas.items():
            workflow_leads = {
                key: lead for key, lead in leads.items() if key[0] == workflow_id
            }
            if workflow_leads:
                insert_leads(conn, workflow_leads, schema)

        event_totals = {
            "total": 0,
            "malformed": 0,
            "duplicate": 0,
        }
        event_files = (
            sorted(events_dir.glob("*.jsonl")) if events_dir.exists() else []
        )
        for event_file in event_files:
            counters = ingest_events_file(conn, str(event_file), schemas)
            event_totals["total"] += counters["total"]
            event_totals["malformed"] += counters["malformed"]
            event_totals["duplicate"] += counters["duplicate"]

        quality = {
            "lead_rows_total": len(leads) + in_drop_duplicates + len(rejections),
            "duplicate_lead_rows_merged": duplicates,
            "field_warnings": warnings,
            "events_total": event_totals["total"],
            "malformed_events": event_totals["malformed"],
            "duplicate_events_dropped": event_totals["duplicate"],
        }
        with conn:
            conn.execute(
                """
                INSERT INTO pipeline_drops (
                    drop_path, lead_rows_total, duplicate_lead_rows_merged,
                    field_warnings, events_total, malformed_events,
                    duplicate_events_dropped
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (str(drop), *(quality[column] for column in _QUALITY_COLUMNS)),
            )
            conn.executemany(
                """
                INSERT INTO pipeline_rejections (
                    drop_path, workflow_id, batch_id, row_number, reason
                ) VALUES (?, ?, ?, ?, ?)
                """,
                ((str(drop), *rejection) for rejection in rejections),
            )
        return True
    finally:
        conn.close()


def generate_reports(db_path: str | Path, output_dir: str | Path) -> None:
    """Generate the five reports from the database and recorded drop totals."""
    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)
        schemas = _load_schemas()
        for schema in schemas.values():
            init_db(conn, schema)
        _ensure_run_tables(conn)
        rejections = conn.execute(
            """
            SELECT workflow_id, batch_id, row_number, reason
            FROM pipeline_rejections
            ORDER BY workflow_id, batch_id, row_number
            """
        ).fetchall()
        quality = {
            column: conn.execute(
                f"SELECT COALESCE(SUM({column}), 0) FROM pipeline_drops"
            ).fetchone()[0]
            for column in _QUALITY_COLUMNS
        }
        write_reports(conn, output_dir, schemas, rejections, quality)
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Ingest Assignment A drops and generate reports."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=_DEFAULT_DB_PATH,
        help=f"SQLite database path (default: {_DEFAULT_DB_PATH})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser("ingest", help="ingest one data drop")
    ingest_parser.add_argument("drop", type=Path, help="path to a drop directory")

    report_parser = subparsers.add_parser("report", help="write the five reports")
    report_parser.add_argument(
        "--out", required=True, type=Path, help="directory for generated reports"
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run a CLI command and return its process exit status."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "ingest":
            processed = ingest_drop(args.drop, args.db)
            if processed:
                print(f"Ingested drop: {args.drop}")
            else:
                print(f"Drop already ingested; no changes made: {args.drop}")
        else:
            generate_reports(args.db, args.out)
            print(f"Reports written to: {args.out}")
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    sys.exit(main())
