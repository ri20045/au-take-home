"""Database utilities for Assignment A.

The pipeline stores leads in SQLite while protecting any field marked
``"pii": true`` in the schema. Protection is performed with an HMAC-SHA256
using a secret key supplied via the ``AGY_PII_KEY`` environment variable.
Only the deterministic hash is persisted - the raw value is never written
to disk.

The module provides:

* ``init_db(path, schema)`` – create (or open) the SQLite file and ensure the
  ``leads`` table exists according to *schema*.
* ``insert_leads(conn, leads, schema)`` – merge the leads map returned by
  :func:`solution.pipeline.leads.ingest_lead_file` into the table.
* ``hash_pii(value)`` – HMAC-SHA256 hex digest helper.
* ``get_lead_by_phone(conn, workflow_id, phone)`` – retrieve a stored lead.
"""

import os
import re
import sqlite3
import hashlib
import hmac
from typing import Dict, Tuple, Any, List

from .schema import WorkflowSchema

# Columns every leads table has, whatever the workflow. The phone number itself
# is never stored: only phone_hash (for matching) and phone_last4 (for the
# masked "+91******1234" shown in lead_report.csv).
_BASE_FIELDS = {"workflow_id", "batch_id", "phone"}

# ---------------------------------------------------------------------------
# HMAC helper
# ---------------------------------------------------------------------------

def _get_secret_key() -> bytes:
    """Return the secret key used for HMAC protection.

    The key must be present in the ``AGY_PII_KEY`` environment variable.
    ``RuntimeError`` is raised if it is missing so the failure is obvious.
    """
    key = os.getenv("AGY_PII_KEY")
    if not key:
        raise RuntimeError(
            "Environment variable AGY_PII_KEY is required for PII protection"
        )
    return key.encode("utf-8")


def hash_pii(value: str) -> str:
    """Return a deterministic HMAC-SHA256 hex digest for *value*.

    Using a secret key makes the stored hash opaque to anyone without the
    secret, yet it is deterministic - the same phone number always yields the
    same hash, enabling joins between leads and events.
    """
    secret = _get_secret_key()
    return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()

# ---------------------------------------------------------------------------
# Schema‑driven table definition
# ---------------------------------------------------------------------------

def _column_name(field) -> str:
    """PII fields are stored as ``<field>_hash``; everything else keeps its name."""
    return f"{field.name}_hash" if field.pii else field.name


