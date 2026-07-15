import httpx
import pytest
from qdrant_client import AsyncQdrantClient, QdrantClient, models

from copilot.rag.index import index_corpus
from copilot.rag.retriever import QdrantEvidenceRetriever

# Integration coverage of the live Qdrant hybrid path (FastEmbed dense+sparse -> RRF fusion ->
# rerank-mapping). Cohere is stubbed so no API key is needed; Qdrant is real. Skipped whenever a
# local Qdrant is not reachable (e.g. CI), so the default suite stays hermetic.
_QDRANT_URL = "http://localhost:6333"
_COLLECTION = "guidelines_it"


def _qdrant_reachable() -> bool:
    try:
        return httpx.get(f"{_QDRANT_URL}/readyz", timeout=1.0).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(
    not _qdrant_reachable(), reason="local Qdrant (localhost:6333) not reachable"
)


class _RerankResult:
    def __init__(self, index: int, score: float) -> None:
        self.index = index
        self.relevance_score = score


class _RerankResponse:
    def __init__(self, results: list[_RerankResult]) -> None:
        self.results = results


class _StubCohere:
    """Identity reranker: preserves fusion order, truncates to top_n. Exercises the mapping."""

    async def rerank(self, *, model: str, query: str, documents: list[str], top_n: int) -> _RerankResponse:  # noqa: E501
        n = min(top_n, len(documents))
        return _RerankResponse([_RerankResult(i, round(1.0 - i * 0.05, 3)) for i in range(n)])

    async def close(self) -> None:
        return None


@pytest.fixture(scope="module")
def _indexed() -> None:
    index_corpus(
        url=_QDRANT_URL,
        api_key=None,
        collection=_COLLECTION,
        dense_model="BAAI/bge-small-en-v1.5",
        sparse_model="Qdrant/bm25",
    )


async def test_hybrid_query_returns_relevant_cited_snippets(_indexed: None) -> None:
    # End-to-end proof of the retrieval pipeline against real Qdrant: a clinical query returns
    # topically-correct, fully-cited guideline snippets. If the Prefetch/FusionQuery wiring or
    # the payload->citation mapping regresses, this catches it where a stub cannot.
    retriever = QdrantEvidenceRetriever(
        qdrant=AsyncQdrantClient(url=_QDRANT_URL),
        cohere_client=_StubCohere(),  # type: ignore[arg-type]
        collection=_COLLECTION,
        dense_model="BAAI/bge-small-en-v1.5",
        sparse_model="Qdrant/bm25",
        rerank_model="rerank-v4.0-fast",
        prefetch_k=20,
        rerank_top_n=3,
    )
    try:
        snippets = await retriever.retrieve(
            "What score estimates stroke risk to guide anticoagulation in atrial fibrillation?"
        )
        assert snippets
        assert all(s.citation.source_type.value == "guideline" for s in snippets)
        assert all(s.citation.field_or_chunk_id and s.citation.source_id for s in snippets)
        # The AF corpus is the relevant one for an AF anticoagulation query.
        assert any(s.guideline == "afib-anticoagulation" for s in snippets)

        scoped = await retriever.retrieve("stroke risk", guideline="afib-anticoagulation", top_n=2)
        assert scoped and all(s.guideline == "afib-anticoagulation" for s in scoped)

        assert await retriever.retrieve("stroke risk", guideline="no-such-topic") == []
    finally:
        await retriever.aclose()


def test_reindex_always_upserts_all_chunks(_indexed: None) -> None:
    # Content edits reuse a chunk's id, so every run must upsert all chunks — a count-based
    # "skip if populated" would serve the OLD text under an unchanged id, and the cited quote
    # (built from the payload text) would no longer match the corpus.
    result = index_corpus(
        url=_QDRANT_URL,
        api_key=None,
        collection=_COLLECTION,
        dense_model="BAAI/bge-small-en-v1.5",
        sparse_model="Qdrant/bm25",
    )
    assert result.upserted == 55
    assert result.total_points == 55


def test_reindex_recreates_collection_on_dimension_mismatch() -> None:
    # After an embedding-model change the stored vectors have the wrong dimension; a stale
    # collection must be recreated, not reused — otherwise every query fails on a dim mismatch.
    name = "guidelines_dimtest"
    client = QdrantClient(url=_QDRANT_URL)
    try:
        if client.collection_exists(name):
            client.delete_collection(name)
        # Pre-create with the WRONG dense dimension (128 != bge-small's 384).
        client.create_collection(
            collection_name=name,
            vectors_config={
                "dense": models.VectorParams(size=128, distance=models.Distance.COSINE)
            },
            sparse_vectors_config={
                "sparse": models.SparseVectorParams(modifier=models.Modifier.IDF)
            },
        )
        result = index_corpus(
            url=_QDRANT_URL,
            api_key=None,
            collection=name,
            dense_model="BAAI/bge-small-en-v1.5",
            sparse_model="Qdrant/bm25",
        )
        assert result.recreated_collection is True
        assert result.total_points == 55
        vectors = client.get_collection(name).config.params.vectors
        assert isinstance(vectors, dict)
        assert vectors["dense"].size == 384
    finally:
        if client.collection_exists(name):
            client.delete_collection(name)
        client.close()
