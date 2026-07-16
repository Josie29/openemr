import re
from pathlib import Path
from typing import Protocol, runtime_checkable

import cohere
from pydantic import ValidationError
from qdrant_client import AsyncQdrantClient, models

from copilot.config import RetrievalMode, Settings
from copilot.rag.corpus import load_corpus
from copilot.rag.models import CorpusChunk, EvidenceSnippet


class RetrievalError(RuntimeError):
    """Raised when evidence retrieval fails (Qdrant unreachable, rerank failed, bad payload).

    Lets the caller degrade gracefully — the evidence-retriever surfaces "no supporting
    guideline found" rather than fabricating one (W2_ARCHITECTURE.md §11) — without leaking
    vendor transport detail into user-facing output.
    """


@runtime_checkable
class EvidenceRetriever(Protocol):
    """Hybrid guideline retrieval, scoped to the non-PHI corpus.

    Two implementations share this protocol: a live Qdrant+Cohere one and an in-process
    fixture one for tests/offline dev. Callers depend on the protocol, never a concrete class,
    so the store/reranker can change without touching agent logic (mirrors ``FhirClient``).
    """

    async def retrieve(
        self,
        query: str,
        *,
        guideline: str | None = None,
        source: str | None = None,
        section: str | None = None,
        top_n: int | None = None,
    ) -> list[EvidenceSnippet]:
        """Return the top reranked guideline snippets for ``query``, most relevant first.

        Args:
            query: The clinical question (no patient identifiers — non-PHI corpus only).
            guideline: Optional topic-slug scope (payload filter).
            source: Optional source-document scope (payload filter).
            section: Optional section scope (payload filter).
            top_n: Number of snippets to return; defaults to the configured rerank top-n.

        Raises:
            RetrievalError: If retrieval or reranking fails.
        """
        ...

    async def aclose(self) -> None:
        """Release any held network clients."""
        ...


class QdrantEvidenceRetriever:
    """Live hybrid retriever: FastEmbed dense+sparse -> Qdrant RRF -> Cohere rerank (W2_ARCH §5).

    Both clients are long-lived (each holds a connection pool) and injected — the retriever is
    constructed once at app startup and closed on shutdown. Embedding is done in-process by
    FastEmbed inside ``qdrant-client`` (``models.Document``), so there is no separate embedding
    service.
    """

    def __init__(
        self,
        *,
        qdrant: AsyncQdrantClient,
        cohere_client: cohere.AsyncClientV2,
        collection: str,
        dense_model: str,
        sparse_model: str,
        rerank_model: str,
        prefetch_k: int,
        rerank_top_n: int,
        relevance_floor: float = 0.0,
    ) -> None:
        self._qdrant = qdrant
        self._cohere = cohere_client
        self._collection = collection
        self._dense_model = dense_model
        self._sparse_model = sparse_model
        self._rerank_model = rerank_model
        self._prefetch_k = prefetch_k
        self._rerank_top_n = rerank_top_n
        self._relevance_floor = relevance_floor

    async def retrieve(
        self,
        query: str,
        *,
        guideline: str | None = None,
        source: str | None = None,
        section: str | None = None,
        top_n: int | None = None,
    ) -> list[EvidenceSnippet]:
        limit = top_n if top_n is not None else self._rerank_top_n
        query_filter = _build_filter(guideline=guideline, source=source, section=section)

        # Hybrid: prefetch a dense (semantic) and a sparse (lexical) candidate set, each scoped
        # by the same payload filter, then fuse by rank with RRF (no score-scale tuning). Passing
        # models.Document triggers in-client FastEmbed embedding of the query. Query objects are
        # built OUTSIDE the try so a qdrant-client API-shape change raises loudly rather than
        # masquerading as a graceful "no evidence" — only the network call degrades.
        prefetch = [
            models.Prefetch(
                query=models.Document(text=query, model=self._dense_model),
                using="dense",
                limit=self._prefetch_k,
                filter=query_filter,
            ),
            models.Prefetch(
                query=models.Document(text=query, model=self._sparse_model),
                using="sparse",
                limit=self._prefetch_k,
                filter=query_filter,
            ),
        ]
        fusion = models.FusionQuery(fusion=models.Fusion.RRF)
        try:
            response = await self._qdrant.query_points(
                collection_name=self._collection,
                prefetch=prefetch,
                query=fusion,
                limit=self._prefetch_k,
                with_payload=True,
            )
        except Exception as exc:  # qdrant/httpx transport + response errors — degrade, don't leak
            raise RetrievalError("qdrant hybrid query failed") from exc

        points = response.points
        if not points:
            return []
        candidates = [_parse_payload(point.payload) for point in points]
        ranked = await self._rerank(query, candidates, limit)
        return _above_floor(ranked, self._relevance_floor)

    async def _rerank(
        self, query: str, candidates: list[CorpusChunk], top_n: int
    ) -> list[EvidenceSnippet]:
        """Rerank the fused candidates with Cohere and project the top-n onto EvidenceSnippets.

        Cohere scores every document and returns the top-n by relevance, so passing the whole fused
        set yields the true best-n — the caller applies the relevance floor to these.

        Raises:
            RetrievalError: If the Cohere rerank call fails.
        """
        documents = [chunk.text for chunk in candidates]
        try:
            reranked = await self._cohere.rerank(
                model=self._rerank_model,
                query=query,
                documents=documents,
                top_n=min(top_n, len(documents)),
            )
        except Exception as exc:  # cohere transport/api errors — degrade, don't leak
            raise RetrievalError("cohere rerank failed") from exc

        # results are sorted best-first; .index maps back into `candidates`.
        return [
            _to_snippet(candidates[result.index], result.relevance_score)
            for result in reranked.results
        ]

    async def aclose(self) -> None:
        # cohere.AsyncClientV2 (7.x) still exposes no public close()/aclose() — only an untyped
        # __aexit__, which strict typing rejects and whose pooled httpx client is released on
        # process exit regardless. Closing it here would buy only a cosmetic teardown at the cost
        # of a vendor type-ignore, so we close just the Qdrant client explicitly.
        await self._qdrant.close()


