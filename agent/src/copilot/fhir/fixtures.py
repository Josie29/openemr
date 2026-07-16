import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from copilot.fhir.client import FhirError
from copilot.fhir.models import (
    Allergy,
    Encounter,
    LabObservation,
    Medication,
    NoteContent,
    PatientDemographics,
    Problem,
    UploadedDocumentSummary,
    bundle_resources,
    dedup_medications,
    resolve_doc_type,
)
from copilot.ingestion.schemas import DocType

_SEED_DIR = Path(__file__).parent / "seed"

# A per-patient record: the Patient resource plus its lists of related resources, keyed by
# FHIR resourceType. Mirrors what the five FHIR read endpoints return for one patient.
PatientRecord = dict[str, Any]


def _is_laboratory(resource: dict[str, Any]) -> bool:
    """Report whether an ``Observation`` is categorised ``laboratory``.

    OpenEMR serves vitals, social history and lab results all as ``Observation``; only the
    category separates them. The live client narrows with a server-side ``category=laboratory``
    filter, so the fixture client applies the same rule locally.

    Args:
        resource: A FHIR ``Observation`` resource.

    Returns:
        True when any category coding carries the ``laboratory`` code.
    """
    categories = resource.get("category")
    if not isinstance(categories, list):
        return False
    for category in categories:
        if not isinstance(category, dict):
            continue
        codings = category.get("coding")
        if not isinstance(codings, list):
            continue
        for coding in codings:
            if isinstance(coding, dict) and coding.get("code") == "laboratory":
                return True
    return False


def _patient_ref_id(resource: dict[str, Any]) -> str | None:
    """Extract the referenced patient logical id from a resource's subject/patient reference.

    Args:
        resource: A FHIR resource that references a patient (via ``subject`` or ``patient``).

    Returns:
        The patient logical id (``"1"`` from ``"Patient/1"``), or None if not present.
    """
    ref = resource.get("subject") or resource.get("patient")
    if not isinstance(ref, dict):
        return None
    reference = ref.get("reference")
    if not isinstance(reference, str) or "/" not in reference:
        return None
    return reference.rsplit("/", 1)[-1]


def _document_paths_by_id(
    patients: dict[str, PatientRecord], paths_by_type: Mapping[DocType, str]
) -> dict[str, str]:
    """Map each seeded document's id to the fixture PDF configured for its type.

    Args:
        patients: The seeded resource map.
        paths_by_type: The fixture PDF configured per document type.

    Returns:
        Document id -> PDF path, for every seeded document whose type has a configured PDF.
    """
    resolved: dict[str, str] = {}
    for record in patients.values():
        for resource in record.get("DocumentReference", []) or []:
            if not isinstance(resource, dict):
                continue
            doc_type = resolve_doc_type(resource)
            resource_id = resource.get("id")
            if doc_type is None or not isinstance(resource_id, str):
                continue
            path = paths_by_type.get(doc_type)
            if path is not None:
                resolved[resource_id] = path
    return resolved


