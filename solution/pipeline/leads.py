"""Lead ingestion, validation and duplicate handling.

The module works with the schema objects from :pymod:`solution.pipeline.schema`
and the phone‑cleaner from :pymod:`solution.pipeline.cleaners`.
It provides two public helpers:

* ``validate_row`` – checks a CSV row against the schema and returns either a
  dictionary of cleaned values or a rejection reason, plus its field-warning
  count.
* ``ingest_lead_file`` – reads a CSV file, applies ``validate_row`` to each
  non‑blank row, merges duplicates according to the assignment rules and
  returns the final lead map, rejections, and warning count.
* ``ingest_lead_drops`` – processes drop directories in the supplied order,
  returning merged leads and drop-level quality counts.
"""

import csv
import pathlib
from typing import Dict, List, Tuple, Optional

from .cleaners import clean_date, clean_decimal, clean_enum, clean_phone, clean_string
from .schema import WorkflowSchema, FieldSpec, load_schema

# Type aliases for readability
LeadKey = Tuple[str, str]  # (workflow_id, canonical_phone)
LeadRecord = Dict[str, Optional[str]]  # field name → cleaned value (or None)
Rejection = Tuple[int, str]  # (row_number, reason)
DropRejection = Tuple[str, str, int, str]  # workflow, batch, row, reason


def _is_blank_row(row: List[str]) -> bool:
    """Return ``True`` if *row* consists only of empty/whitespace cells."""
    return all(cell.strip() == "" for cell in row)


def _clean_value(field: FieldSpec, raw: str) -> Tuple[Optional[str], bool]:
    """Return the normalized value and whether a non-empty value was invalid."""
    value = raw.strip()
    if value == "":
        return None, False

    cleaners = {
        "string": lambda: clean_string(value),
        "phone": lambda: clean_phone(value),
        "date": lambda: clean_date(value),
        "decimal": lambda: clean_decimal(value),
        "enum": lambda: clean_enum(value, field.values),
    }
    try:
        cleaner = cleaners[field.type]
    except KeyError as exc:
        raise ValueError(f"Unsupported field type: {field.type}") from exc

    cleaned = cleaner()
    return cleaned, cleaned is None


def validate_row(
    row_num: int,
    raw_row: Dict[str, str],
    schema: WorkflowSchema,
    batch_id: str,
) -> Tuple[Optional[LeadRecord], Optional[Rejection], int]:
    """Validate a CSV *raw_row* according to *schema*.

    The function walks the fields **in schema order**. The first rejection
    becomes the row's single reason. Invalid optional values are stored as
    ``None`` and counted as warnings, but rejected rows contribute no warnings.

    Returns ``(lead_dict, rejection, warning_count)``. Rejected rows never
    contribute field warnings.
    """
    lead: LeadRecord = {}
    warning_count = 0
    # Walk fields in the order defined by the schema.
    for field in schema.fields:
        # Find the column that maps to this field (if any).
        col_spec = None
        for raw_header, value in raw_row.items():
            if schema.match_header(raw_header) is field:
                col_spec = raw_header
                break
        raw_val = raw_row.get(col_spec, "") if col_spec else ""
        cleaned, invalid_value = _clean_value(field, raw_val)

        if invalid_value and field.type == "phone":
            return None, (row_num, "invalid_phone"), 0
        if field.required and cleaned is None:
            return None, (row_num, f"missing_required:{field.name}"), 0
        if invalid_value:
            warning_count += 1

        lead[field.name] = cleaned

    # Attach immutable identifiers.
    lead["batch_id"] = batch_id
    lead["workflow_id"] = schema.workflow_id
    return lead, None, warning_count


def _merge_lead(
    leads: Dict[LeadKey, LeadRecord], lead: LeadRecord, schema: WorkflowSchema
) -> bool:
    """Merge a valid lead and return whether its identity was already present."""
    phone = lead.get("phone")
    if phone is None:
        raise ValueError("A validated lead must have a phone number")

    key: LeadKey = (schema.workflow_id, phone)
    if key not in leads:
        leads[key] = lead
        return False

    existing = leads[key]
    for field_name, value in lead.items():
        if field_name in {"workflow_id", "batch_id"}:
            continue
        if value is not None:
            existing[field_name] = value
    return True


