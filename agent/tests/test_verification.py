from copilot.schemas import ChatResponse, Claim, SourceRef
from copilot.verification import FetchLog, unattributed_claims


def _claim(text: str, resource_type: str, resource_id: str) -> Claim:
    return Claim(text=text, source=SourceRef(resource_type=resource_type, resource_id=resource_id))


def test_grounded_claim_passes() -> None:
    # Guards: a claim citing a resource a tool actually returned this turn must be accepted —
    # otherwise the gate would reject every truthful answer and the agent could never respond.
    fetched = FetchLog()
    fetched.record("Patient", "1")
    response = ChatResponse(summary="...", claims=[_claim("DOB is 1958-03-12", "Patient", "1")])

    assert unattributed_claims(response, fetched) == []


def test_claim_citing_unfetched_resource_is_flagged() -> None:
    # Guards the core hallucination defense: a claim citing a resource that was never fetched
    # (a fabricated fact dressed up with a plausible citation) must be caught, so it can be
    # turned into a ModelRetry rather than reaching the physician.
    fetched = FetchLog()
    fetched.record("Patient", "1")
    response = ChatResponse(
        summary="...",
        claims=[
            _claim("DOB is 1958-03-12", "Patient", "1"),
            _claim("A1c was 9.2 last week", "Observation", "999"),  # never fetched
        ],
    )

    offenders = unattributed_claims(response, fetched)

    assert [c.source.resource_id for c in offenders] == ["999"]


def test_empty_registry_flags_every_claim() -> None:
    # Guards the case where the model answers without calling any tool: with nothing fetched,
    # no claim can be grounded, so all must be flagged (the agent must fetch before it asserts).
    response = ChatResponse(summary="...", claims=[_claim("anything", "Patient", "1")])

    assert len(unattributed_claims(response, FetchLog())) == 1
