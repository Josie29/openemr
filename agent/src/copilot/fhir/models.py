from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PatientDemographics(BaseModel):
    """Typed projection of a FHIR R4 ``Patient`` resource (ARCHITECTURE.md §4 tool table).

    Only the fields UC-1 orientation needs. Produced by parsing the raw FHIR resource at the
    boundary (parse-don't-validate), so downstream code — the agent and the verification gate
    — works with a value that guarantees its own validity.

    NOTE (flagged per implementation-prompt-01 §5): the architecture names ``fhir.resources``
    for boundary parsing. The skeleton parses the ``Patient`` dict directly to stay runnable
    without pinning that library's R4-vs-R5 module layout; ``from_fhir`` is the single seam to
    swap in ``fhir.resources.R4B.patient.Patient`` when the med/problem tools land in ``-02``.
    """

    model_config = ConfigDict(frozen=True)

    patient_id: str = Field(description="FHIR Patient.id")
    full_name: str | None = Field(default=None, description="Preferred human name, rendered")
    birth_date: date | None = Field(default=None, description="Patient.birthDate")
    gender: str | None = Field(default=None, description="Patient.gender")

    @classmethod
    def from_fhir(cls, resource: dict[str, Any]) -> "PatientDemographics":
        """Parse a FHIR R4 ``Patient`` resource dict into typed demographics.

        Args:
            resource: A FHIR ``Patient`` resource as returned by the FHIR API (parsed JSON).

        Returns:
            The typed ``PatientDemographics`` projection.

        Raises:
            ValueError: If ``resource`` is not a ``Patient`` resource or lacks an ``id``.
        """
        if resource.get("resourceType") != "Patient":
            raise ValueError(f"expected a Patient resource, got {resource.get('resourceType')!r}")

        patient_id = resource.get("id")
        if not isinstance(patient_id, str) or not patient_id:
            raise ValueError("Patient resource is missing a string 'id'")

        birth_date_raw = resource.get("birthDate")
        birth_date = date.fromisoformat(birth_date_raw) if isinstance(birth_date_raw, str) else None

        return cls(
            patient_id=patient_id,
            full_name=_render_name(resource.get("name")),
            birth_date=birth_date,
            gender=resource.get("gender"),
        )


def _render_name(names: Any) -> str | None:
    """Render a FHIR ``HumanName`` list into a single display string.

    Prefers an ``official`` use, then the first entry. Returns None when no usable name is
    present — a data-quality gap the agent must state plainly rather than paper over.

    Args:
        names: The ``Patient.name`` value (a list of ``HumanName`` dicts, or anything).

    Returns:
        A rendered name, or None if none is available.
    """
    if not isinstance(names, list) or not names:
        return None

    chosen = next((n for n in names if isinstance(n, dict) and n.get("use") == "official"), None)
    chosen = chosen or next((n for n in names if isinstance(n, dict)), None)
    if chosen is None:
        return None

    if isinstance(chosen.get("text"), str) and chosen["text"].strip():
        return chosen["text"].strip()

    given = chosen.get("given")
    given_part = " ".join(g for g in given if isinstance(g, str)) if isinstance(given, list) else ""
    family = chosen.get("family") if isinstance(chosen.get("family"), str) else ""
    rendered = f"{given_part} {family}".strip()
    return rendered or None
