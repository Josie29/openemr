from typing import Protocol

import httpx

from copilot.fhir.models import (
    Allergy,
    Encounter,
    LabDocumentSummary,
    Medication,
    NoteContent,
    PatientDemographics,
    Problem,
    bundle_resources,
    dedup_medications,
)


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
    class, so the SMART-token source can change (env var → module-minted per request) without
    touching agent logic.
    """

    async def get_patient(self, patient_id: str) -> PatientDemographics:
        """Read one ``Patient`` resource and return its typed demographics.

        Raises:
            FhirError: If the read fails, times out, or the resource is unusable.
        """
        ...

    async def get_problems(self, patient_id: str) -> list[Problem]:
        """Read the patient's ``Condition`` (problem-list) resources.

        Raises:
            FhirError: If the read fails or times out.
        """
        ...

    async def get_medications(self, patient_id: str) -> list[Medication]:
        """Read the patient's ``MedicationRequest`` resources, deduplicated.

        Raises:
            FhirError: If the read fails or times out.
        """
        ...

    async def get_allergies(self, patient_id: str) -> list[Allergy]:
        """Read the patient's ``AllergyIntolerance`` resources.

        Raises:
            FhirError: If the read fails or times out.
        """
        ...

    async def get_encounters(self, patient_id: str) -> list[Encounter]:
        """Read the patient's ``Encounter`` resources (metadata only, no note bodies).

        Raises:
            FhirError: If the read fails or times out.
        """
        ...

    async def get_encounter_note(self, patient_id: str, encounter_id: str) -> list[NoteContent]:
        """Read the free-text clinical note(s) for one encounter.

        Reads ``DocumentReference`` (category ``clinical-note``) for the patient and keeps those
        tied to ``encounter_id`` — the FHIR API has no ``encounter`` search param, so the filter is
        client-side (verified against OpenEMR's FHIR service).

        Raises:
            FhirError: If the read fails or times out.
        """
        ...

    async def get_lab_documents(self, patient_id: str) -> list[LabDocumentSummary]:
        """List the patient's uploaded lab documents (``DocumentReference`` with binary content).

        Returns the uploaded documents (PDF/Binary attachment) only — inline ``text/plain`` clinical
        notes are excluded — so the intake-extractor can find a lab report's id and OCR it.

        Raises:
            FhirError: If the read fails or times out.
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
    than hanging the request. The token is per-instance: the route builds one client per
    request from the inbound ``Authorization`` header, so a client is bound to exactly the
    patient its token is scoped to. A tokenless instance is allowed for the ``/ready`` metadata
    probe, which hits the unauthenticated capability statement.
    """

    def __init__(
        self,
        base_url: str,
        bearer_token: str | None = None,
        *,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
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

    async def _search(
        self, resource_type: str, patient_id: str, extra_params: dict[str, str] | None = None
    ) -> list[dict[str, object]]:
        """Run a ``GET /<resource_type>?patient=<id>`` search and return the matching resources.

        Args:
            resource_type: The FHIR resource type to search, e.g. ``"Condition"``.
            patient_id: The patient logical id to scope the search to.
            extra_params: Extra query params to merge in (e.g. a ``category`` filter).

        Returns:
            The resources from the returned searchset ``Bundle``.

        Raises:
            FhirError: If the search request fails.
        """
        try:
            params = {"patient": patient_id, **(extra_params or {})}
            response = await self._client.get(f"/{resource_type}", params=params)
            response.raise_for_status()
            bundle = response.json()
        except httpx.HTTPError as exc:
            raise FhirError(f"failed to search {resource_type} for patient {patient_id}") from exc
        return bundle_resources(bundle, resource_type)

    async def get_patient(self, patient_id: str) -> PatientDemographics:
        try:
            response = await self._client.get(f"/Patient/{patient_id}")
            response.raise_for_status()
            resource = response.json()
        except httpx.HTTPError as exc:
            raise FhirError(f"failed to read Patient/{patient_id}") from exc
        return PatientDemographics.from_fhir(resource)

    async def get_problems(self, patient_id: str) -> list[Problem]:
        return [Problem.from_fhir(r) for r in await self._search("Condition", patient_id)]

    async def get_medications(self, patient_id: str) -> list[Medication]:
        parsed = [
            Medication.from_fhir(r) for r in await self._search("MedicationRequest", patient_id)
        ]
        return dedup_medications(parsed)

    async def get_allergies(self, patient_id: str) -> list[Allergy]:
        return [Allergy.from_fhir(r) for r in await self._search("AllergyIntolerance", patient_id)]

    async def get_encounters(self, patient_id: str) -> list[Encounter]:
        return [Encounter.from_fhir(r) for r in await self._search("Encounter", patient_id)]

    async def get_encounter_note(self, patient_id: str, encounter_id: str) -> list[NoteContent]:
        resources = await self._search(
            "DocumentReference", patient_id, {"category": "clinical-note"}
        )
        notes = [NoteContent.from_fhir(r) for r in resources]
        return [note for note in notes if note.encounter_id == encounter_id]

    async def get_lab_documents(self, patient_id: str) -> list[LabDocumentSummary]:
        # Search all of the patient's DocumentReferences and keep the uploaded (PDF/Binary) ones;
        # the exact OpenEMR lab category token is unreliable, so the attachment kind is the filter.
        resources = await self._search("DocumentReference", patient_id)
        summaries = (LabDocumentSummary.try_from_fhir(r) for r in resources)
        return [summary for summary in summaries if summary is not None]

    async def ping(self) -> None:
        try:
            # Metadata (the FHIR capability statement) is unauthenticated and cheap — a
            # reachability probe, not a data read, so it works on a tokenless instance.
            response = await self._client.get("/metadata")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FhirError("FHIR endpoint is not reachable") from exc

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._client.aclose()
