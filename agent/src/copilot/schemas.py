from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from copilot.ingestion.schemas import BoundingBox, DocType


class SourceRef(BaseModel):
    """A citation binding a claim to a specific field of a resource the agent actually read.

    The load-bearing contract for the verification gate (ARCHITECTURE.md §7). Two citation modes,
    both resolved deterministically against the resource a tool returned:

    - **Structured** (coded fields): set ``field``; the gate resolves ``(resource_type,
      resource_id, field)`` to the exact record value.
    - **Free-text note**: set ``quote`` to the verbatim supporting span; the gate checks it is a
      substring of the fetched note's text.

    Either way the gate rejects a claim that does not resolve, and ``value`` is stamped in **by
    code, from the fetched record** — never written by the model.
    """

    model_config = ConfigDict(frozen=True)

    resource_type: str = Field(description="FHIR resource type, e.g. 'Patient'")
    resource_id: str = Field(description="FHIR resource logical id")
    field: str | None = Field(
        default=None,
        description="Field name in the tool's returned data the claim draws from, e.g. birth_date",
    )
    quote: str | None = Field(
        default=None,
        description=(
            "For a free-text note citation only: the EXACT verbatim span from the note that "
            "supports the claim, copied word-for-word (not paraphrased). Use `field` instead for "
            "structured resources."
        ),
    )
    value: str | None = Field(
        default=None,
        description="The actual record value. Leave empty — the system fills this from the record.",
    )
    label: str | None = Field(
        default=None,
        description=(
            "The record's human-recognizable name (e.g. 'Asthma'). Leave empty — the system fills "
            "it from the cited record so the card names the specific record, not just its type."
        ),
    )
    date: str | None = Field(
        default=None,
        description="The cited record's key date (e.g. onset). Leave empty — the system fills it.",
    )
    date_label: str | None = Field(
        default=None,
        description="What `date` means for this record (e.g. 'Onset'). Leave empty — system-set.",
    )

    # --- Click-to-source document provenance (JOS-57) ---
    # Present only when the cited record derives from an uploaded document (lab_pdf / intake_form).
    # System-stamped from the extraction sidecar (the derived FHIR record carries the value but not
    # the pixel box — W2_ARCHITECTURE §3.3/§6), NEVER written by the model. The verification gate
    # ignores these — overlay provenance, not verified fields. `to_citation` projects them onto the
    # document `Citation` arm named by `doc_type`, which the sidebar's click-to-source consumes.
    document_id: str | None = Field(
        default=None,
        description="Binary/DocumentReference id of the source document. Leave empty — system-set.",
    )
    doc_type: DocType | None = Field(
        default=None,
        description=(
            "Which kind of document the fact was read from. Leave empty — the system stamps this "
            "from the document's category."
        ),
    )
    page: int | None = Field(
        default=None,
        description="1-based source page the value was read from. Leave empty — system-set.",
    )
    bounding_box: BoundingBox | None = Field(
        default=None,
        description=(
            "Box of the value on the source page in PDF points (72-DPI), for the click-to-source "
            "overlay. Leave empty — system-set from the extraction sidecar; absent means no box."
        ),
    )

    def to_citation(self) -> "Citation":
        """Project this grounded citation onto the canonical wire ``Citation`` (§3.3).

        A *pure* projection of the **stamped** ``SourceRef`` (``value``/``label``/``date`` already
        filled by the grounding gate), so the sidebar's click-to-source (JOS-57) gets the
        machine-readable contract with no second lookup: a **document-extraction** fact (carrying
        the JOS-57 overlay provenance) projects to the document arm its ``doc_type`` names, with
        its page + bounding box; a guideline reference reads the stamped ``label``/``date``; a FHIR
        reads its resource type/id and field. Kept off :class:`Claim` so it never enters an LLM
        output schema.

        The document arm is selected by ``doc_type`` — deliberately **not** by ``resource_type`` and
        **not** by the presence of a ``bounding_box``:

        - Document facts are tagged by their eventual write target
          (``ingestion.registry.resource_type_for``), so a ``Patient`` read from FHIR and a
          ``Patient`` fact read off an intake form both exist. Only ``doc_type`` — stamped by the
          gate from the document's OpenEMR category — tells them apart, which makes inferring the
          arm from the resource type broken by design.
        - A document fact whose value could not be boxed still belongs to its own arm; branching on
          the box would silently demote it to a FHIR citation.

        Returns:
            The typed :data:`Citation` variant for this reference, carrying the claim's specific
            grounded value/quote (and, for a document fact, the click-to-source box).
        """
        quote_or_value = self.value or self.quote or ""
        if self.doc_type is not None:
            # Document-extraction fact: carries page + box (PDF points) for the overlay.
            source_id = self.document_id or f"{self.resource_type}/{self.resource_id}"
            page_or_section = str(self.page) if self.page is not None else self.resource_type
            field_or_chunk_id = self.field or self.resource_id
            match self.doc_type:
                case DocType.LAB_PDF:
                    return LabPdfCitation(
                        source_id=source_id,
                        page_or_section=page_or_section,
                        field_or_chunk_id=field_or_chunk_id,
                        quote_or_value=quote_or_value,
                        page=self.page,
                        bounding_box=self.bounding_box,
                    )
                case DocType.INTAKE_FORM:
                    return IntakeFormCitation(
                        source_id=source_id,
                        page_or_section=page_or_section,
                        field_or_chunk_id=field_or_chunk_id,
                        quote_or_value=quote_or_value,
                        page=self.page,
                        bounding_box=self.bounding_box,
                    )
        if self.resource_type == CitationSourceType.GUIDELINE.value:
            return GuidelineCitation(
                source_id=self.label or self.resource_id,
                page_or_section=self.date or self.resource_id,
                field_or_chunk_id=self.resource_id,
                quote_or_value=quote_or_value,
            )
        return FhirCitation(
            source_id=f"{self.resource_type}/{self.resource_id}",
            page_or_section=self.resource_type,
            field_or_chunk_id=self.field or "(none)",
            quote_or_value=quote_or_value,
        )


