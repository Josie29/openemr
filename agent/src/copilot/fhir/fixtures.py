import json
from pathlib import Path
from typing import Any

from copilot.fhir.client import FhirError
from copilot.fhir.models import (
    Allergy,
    Encounter,
    Medication,
    NoteContent,
    PatientDemographics,
    Problem,
    bundle_resources,
    dedup_medications,
)

_SEED_DIR = Path(__file__).parent / "seed"

# A per-patient record: the Patient resource plus its lists of related resources, keyed by
# FHIR resourceType. Mirrors what the five FHIR read endpoints return for one patient.
PatientRecord = dict[str, Any]


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


class FixtureFhirClient:
    """FHIR client that replays recorded resources from memory, grouped by patient.

    Satisfies the ``FhirClient`` protocol with zero network and zero token, so the whole agent
    service is buildable and testable without the PHP module or a live FHIR server. Tests
    construct it directly from a resource map; ``fixture`` dev mode loads the bundled seed via
    :meth:`from_seed`.
    """

    def __init__(self, patients: dict[str, PatientRecord]) -> None:
        self._patients = patients

    @classmethod
    def from_seed(cls) -> "FixtureFhirClient":
        """Build a client from the FHIR fixtures bundled in ``fhir/seed/``.

        Returns:
            A client seeded with every patient found in the seed directory.
        """
        return cls.from_directory(_SEED_DIR)

    @classmethod
    def from_directory(cls, directory: Path) -> "FixtureFhirClient":
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
        return cls(patients)

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

    async def ping(self) -> None:
        # In-memory fixtures are always reachable.
        return None
