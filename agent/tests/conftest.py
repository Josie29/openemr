import json
from pathlib import Path
from typing import Any

import pytest

from copilot.config import FhirClientMode, ModelTier, Settings
from copilot.fhir.fixtures import FixtureFhirClient

_FIXTURE_PATIENT = Path(__file__).parent / "fixtures" / "patient.json"


@pytest.fixture
def patient_resource() -> dict[str, Any]:
    """The recorded FHIR Patient resource used across tests (seed patient id '1')."""
    return json.loads(_FIXTURE_PATIENT.read_text())


@pytest.fixture
def fhir_client(patient_resource: dict[str, Any]) -> FixtureFhirClient:
    """A fixture-backed FHIR client seeded with the recorded patient."""
    return FixtureFhirClient({patient_resource["id"]: patient_resource})


@pytest.fixture
def settings() -> Settings:
    """Deterministic settings: fixture FHIR, no LLM key, no Langfuse.

    No real network dependency, so the whole service is exercised offline.
    """
    return Settings(
        model_tier=ModelTier.SONNET,
        fhir_client_mode=FhirClientMode.FIXTURE,
        anthropic_api_key=None,
        langfuse_public_key=None,
        langfuse_secret_key=None,
    )
