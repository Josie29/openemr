import json
from pathlib import Path
from typing import Any

from copilot.fhir.client import FhirError
from copilot.fhir.models import PatientDemographics

_SEED_DIR = Path(__file__).parent / "seed"


class FixtureFhirClient:
    """FHIR client that replays recorded ``Patient`` resources from memory.

    Satisfies the ``FhirClient`` protocol with zero network and zero token, so the whole agent
    service is buildable and testable without the PHP module or a live FHIR server
    (implementation-prompt-01 §1.2). Tests construct it directly; ``fixture`` dev mode loads
    the bundled seed via :meth:`from_seed`.
    """

    def __init__(self, patients: dict[str, dict[str, Any]]) -> None:
        self._patients = patients

    @classmethod
    def from_seed(cls) -> "FixtureFhirClient":
        """Build a client from the FHIR ``Patient`` fixtures bundled in ``fhir/seed/``.

        Returns:
            A client seeded with every ``*.json`` Patient resource in the seed directory.
        """
        return cls.from_directory(_SEED_DIR)

    @classmethod
    def from_directory(cls, directory: Path) -> "FixtureFhirClient":
        """Build a client from every FHIR ``Patient`` JSON file in a directory.

        Args:
            directory: Directory containing FHIR ``Patient`` resource JSON files.

        Returns:
            A client keyed by each resource's ``id``.

        Raises:
            ValueError: If a file is not a ``Patient`` resource or lacks a string ``id``.
        """
        patients: dict[str, dict[str, Any]] = {}
        for path in sorted(directory.glob("*.json")):
            resource = json.loads(path.read_text())
            resource_id = resource.get("id")
            if resource.get("resourceType") != "Patient" or not isinstance(resource_id, str):
                raise ValueError(f"{path} is not a Patient resource with a string id")
            patients[resource_id] = resource
        return cls(patients)

    async def get_patient(self, patient_id: str) -> PatientDemographics:
        resource = self._patients.get(patient_id)
        if resource is None:
            raise FhirError(f"no fixture Patient for id {patient_id!r}")
        return PatientDemographics.from_fhir(resource)

    async def ping(self) -> None:
        # In-memory fixtures are always reachable.
        return None
