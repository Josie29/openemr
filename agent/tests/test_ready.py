from fastapi.testclient import TestClient

from copilot.config import Settings
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


def test_response_carries_correlation_id_header(settings: Settings) -> None:
    # Guards observability's entry point: every response must echo a correlation id so a full
    # trace can be reconstructed from logs alone (ARCHITECTURE.md §10).
    with TestClient(create_app(settings)) as client:
        response = client.get("/health", headers={"X-Correlation-ID": "test-trace-123"})

    assert response.headers["x-correlation-id"] == "test-trace-123"
