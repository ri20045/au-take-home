import json
from pathlib import Path

import pytest

from solution.pipeline.schema import (
    normalize_header,
    load_schema,
    WorkflowSchema,
    FieldSpec,
)

def test_normalize_header():
    assert normalize_header("  Customer_Name  ") == "customer name"
    assert normalize_header("mobile_no") == "mobile no"
    assert normalize_header("Phone   Number") == "phone number"
    assert normalize_header("___policy   _expiry___") == "policy expiry"

def test_load_motor_renewal_schema():
    schema: WorkflowSchema = load_schema("motor_renewal")
    # verify basic attributes
    assert schema.workflow_id == "motor_renewal"
    assert "Outbound motor-insurance renewal" in schema.description
    # there are 6 fields defined
    assert len(schema.fields) == 6
    # check a couple of fields for expected properties
    phone_field = next(f for f in schema.fields if f.name == "phone")
    assert phone_field.type == "phone"
    assert phone_field.required is True
    assert phone_field.pii is True
    # check alias matching via match_header
    assert schema.match_header("Phone Number") is phone_field
    assert schema.match_header("mobile") is phone_field
    # a header that does not exist returns None
    assert schema.match_header("unknown column") is None

def test_load_policy_feedback_schema():
    schema = load_schema("policy_feedback")
    # ensure file exists and loads without error
    assert isinstance(schema, WorkflowSchema)
    # ensure at least one field is marked as reference (policy_number)
    reference_fields = [f for f in schema.fields if f.reference]
    assert len(reference_fields) >= 1