class Claim(BaseModel):
    """A single factual statement in the agent's answer, with its supporting citation.

    Structuring the answer as ``list[Claim]`` (rather than free prose) is what makes the
    grounding gate a deterministic code check instead of an LLM judgement.
    """

    model_config = ConfigDict(frozen=True)

    text: str = Field(description="The factual statement, phrased for the physician")
    source: SourceRef = Field(description="The primary record this statement is traceable to")
    supporting: list[SourceRef] = Field(
        default_factory=list,
        description=(
            "Any ADDITIONAL records this statement also draws on, beyond `source`. If a statement "
            "mentions more than one record (say a visit and a diagnosis), cite the primary one in "
            "`source` and every other one here; the gate verifies all of them, so an uncited or "
            "merely-inferred record is rejected. Prefer atomic statements about one record; leave "
            "this empty then."
        ),
    )


class ChatResponse(BaseModel):
    """The agent's structured, verifiable answer to one ``POST /chat`` turn.

    Every factual assertion lives in ``claims`` with a citation; ``summary`` is the
    human-facing prose the physician reads, which must not assert anything not covered by a
    claim. The verification gate runs over ``claims``. ``follow_ups`` are suggested next
    questions — not factual assertions — so the gate does not touch them.
    """

    summary: str = Field(description="Short prose orientation for the physician")
    claims: list[Claim] = Field(description="Every factual statement, each citing a source")
    follow_ups: list[str] = Field(
        default_factory=list,
        description=(
            "Two or three short next questions this physician is most likely to ask given THIS "
            "patient and THIS answer — the natural next click, phrased as the physician would type "
            "it (e.g. 'Is the epinephrine auto-injector current?'). Each must be answerable from "
            "this patient's record via the available tools. Omit rather than pad; leave empty when "
            "nothing meaningful follows."
        ),
    )


