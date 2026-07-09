from enum import StrEnum

import httpx
from pydantic import BaseModel

from copilot.config import Settings
from copilot.fhir.client import FhirClient, FhirError


class DependencyName(StrEnum):
    """External dependencies the ``/ready`` probe validates (ARCHITECTURE.md §10)."""

    FHIR = "fhir"
    LLM = "llm"
    LANGFUSE = "langfuse"


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
