from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


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
# This increment (JOS-53, hybrid RAG) produces only ``GuidelineCitation``. The Week-1 FHIR
# ``SourceRef`` above is untouched; converging it into this union as a ``fhir`` arm — and
# routing the grounding gate by ``source_type`` — is a tracked follow-up (see
# context/specs/hybrid-rag-pipeline.md §3.3).
# ---------------------------------------------------------------------------


class CitationSourceType(StrEnum):
    """The kind of source a :class:`Citation` points at (W2_ARCHITECTURE.md §3.3).

    ``GUIDELINE`` is produced this increment. ``LAB_PDF`` / ``INTAKE_FORM`` are reserved for
    the document-extraction increment: their variants are declared so the union is extensible,
    but nothing produces them yet.
    """

    GUIDELINE = "guideline"
    LAB_PDF = "lab_pdf"
    INTAKE_FORM = "intake_form"


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


class LabPdfCitation(CitationBase):
    """RESERVED (document-extraction increment): a citation to an extracted lab-PDF field.

    Declared so :data:`Citation` is a genuine, extensible union today. Not produced by any
    code yet. When the extractor lands it will additionally carry the native bounding-box
    coordinates + page for the click-to-source overlay (W2_ARCHITECTURE.md §3.3).
    """

    source_type: Literal[CitationSourceType.LAB_PDF] = CitationSourceType.LAB_PDF


class IntakeFormCitation(CitationBase):
    """RESERVED (document-extraction increment): a citation to an extracted intake-form field.

    Declared for union extensibility; not produced by any code yet.
    """

    source_type: Literal[CitationSourceType.INTAKE_FORM] = CitationSourceType.INTAKE_FORM


# The general citation-contract type (for the eventual final-answer gate). The retriever
# narrows to GuidelineCitation — it only ever emits guideline citations this increment.
Citation = Annotated[
    GuidelineCitation | LabPdfCitation | IntakeFormCitation,
    Field(discriminator="source_type"),
]
