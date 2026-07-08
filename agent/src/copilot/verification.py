from dataclasses import dataclass, field

from copilot.schemas import ChatResponse, Claim, SourceRef


@dataclass
class FetchLog:
    """Registry of the FHIR resources the agent's tools actually returned this turn.

    Tools call :meth:`record` as they return data; the verification gate calls
    :meth:`resolves` to confirm a claim's citation points at something really fetched. This
    is what makes the grounding check deterministic — it compares against fact, not the
    model's say-so (ARCHITECTURE.md §7).
    """

    _fetched: set[tuple[str, str]] = field(default_factory=set)

    def record(self, resource_type: str, resource_id: str) -> None:
        """Record that a tool returned a specific FHIR resource this turn.

        Args:
            resource_type: FHIR resource type, e.g. ``"Patient"``.
            resource_id: FHIR resource logical id.
        """
        self._fetched.add((resource_type, resource_id))

    def resolves(self, ref: SourceRef) -> bool:
        """Whether a citation points at a resource that was actually fetched this turn.

        Args:
            ref: The claim's citation.

        Returns:
            True if a tool returned that exact resource this turn, else False.
        """
        return (ref.resource_type, ref.resource_id) in self._fetched


def unattributed_claims(response: ChatResponse, fetched: FetchLog) -> list[Claim]:
    """Return the claims whose citation does not resolve to a fetched resource.

    An empty result means every claim is grounded — the response may reach the physician.
    A non-empty result is a grounding violation the gate turns into a ``ModelRetry``.

    Args:
        response: The agent's candidate structured answer.
        fetched: The registry of resources tools returned this turn.

    Returns:
        The offending claims (empty when the response is fully grounded).
    """
    return [claim for claim in response.claims if not fetched.resolves(claim.source)]