def _apply_defaults(
    leads: Dict[LeadKey, LeadRecord], schema: WorkflowSchema
) -> None:
    """Apply schema defaults to null enum values after duplicate merging."""
    for lead in leads.values():
        for field in schema.fields:
            if field.default is not None and lead.get(field.name) is None:
                if field.type == "enum":
                    lead[field.name] = field.default.lower()


def _process_lead_file(
    file_path: str,
    schema: WorkflowSchema,
    leads: Dict[LeadKey, LeadRecord],
    rejections: List[DropRejection],
) -> Tuple[int, int]:
    """Read one CSV into shared lead state; return warning and duplicate counts."""
    field_warnings = 0
    duplicate_count = 0
    batch_id = pathlib.Path(file_path).stem
    with open(file_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            header_row = next(reader)
        except StopIteration:
            return field_warnings, duplicate_count

        row_num = 0
        for raw in reader:
            if _is_blank_row(raw):
                continue
            row_num += 1
            raw_dict = dict(zip(header_row, raw))
            lead, rejection, row_warnings = validate_row(
                row_num, raw_dict, schema, batch_id
            )
            if rejection:
                rejections.append(
                    (schema.workflow_id, batch_id, rejection[0], rejection[1])
                )
                continue
            if lead is None:
                raise RuntimeError("Row validation returned no lead or rejection")
            field_warnings += row_warnings
            duplicate_count += _merge_lead(leads, lead, schema)

    return field_warnings, duplicate_count


def ingest_lead_file(
    file_path: str, schema: WorkflowSchema
) -> Tuple[Dict[LeadKey, LeadRecord], List[Rejection], int]:
    """Ingest one lead CSV, merging duplicates within that file.

    Returns ``(leads, rejections, field_warning_count)``. Row rejection tuples
    contain ``(row_number, reason)``; the batch ID is the file stem.
    """
    leads: Dict[LeadKey, LeadRecord] = {}
    tagged_rejections: List[DropRejection] = []
    field_warnings, _ = _process_lead_file(
        file_path, schema, leads, tagged_rejections
    )
    _apply_defaults(leads, schema)
    rejections = [(row, reason) for _, _, row, reason in tagged_rejections]
    return leads, rejections, field_warnings


def ingest_lead_drops(
    drop_paths: List[str],
) -> Tuple[Dict[LeadKey, LeadRecord], List[DropRejection], int, int]:
    """Ingest lead CSVs from drops in order, sorting workflows and files.

    Each drop must contain ``leads/<workflow_id>/*.csv``. Drops are processed
    in the order supplied; workflow directories and CSV files are sorted by
    name within each drop. Returns ``(leads, rejections, warnings, duplicates)``.
    Rejections include workflow and batch identifiers for reporting.
    """
    leads: Dict[LeadKey, LeadRecord] = {}
    rejections: List[DropRejection] = []
    schemas: Dict[str, WorkflowSchema] = {}
    field_warnings = 0
    duplicate_count = 0

    for drop_path in drop_paths:
        leads_root = pathlib.Path(drop_path) / "leads"
        if not leads_root.is_dir():
            raise FileNotFoundError(f"Lead directory not found: {leads_root}")

        for workflow_dir in sorted(
            (path for path in leads_root.iterdir() if path.is_dir()),
            key=lambda path: path.name,
        ):
            schema = schemas.get(workflow_dir.name)
            if schema is None:
                schema = load_schema(workflow_dir.name)
                schemas[workflow_dir.name] = schema

            for file_path in sorted(workflow_dir.glob("*.csv"), key=lambda path: path.name):
                warnings, duplicates = _process_lead_file(
                    str(file_path), schema, leads, rejections
                )
                field_warnings += warnings
                duplicate_count += duplicates

    for workflow_id, schema in schemas.items():
        workflow_leads = {
            key: lead for key, lead in leads.items() if key[0] == workflow_id
        }
        _apply_defaults(workflow_leads, schema)

    return leads, rejections, field_warnings, duplicate_count
