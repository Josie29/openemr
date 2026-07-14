from collections.abc import Sequence
from dataclasses import dataclass, field

from copilot.fhir.models import ResourceIdentity
from copilot.rag.models import EvidenceSnippet
from copilot.schemas import CitationSourceType, SourceRef
from copilot.verification import Resolution, quote_in_text

# The resource-type tag a guideline citation carries on its SourceRef, so a corpus claim flows
# through the SAME SourceRef/gate machinery as a FHIR-record claim: the claim cites
# (GUIDELINE_RESOURCE_TYPE, chunk_id) with a verbatim quote, and this registry grounds it. Sourced
# from the canonical citation vocabulary (``schemas.CitationSourceType``) so the same token names a
# guideline source in the gate and in the wire-level ``Citation``. The chunk id rides in
# ``SourceRef.resource_id`` — which equals the snippet's ``citation.field_or_chunk_id``.
GUIDELINE_RESOURCE_TYPE = CitationSourceType.GUIDELINE.value


@dataclass
class ChunkRegistry:
    """Registry of the guideline snippets a turn retrieved — the guideline-evidence resolver.

    The evidence counterpart to :class:`copilot.verification.FetchLog`: it records the snippets the
    evidence-retriever pulled (JOS-53's :class:`~copilot.rag.models.EvidenceSnippet`) and resolves a
    claim's citation against them, so the one grounding gate that checks FHIR claims also checks
    guideline claims. A claim grounds only when its verbatim ``quote`` appears in the cited chunk's
    text — no unattributable evidence ships. JOS-53's retriever produces the snippets; this is the
    graph's grounding gate over them (the retriever itself does not verify a model's quote).
    """

    _snippets: dict[str, EvidenceSnippet] = field(default_factory=dict)

    def record_all(self, snippets: Sequence[EvidenceSnippet]) -> None:
        """Record retrieved snippets, keyed by chunk id, so claims can later cite them.

        Args:
            snippets: The snippets the retriever returned this turn.
        """
        for snippet in snippets:
            self._snippets[snippet.citation.field_or_chunk_id] = snippet

    def resolve(self, ref: SourceRef) -> Resolution | None:
        """Ground a guideline citation and identify its chunk in one lookup.

        The claim's quote must appear verbatim in the cited chunk; the identity (guideline source
        as the label, section in the date slot) is read from the same snippet.

        Args:
            ref: The claim's citation (expected to name a guideline chunk and carry a quote).

        Returns:
            The :class:`Resolution` (matched quote + chunk identity) when the chunk was retrieved
            this turn and its text contains the quote; otherwise None (wrong resource type, chunk
            not retrieved, or no/absent quote).
        """
        if ref.resource_type != GUIDELINE_RESOURCE_TYPE:
            return None
        snippet = self._snippets.get(ref.resource_id)
        if snippet is None or ref.quote is None:
            return None
        value = quote_in_text(ref.quote, snippet.text)
        if value is None:
            return None
        identity = ResourceIdentity(
            label=snippet.citation.source_id,
            date=snippet.citation.page_or_section,
            date_label="Section",
        )
        return Resolution(value=value, identity=identity)
