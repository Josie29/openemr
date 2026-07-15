import json
from pathlib import Path
from typing import Any

import pytest

from copilot.config import FhirClientMode, ModelTier, RetrievalMode, Settings
from copilot.fhir.fixtures import FixtureFhirClient

_FIXTURE_PATIENT = Path(__file__).parent / "fixtures" / "patient.json"


@pytest.fixture
def patient_resource() -> dict[str, Any]:
    """The recorded FHIR Patient resource used across tests (seed patient id '1')."""
    resource: dict[str, Any] = json.loads(_FIXTURE_PATIENT.read_text())
    return resource


@pytest.fixture
def fhir_client(patient_resource: dict[str, Any]) -> FixtureFhirClient:
    """A fixture-backed FHIR client seeded with the recorded patient."""
    return FixtureFhirClient({patient_resource["id"]: {"Patient": patient_resource}})


@pytest.fixture
def seed_client() -> FixtureFhirClient:
    """A fixture-backed client loaded from the bundled seed (patient '1' with full record)."""
    return FixtureFhirClient.from_seed()


@pytest.fixture
def settings() -> Settings:
    """Deterministic settings: fixture FHIR, fixture retrieval, no LLM key, no Langfuse.

    No real network dependency, so the whole service is exercised offline.
    """
    return Settings(
        model_tier=ModelTier.SONNET,
        fhir_client_mode=FhirClientMode.FIXTURE,
        retrieval_mode=RetrievalMode.FIXTURE,
        anthropic_api_key=None,
        langfuse_public_key=None,
        langfuse_secret_key=None,
    )
