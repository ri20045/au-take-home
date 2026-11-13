# pytest fixtures for the repository

import pytest
from solution.pipeline.schema import load_schema

@pytest.fixture
def motor_schema():
    """Load and return the motor_renewal workflow schema used in tests.

    The test suite expects a ``motor_schema`` fixture that provides a
    :class:`WorkflowSchema` instance for the ``motor_renewal`` workflow.
    """
    return load_schema("motor_renewal")
