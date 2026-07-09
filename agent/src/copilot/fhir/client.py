from typing import Protocol

import httpx

from copilot.fhir.models import PatientDemographics


class FhirError(RuntimeError):
    """Raised when a FHIR read fails or returns an unusable resource.

    Carries enough context to log and to let the agent degrade gracefully (report the gap
    rather than fabricate around it — ARCHITECTURE.md §8), without leaking transport detail
    into user-facing output.
    """


class FhirClient(Protocol):
    """Read-only FHIR R4 access, scoped to a single patient by the caller's token.

    Two implementations share this protocol: a fixture-backed one for tests/dev and an
    httpx-backed one for live OpenEMR. The agent depends on the protocol, never a concrete
    class, so the SMART-token source can change (env var → module-minted) without touching
    agent logic (implementation-prompt-01 §1.2).
    """

    async def get_patient(self, patient_id: str) -> PatientDemographics:
        """Read one ``Patient`` resource and return its typed demographics.

        Args:
            patient_id: The FHIR ``Patient`` logical id.

        Returns:
            The parsed ``PatientDemographics``.

        Raises:
            FhirError: If the read fails, times out, or the resource is unusable.
        """
        ...

    async def ping(self) -> None:
        """Cheaply verify the FHIR endpoint is reachable, for the ``/ready`` probe.

        Raises:
            FhirError: If the endpoint is not reachable.
        """
        ...


class HttpFhirClient:
    """FHIR R4 client that reads OpenEMR over HTTPS under a SMART patient-scoped token.

    Holds no database credentials — every read rides the FHIR/OAuth2 path (ARCHITECTURE.md
    §2, §4). Bounded timeouts and retries so a slow upstream degrades transparently rather
    than hanging the request.
    """

    def __init__(
        self,
        base_url: str,
        bearer_token: str,
        *,
        timeout_seconds: float,
        max_retries: int,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        transport = httpx.AsyncHTTPTransport(retries=max_retries)
        headers = {"Accept": "application/fhir+json"}
        # An empty token must not become "Authorization: Bearer " — httpx rejects the trailing space
        # as an illegal header value, which would fail every request including the unauthenticated
        # /metadata readiness probe. Reads without a token then fail closed at the server (401);
        # the probe needs no credential and still works.
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=timeout_seconds,
            transport=transport,
        )

    async def get_patient(self, patient_id: str) -> PatientDemographics:
        try:
            response = await self._client.get(f"/Patient/{patient_id}")
            response.raise_for_status()
            resource = response.json()
        except httpx.HTTPError as exc:
            raise FhirError(f"failed to read Patient/{patient_id}") from exc
        return PatientDemographics.from_fhir(resource)

    async def ping(self) -> None:
        try:
            # Metadata is unauthenticated and cheap — a reachability probe, not a data read.
            response = await self._client.get("/metadata")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FhirError("FHIR endpoint is not reachable") from exc

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._client.aclose()
