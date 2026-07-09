from dataclasses import dataclass, field
from typing import Any

from copilot.schemas import ChatResponse, Claim, SourceRef

_MISSING = object()


@dataclass
class FetchLog:
    """Registry of the typed resources a turn's tools returned, keyed by (type, id).

    Stores the actual parsed objects (not just their ids) so the verification gate can resolve
    a claim's cited field to the real value in the record — the field-level grounding check
    (ARCHITECTURE.md §7). This is what makes grounding deterministic: it compares against the
    fetched data, not the model's say-so.
    """

    _objects: dict[tuple[str, str], Any] = field(default_factory=dict)

    def record(self, resource_type: str, resource_id: str, resource: Any) -> None:
        """Record a resource a tool returned this turn.

        Args:
            resource_type: FHIR resource type, e.g. ``"Patient"``.
            resource_id: FHIR resource logical id.
            resource: The parsed, typed resource object (e.g. ``PatientDemographics``).
        """
        self._objects[(resource_type, resource_id)] = resource

    def resolve(self, ref: SourceRef) -> str | None:
        """Resolve a citation to the real value in the fetched resource.

        Args:
            ref: The claim's citation.

        Returns:
            The stringified record value, or None when the claim cannot be grounded — the
            resource was not fetched this turn, no field is cited, the field does not exist on
            the resource, or its value is null.
        """
        if ref.field is None:
            return None
        resource = self._objects.get((ref.resource_type, ref.resource_id))
        if resource is None:
            return None
        value = getattr(resource, ref.field, _MISSING)
        if value is _MISSING or value is None:
            return None
        return str(value)


def resolve_claims(response: ChatResponse, fetched: FetchLog) -> tuple[ChatResponse, list[Claim]]:
    """Ground each claim against the fetched data, stamping the real value into its citation.

    For every claim, resolve its citation to the actual value in the record a tool returned.
    Grounded claims get that value stamped into ``source.value`` — code-populated, never
    model-authored — so a reader can compare the claim to the exact record value. Claims that
    cannot be resolved are returned as offenders for the gate to reject.

    Args:
        response: The agent's candidate answer.
        fetched: The registry of resources tools returned this turn.

    Returns:
        A tuple of (the response with values stamped onto grounded claims, the offending
        claims that could not be grounded). An empty offender list means every claim is
        grounded and the stamped response is safe to return.
    """
    grounded: list[Claim] = []
    offenders: list[Claim] = []
    for claim in response.claims:
        value = fetched.resolve(claim.source)
        if value is None:
            offenders.append(claim)
        else:
            stamped_source = claim.source.model_copy(update={"value": value})
            grounded.append(claim.model_copy(update={"source": stamped_source}))
    return response.model_copy(update={"claims": grounded}), offenders
