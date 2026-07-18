import json
from pathlib import Path
from typing import Any

from copilot.config import ExtractorMode, FhirClientMode, ModelTier, RetrievalMode, Settings
from copilot.main import create_app

# The committed OpenAPI spec + the machinery that keeps it in sync (JOS-63).
#
# FastAPI generates the spec from the endpoints' Pydantic response models, so the schema IS the
# implementation — there is no hand-authored spec to drift. `scripts/dump_openapi.py` writes
# `agent/openapi.json` from `build_openapi_spec()`, and `tests/test_openapi_contract.py` fails when
# the committed file no longer matches, pointing the developer at the one command that fixes it.

# The spec file lives at the agent root (copilot/ -> src/ -> agent/, i.e. two parents up).
OPENAPI_PATH = Path(__file__).resolve().parents[2] / "openapi.json"


def _spec_settings() -> Settings:
    """Deterministic, offline settings for spec generation.

    Every dependency is in fixture/no-network mode so the spec is reproducible on any machine
    without keys: the schema comes from the route + response-model definitions, which do not depend
    on runtime configuration. Keeping this identical between the dump script and the contract test
    is what makes the drift check exact.
    """
    return Settings(
        model_tier=ModelTier.SONNET,
        fhir_client_mode=FhirClientMode.FIXTURE,
        retrieval_mode=RetrievalMode.FIXTURE,
        extractor_mode=ExtractorMode.FIXTURE,
        anthropic_api_key=None,
        langfuse_public_key=None,
        langfuse_secret_key=None,
    )


def build_openapi_spec() -> dict[str, Any]:
    """Return the app's OpenAPI 3.x schema (FastAPI-generated from the route response models)."""
    return create_app(_spec_settings()).openapi()


def dump_spec(spec: dict[str, Any]) -> str:
    """Serialize a spec to the committed on-disk form: 2-space indent, sorted keys, trailing NL.

    ``sort_keys`` makes the file a stable, reviewable diff regardless of dict ordering.
    """
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"
