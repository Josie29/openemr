from datetime import date

from copilot.fhir.models import Allergy, NoteContent, PatientDemographics, Problem
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


def test_grounded_claim_stamps_the_record_identity_for_the_card() -> None:
    # Catches the provenance gap where every active Condition rendered an identical
    # "Condition — Clinical status: active" card: the specific record's name and onset date must be
    # stamped from the fetched record (never the model) so the asthma proof is distinguishable from
    # any other active condition. If this breaks, proof cards stop naming the record they cite.
    log = FetchLog()
    log.record(
        "Condition",
        "c1",
        Problem(
            resource_id="c1",
            display="Asthma",
            code="195967001",
            clinical_status="active",
            onset_date="2019-04-02",
        ),
    )

    grounded, offenders = resolve_claims(
        ChatResponse(
            summary="...",
            claims=[
                _claim(
                    "Asthma is active",
                    "clinical_status",
                    resource_type="Condition",
                    resource_id="c1",
                )
            ],
        ),
        log,
    )

    assert offenders == []
    source = grounded.claims[0].source
    assert source.label == "Asthma"
    assert source.date == "2019-04-02"
    assert source.date_label == "Onset"


def test_identity_omits_a_date_for_records_with_no_single_defining_date() -> None:
    # An AllergyIntolerance has no onset/authored date in our projection; the card must show the
    # substance name without inventing a date. Guards against a stray "Date None" on the card or a
    # crash when date/date_label are absent.
    log = FetchLog()
    log.record(
        "AllergyIntolerance",
        "a1",
        Allergy(resource_id="a1", substance="Peanut", clinical_status="active"),
    )

    grounded, _ = resolve_claims(
        ChatResponse(
            summary="...",
            claims=[
                _claim(
                    "Peanut allergy is active",
                    "clinical_status",
                    resource_type="AllergyIntolerance",
                    resource_id="a1",
                )
            ],
        ),
        log,
    )

    source = grounded.claims[0].source
    assert source.label == "Peanut"
    assert source.date is None
    assert source.date_label is None


def test_note_quote_citation_is_stamped_with_the_note_identity() -> None:
    # A free-text note citation grounds via the verbatim quote, not a field; the card still needs to
    # name which note (type + date). Guards that identity stamping covers the quote-mode branch, not
    # just structured-field citations.
    log = FetchLog()
    log.record(
        "DocumentReference",
        "d1",
        NoteContent(
            resource_id="d1",
            type_display="Progress note",
            date="2026-01-15",
            text="Patient reports wheezing at night.",
        ),
    )
    claim = Claim(
        text="Patient reported nocturnal wheezing",
        source=SourceRef(
            resource_type="DocumentReference", resource_id="d1", quote="wheezing at night"
        ),
    )

    grounded, offenders = resolve_claims(
        ChatResponse(summary="...", claims=[claim]),
        log,
    )

    assert offenders == []
    source = grounded.claims[0].source
    assert source.label == "Progress note"
    assert source.date == "2026-01-15"