class FixtureEvidenceRetriever:
    """In-process keyword retriever over the in-repo corpus — no network, no Docker.

    For tests and offline dev (mirrors ``FixtureFhirClient``). Ranks chunks by term overlap
    with the query, applies the same payload-scope filters, and returns real
    :class:`GuidelineCitation`-carrying snippets so the whole downstream path is exercised
    without Qdrant or Cohere.
    """

    def __init__(
        self, chunks: list[CorpusChunk], rerank_top_n: int, relevance_floor: float = 0.0
    ) -> None:
        self._chunks = chunks
        self._rerank_top_n = rerank_top_n
        self._relevance_floor = relevance_floor

    @classmethod
    def from_corpus(
        cls, rerank_top_n: int, corpus_dir: Path | None = None, relevance_floor: float = 0.0
    ) -> "FixtureEvidenceRetriever":
        """Build a fixture retriever from the in-repo corpus.

        Args:
            rerank_top_n: Default number of snippets to return.
            corpus_dir: Optional override corpus directory; defaults to the repo corpus.
            relevance_floor: Minimum normalized term-overlap score a chunk must clear (mirrors the
                live rerank floor). Defaults to 0.0 (ungated) so offline dev/tests stay
                deterministic; the app wires the configured floor via ``build_retriever``.
        """
        return cls(load_corpus(corpus_dir), rerank_top_n, relevance_floor)

    async def retrieve(
        self,
        query: str,
        *,
        guideline: str | None = None,
        source: str | None = None,
        section: str | None = None,
        top_n: int | None = None,
    ) -> list[EvidenceSnippet]:
        limit = top_n if top_n is not None else self._rerank_top_n
        candidates = self._chunks
        if guideline is not None:
            candidates = [c for c in candidates if c.guideline == guideline]
        if source is not None:
            candidates = [c for c in candidates if c.source == source]
        if section is not None:
            candidates = [c for c in candidates if c.section == section]

        query_terms = _tokenize(query)
        scored: list[tuple[int, CorpusChunk]] = []
        for chunk in candidates:
            haystack = _tokenize(f"{chunk.text} {chunk.section} {chunk.guideline}")
            overlap = len(query_terms & haystack)
            if overlap:
                scored.append((overlap, chunk))
        scored.sort(key=lambda item: item[0], reverse=True)
        top = scored[:limit]
        # Normalize overlap to a [0, 1] pseudo-relevance so the shape (and the floor) match the live
        # path; `top`'s head is the global max (scored is sorted desc), and `default=1` covers the
        # empty case (only positive-overlap chunks enter `scored`, so no zero-division).
        max_overlap = max((overlap for overlap, _ in top), default=1)
        ranked = [_to_snippet(chunk, round(overlap / max_overlap, 4)) for overlap, chunk in top]
        return _above_floor(ranked, self._relevance_floor)

    async def aclose(self) -> None:
        return None


