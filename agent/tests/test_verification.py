from datetime import date

from copilot.fhir.models import Allergy, Encounter, NoteContent, PatientDemographics, Problem
from copilot.schemas import ChatResponse, Claim, SourceRef
from copilot.verification import FetchLog, quote_in_text, resolve_claims


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


def test_quote_grounds_despite_leading_capitalization_of_a_lifted_span() -> None:
    # Regression (JOS-89, prod trace d42c2738): the model lifted a clause from mid-sentence and
    # capitalized its first letter as a standalone quote ("The diagnosis..." for the source's
    # "...the diagnosis..."). The gate was case-sensitive, so it false-rejected a verbatim span,
    # the model's retry then broke the schema, and the whole turn degraded to the "could not
    # attribute to record" refusal. If this breaks, that intermittent prod refusal returns.
    chunk = (
        "An abnormal screening result should be confirmed with repeat testing: "
        "the diagnosis of type 2 diabetes should be confirmed with repeat testing."
    )
    quote = "The diagnosis of type 2 diabetes should be confirmed with repeat testing"

    assert quote_in_text(quote, chunk) == quote


def test_quote_matching_still_rejects_a_paraphrase() -> None:
    # Guards that case-folding only tolerates capitalization, not meaning: a reworded span that is
    # not a verbatim substring must still fail, or the anti-hallucination gate stops doing its job.
    chunk = "Kidney function tests should be repeated within 2 weeks of the initial finding."
    paraphrase = "Repeat kidney tests after a fortnight."

    assert quote_in_text(paraphrase, chunk) is None


def test_capitalized_note_quote_grounds_end_to_end() -> None:
    # Same regression as above, but through the full resolve_claims gate on a free-text note: a
    # claim whose quote differs from the note only by a capitalized first letter must ground, not
    # land in offenders.
    log = FetchLog()
    log.record(
        "DocumentReference",
        "d1",
        NoteContent(
            resource_id="d1",
            type_display="Progress note",
            date="2026-01-15",
            text="the patient reports wheezing at night.",
        ),
    )
    claim = Claim(
        text="Patient reported nocturnal wheezing",
        source=SourceRef(
            resource_type="DocumentReference",
            resource_id="d1",
            quote="The patient reports wheezing at night",
        ),
    )

    grounded, offenders = resolve_claims(ChatResponse(summary="...", claims=[claim]), log)

    assert offenders == []
    assert grounded.claims[0].source.label == "Progress note"


def _encounter_and_condition_log() -> FetchLog:
    log = FetchLog()
    log.record(
        "Encounter",
        "e1",
        Encounter(
            resource_id="e1",
            type="Encounter for check up",
            reason="Emergency room admission",
            start_date="2026-01-06",
        ),
    )
    log.record(
        "Condition",
        "c9",
        Problem(
            resource_id="c9",
            display="Concussion",
            clinical_status="active",
            onset_date="2026-07-07",
        ),
    )
    return log


def test_claim_with_supporting_citations_grounds_and_stamps_each_record() -> None:
    # A statement that legitimately draws on two records (a visit and a diagnosis) must ground BOTH:
    # the primary in source and the other in supporting, each stamped with its own value + identity.
    # If this breaks, a multi-record claim would show only one record's provenance on the card.
    claim = Claim(
        text="Emergency room admission on 6 January 2026; concussion is on the problem list",
        source=SourceRef(resource_type="Encounter", resource_id="e1", field="start_date"),
        supporting=[
            SourceRef(resource_type="Condition", resource_id="c9", field="clinical_status")
        ],
    )

    grounded, offenders = resolve_claims(
        ChatResponse(summary="...", claims=[claim]),
        _encounter_and_condition_log(),
    )

    assert offenders == []
    src = grounded.claims[0].source
    sup = grounded.claims[0].supporting[0]
    assert src.label == "Emergency room admission"  # reason preferred over the hardcoded type
    assert sup.label == "Concussion"
    assert sup.value == "active"


def test_claim_is_rejected_when_a_supporting_citation_is_ungrounded() -> None:
    # The whole point of citing every record a statement draws on: if the model asserts a second
    # record (e.g. a diagnosis) but cites one that was never fetched, the claim must be rejected —
    # otherwise an uncited/inferred record slips through on the back of a valid primary citation.
    claim = Claim(
        text="ER visit for a diagnosis we never actually read",
        source=SourceRef(resource_type="Encounter", resource_id="e1", field="start_date"),
        supporting=[
            SourceRef(resource_type="Condition", resource_id="ghost", field="clinical_status")
        ],
    )

    _, offenders = resolve_claims(
        ChatResponse(summary="...", claims=[claim]),
        _encounter_and_condition_log(),
    )

    assert len(offenders) == 1


def test_encounter_identity_prefers_real_category_over_hardcoded_type() -> None:
    # OpenEMR hardcodes FHIR Encounter.type to a generic "check up" for every visit, so the identity
    # must fall back to the reason (the real category) or every encounter card looks identical and
    # an ER visit is mislabeled a checkup.
    assert (
        Encounter(
            resource_id="e1", type="Encounter for check up", reason="Emergency room admission"
        ).citation_identity.label
        == "Emergency room admission"
    )
    # ...and when there is no reason, it falls back to the type rather than showing nothing.
    assert (
        Encounter(resource_id="e2", type="Encounter for check up").citation_identity.label
        == "Encounter for check up"
    )
