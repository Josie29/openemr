from copilot.retrieval import GUIDELINE_RESOURCE_TYPE
from copilot.schemas import CitationSourceType, SourceRef

# SourceRef.to_citation() projects a GROUNDED (gate-stamped) SourceRef onto the canonical wire
# Citation. These tests build already-stamped refs (value/label/date set, as the gate leaves them)
# and check the projection — the same shape the /chat response emits per claim.


def test_guideline_ref_projects_to_a_guideline_citation() -> None:
    # Guards the wire contract for evidence: a stamped guideline ref maps to a GuidelineCitation
    # carrying the guideline source id (from the stamped label), section (from date), chunk id, and
    # the claim's specific grounded span — what JOS-57's click-to-source links the card to.
    ref = SourceRef(
        resource_type=GUIDELINE_RESOURCE_TYPE,
        resource_id="ada-1",  # the chunk id
        quote="Screen adults aged 35 years or older",
        value="Screen adults aged 35 years or older",  # gate-stamped matched span
        label="ada-soc-2025",  # gate-stamped guideline source id
        date="Screening",  # gate-stamped section
    )
    citation = ref.to_citation()

    assert citation.source_type is CitationSourceType.GUIDELINE
    assert citation.source_id == "ada-soc-2025"
    assert citation.page_or_section == "Screening"
    assert citation.field_or_chunk_id == "ada-1"
    assert citation.quote_or_value == "Screen adults aged 35 years or older"


def test_fhir_ref_projects_to_a_fhir_citation() -> None:
    # Guards that a record-derived claim maps to the FHIR arm of the citation union with a
    # resource-typed source id, so a patient fact and a guideline fact are distinguishable.
    ref = SourceRef(
        resource_type="Patient", resource_id="1", field="birth_date", value="1958-03-12"
    )
    citation = ref.to_citation()

    assert citation.source_type is CitationSourceType.FHIR
    assert citation.source_id == "Patient/1"
    assert citation.field_or_chunk_id == "birth_date"
    assert citation.quote_or_value == "1958-03-12"


def test_fhir_ref_without_a_field_uses_the_required_fallback() -> None:
    # Guards the contract's required-string invariant: a note claim (quote mode, no field) must
    # still produce a valid FhirCitation — field_or_chunk_id falls back rather than being empty.
    ref = SourceRef(
        resource_type="DocumentReference", resource_id="n1", quote="x", value="x"
    )
    citation = ref.to_citation()

    assert citation.source_type is CitationSourceType.FHIR
    assert citation.field_or_chunk_id == "(none)"
    assert citation.quote_or_value == "x"
