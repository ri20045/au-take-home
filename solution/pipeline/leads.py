"""Lead ingestion, validation and duplicate handling.

The module works with the schema objects from :pymod:`solution.pipeline.schema`
and the phone‑cleaner from :pymod:`solution.pipeline.cleaners`.
It provides two public helpers:

* ``validate_row`` – checks a CSV row against the schema and returns either a
  dictionary of cleaned values or a rejection reason.
* ``ingest_lead_file`` – reads a CSV file, applies ``validate_row`` to each
  non‑blank row, merges duplicates according to the assignment rules and
  returns the final lead map together with any rejections.
"""

import csv
import pathlib
from typing import Dict, List, Tuple, Optional

from .cleaners import clean_phone
from .schema import WorkflowSchema, FieldSpec, normalize_header

# Type aliases for readability
LeadKey = Tuple[str, str]  # (workflow_id, canonical_phone)
LeadRecord = Dict[str, Optional[str]]  # field name → cleaned value (or None)
Rejection = Tuple[int, str]  # (row_number, reason)


def _is_blank_row(row: List[str]) -> bool:
    """Return ``True`` if *row* consists only of empty/whitespace cells."""
    return all(cell.strip() == "" for cell in row)


def _clean_value(field: FieldSpec, raw: str) -> Tuple[Optional[str], Optional[str]]:
    """Clean *raw* according to *field* type.

    Returns ``(cleaned_value, error_reason)`` where ``error_reason`` is ``None``
    for a successful clean and a string like ``"invalid_phone"`` for a failure.
    Currently only the ``phone`` type requires special handling – other types
    are returned unchanged (trimmed) because their cleaners will be added later.
    """
    value = raw.strip()
    if value == "":
        return None, None  # treated as missing / null

    if field.type == "phone":
        cleaned = clean_phone(value)
        if cleaned is None:
            return None, "invalid_phone"
        return cleaned, None

    # For now we simply return the trimmed string for other types.
    return value, None


def validate_row(
    row_num: int,
    raw_row: Dict[str, str],
    schema: WorkflowSchema,
    batch_id: str,
) -> Tuple[Optional[LeadRecord], Optional[Rejection]]:
    """Validate a CSV *raw_row* according to *schema*.

    The function walks the fields **in schema order**.  The first failure
    becomes the row's single rejection reason, matching the assignment
    specification.

    Returns a tuple ``(lead_dict, rejection)`` where only one element is non‑
    ``None``.
    """
    lead: LeadRecord = {}
    # Walk fields in the order defined by the schema.
    for field in schema.fields:
        # Find the column that maps to this field (if any).
        col_spec = None
        for raw_header, value in raw_row.items():
            if schema.match_header(raw_header) is field:
                col_spec = raw_header
                break
        raw_val = raw_row.get(col_spec, "") if col_spec else ""
        cleaned, error = _clean_value(field, raw_val)

        # Required field missing?
        if field.required and cleaned is None:
            # Distinguish missing vs invalid for phone.
            if field.type == "phone" and raw_val.strip() != "":
                # Phone present but invalid → invalid_phone
                return None, (row_num, "invalid_phone")
            return None, (row_num, f"missing_required:{field.name}")

        # Optional field present but invalid → store None (warning counted elsewhere)
        if error:
            return None, (row_num, error)

        lead[field.name] = cleaned

    # Attach immutable identifiers.
    lead["batch_id"] = batch_id
    lead["workflow_id"] = schema.workflow_id
    return lead, None


def ingest_lead_file(
    file_path: str, schema: WorkflowSchema
) -> Tuple[Dict[LeadKey, LeadRecord], List[Rejection]]:
    """Read *file_path* (a lead CSV) and return a map of unique leads.

    - Blank rows are skipped and **not** counted.
    - Row numbers start at 1 for the first non‑blank data row.
    - Duplicate leads (same workflow + canonical phone) are merged; later rows
      overwrite only fields where the new value is not ``None``.
    - The ``batch_id`` is derived from the filename stem (e.g. ``batch_001``).
    """
    leads: Dict[LeadKey, LeadRecord] = {}
    rejections: List[Rejection] = []
    batch_id = pathlib.Path(file_path).stem
    with open(file_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            header_row = next(reader)
        except StopIteration:
            return leads, rejections
        # Build a map raw_header -> value for each row.
        row_num = 0
        for raw in reader:
            if _is_blank_row(raw):
                continue
            row_num += 1
            raw_dict = dict(zip(header_row, raw))
            lead, rejection = validate_row(row_num, raw_dict, schema, batch_id)
            if rejection:
                rejections.append(rejection)
                continue
            # At this point lead is a dict with cleaned values.
            phone = lead.get("phone")
            if phone is None:
                # Should not happen because phone is required.
                continue
            key: LeadKey = (schema.workflow_id, phone)
            if key not in leads:
                leads[key] = lead
            else:
                # Merge: overwrite only non‑null fields (except immutable ids).
                existing = leads[key]
                for k, v in lead.items():
                    if k in {"workflow_id", "batch_id"}:
                        continue
                    if v is not None:
                        existing[k] = v
                leads[key] = existing

    # Apply defaults after all rows processed.
    for lead in leads.values():
        for field in schema.fields:
            if (
                field.default is not None
                and lead.get(field.name) is None
                and field.type == "enum"
            ):
                # enums stored lower‑case
                lead[field.name] = field.default.lower()
    return leads, rejections
