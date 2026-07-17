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
    anchor_quote: str | None = Field(
        default=None,
        description="A verbatim span copied from the source document (unlike `text`, which is "
        "lightly reworded). The sidebar builds a text-fragment deep link from it so 'View source' "
        "highlights the exact passage. None when no reliable span was found (e.g. a source that "
        "blocks fetching). Backfilled by scripts/backfill_corpus_anchors.py.",
    )

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
    anchor_quote: str | None = Field(
        default=None,
        description="Verbatim source span for deep-linking the source card to the cited passage.",
    )
    rerank_score: float = Field(
        description="Cohere relevance score in [0, 1]; higher is more relevant to the query"
    )

    @property
    def text(self) -> str:
        """The snippet text (the cited quote)."""
        return self.citation.quote_or_value


class RetrievedGuideline(BaseModel):
    """The evidence-retriever model's view of one retrieved snippet: only what it must cite from.

    Deliberately minimal — the chunk id to cite and the text to quote from. Every other field of the
    underlying :class:`EvidenceSnippet` (source, section, url, year, rerank score, and the verbatim
    ``anchor_quote`` used to deep-link the source card) is system-owned provenance: it is stamped
    onto the citation from the chunk id at grounding and serialization time, so the model neither
    authors nor sees it — the same discipline ``SourceRef``'s ``value``/``label``/``date`` already
    follow.

    Withholding ``anchor_quote`` in particular is load-bearing (JOS-89): the model used to copy that
    verbatim *source* span into its claim ``quote``, but the grounding gate checks the (lightly
    reworded) chunk ``text`` — so an anchor that differs from the text by as little as a leading
    capital could never match, and the turn failed its retries into a refusal. With only ``text`` in
    view, the model can only quote the exact field the gate verifies, so that mismatch is
    unrepresentable rather than merely tolerated.
    """

    model_config = ConfigDict(frozen=True)

    chunk_id: str = Field(description="Cite this as the claim's `resource_id`.")
    text: str = Field(
        description="The guideline snippet. Copy a verbatim span of THIS as the claim's `quote`."
    )

    @classmethod
    def from_snippet(cls, snippet: EvidenceSnippet) -> "RetrievedGuideline":
        """Project a retrieved snippet onto the minimal view handed to the model.

        Args:
            snippet: The full snippet the retriever recorded (kept server-side for stamping).

        Returns:
            The chunk-id + text view — provenance dropped so the model cannot cite a field the
            grounding gate does not check.
        """
        return cls(chunk_id=snippet.citation.field_or_chunk_id, text=snippet.text)