class Evidence(BaseModel):
    """One distinct guideline source that grounded the answer — a source card for the sidebar.

    The evidence panel shows SOURCES, not claim sentences: this is deduplicated by chunk and
    ordered by relevance, so its count reflects how many distinct sources back the answer, not how
    many claims the model wrote. Built by code from the retrieved
    :class:`~copilot.rag.models.EvidenceSnippet` (never the model), so it carries the reranker
    ``relevance_score`` and the presentation metadata (``source_url``, ``year``) the minimal
    citation contract omits. Added to the response body in ``_answer_payload`` — deliberately NOT a
    field on the LLM-facing :class:`ChatResponse` (mirrors the per-claim ``citations`` projection).
    See context/specs/evidence-gating-and-presentation.md §3.2.
    """

    model_config = ConfigDict(frozen=True)

    source_id: str = Field(description="The guideline source id, e.g. 'gina-main-report-2022'")
    section: str = Field(description="Section heading the snippet was drawn from")
    quote: str = Field(description="The verbatim guideline text that grounded the answer")
    chunk_id: str = Field(description="The retrieval chunk id — the dedup key")
    relevance_score: float = Field(
        description=(
            "Cohere rerank score in [0, 1]; higher is more relevant. Used to order the cards; not "
            "displayed (uncalibrated across queries, so a band would imply false precision)."
        )
    )
    source_url: str | None = Field(default=None, description="Public URL of the source guideline")
    year: str | None = Field(default=None, description="Source publication year, e.g. '2022'")
    anchor_quote: str | None = Field(
        default=None,
        description=(
            "A verbatim span from the source; the sidebar builds a text-fragment deep link "
            "(…#:~:text=) from it so 'View source' highlights the exact passage instead of "
            "opening at page 1. None when the chunk has no reliable anchor."
        ),
    )


class ChatRequest(BaseModel):
    """The inbound ``POST /chat`` payload for a single agent turn."""

    patient_id: str = Field(description="FHIR Patient logical id the turn is scoped to")
    message: str = Field(description="The physician's question")
    conversation_id: str | None = Field(
        default=None,
        description="Opaque id echoed from a prior turn's response; omit to start a new one",
    )


# ---------------------------------------------------------------------------
# Week-2 unified citation contract (W2_ARCHITECTURE.md §3.3, §6)
#
# Every clinical claim in a Week-2 answer — retrieved OR extracted — carries citation
# metadata in one machine-readable shape, keyed on ``source_type``:
#     { source_type, source_id, page_or_section, field_or_chunk_id, quote_or_value }
# We model it as a discriminated (tagged) union so each source kind is a typed variant and
# adding a new one (document extraction) is additive, not a rewrite.
#
# All four arms are produced today: the supervisor graph's final answer emits ``GuidelineCitation``
# (guideline evidence) and ``FhirCitation`` (the converged projection of the Week-1 FHIR
# ``SourceRef``), and ``SourceRef.to_citation`` routes a document-extraction fact to
# ``LabPdfCitation`` or ``IntakeFormCitation`` on its stamped ``doc_type``.
# Routing the grounding gate by ``source_type`` is still a tracked follow-up (see
# context/specs/hybrid-rag-pipeline.md §3.3).
# ---------------------------------------------------------------------------


class CitationSourceType(StrEnum):
    """The kind of source a :class:`Citation` points at (W2_ARCHITECTURE.md §3.3).

    ``GUIDELINE`` (evidence) and ``FHIR`` (patient-record claims) come from the supervisor graph's
    final answer; ``LAB_PDF`` / ``INTAKE_FORM`` come from a document-extraction fact, selected by
    the ``doc_type`` the grounding gate stamps onto its citation.
    """

    GUIDELINE = "guideline"
    LAB_PDF = "lab_pdf"
    INTAKE_FORM = "intake_form"
    FHIR = "fhir"


class CitationBase(BaseModel):
    """The five-field citation shape shared by every source type (W2_ARCHITECTURE.md §3.3).

    Inheritance is deliberate here: :data:`Citation` is a discriminated (tagged) union whose
    variants share this exact contract and differ only by their ``source_type`` tag. That is
    the idiomatic Pydantic tagged-union shape — distinct from the general compose-over-inherit
    guidance, which targets domain models, not union arms.
    """

    model_config = ConfigDict(frozen=True)

    source_id: str = Field(
        description="Stable id of the source document/record the claim is traceable to"
    )
    page_or_section: str = Field(
        description="Where in the source the support lives — PDF page or section heading"
    )
    field_or_chunk_id: str = Field(
        description="The specific unit within the source — schema field name or retrieval chunk id"
    )
    quote_or_value: str = Field(
        description="The verbatim supporting text (retrieved snippet) or extracted value"
    )


