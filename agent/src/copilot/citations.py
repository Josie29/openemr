from copilot.ingestion import Citation, SourceType
from copilot.retrieval import GUIDELINE_RESOURCE_TYPE, ChunkRegistry, EvidenceSnippet
from copilot.schemas import Claim, SourceRef


def to_citation(ref: SourceRef, snippet: EvidenceSnippet | None = None) -> Citation:
    """Map a grounded, gate-stamped ``SourceRef`` to the canonical wire ``Citation``.

    The two citation shapes are layers, not competitors: ``SourceRef`` is the grounding gate's
    internal shape (resolve a field/quote, stamp the real value); :class:`Citation` is the
    project-wide wire/UI contract (``copilot.ingestion``) the sidebar's click-to-source (JOS-57)
    consumes. This maps the former onto the latter *after* grounding, so the emitted citation
    carries the code-verified value, never a model-authored one.

    For a guideline reference, the chunk the claim grounded on carries richer provenance than the
    ``SourceRef`` does (the source guideline slug, section, URL), so ``snippet`` is used when
    available to fill ``source_id``/``page_or_section`` accurately; otherwise the mapping falls back
    to what the ``SourceRef`` itself carries.

    Args:
        ref: The claim's grounded citation (``value`` already stamped by the gate).
        snippet: For a guideline reference, the chunk it grounded on (from the chunk registry);
            ignored for FHIR-record references.

    Returns:
        The canonical :class:`Citation`. ``bounding_box`` is None here — pixel boxes come only from
        the ``lab_pdf`` document-extraction path (JOS-54), not from record or guideline claims.
    """
    quote_or_value = ref.value or ref.quote or ""
    if ref.resource_type == GUIDELINE_RESOURCE_TYPE:
        return Citation(
            quote_or_value=quote_or_value,
            source_type=SourceType.GUIDELINE,
            source_id=snippet.source_id if snippet is not None else ref.resource_id,
            page_or_section=snippet.section if snippet is not None else ref.date,
            field_or_chunk_id=ref.resource_id,
        )
    return Citation(
        quote_or_value=quote_or_value,
        source_type=SourceType.OPENEMR_RECORD,
        source_id=f"{ref.resource_type}/{ref.resource_id}",
        page_or_section=None,
        field_or_chunk_id=ref.field,
    )


def build_claim_citations(claim: Claim, chunks: ChunkRegistry) -> list[Citation]:
    """Build the canonical wire citations for every source a grounded claim draws on.

    One :class:`Citation` per reference — the primary ``source`` and each ``supporting`` entry — so
    a multi-source claim exposes all of its provenance in the shared shape, not just its primary.

    Args:
        claim: A grounded claim from the final answer (citations already stamped by the gate).
        chunks: The turn's chunk registry, used to enrich guideline citations with real provenance.

    Returns:
        The claim's citations, primary first, then supporting in order.
    """
    refs = [claim.source, *claim.supporting]
    return [to_citation(ref, chunks.get(ref.resource_id)) for ref in refs]
