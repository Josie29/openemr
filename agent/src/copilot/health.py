from enum import StrEnum

import httpx
from pydantic import BaseModel

from copilot.config import RetrievalMode, Settings
from copilot.fhir.client import FhirClient, FhirError


class DependencyName(StrEnum):
    """External dependencies the ``/ready`` probe validates (ARCHITECTURE.md §10, W2 §5/§10).

    The string values are the exact dependency names W2 §10 surfaces — ``document_storage``,
    ``vector_index``, ``reranker`` — rather than the concrete backend product (OpenEMR, Qdrant,
    Cohere), which each probe's docstring records instead.
    """

    FHIR = "fhir"
    LLM = "llm"
    LANGFUSE = "langfuse"
    DOCUMENT_STORAGE = "document_storage"
    VECTOR_INDEX = "vector_index"
    RERANKER = "reranker"


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
        await _check_document_storage(fhir),
        await _check_vector_index(settings),
        await _check_reranker(settings),
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


async def _check_document_storage(fhir: FhirClient) -> DependencyStatus:
    """Probe the document storage backend's reachability (W2_ARCHITECTURE.md §10).

    Document storage is co-located on the OpenEMR service: uploaded documents are filed as
    ``DocumentReference``/``Binary`` and fetched over FHIR, so its reachability is OpenEMR's.
    This shares the FHIR probe's unauthenticated capability-statement ping (no patient token is
    available at readiness time), and is surfaced under its own name because W2 §10 tracks
    document storage as a distinct dependency that on-call reads separately from FHIR.
    """
    try:
        await fhir.ping()
    except FhirError:
        return DependencyStatus(
            name=DependencyName.DOCUMENT_STORAGE, ok=False, detail="unreachable"
        )
    return DependencyStatus(name=DependencyName.DOCUMENT_STORAGE, ok=True)


async def _check_vector_index(settings: Settings) -> DependencyStatus:
    """Probe the vector index (Qdrant) reachability (W2_ARCHITECTURE.md §5, §10).

    Hits the unauthenticated ``/readyz`` endpoint (auth-exempt in current Qdrant), the cheapest
    "ready to serve" check. In fixture retrieval mode the index is served in-process, so the probe
    reports ready without a network call.
    """
    if settings.retrieval_mode is RetrievalMode.FIXTURE:
        return DependencyStatus(name=DependencyName.VECTOR_INDEX, ok=True, detail="fixture mode")
    if not settings.qdrant_url:
        return DependencyStatus(name=DependencyName.VECTOR_INDEX, ok=False, detail="not configured")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{settings.qdrant_url.rstrip('/')}/readyz")
            response.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL):
        # InvalidURL (a malformed QDRANT_URL) is NOT an HTTPError subclass; catch it too so a
        # misconfigured URL degrades /ready to 503 rather than escaping as a 500.
        return DependencyStatus(name=DependencyName.VECTOR_INDEX, ok=False, detail="unreachable")
    return DependencyStatus(name=DependencyName.VECTOR_INDEX, ok=True)


async def _check_reranker(settings: Settings) -> DependencyStatus:
    """Probe the reranker (Cohere Rerank) API reachability (W2_ARCHITECTURE.md §5, §10).

    Uses a lightweight authenticated models-list GET — never a rerank call, which is billable.
    The models endpoint is version-stable, so this checks reachability + auth, not the (v2)
    rerank contract. In fixture retrieval mode the reranker is not used, so the probe reports
    ready without a call.
    """
    if settings.retrieval_mode is RetrievalMode.FIXTURE:
        return DependencyStatus(name=DependencyName.RERANKER, ok=True, detail="fixture mode")
    if not settings.cohere_api_key:
        return DependencyStatus(
            name=DependencyName.RERANKER, ok=False, detail="no api key configured"
        )
    headers = {"Authorization": f"Bearer {settings.cohere_api_key}"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get("https://api.cohere.com/v1/models", headers=headers)
            response.raise_for_status()
    except httpx.HTTPError:
        return DependencyStatus(name=DependencyName.RERANKER, ok=False, detail="unreachable")
    return DependencyStatus(name=DependencyName.RERANKER, ok=True)
