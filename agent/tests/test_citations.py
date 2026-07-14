from copilot.citations import build_claim_citations, to_citation
from copilot.ingestion import SourceType
from copilot.retrieval import GUIDELINE_RESOURCE_TYPE, ChunkRegistry, EvidenceSnippet
from copilot.schemas import Claim, SourceRef

_SNIPPET = EvidenceSnippet(
    chunk_id="ada-1",
    source_id="ada-soc-2025",
    title="ADA Standards of Care 2025",
    section="Screening",
    text="Screen adults aged 35 years or older for type 2 diabetes.",
    source_url="https://example.org/ada",
    date="2025",
)


def test_guideline_ref_maps_to_a_guideline_citation_with_chunk_provenance() -> None:
    # Guards the wire contract for evidence: a guideline claim must expose the GUIDELINE source
    # type and the real guideline slug/section (from the retrieved chunk, not the chunk id), so
    # JOS-57's click-to-source can link the card to the actual guideline.
    ref = SourceRef(
        resource_type=GUIDELINE_RESOURCE_TYPE,
        resource_id="ada-1",
        quote="Screen adults aged 35 years or older",
        value="Screen adults aged 35 years or older",  # stamped by the gate
    )
    citation = to_citation(ref, _SNIPPET)

    assert citation.source_type is SourceType.GUIDELINE
    assert citation.source_id == "ada-soc-2025"  # the guideline slug, not the chunk id
    assert citation.page_or_section == "Screening"
    assert citation.field_or_chunk_id == "ada-1"
    assert citation.quote_or_value == "Screen adults aged 35 years or older"


def test_fhir_ref_maps_to_an_openemr_record_citation() -> None:
    # Guards that a record-derived claim carries the OPENEMR_RECORD source type and a resource-typed
    # source id, so a patient fact and a guideline fact are distinguishable in the citation payload.
    ref = SourceRef(
        resource_type="Patient", resource_id="1", field="birth_date", value="1958-03-12"
    )
    citation = to_citation(ref)

    assert citation.source_type is SourceType.OPENEMR_RECORD
    assert citation.source_id == "Patient/1"
    assert citation.field_or_chunk_id == "birth_date"
    assert citation.quote_or_value == "1958-03-12"
    assert citation.bounding_box is None  # boxes come only from lab_pdf extraction (JOS-54)


def test_guideline_citation_falls_back_when_chunk_is_unknown() -> None:
    # Guards the degrade path: if the chunk isn't in the registry, the citation still emits usable
    # provenance from the SourceRef itself rather than crashing or dropping the citation.
    ref = SourceRef(
        resource_type=GUIDELINE_RESOURCE_TYPE, resource_id="ada-1", quote="x", value="x", date="S1"
    )
    citation = to_citation(ref, None)

    assert citation.source_type is SourceType.GUIDELINE
    assert citation.source_id == "ada-1"  # falls back to the chunk id
    assert citation.page_or_section == "S1"  # falls back to the ref's stamped section


def test_build_claim_citations_covers_primary_and_supporting() -> None:
    # Guards that a multi-source claim exposes ALL its provenance in the wire shape: a claim drawing
    # on a guideline AND a patient record must yield one citation per source, not just the primary.
    chunks = ChunkRegistry()
    chunks.record_all([_SNIPPET])
    claim = Claim(
        text="ADA advises screening; the patient is 68.",
        source=SourceRef(
            resource_type=GUIDELINE_RESOURCE_TYPE,
            resource_id="ada-1",
            quote="Screen",
            value="Screen",
        ),
        supporting=[
            SourceRef(
                resource_type="Patient", resource_id="1", field="birth_date", value="1958-03-12"
            )
        ],
    )

    citations = build_claim_citations(claim, chunks)

    assert [c.source_type for c in citations] == [SourceType.GUIDELINE, SourceType.OPENEMR_RECORD]
    assert citations[0].source_id == "ada-soc-2025"  # enriched from the registry
    assert citations[1].source_id == "Patient/1"
