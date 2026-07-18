from pydantic import BaseModel, Field

from copilot.fhir.models import UploadedDocumentSummary
from copilot.ingestion.extractor import ExtractedDocument
from copilot.ingestion.schemas import DocType, IntakeForm, LabReport
from copilot.rag.models import EvidenceSnippet

# Response models for the read-only Week-2 HTTP endpoints (JOS-63 / JOS-67).
#
# These are the WIRE contract for the three subsystem endpoints — /documents,
# /documents/{id}/extraction, /evidence — that let a caller exercise each Week-2 subsystem
# directly, without a full /chat turn. They are kept OFF the LLM-facing models in `schemas.py`
# (the agent never sees them) and reuse the existing domain models (`UploadedDocumentSummary`,
# `LabReport`/`IntakeForm`, `EvidenceSnippet`) so the endpoints stay thin projections. FastAPI
# generates the committed OpenAPI (`agent/openapi.json`) from exactly these types.


class DocumentsResponse(BaseModel):
    """``GET /documents`` — the patient's uploaded, extractable documents (metadata only)."""

    patient_id: str = Field(description="FHIR Patient logical id the listing is scoped to")
    documents: list[UploadedDocumentSummary] = Field(
        description="Uploaded PDF/Binary documents whose category maps to an extraction schema"
    )


class ExtractionResponse(BaseModel):
    """``GET /documents/{id}/extraction`` — one document's strict-schema facts.

    ``report`` is the schema the document's TYPE selected — a ``LabReport`` for a ``lab_pdf``, an
    ``IntakeForm`` for an ``intake_form`` — with each fact carrying its citation (bounding box +
    verbatim value) and per-value confidence. ``doc_type`` is the human-readable discriminant,
    resolved server-side from the document's OpenEMR category (never a caller input).
    """

    document_id: str = Field(description="The extracted DocumentReference id")
    doc_type: DocType = Field(description="Which schema the document's category selected")
    report: LabReport | IntakeForm = Field(
        description="LabReport for a lab_pdf, IntakeForm for an intake_form"
    )

    @classmethod
    def from_extracted(cls, extracted: ExtractedDocument) -> "ExtractionResponse":
        """Project an :class:`ExtractedDocument` (frozen dataclass) onto the wire response."""
        return cls(
            document_id=extracted.document_id,
            doc_type=extracted.doc_type,
            report=extracted.report,
        )


class EvidenceItem(BaseModel):
    """One ranked guideline chunk in the flat evidence-snippet wire shape.

    A projection of the retriever's :class:`EvidenceSnippet` onto the minimal citation contract
    (source_id / section / chunk_id / text) plus the reranker ``score`` and presentation
    ``topic``/``source_url`` — the fields the citation card and click-to-source consume.
    """

    source_id: str = Field(description="The source-guideline document id")
    section: str = Field(description="Section heading the chunk was drawn from")
    chunk_id: str = Field(description="Stable id of the retrieved chunk")
    text: str = Field(description="The chunk text — the cited quote")
    score: float = Field(description="Reranker relevance score in [0, 1]; higher is more relevant")
    source_url: str | None = Field(default=None, description="Public URL of the source guideline")
    topic: str = Field(description="Guideline topic slug, e.g. 'hypertension'")

    @classmethod
    def from_snippet(cls, snippet: EvidenceSnippet) -> "EvidenceItem":
        """Project a retrieved snippet onto the flat wire item."""
        citation = snippet.citation
        return cls(
            source_id=citation.source_id,
            section=citation.page_or_section,
            chunk_id=citation.field_or_chunk_id,
            text=snippet.text,
            score=snippet.rerank_score,
            source_url=snippet.source_url,
            topic=snippet.guideline,
        )


class EvidenceResponse(BaseModel):
    """``GET /evidence`` — the ranked guideline chunks retrieved for a query."""

    query: str = Field(description="The retrieval query that was run")
    top_n: int = Field(description="The effective number of snippets requested")
    evidence: list[EvidenceItem] = Field(description="Ranked guideline chunks, most relevant first")
