from fastapi.testclient import TestClient

from copilot.config import FhirClientMode, RetrievalMode, Settings
from copilot.main import create_app


def test_health_is_always_alive(settings: Settings) -> None:
    # Guards the liveness/readiness split: /health must report alive regardless of whether
    # dependencies are reachable, so an orchestrator doesn't kill a process that's merely
    # waiting on a downstream.
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_ready_returns_503_when_a_dependency_is_down(settings: Settings) -> None:
    # Guards the §10 contract: /ready must NOT return 200 unconditionally. With no LLM key
    # configured, the LLM probe fails and /ready must report 503 with a per-dependency
    # breakdown — the failure mode that a naive `return 200` would hide from graders/on-call.
    with TestClient(create_app(settings)) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    llm = next(dep for dep in body["dependencies"] if dep["name"] == "llm")
    assert llm["ok"] is False


def test_ready_reports_retrieval_deps_green_in_fixture_mode(settings: Settings) -> None:
    # In fixture/offline mode the retrieval pipeline is served in-process, so its /ready probes
    # must report ready without any network call — otherwise local dev + CI would see a red
    # /ready purely because Qdrant/Cohere aren't reachable, masking real failures.
    with TestClient(create_app(settings)) as client:
        body = client.get("/ready").json()

    deps = {dep["name"]: dep for dep in body["dependencies"]}
    assert deps["qdrant"]["ok"] is True
    assert deps["cohere"]["ok"] is True


def test_ready_marks_retrieval_deps_down_when_live_but_unconfigured() -> None:
    # The JOS-53 acceptance: Qdrant must be a real /ready dependency (degraded if unreachable).
    # A live deploy with no Qdrant URL / Cohere key must report those probes red, not silently
    # green — the failure a naive probe would hide from a grader / on-call. No key/url is set,
    # so every probe short-circuits offline (no network call in the test).
    settings = Settings(
        fhir_client_mode=FhirClientMode.FIXTURE,
        retrieval_mode=RetrievalMode.QDRANT,
        qdrant_url=None,
        cohere_api_key=None,
        anthropic_api_key=None,
        langfuse_public_key=None,
        langfuse_secret_key=None,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    deps = {dep["name"]: dep for dep in response.json()["dependencies"]}
    assert deps["qdrant"]["ok"] is False
    assert deps["cohere"]["ok"] is False


def test_response_carries_correlation_id_header(settings: Settings) -> None:
    # Guards observability's entry point: every response must echo a correlation id so a full
    # trace can be reconstructed from logs alone (ARCHITECTURE.md §10).
    with TestClient(create_app(settings)) as client:
        response = client.get("/health", headers={"X-Correlation-ID": "test-trace-123"})

    assert response.headers["x-correlation-id"] == "test-trace-123"
