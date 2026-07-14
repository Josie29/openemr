from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from copilot.fhir.models import ResourceIdentity
from copilot.schemas import SourceRef
from copilot.verification import quote_in_text

# The synthetic FHIR-shaped resource type a guideline citation uses, so a claim drawn from the
# corpus flows through the SAME SourceRef/gate machinery as a claim drawn from a FHIR record: the
# claim cites (GUIDELINE_RESOURCE_TYPE, chunk_id) with a verbatim quote, and the guideline resolver
# grounds it. Keeping one citation shape is what lets the final answer ground FHIR facts and
# guideline evidence in a single pass. The exact wire-level citation contract
# (source_type/source_id/page_or_section/field_or_chunk_id) is JOS-55's to formalize.
GUIDELINE_RESOURCE_TYPE = "GuidelineChunk"


class EvidenceSnippet(BaseModel):
    """One retrieved, source-attributed guideline chunk the evidence-retriever can cite.

    The unit the hybrid-RAG pipeline (JOS-53) returns and the evidence-retriever grounds against:
    the chunk ``text`` plus the provenance a citation needs (guideline ``source_id``/``title`` and
    the ``section`` it came from). A claim grounds against a snippet by quoting its ``text``
    verbatim, exactly as a note citation grounds against a note body.
    """

    model_config = ConfigDict(frozen=True)

    chunk_id: str = Field(description="Stable id of this chunk within its source guideline")
    source_id: str = Field(description="Stable guideline slug, e.g. 'ada-soc-2025'")
    title: str = Field(description="Human-recognizable guideline title/publisher for the card")
    section: str | None = Field(
        default=None, description="Section/heading the chunk came from, if known"
    )
    text: str = Field(description="The chunk text; a claim grounds by quoting this verbatim")
    score: float | None = Field(
        default=None, description="Reranker relevance score, if the retriever supplied one"
    )

    @property
    def resource_type(self) -> str:
        """The synthetic resource type a claim cites this chunk by."""
        return GUIDELINE_RESOURCE_TYPE

    @property
    def resource_id(self) -> str:
        """The chunk id a claim cites this snippet by (the SourceRef ``resource_id``)."""
        return self.chunk_id

    @property
    def citation_identity(self) -> ResourceIdentity:
        """Name the guideline (and section) this chunk came from, for the evidence card.

        Section rides in ``date_label`` because :class:`ResourceIdentity` has no section slot yet;
        JOS-55's citation contract will give it a first-class field. Until then this keeps the
        section visible on the card rather than dropping it.
        """
        return ResourceIdentity(label=self.title, date=self.section, date_label="Section")


class Retriever(Protocol):
    """The hybrid-RAG retrieval surface the evidence-retriever worker depends on.

    Defined as a protocol so JOS-56 can be built and tested against a :class:`FakeRetriever`
    while JOS-53 (Qdrant hybrid search + Cohere rerank) is built independently. The real
    implementation — FastEmbed dense+sparse → Qdrant ``Fusion.RRF`` → Cohere rerank — satisfies
    this same interface and swaps in with no change to the worker.
    """

    async def retrieve(self, query: str, *, limit: int) -> list[EvidenceSnippet]:
        """Return the top grounded guideline snippets for a query, best first."""
        ...


@dataclass
class FakeRetriever:
    """An in-memory :class:`Retriever` for tests and offline runs (the JOS-53 seam).

    Returns a fixed, deterministic set of snippets filtered by a naive case-insensitive keyword
    overlap, so a driving test can exercise the full graph — retrieve, ground, cite — with no
    Qdrant, no embeddings, and no network. Not a search engine; just enough to make the
    evidence-retriever's grounding path real end to end.
    """

    snippets: Sequence[EvidenceSnippet] = field(default_factory=tuple)

    async def retrieve(self, query: str, *, limit: int) -> list[EvidenceSnippet]:
        """Return up to ``limit`` seeded snippets whose text overlaps the query's words.

        Args:
            query: The retrieval query (the physician's information need, reformulated).
            limit: The maximum number of snippets to return.

        Returns:
            The matching snippets (all seeded snippets when nothing overlaps, so a test still
            gets evidence), truncated to ``limit``.
        """
        words = {w for w in query.lower().split() if len(w) > 3}
        matched = [s for s in self.snippets if words & set(s.text.lower().split())]
        return list(matched or self.snippets)[:limit]


@dataclass
class ChunkRegistry:
    """Registry of the guideline snippets a turn retrieved — the guideline-evidence resolver.

    The evidence counterpart to :class:`copilot.verification.FetchLog`: it records the snippets
    the evidence-retriever pulled and resolves a claim's citation against them, so the same
    grounding gate that checks FHIR claims also checks guideline claims. A claim grounds only when
    its verbatim ``quote`` appears in the cited chunk's text — no unattributable evidence ships.
    """

    _snippets: dict[str, EvidenceSnippet] = field(default_factory=dict)

    def record_all(self, snippets: Sequence[EvidenceSnippet]) -> None:
        """Record retrieved snippets, keyed by chunk id, so claims can later cite them.

        Args:
            snippets: The snippets the retriever returned this turn.
        """
        for snippet in snippets:
            self._snippets[snippet.chunk_id] = snippet

    def resolve(self, ref: SourceRef) -> str | None:
        """Ground a guideline citation: its quote must appear verbatim in the cited chunk.

        Args:
            ref: The claim's citation (expected to name a guideline chunk and carry a quote).

        Returns:
            The matched quote when the chunk was retrieved this turn and its text contains the
            quote; otherwise None (wrong resource type, chunk not retrieved, or no/absent quote).
        """
        if ref.resource_type != GUIDELINE_RESOURCE_TYPE:
            return None
        snippet = self._snippets.get(ref.resource_id)
        if snippet is None or ref.quote is None:
            return None
        return quote_in_text(ref.quote, snippet.text)

    def identify(self, ref: SourceRef) -> ResourceIdentity | None:
        """Name the guideline (and section) a citation points at, for the evidence card.

        Args:
            ref: The claim's citation.

        Returns:
            The chunk's :class:`ResourceIdentity`, or None when it names a non-guideline resource
            or a chunk not retrieved this turn.
        """
        if ref.resource_type != GUIDELINE_RESOURCE_TYPE:
            return None
        snippet = self._snippets.get(ref.resource_id)
        return snippet.citation_identity if snippet is not None else None
