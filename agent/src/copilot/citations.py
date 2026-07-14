from copilot.rag.models import EvidenceSnippet
from copilot.retrieval import GUIDELINE_RESOURCE_TYPE, ChunkRegistry
from copilot.schemas import Citation, Claim, FhirCitation, GuidelineCitation, SourceRef


def to_citation(ref: SourceRef, snippet: EvidenceSnippet | None = None) -> Citation:
    """Map a grounded, gate-stamped ``SourceRef`` onto the canonical wire ``Citation`` union.

    The two citation shapes are layers, not competitors: ``SourceRef`` is the grounding gate's
    internal shape (resolve a field/quote, stamp the real value); the ``schemas`` ``Citation`` union
    (``GuidelineCitation`` / ``FhirCitation`` / …) is the project-wide wire/UI contract the
    sidebar's click-to-source (JOS-57) consumes. This maps the former onto the latter *after*
    grounding, so the emitted citation carries the code-verified value, never a model-authored one.

    A guideline reference reuses the retrieved chunk's own ``GuidelineCitation`` (built by JOS-53's
    retriever from validated corpus provenance) when the chunk is available; a FHIR-record reference
    is projected onto a ``FhirCitation``.

    Args:
        ref: The claim's grounded citation (``value`` already stamped by the gate).
        snippet: For a guideline reference, the chunk it grounded on (from the chunk registry);
            ignored for FHIR-record references.

    Returns:
        The canonical :class:`~copilot.schemas.Citation` variant for this reference.
    """
    quote_or_value = ref.value or ref.quote or ""
    if ref.resource_type == GUIDELINE_RESOURCE_TYPE:
        if snippet is not None:
            # Keep the chunk's real provenance, but carry the claim's SPECIFIC grounded span as the
            # quote (not the whole chunk text the snippet's own citation holds) — that is the exact
            # text the claim was verified against.
            return snippet.citation.model_copy(update={"quote_or_value": quote_or_value})
        # Fallback when the chunk isn't in the registry: build the guideline citation from the ref.
        return GuidelineCitation(
            source_id=ref.resource_id,
            page_or_section=ref.date or ref.resource_id,
            field_or_chunk_id=ref.resource_id,
            quote_or_value=quote_or_value,
        )
    return FhirCitation(
        source_id=f"{ref.resource_type}/{ref.resource_id}",
        page_or_section=ref.resource_type,
        field_or_chunk_id=ref.field or "(none)",
        quote_or_value=quote_or_value,
    )


def build_claim_citations(claim: Claim, chunks: ChunkRegistry) -> list[Citation]:
    """Build the canonical wire citations for every source a grounded claim draws on.

    One :class:`~copilot.schemas.Citation` per reference — the primary ``source`` and each
    ``supporting`` entry — so a multi-source claim exposes all of its provenance in the shared
    contract, not just its primary.

    Args:
        claim: A grounded claim from the final answer (citations already stamped by the gate).
        chunks: The turn's chunk registry, used to recover a guideline claim's own citation.

    Returns:
        The claim's citations, primary first, then supporting in order.
    """
    refs = [claim.source, *claim.supporting]
    return [to_citation(ref, chunks.get(ref.resource_id)) for ref in refs]
