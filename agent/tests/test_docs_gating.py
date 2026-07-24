from fastapi.testclient import TestClient

from copilot.config import FhirClientMode, RetrievalMode, Settings
from copilot.main import create_app

# AF-VULN-0003: the OpenAPI schema and interactive docs must be hidden from anonymous callers in
# prod. These catch a regression where /openapi.json or /docs is served unauthenticated — the recon
# aid the pentest flagged (probe AF-P006 expects 401/403/404 for an anonymous caller).


def _settings(*, expose_api_docs: bool) -> Settings:
    """Offline settings toggling only the docs-exposure flag."""
    return Settings(
        fhir_client_mode=FhirClientMode.FIXTURE,
        retrieval_mode=RetrievalMode.FIXTURE,
        anthropic_api_key=None,
        langfuse_public_key=None,
        langfuse_secret_key=None,
        expose_api_docs=expose_api_docs,
    )


def test_schema_and_docs_are_hidden_by_default() -> None:
    """With the prod default (expose_api_docs=False), the schema/docs routes 404 for anyone.

    Breaks if a future change re-enables FastAPI's default docs routes in prod, republishing the
    full API map to anonymous callers.
    """
    client = TestClient(create_app(_settings(expose_api_docs=False)))
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404


def test_schema_and_docs_are_served_when_explicitly_enabled() -> None:
    """Opting in (local dev) restores the schema + docs, so developers keep the interactive UI."""
    client = TestClient(create_app(_settings(expose_api_docs=True)))
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200
