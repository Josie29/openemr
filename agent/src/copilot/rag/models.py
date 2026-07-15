from pydantic import BaseModel, ConfigDict, Field

from copilot.schemas import GuidelineCitation


class CorpusChunk(BaseModel):
    """One row of a corpus ``*.jsonl`` file (the JOS-52 curation output).

    This is the on-disk contract for the guideline corpus and the Qdrant payload shape — the
    indexer parses each JSONL line into this model (rejecting malformed rows at the boundary),
    and the retriever validates each Qdrant payload back into it before building a citation, so
    a snippet can never reach the answer model without full, well-formed provenance.
    """

    model_config = ConfigDict(frozen=True)

    chunk_id: str = Field(description="Stable unique id for this chunk; the Qdrant point key")
    guideline: str = Field(description="Topic slug, e.g. 'afib-anticoagulation'")
    source: str = Field(description="The source-document id, e.g. 'statpearls-paroxysmal-af-2023'")
    source_url: str = Field(description="Public URL of the source guideline")
    section: str = Field(description="Section heading the chunk was drawn from")
    date: str = Field(description="Source date/year, e.g. '2023'")
    text: str = Field(description="The chunk text — what is embedded and, when retrieved, cited")

    def to_citation(self) -> GuidelineCitation:
        """Project this chunk onto the unified guideline citation contract (W2_ARCHITECTURE §3.3).

        Returns:
            The ``GuidelineCitation`` for this chunk: ``source`` -> ``source_id``, ``section`` ->
            ``page_or_section``, ``chunk_id`` -> ``field_or_chunk_id``, ``text`` ->
            ``quote_or_value``.
        """
        return GuidelineCitation(
            source_id=self.source,
            page_or_section=self.section,
            field_or_chunk_id=self.chunk_id,
            quote_or_value=self.text,
        )


class EvidenceSnippet(BaseModel):
    """One reranked guideline snippet returned by the evidence-retriever.

    Carries the machine-readable :class:`~copilot.schemas.GuidelineCitation` (the contract the
    answer model must attach to any guideline claim) plus presentation metadata — the topic
    slug and ``source_url`` for the citation card / click-to-source — and the reranker's
    relevance score for ordering and observability. Every snippet is guaranteed to carry full
    chunk metadata by construction (built from a validated :class:`CorpusChunk`), which is the
    data-side of the evidence guardrail (W2_ARCHITECTURE §4.3): the retriever cannot emit a
    snippet lacking a citation.
    """

    model_config = ConfigDict(frozen=True)

    citation: GuidelineCitation = Field(description="The machine-readable guideline citation")
    guideline: str = Field(description="Topic slug the snippet belongs to")
    source_url: str | None = Field(default=None, description="Public URL of the source guideline")
    year: str | None = Field(
        default=None, description="Source publication year (the chunk's `date`), e.g. '2022'"
    )
    rerank_score: float = Field(
        description="Cohere relevance score in [0, 1]; higher is more relevant to the query"
    )

    @property
    def text(self) -> str:
        """The snippet text (the cited quote)."""
        return self.citation.quote_or_value
