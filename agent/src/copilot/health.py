from enum import StrEnum

import httpx
from pydantic import BaseModel

from copilot.config import RetrievalMode, Settings
from copilot.fhir.client import FhirClient, FhirError


class DependencyName(StrEnum):
    """External dependencies the ``/ready`` probe validates (ARCHITECTURE.md §10, W2 §5/§10)."""

    FHIR = "fhir"
    LLM = "llm"
    LANGFUSE = "langfuse"
    QDRANT = "qdrant"
    COHERE = "cohere"


class DependencyStatus(BaseModel):
    """Reachability result for one dependency."""

    name: DependencyName
    ok: bool
    detail: str | None = None


class ReadinessReport(BaseModel):
    """Aggregate readiness across all probed dependencies."""

    ready: bool
    dependencies: list[DependencyStatus]


async def check_readiness(settings: Settings, fhir: FhirClient) -> ReadinessReport:
    """Probe every dependency and report per-dependency status (ARCHITECTURE.md §10).

    ``/ready`` returns 200 only when all probes pass; the report is 503's body otherwise. The
    LLM probe is a cheap metadata call (list models), never a completion.

    Args:
        settings: Service settings (endpoints and credentials for the probes).
        fhir: The FHIR client to ping.

    Returns:
        The aggregate readiness report.
    """
    statuses = [
        await _check_fhir(fhir),
        await _check_llm(settings),
        await _check_langfuse(settings),
        await _check_qdrant(settings),
        await _check_cohere(settings),
    ]
    return ReadinessReport(ready=all(s.ok for s in statuses), dependencies=statuses)


async def _check_fhir(fhir: FhirClient) -> DependencyStatus:
    """Probe FHIR reachability via the client's ping."""
    try:
        await fhir.ping()
    except FhirError:
        return DependencyStatus(name=DependencyName.FHIR, ok=False, detail="unreachable")
    return DependencyStatus(name=DependencyName.FHIR, ok=True)


async def _check_llm(settings: Settings) -> DependencyStatus:
    """Probe the Claude API with a metadata call (list models), not a completion."""
    if not settings.anthropic_api_key:
        return DependencyStatus(name=DependencyName.LLM, ok=False, detail="no api key configured")
    headers = {"x-api-key": settings.anthropic_api_key, "anthropic-version": "2023-06-01"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get("https://api.anthropic.com/v1/models", headers=headers)
            response.raise_for_status()
    except httpx.HTTPError:
        return DependencyStatus(name=DependencyName.LLM, ok=False, detail="unreachable")
    return DependencyStatus(name=DependencyName.LLM, ok=True)


async def _check_langfuse(settings: Settings) -> DependencyStatus:
    """Probe the Langfuse ingestion endpoint's public health check."""
    if not settings.langfuse_enabled:
        return DependencyStatus(name=DependencyName.LANGFUSE, ok=False, detail="not configured")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{settings.langfuse_host.rstrip('/')}/api/public/health")
            response.raise_for_status()
    except httpx.HTTPError:
        return DependencyStatus(name=DependencyName.LANGFUSE, ok=False, detail="unreachable")
    return DependencyStatus(name=DependencyName.LANGFUSE, ok=True)


async def _check_qdrant(settings: Settings) -> DependencyStatus:
    """Probe the Qdrant vector index reachability (W2_ARCHITECTURE.md §5, §10).

    Hits the unauthenticated ``/readyz`` endpoint (auth-exempt in current Qdrant), the cheapest
    "ready to serve" check. In fixture retrieval mode Qdrant is not used, so the probe reports
    ready without a network call.
    """
    if settings.retrieval_mode is RetrievalMode.FIXTURE:
        return DependencyStatus(name=DependencyName.QDRANT, ok=True, detail="fixture mode")
    if not settings.qdrant_url:
        return DependencyStatus(name=DependencyName.QDRANT, ok=False, detail="not configured")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{settings.qdrant_url.rstrip('/')}/readyz")
            response.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL):
        # InvalidURL (a malformed QDRANT_URL) is NOT an HTTPError subclass; catch it too so a
        # misconfigured URL degrades /ready to 503 rather than escaping as a 500.
        return DependencyStatus(name=DependencyName.QDRANT, ok=False, detail="unreachable")
    return DependencyStatus(name=DependencyName.QDRANT, ok=True)


async def _check_cohere(settings: Settings) -> DependencyStatus:
    """Probe the Cohere rerank API reachability (W2_ARCHITECTURE.md §5, §10).

    Uses a lightweight authenticated models-list GET — never a rerank call, which is billable.
    In fixture retrieval mode Cohere is not used, so the probe reports ready without a call.
    """
    if settings.retrieval_mode is RetrievalMode.FIXTURE:
        return DependencyStatus(name=DependencyName.COHERE, ok=True, detail="fixture mode")
    if not settings.cohere_api_key:
        return DependencyStatus(
            name=DependencyName.COHERE, ok=False, detail="no api key configured"
        )
    headers = {"Authorization": f"Bearer {settings.cohere_api_key}"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get("https://api.cohere.com/v1/models", headers=headers)
            response.raise_for_status()
    except httpx.HTTPError:
        return DependencyStatus(name=DependencyName.COHERE, ok=False, detail="unreachable")
    return DependencyStatus(name=DependencyName.COHERE, ok=True)
