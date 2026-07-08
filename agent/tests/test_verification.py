from datetime import date

from copilot.fhir.models import PatientDemographics
from copilot.schemas import ChatResponse, Claim, SourceRef
from copilot.verification import FetchLog, resolve_claims


def _log_with_patient() -> FetchLog:
    log = FetchLog()
    log.record(
        "Patient",
        "1",
        PatientDemographics(
            resource_id="1",
            full_name="Marisol A Reyes",
            birth_date=date(1958, 3, 12),
            gender="female",
        ),
    )
    return log


def _claim(
    text: str, field: str | None, *, resource_type: str = "Patient", resource_id: str = "1"
) -> Claim:
    return Claim(
        text=text,
        source=SourceRef(resource_type=resource_type, resource_id=resource_id, field=field),
    )


def test_grounded_claim_gets_the_real_record_value_stamped() -> None:
    # Guards the trust feature: a claim citing a real field must pass AND have the actual record
    # value stamped in by code (not the model), so a reader can compare the claim against the
    # exact value it came from. If this breaks, citations show no verifiable value.
    grounded, offenders = resolve_claims(
        ChatResponse(summary="...", claims=[_claim("Born March 1958", "birth_date")]),
        _log_with_patient(),
    )

    assert offenders == []
    assert grounded.claims[0].source.value == "1958-03-12"


def test_claim_citing_a_nonexistent_field_is_rejected() -> None:
    # Guards the tightened field-level grounding: a claim citing a field that isn't in the
    # fetched data (a fabricated datum dressed up with a plausible citation) must be caught,
    # so it becomes a ModelRetry rather than reaching the physician.
    _, offenders = resolve_claims(
        ChatResponse(summary="...", claims=[_claim("A1c was 9.2% last week", "a1c")]),
        _log_with_patient(),
    )

    assert len(offenders) == 1


def test_claim_citing_an_unfetched_resource_is_rejected() -> None:
    # Guards resource-level grounding: a claim citing a resource no tool returned this turn.
    unfetched = _claim("...", "code", resource_type="Observation", resource_id="999")
    _, offenders = resolve_claims(
        ChatResponse(summary="...", claims=[unfetched]),
        _log_with_patient(),
    )

    assert len(offenders) == 1


def test_claim_without_a_field_is_rejected() -> None:
    # Guards that a claim must cite a specific field, not just a resource — a value can only be
    # resolved (and thus grounded) against a named field.
    _, offenders = resolve_claims(
        ChatResponse(summary="...", claims=[_claim("something", None)]),
        _log_with_patient(),
    )

    assert len(offenders) == 1
