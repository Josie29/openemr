import argparse
import uuid

import httpx
from pydantic import BaseModel
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from copilot.config import get_settings
from copilot.rag.corpus import CorpusError, load_corpus
from copilot.rag.models import CorpusChunk

# Deterministic point ids: uuid5 over chunk_id means re-indexing the same chunk overwrites its
# point rather than inserting a duplicate — the index is idempotent (W2_ARCHITECTURE.md §6).
_POINT_ID_NAMESPACE = uuid.UUID("6f1c0b2e-9d3a-5e47-8b21-0a1f2c3d4e5f")

DEFAULT_LOCAL_URL = "http://localhost:6333"


class IndexResult(BaseModel):
    """Outcome of an indexing run."""

    collection: str
    created_collection: bool
    recreated_collection: bool
    upserted: int
    total_points: int


def _point_id(chunk_id: str) -> str:
    """Stable Qdrant point id for a chunk id (uuid5 — deterministic, so upserts are idempotent)."""
    return str(uuid.uuid5(_POINT_ID_NAMESPACE, chunk_id))


def _dense_dim(model: str) -> int:
    """Look up a FastEmbed dense model's output dimension (no hard-coded size).

    Raises:
        CorpusError: If the model id is not a known FastEmbed dense text model.
    """
    from fastembed import TextEmbedding

    for description in TextEmbedding.list_supported_models():
        if description["model"] == model:
            return int(description["dim"])
    raise CorpusError(f"unknown dense embedding model: {model}")


def _create_collection(
    client: QdrantClient, collection: str, dense_model: str, sparse_model: str
) -> None:
    """Create a collection with a named dense vector + named sparse vector (IDF modifier).

    ``sparse_model`` is accepted for symmetry/validation but the sparse vector is defined by the
    IDF modifier at collection level; the actual model is applied when embedding at upsert/query.
    """
    _ = sparse_model  # documented: sparse model is applied at embed time, not in collection config
    client.create_collection(
        collection_name=collection,
        vectors_config={
            "dense": models.VectorParams(
                size=_dense_dim(dense_model), distance=models.Distance.COSINE
            ),
        },
        sparse_vectors_config={
            # IDF is required for bm25 (and miniCOIL) so term rarity weights the sparse score.
            "sparse": models.SparseVectorParams(modifier=models.Modifier.IDF),
        },
    )
    # Payload indexes make the guideline/source/section scope filters exact + fast (cheap at 55).
    for field in ("guideline", "source", "section"):
        client.create_payload_index(
            collection_name=collection,
            field_name=field,
            field_schema=models.PayloadSchemaType.KEYWORD,
        )


def _existing_dense_dim(client: QdrantClient, collection: str) -> int | None:
    """Return an existing collection's named ``dense`` vector dimension, or None if undetermined.

    Best-effort introspection: any failure to read the config returns None (treated as "cannot
    tell" — never a reason to destroy a collection), so only a concretely mismatching dimension
    triggers a recreate.
    """
    try:
        vectors = client.get_collection(collection).config.params.vectors
    except (UnexpectedResponse, httpx.HTTPError, ConnectionError):
        # Only a transport/connection failure returns None ("can't tell" -> don't recreate). A
        # structural change in the response shape (e.g. an AttributeError after a client upgrade)
        # must surface loudly rather than silently disable the dimension-mismatch guard.
        return None
    if isinstance(vectors, dict) and "dense" in vectors:
        return int(vectors["dense"].size)
    return None


def _point(chunk: CorpusChunk, dense_model: str, sparse_model: str) -> models.PointStruct:
    """Build an upsertable point that embeds the chunk text with both models in-client."""
    return models.PointStruct(
        id=_point_id(chunk.chunk_id),
        vector={
            "dense": models.Document(text=chunk.text, model=dense_model),
            "sparse": models.Document(text=chunk.text, model=sparse_model),
        },
        payload=chunk.model_dump(),
    )


def index_corpus(
    *,
    url: str,
    api_key: str | None,
    collection: str,
    dense_model: str,
    sparse_model: str,
    force: bool = False,
) -> IndexResult:
    """Index the in-repo guideline corpus into Qdrant so the collection mirrors the corpus.

    Every run upserts all chunks with deterministic ids, so an edit to an existing chunk's text
    (same ``chunk_id``) is reflected in place — never served stale — and re-runs never duplicate.
    The collection is recreated (dropping any orphan points for removed chunks) when ``force`` is
    set or when a stale collection's dense-vector dimension no longer matches the embedding model
    (a silent model change would otherwise make every query fail on a dimension mismatch). This
    is what makes the indexer safe to run on every service start (W2_ARCHITECTURE.md §10) — it
    is content-correct, not merely append-safe.

    Args:
        url: Qdrant REST URL.
        api_key: Qdrant API key, or None for an unauthenticated local instance.
        collection: Target collection name.
        dense_model: FastEmbed dense model id.
        sparse_model: FastEmbed sparse model id.
        force: Drop and recreate the collection before indexing (clean slate — clears orphans).

    Returns:
        The :class:`IndexResult` describing what happened.

    Raises:
        CorpusError: If the corpus is missing/malformed or the dense model is unknown.
    """
    chunks = load_corpus()
    client = QdrantClient(url=url, api_key=api_key)
    try:
        exists = client.collection_exists(collection)
        recreated = False
        if exists and (
            force or _existing_dense_dim(client, collection) not in (None, _dense_dim(dense_model))
        ):
            client.delete_collection(collection)
            exists = False
            recreated = True

        created = False
        if not exists:
            _create_collection(client, collection, dense_model, sparse_model)
            created = not recreated  # a fresh create, vs a recreate of a pre-existing collection

        client.upsert(
            collection_name=collection,
            points=[_point(chunk, dense_model, sparse_model) for chunk in chunks],
        )
        total = client.count(collection_name=collection, exact=True).count
        return IndexResult(
            collection=collection,
            created_collection=created,
            recreated_collection=recreated,
            upserted=len(chunks),
            total_points=total,
        )
    finally:
        client.close()


def main() -> None:
    """CLI entry point: ``python -m copilot.rag.index`` (defaults to local Docker Qdrant)."""
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Index the guideline corpus into Qdrant.")
    parser.add_argument("--url", default=settings.qdrant_url or DEFAULT_LOCAL_URL)
    parser.add_argument("--api-key", default=settings.qdrant_api_key)
    parser.add_argument("--collection", default=settings.qdrant_collection)
    parser.add_argument("--dense-model", default=settings.dense_embedding_model)
    parser.add_argument("--sparse-model", default=settings.sparse_embedding_model)
    parser.add_argument("--force", action="store_true", help="Re-index even if already populated.")
    args = parser.parse_args()

    result = index_corpus(
        url=args.url,
        api_key=args.api_key,
        collection=args.collection,
        dense_model=args.dense_model,
        sparse_model=args.sparse_model,
        force=args.force,
    )
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