class GuidelineCitation(CitationBase):
    """A citation to a retrieved clinical-guideline chunk (the JOS-53 evidence path).

    Field mapping from the corpus chunk: ``source_id`` <- ``source``, ``page_or_section`` <-
    ``section``, ``field_or_chunk_id`` <- ``chunk_id``, ``quote_or_value`` <- the retrieved
    ``text``. The topic slug (``guideline``) and ``source_url`` ride on the surrounding
    :class:`~copilot.rag.models.EvidenceSnippet` as presentation metadata, not on the minimum
    citation shape.
    """

    source_type: Literal[CitationSourceType.GUIDELINE] = CitationSourceType.GUIDELINE


class DocumentCitationBase(CitationBase):
    """The extra provenance a citation to an *uploaded document* carries: where on the page it sits.

    A second level under the tagged-union carve-out :class:`CitationBase` documents — the same
    reasoning applies one step down: the ``lab_pdf`` and ``intake_form`` arms are one source kind
    (a document the extractor read and boxed) and differ only by their ``source_type`` tag, so the
    click-to-source geometry they both carry belongs on a shared base rather than repeated per arm.

    Absent ``bounding_box`` means the value could not be located on the page — the sidebar shows the
    citation without a rectangle, never a fabricated box (W2_ARCHITECTURE.md §3.3).
    """

    page: int | None = Field(
        default=None, description="1-based source page the value was read from."
    )
    bounding_box: BoundingBox | None = Field(
        default=None,
        description="Box of the value on the page in PDF points (72-DPI), for the overlay.",
    )


class LabPdfCitation(DocumentCitationBase):
    """A citation to an extracted lab-PDF field, with the click-to-source overlay geometry.

    Produced by :meth:`SourceRef.to_citation` for a fact whose ``doc_type`` is ``LAB_PDF`` (JOS-57).
    """

    source_type: Literal[CitationSourceType.LAB_PDF] = CitationSourceType.LAB_PDF


class IntakeFormCitation(DocumentCitationBase):
    """A citation to an extracted intake-form field, with the click-to-source overlay geometry.

    Produced by :meth:`SourceRef.to_citation` for a fact whose ``doc_type`` is ``INTAKE_FORM``.
    Unlike a lab fact — always an ``Observation`` — an intake fact is tagged by its eventual write
    target (``Patient``/``AllergyIntolerance``/``MedicationRequest``/``FamilyMemberHistory``), types
    a FHIR read also returns, so this arm is reachable only via ``doc_type``.
    """

    source_type: Literal[CitationSourceType.INTAKE_FORM] = CitationSourceType.INTAKE_FORM


class FhirCitation(CitationBase):
    """A citation to a patient-record claim read from a FHIR resource (the Week-1 read path).

    The converged form of the Week-1 :class:`SourceRef` on the shared five-field contract, so the
    supervisor's final answer emits patient-record and guideline claims in one machine-readable
    shape. Field mapping from a resolved ``SourceRef``: ``source_id`` <- ``resource_type/id``,
    ``page_or_section`` <- the resource type, ``field_or_chunk_id`` <- the cited field, and
    ``quote_or_value`` <- the gate-stamped record value. ``SourceRef`` remains the gate's internal
    grounding shape; this is its wire/UI projection (routing the grounding gate by ``source_type``
    is still the tracked follow-up).
    """

    source_type: Literal[CitationSourceType.FHIR] = CitationSourceType.FHIR


# The general citation-contract type. Today the retriever narrows to GuidelineCitation and the
# final-answer response emits GuidelineCitation (evidence) + FhirCitation (record) per claim.
Citation = Annotated[
    GuidelineCitation | LabPdfCitation | IntakeFormCitation | FhirCitation,
    Field(discriminator="source_type"),
]
