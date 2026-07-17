from typing import cast

from copilot.fhir.client import FhirClient
from copilot.graph.deps import GraphDeps
from copilot.ingestion.extractor import DocumentExtractor, ExtractedDocument
from copilot.ingestion.registry import DocumentFactRegistry
from copilot.ingestion.schemas import (
    AbnormalFlag,
    BoundingBox,
    Citation,
    DocType,
    LabReport,
    LabResult,
)
from copilot.main import _answer_payload, _build_evidence
from copilot.rag.models import EvidenceSnippet
from copilot.rag.retriever import EvidenceRetriever
from copilot.retrieval import GUIDELINE_RESOURCE_TYPE, ChunkRegistry
from copilot.schemas import ChatResponse, CitationSourceType, Claim, GuidelineCitation, SourceRef
from copilot.verification import FetchLog


def _snippet(
    chunk_id: str, section: str, text: str, score: float, *, year: str = "2022"
) -> EvidenceSnippet:
    """A recorded guideline snippet, as the retriever would leave it in the ChunkRegistry."""
    return EvidenceSnippet(
        citation=GuidelineCitation(
            source_id="gina-main-report-2022",
            page_or_section=section,
            field_or_chunk_id=chunk_id,
            quote_or_value=text,
        ),
        guideline="asthma",
        source_url="https://ginasthma.org/2022",
        year=year,
        rerank_score=score,
    )


def _guideline_claim(text: str, chunk_id: str, quote: str) -> Claim:
    """A final claim citing a guideline chunk (as the answerer emits, pre-gate-stamping)."""
    return Claim(
        text=text,
        source=SourceRef(resource_type=GUIDELINE_RESOURCE_TYPE, resource_id=chunk_id, quote=quote),
    )

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


def test_build_evidence_dedupes_by_chunk_and_orders_by_relevance() -> None:
    # The evidence panel counts distinct SOURCES, not claim sentences: two claims citing the same
    # chunk collapse to one card, and cards order best-match-first. This is the decoupling that
    # fixes the "(N) = claim count" defect — a card also carries the score/year/url the claim omits.
    registry = ChunkRegistry()
    registry.record_all(
        [
            _snippet("c-low", "Key Points", "risk factors for exacerbations", 0.55),
            _snippet("c-high", "Assessment of asthma", "symptom control has two domains", 0.95),
        ]
    )
    answer = ChatResponse(
        summary="…",
        claims=[
            _guideline_claim("two domains", "c-high", "symptom control has two domains"),
            _guideline_claim("restates it", "c-high", "symptom control has two domains"),
            _guideline_claim("exacerbation risks", "c-low", "risk factors for exacerbations"),
        ],
    )

    evidence = _build_evidence(answer, registry)

    assert [e["chunk_id"] for e in evidence] == ["c-high", "c-low"]  # deduped, ranked by relevance
    assert evidence[0]["relevance_score"] == 0.95
    assert evidence[0]["year"] == "2022"
    assert evidence[0]["source_url"] == "https://ginasthma.org/2022"
    assert evidence[0]["quote"] == "symptom control has two domains"


def _deps_with_extractions(extractions: dict[str, ExtractedDocument]) -> GraphDeps:
    """A GraphDeps carrying the given extractions; deps _answer_payload never reads are stubbed."""
    return GraphDeps(
        fhir=cast(FhirClient, None),
        patient_id="1",
        correlation_id="cid",
        retriever=cast(EvidenceRetriever, None),
        fetched=FetchLog(),
        chunks=ChunkRegistry(),
        documents=DocumentFactRegistry(),
        extractor=cast(DocumentExtractor, None),
        extractions=extractions,
    )


def _lab_extraction() -> ExtractedDocument:
    return ExtractedDocument(
        document_id="doc-lab",
        doc_type=DocType.LAB_PDF,
        report=LabReport(
            results=[
                LabResult(
                    test_name="Hemoglobin A1c",
                    loinc="4548-4",
                    value="8.2",
                    unit="%",
                    abnormal_flag=AbnormalFlag.HIGH,
                    citation=Citation(
                        quote_or_value="8.2",
                        bounding_box=BoundingBox(page=1, x=72.0, y=144.0, width=96.0, height=12.0),
                    ),
                )
            ]
        ),
    )


def test_answer_payload_carries_derived_facts_from_this_turns_extractions() -> None:
    # The sidebar posts these to the write-back endpoint (JOS-81). If they don't reach the response
    # body, a physician's uploaded labs are read but never offered for the chart.
    answer = ChatResponse(summary="…", claims=[])
    deps = _deps_with_extractions({"doc-lab": _lab_extraction()})

    payload = _answer_payload(answer, ChunkRegistry(), deps)

    assert len(payload["derived_facts"]) == 1
    assert payload["derived_facts"][0]["document_id"] == "doc-lab"
    assert payload["derived_facts"][0]["facts"][0]["loinc"] == "4548-4"


def test_answer_payload_derived_facts_is_empty_when_nothing_was_extracted() -> None:
    # A turn that read no document must not carry a derived_facts group — the sidebar fires no POST.
    answer = ChatResponse(summary="…", claims=[])
    payload = _answer_payload(answer, ChunkRegistry(), _deps_with_extractions({}))

    assert payload["derived_facts"] == []


def test_build_evidence_excludes_non_guideline_citations() -> None:
    # Only guideline chunks the answer cites are evidence — a FHIR record claim is a patient fact,
    # not guideline evidence, so it must not surface as a source card.
    registry = ChunkRegistry()
    registry.record_all([_snippet("c1", "Assessment of asthma", "text one", 0.9)])
    answer = ChatResponse(
        summary="…",
        claims=[
            Claim(
                text="date of birth",
                source=SourceRef(resource_type="Patient", resource_id="1", field="birth_date"),
            )
        ],
    )

    assert _build_evidence(answer, registry) == []