def build_retriever(settings: Settings) -> EvidenceRetriever | None:
    """Construct the app-lifetime evidence retriever from settings (DI at the edge).

    Returns ``None`` when live retrieval is selected but unconfigured (no Qdrant URL or Cohere
    key) — the service still starts, ``/ready`` reports the gap, and the evidence tool degrades
    to "no guideline evidence available" rather than crash-looping the deploy.

    Args:
        settings: Service settings selecting the retrieval mode and endpoints/credentials.

    Returns:
        A retriever, or ``None`` when live mode is selected without full configuration.
    """
    if settings.retrieval_mode is RetrievalMode.FIXTURE:
        return FixtureEvidenceRetriever.from_corpus(
            settings.rerank_top_n, relevance_floor=settings.retrieval_relevance_floor
        )
    if not (settings.qdrant_url and settings.cohere_api_key):
        return None
    qdrant = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    cohere_client = cohere.AsyncClientV2(api_key=settings.cohere_api_key)
    return QdrantEvidenceRetriever(
        qdrant=qdrant,
        cohere_client=cohere_client,
        collection=settings.qdrant_collection,
        dense_model=settings.dense_embedding_model,
        sparse_model=settings.sparse_embedding_model,
        rerank_model=settings.rerank_model,
        prefetch_k=settings.retrieval_prefetch_k,
        rerank_top_n=settings.rerank_top_n,
        relevance_floor=settings.retrieval_relevance_floor,
    )


def _build_filter(
    *, guideline: str | None, source: str | None, section: str | None
) -> models.Filter | None:
    """Build a Qdrant payload filter scoping retrieval by guideline/source/section (or None)."""
    conditions: list[models.FieldCondition] = []
    for key, value in (("guideline", guideline), ("source", source), ("section", section)):
        if value is not None:
            conditions.append(
                models.FieldCondition(key=key, match=models.MatchValue(value=value))
            )
    return models.Filter(must=conditions) if conditions else None


def _parse_payload(payload: dict[str, object] | None) -> CorpusChunk:
    """Validate a Qdrant point payload back into a CorpusChunk (metadata guardrail, §4.3).

    Raises:
        RetrievalError: If the payload is absent or missing required chunk metadata — the
            retriever must never emit a snippet without a full, well-formed citation.
    """
    if payload is None:
        raise RetrievalError("qdrant point has no payload")
    try:
        return CorpusChunk.model_validate(payload)
    except ValidationError as exc:
        raise RetrievalError("qdrant payload is missing required chunk metadata") from exc


def _above_floor(snippets: list[EvidenceSnippet], floor: float) -> list[EvidenceSnippet]:
    """Keep the reranked snippets whose relevance clears ``floor``, preserving their order.

    The upstream relevance gate: a snippet below the floor never reaches the answer model, so a weak
    match cannot ground an answer (the lipids-for-a-23-year-old failure). It runs on the reranked
    :class:`EvidenceSnippet` list both retrievers already produce — one gate shared by the live and
    fixture paths — and preserves order (the input is sorted best-first and already capped).

    Args:
        snippets: Reranked snippets, most relevant first, already capped to the top-n.
        floor: Minimum ``rerank_score`` a snippet must clear to survive.

    Returns:
        The snippets at or above ``floor``, in their original order.
    """
    return [snippet for snippet in snippets if snippet.rerank_score >= floor]


def _to_snippet(chunk: CorpusChunk, rerank_score: float) -> EvidenceSnippet:
    """Project a corpus chunk + relevance score onto an EvidenceSnippet with its citation."""
    return EvidenceSnippet(
        citation=chunk.to_citation(),
        guideline=chunk.guideline,
        source_url=chunk.source_url,
        year=chunk.date,
        anchor_quote=chunk.anchor_quote,
        rerank_score=rerank_score,
    )


def _tokenize(text: str) -> set[str]:
    """Lowercase alphanumeric word set, for the fixture retriever's term-overlap ranking."""
    return set(re.findall(r"[a-z0-9]+", text.lower()))