def _quote_identifier(identifier: str) -> str:
    """Validate and quote a schema field name used as a SQLite column."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise ValueError(f"Invalid schema field name: {identifier!r}")
    return f'"{identifier}"'


def _lead_table_columns(schema: WorkflowSchema) -> Tuple[str, ...]:
    """Return column definitions for the ``leads`` table based on *schema*.

    The composite primary key is the assignment's lead identity. Schema-defined
    fields are stored in columns, with PII values stored under hashed names.
    """
    cols: List[str] = [
        "lead_id TEXT UNIQUE",
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "workflow_id TEXT NOT NULL",
        "batch_id TEXT NOT NULL",
        "phone_hash TEXT NOT NULL",
        "phone_last4 TEXT",
    ]
    seen_columns = {"lead_id", "created_at", "workflow_id", "batch_id", "phone_hash", "phone_last4"}
    for field in schema.fields:
        if field.name in _BASE_FIELDS:
            continue
        column_name = _column_name(field)
        _quote_identifier(column_name)
        if column_name in seen_columns:
            raise ValueError(f"Schema field conflicts with a database column: {field.name}")
        seen_columns.add(column_name)
        cols.append(f"{_quote_identifier(column_name)} TEXT")
    cols.append("PRIMARY KEY (workflow_id, phone_hash)")
    return tuple(cols)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init_db(
    db_path_or_conn: str | os.PathLike[str] | sqlite3.Connection,
    schema: WorkflowSchema | None = None,
) -> sqlite3.Connection:
    """Open/create the SQLite database and ensure the ``leads`` table exists.

    Calling this more than once for the same database with different workflow
    schemas adds any missing schema columns, allowing workflows to share a DB.
    """
    if isinstance(db_path_or_conn, sqlite3.Connection):
        conn = db_path_or_conn
    else:
        conn = sqlite3.connect(db_path_or_conn)

    conn.execute("PRAGMA foreign_keys = ON")
    if schema is None:
        cols_sql = (
            "lead_id TEXT UNIQUE, "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "workflow_id TEXT NOT NULL, "
            "batch_id TEXT NOT NULL, "
            "phone_hash TEXT NOT NULL, "
            "phone_last4 TEXT, "
            "PRIMARY KEY (workflow_id, phone_hash)"
        )
    else:
        cols_sql = ", ".join(_lead_table_columns(schema))

    with conn:
        conn.execute(f"CREATE TABLE IF NOT EXISTS leads ({cols_sql})")
        if schema is not None:
            existing_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(leads)")
            }
            for field in schema.fields:
                if field.name in _BASE_FIELDS:
                    continue
                column_name = _column_name(field)
                _quote_identifier(column_name)
                if column_name not in existing_columns:
                    conn.execute(
                        f"ALTER TABLE leads ADD COLUMN {_quote_identifier(column_name)} TEXT"
                    )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS calls (
                workflow_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                phone_hash TEXT,
                PRIMARY KEY (workflow_id, call_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                call_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                phone_hash TEXT,
                payload TEXT NOT NULL,
                is_reportable INTEGER NOT NULL DEFAULT 1
                    CHECK (is_reportable IN (0, 1))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS event_links (
                event_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                phone_hash TEXT NOT NULL,
                FOREIGN KEY (event_id) REFERENCES events (event_id),
                FOREIGN KEY (workflow_id, phone_hash)
                    REFERENCES leads (workflow_id, phone_hash)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_call "
            "ON events (workflow_id, call_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_phone "
            "ON events (workflow_id, phone_hash)"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS events_no_update "
            "BEFORE UPDATE ON events BEGIN "
            "SELECT RAISE(ABORT, 'events ledger is append-only'); END"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS events_no_delete "
            "BEFORE DELETE ON events BEGIN "
            "SELECT RAISE(ABORT, 'events ledger is append-only'); END"
        )
    return conn

def _prepare_insert_values(lead: Dict[str, Any], schema: WorkflowSchema) -> Tuple[Tuple[str, ...], Tuple[Any, ...]]:
    """Convert a single ``lead`` dict into ``(columns, values)`` for SQL.

    * ``phone`` is stored as ``phone_hash`` plus its last four digits (the
    canonical ``+91`` format is already normalised by the phone cleaner).
    * For any other PII field we store the HMAC hash under ``<field>_hash``.
    * Non‑PII fields keep their original column name. Decimals and dates are
    stored as text, because SQLite cannot bind those Python types directly.
    """
    columns = ["workflow_id", "batch_id", "phone_hash", "phone_last4"]
    values = [
        lead["workflow_id"],
        lead["batch_id"],
        hash_pii(lead["phone"]),
        lead["phone"][-4:],
    ]
    for field in schema.fields:
        if field.name in _BASE_FIELDS:
            continue
        raw = lead.get(field.name)
        if raw is not None and not isinstance(raw, str):
            raw = str(raw)
        if field.pii and raw is not None:
            raw = hash_pii(raw)
        columns.append(_column_name(field))
        values.append(raw)
    return tuple(columns), tuple(values)


def link_pending_events(conn: sqlite3.Connection, workflow_id: str) -> None:
    """Link stored events to leads when their call or lead becomes available."""
    conn.execute(
        """
        INSERT OR IGNORE INTO event_links (event_id, workflow_id, phone_hash)
        SELECT e.event_id, e.workflow_id, COALESCE(e.phone_hash, c.phone_hash)
        FROM events AS e
        LEFT JOIN calls AS c
            ON c.workflow_id = e.workflow_id AND c.call_id = e.call_id
        JOIN leads AS l
            ON l.workflow_id = e.workflow_id
            AND l.phone_hash = COALESCE(e.phone_hash, c.phone_hash)
        WHERE e.workflow_id = ?
            AND NOT EXISTS (
                SELECT 1 FROM event_links AS el WHERE el.event_id = e.event_id
            )
        """,
        (workflow_id,),
    )


def insert_leads(
    conn: sqlite3.Connection,
    leads: Dict[Tuple[str, str], Dict[str, Any]],
    schema: WorkflowSchema,
) -> None:
    """Merge *leads* into the ``leads`` table.

    The spec's duplicate rule is applied against what is already stored:

    * a new identity creates the row, and its ``batch_id`` is kept forever;
    * an identity that already exists is updated only where the incoming value
      is non‑null (last write wins) - ``COALESCE(new, old)``.

    ``INSERT OR REPLACE`` would be wrong here: it deletes the old row, so a
    lead seen again in a later batch would lose its original batch_id and any
    field the later row left empty.
    """
    with conn:
        cur = conn.cursor()
        for lead in leads.values():
            cols, vals = _prepare_insert_values(lead, schema)
            quoted_cols = [_quote_identifier(column) for column in cols]
            placeholders = ", ".join(["?"] * len(vals))
            keep = {"workflow_id", "phone_hash", "batch_id", "phone_last4"}
            updates = ", ".join(
                f"{_quote_identifier(column)} = "
                f"COALESCE(excluded.{_quote_identifier(column)}, leads.{_quote_identifier(column)})"
                for column in cols
                if column not in keep
            )
            sql = (
                f"INSERT INTO leads ({', '.join(quoted_cols)}) "
                f"VALUES ({placeholders}) "
                f"ON CONFLICT (workflow_id, phone_hash) DO UPDATE SET {updates}"
            )
            cur.execute(sql, vals)
        link_pending_events(conn, schema.workflow_id)

def get_lead_by_phone(
    conn: sqlite3.Connection, workflow_id: str, phone: str
) -> Dict[str, Any] | None:
    """Fetch a lead row by *workflow_id* and normalised *phone*.

    Returns a ``dict`` mapping column names to values, or ``None`` if the lead
    is not present.
    """
    phone_hash = hash_pii(phone)
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM leads WHERE workflow_id = ? AND phone_hash = ?",
        (workflow_id, phone_hash),
    )
    row = cur.fetchone()
    if row is None:
        return None
    col_names = [desc[0] for desc in cur.description]
    return dict(zip(col_names, row))