class FixtureFhirClient:
    """FHIR client that replays recorded resources from memory, grouped by patient.

    Satisfies the ``FhirClient`` protocol with zero network and zero token, so the whole agent
    service is buildable and testable without the PHP module or a live FHIR server. Tests
    construct it directly from a resource map; ``fixture`` dev mode loads the bundled seed via
    :meth:`from_seed`.
    """

    def __init__(
        self,
        patients: dict[str, PatientRecord],
        document_pdf_paths: Mapping[DocType, str] | None = None,
    ) -> None:
        self._patients = patients
        # Bytes served by get_document_bytes so the fixture path exercises the real OCR pipeline
        # (mirrors HttpFhirClient fetching Binary bytes in prod).
        #
        # Configured per document TYPE but resolved to an id->path map here, because the byte source
        # is asked for an ID, not a type. Serving one global PDF for every id — as this client used
        # to — hands the lab report's page to an intake extraction: the recorded intake values are
        # right, but none of them can be located on the wrong document's text layer, so every fact
        # is silently dropped. Resolving each seeded DocumentReference's own category is what keeps
        # the two apart.
        self._document_pdf_by_id = _document_paths_by_id(patients, document_pdf_paths or {})

    @classmethod
    def from_seed(
        cls, document_pdf_paths: Mapping[DocType, str] | None = None
    ) -> "FixtureFhirClient":
        """Build a client from the FHIR fixtures bundled in ``fhir/seed/``.

        Args:
            document_pdf_paths: Optional fixture PDFs, one per document type, served by
                ``get_document_bytes`` to whichever seeded document has that type.

        Returns:
            A client seeded with every patient found in the seed directory.
        """
        return cls.from_directory(_SEED_DIR, document_pdf_paths)

    @classmethod
    def from_directory(
        cls, directory: Path, document_pdf_paths: Mapping[DocType, str] | None = None
    ) -> "FixtureFhirClient":
        """Build a client from every FHIR JSON file in a directory.

        Each file may be a single ``Patient`` resource or a collection/searchset ``Bundle``
        containing a ``Patient`` and its related resources. Related resources are filed under the
        patient they reference.

        Args:
            directory: Directory of FHIR resource / ``Bundle`` JSON files.

        Returns:
            A client keyed by patient logical id.

        Raises:
            ValueError: If a Bundle carries no ``Patient`` resource.
        """
        patients: dict[str, PatientRecord] = {}
        for path in sorted(directory.glob("*.json")):
            document = json.loads(path.read_text())
            cls._ingest_document(document, patients, source=str(path))
        return cls(patients, document_pdf_paths)

    @classmethod
    def _ingest_document(
        cls, document: dict[str, Any], patients: dict[str, PatientRecord], *, source: str
    ) -> None:
        """Fold one JSON document (bare Patient or Bundle) into the patient map."""
        if document.get("resourceType") == "Patient":
            patient_id = document.get("id")
            if isinstance(patient_id, str):
                patients.setdefault(patient_id, {})["Patient"] = document
            return

        patient_resources = bundle_resources(document, "Patient")
        if not patient_resources:
            raise ValueError(f"{source}: Bundle has no Patient resource")
        for patient in patient_resources:
            patient_id = patient.get("id")
            if isinstance(patient_id, str):
                patients.setdefault(patient_id, {})["Patient"] = patient
        for resource_type in (
            "Condition",
            "MedicationRequest",
            "AllergyIntolerance",
            "Encounter",
            "Observation",
            "DocumentReference",
        ):
            for resource in bundle_resources(document, resource_type):
                patient_id = _patient_ref_id(resource)
                if patient_id is None:
                    continue
                patients.setdefault(patient_id, {}).setdefault(resource_type, []).append(resource)

    def _resources(self, patient_id: str, resource_type: str) -> list[dict[str, Any]]:
        """Return the recorded resources of one type for a patient (empty list if none)."""
        record = self._patients.get(patient_id)
        if record is None:
            return []
        resources = record.get(resource_type, [])
        return resources if isinstance(resources, list) else []

    async def get_patient(self, patient_id: str) -> PatientDemographics:
        record = self._patients.get(patient_id)
        if record is None or "Patient" not in record:
            raise FhirError(f"no fixture Patient for id {patient_id!r}")
        return PatientDemographics.from_fhir(record["Patient"])

    async def get_problems(self, patient_id: str) -> list[Problem]:
        return [Problem.from_fhir(r) for r in self._resources(patient_id, "Condition")]

    async def get_medications(self, patient_id: str) -> list[Medication]:
        parsed = [Medication.from_fhir(r) for r in self._resources(patient_id, "MedicationRequest")]
        return dedup_medications(parsed)

    async def get_allergies(self, patient_id: str) -> list[Allergy]:
        return [Allergy.from_fhir(r) for r in self._resources(patient_id, "AllergyIntolerance")]

    async def get_encounters(self, patient_id: str) -> list[Encounter]:
        return [Encounter.from_fhir(r) for r in self._resources(patient_id, "Encounter")]

    async def get_encounter_note(self, patient_id: str, encounter_id: str) -> list[NoteContent]:
        notes = [NoteContent.from_fhir(r) for r in self._resources(patient_id, "DocumentReference")]
        return [note for note in notes if note.encounter_id == encounter_id]

    async def get_lab_observations(
        self, patient_id: str, *, code: str | None = None
    ) -> list[LabObservation]:
        # The live client filters category/code server-side; do it here so fixture-backed runs
        # exercise the same narrowing and a fixture cannot leak a vital into a lab trend.
        observations = [
            LabObservation.from_fhir(r)
            for r in self._resources(patient_id, "Observation")
            if _is_laboratory(r)
        ]
        if code:
            observations = [o for o in observations if o.code == code]
        return sorted(observations, key=lambda observation: observation.effective_date or "")

    async def get_documents(self, patient_id: str) -> list[UploadedDocumentSummary]:
        summaries = (
            UploadedDocumentSummary.try_from_fhir(r)
            for r in self._resources(patient_id, "DocumentReference")
        )
        return [summary for summary in summaries if summary is not None]

    async def get_document_bytes(self, document_id: str) -> bytes:
        path = self._document_pdf_by_id.get(document_id)
        if path is None:
            raise FhirError(f"fixture FHIR client has no document PDF for {document_id}")
        try:
            return Path(path).read_bytes()
        except OSError as exc:
            raise FhirError(f"could not read fixture document {path}") from exc

    async def ping(self) -> None:
        # In-memory fixtures are always reachable.
        return None
