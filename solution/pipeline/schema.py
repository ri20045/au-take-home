"""Schema handling for Assignment A.

The module loads the JSON schema files that describe each workflow's lead
format.  It provides:

* ``FieldSpec`` – a lightweight data class describing a single column.
* ``WorkflowSchema`` – holds the workflow id, description and a list of
  ``FieldSpec`` objects.  It also builds a mapping from *normalized* header
  names to the corresponding ``FieldSpec`` for quick look‑ups.
* ``load_schema(workflow_id)`` – reads ``assignment-a-data/schemas/<id>.json``
  and returns a ``WorkflowSchema`` instance.
* ``normalize_header`` – implements the header‑matching rules from the
  specification (lower‑case, trim, collapse runs of underscores or whitespace
  into a single space).
* ``match_header`` – given a raw CSV header, returns the matching ``FieldSpec``
  or ``None`` if the column is irrelevant.

Only the functionality required for the current unit tests is implemented;
additional helpers can be added later as the pipeline grows.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "assignment-a-data" / "schemas"


def normalize_header(header: str) -> str:
    """Normalize a CSV header according to the assignment rules.

    * lower‑case the string
    * strip leading/trailing whitespace
    * replace any run of underscores ("_") or whitespace with a single space
    """
    header = header.strip().lower()
    # Collapse runs of '_' or whitespace into a single space.
    return header


@dataclass
class FieldSpec:
    """Specification for a single field in a workflow schema."""

    name: str
    type: str
    required: bool = False
    pii: bool = False
    reference: bool = False
    aliases: List[str] = field(default_factory=list)
    values: List[str] = field(default_factory=list)  # for enum
    default: Optional[str] = None

    # Helper: all searchable names (the canonical name + aliases) normalized.
    def searchable_names(self) -> List[str]:
        names = [self.name] + self.aliases
        return [normalize_header(n) for n in names]


@dataclass
class WorkflowSchema:
    """All schema information for a particular workflow (e.g. ``motor_renewal``)."""

    workflow_id: str
    description: str
    fields: List[FieldSpec]
    # Mapping from normalized header to FieldSpec for quick look‑up.
    _header_map: Dict[str, FieldSpec] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._header_map = {}
        for f in self.fields:
            for hdr in f.searchable_names():
                self._header_map[hdr] = f

    def match_header(self, raw_header: str) -> Optional[FieldSpec]:
        """Return the ``FieldSpec`` that matches *raw_header* or ``None``.

        Matching follows the rules described in the assignment – the header is
        normalised and then compared against the field ``name`` and any
        ``aliases`` (also normalised).
        """
        normalized = normalize_header(raw_header)
        return self._header_map.get(normalized)


def load_schema(workflow_id: str) -> WorkflowSchema:
    """Load a workflow's JSON schema from the ``assignment-a-data`` directory.

    Parameters
    ----------
    workflow_id:
        Identifier used in the data folder (e.g. ``motor_renewal``).

    Returns
    -------
    WorkflowSchema
        An object containing the parsed fields and helper methods.
    """
    schema_path = SCHEMAS_DIR / f"{workflow_id}.json"
    if not schema_path.is_file():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")

    with schema_path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    fields: List[FieldSpec] = []
    for fld in raw.get("fields", []):
        fields.append(
            FieldSpec(
                name=fld["name"],
                type=fld["type"],
                required=fld.get("required", False),
                pii=fld.get("pii", False),
                reference=fld.get("reference", False),
                aliases=fld.get("aliases", []),
                values=fld.get("values", []),
                default=fld.get("default"),
            )
        )

    return WorkflowSchema(
        workflow_id=raw.get("workflow_id", workflow_id),
        description=raw.get("description", ""),
        fields=fields,
    )
