"""Database utilities for Assignment A.

The pipeline stores leads in SQLite while protecting any field marked
``"pii": true`` in the schema. Protection is performed with an HMAC‑SHA256
using a secret key supplied via the ``AGY_PII_KEY`` environment variable.
Only the deterministic hash is persisted – the raw value is never written
to disk.

The module provides:

* ``init_db(path, schema)`` – create (or open) the SQLite file and ensure the
  leads table exists according to *schema*.
* ``insert_leads(conn, leads, schema)`` – bulk‑insert the leads map returned
  by :func:`solution.pipeline.leads.ingest_lead_file`.
* ``hash_pii(value)`` – HMAC‑SHA256 hex digest helper.
* ``get_lead_by_phone(conn, workflow_id, phone)`` – retrieve a stored lead.
"""

import os
import sqlite3
import hashlib
import hmac
from typing import Dict, Tuple, Any

from .schema import WorkflowSchema, FieldSpec

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
    """Return a deterministic HMAC‑SHA256 hex digest for *value*.

    Using a secret key makes the stored hash opaque to anyone without the
    secret, yet it is deterministic – the same phone number always yields the
    same hash, enabling joins between leads and events.
    """
    secret = _get_secret_key()
    return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()

# ---------------------------------------------------------------------------
# Schema‑driven table definition
# ---------------------------------------------------------------------------

def _lead_table_columns(schema: WorkflowSchema) -> Tuple[str, ...]:
    """Return column definitions for the ``leads`` table based on *schema*.

    PII fields (except the primary ``phone`` column) are stored as
    ``<field>_hash``.  All columns are ``TEXT`` for simplicity – dates and
    decimals are stored as their normalised string representation.
    """
    # Base columns (always present)
    cols = [
        "workflow_id TEXT NOT NULL",
        "batch_id TEXT NOT NULL",
        "phone_hash TEXT NOT NULL",
    ]
    # Add a column for each field defined in the workflow schema.
    for field in schema.fields:
        col_name = f"{field.name}_hash" if field.pii and field.name != "phone" else field.name
        cols.append(f"{col_name} TEXT")
    # Table‑level primary key – must come after all column definitions.
    cols.append("PRIMARY KEY (workflow_id, phone_hash)")
    return tuple(cols)


def init_db(db_path: str, schema: WorkflowSchema) -> sqlite3.Connection:
    """Open/create the SQLite database and ensure the ``leads`` table exists.

    Parameters
    ----------
    db_path:
        Path to the SQLite file.
    schema:
        Workflow schema that drives column creation.
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cols_sql = ", ".join(_lead_table_columns(schema))
    cur.execute(f"CREATE TABLE IF NOT EXISTS leads ({cols_sql});")
    conn.commit()
    return conn

# ---------------------------------------------------------------------------
# Insert helpers
# ---------------------------------------------------------------------------

def _prepare_insert_values(lead: Dict[str, Any], schema: WorkflowSchema) -> Tuple[Tuple[str, ...], Tuple[Any, ...]]:
    """Convert a single ``lead`` dict into ``(columns, values)`` for SQL.

    * ``phone`` is always stored as ``phone_hash`` (the canonical ``+91``
      format is already normalised by the phone cleaner).
    * For any other PII field we store the HMAC hash under ``<field>_hash``.
    * Non‑PII fields keep their original column name.
    """
    columns = ["workflow_id", "batch_id", "phone_hash"]
    values = [lead["workflow_id"], lead["batch_id"], hash_pii(lead["phone"])]
    for field in schema.fields:
        if field.name in {"workflow_id", "batch_id", "phone"}:
            continue
        raw = lead.get(field.name)
        if field.pii and raw is not None:
            values.append(hash_pii(raw))
            columns.append(f"{field.name}_hash")
        else:
            values.append(raw)
            columns.append(field.name)
    return tuple(columns), tuple(values)


def insert_leads(conn: sqlite3.Connection, leads: Dict[Tuple[str, str], Dict[str, Any]], schema: WorkflowSchema) -> None:
    """Bulk‑insert *leads* into the ``leads`` table.

    ``INSERT OR REPLACE`` is used – the caller already performed duplicate
    merging, so a replace simply updates the row if it already exists.
    """
    cur = conn.cursor()
    for lead in leads.values():
        cols, vals = _prepare_insert_values(lead, schema)
        placeholders = ", ".join(["?"] * len(vals))
        sql = f"INSERT OR REPLACE INTO leads ({', '.join(cols)}) VALUES ({placeholders});"
        cur.execute(sql, vals)
    conn.commit()

# ---------------------------------------------------------------------------
# Retrieval helper (used by later tasks and tests)
# ---------------------------------------------------------------------------

def get_lead_by_phone(conn: sqlite3.Connection, workflow_id: str, phone: str) -> Dict[str, Any] | None:
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
