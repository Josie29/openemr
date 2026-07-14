import pytest

from copilot.config import FhirClientMode, RetrievalMode, Settings
from copilot.rag.models import EvidenceSnippet
from copilot.rag.retriever import (
    FixtureEvidenceRetriever,
    QdrantEvidenceRetriever,
    RetrievalError,
    _parse_payload,
    build_retriever,
)


async def test_fixture_retriever_returns_cited_guideline_snippets() -> None:
    # The core acceptance for JOS-53: the evidence-retriever returns cited guideline snippets.
    # If this breaks, an answer could cite guideline evidence that carries no resolvable source.
    retriever = FixtureEvidenceRetriever.from_corpus(rerank_top_n=3)
    snippets = await retriever.retrieve("CHA2DS2-VASc stroke risk in atrial fibrillation")

    assert snippets
    assert all(isinstance(s, EvidenceSnippet) for s in snippets)
    assert all(s.citation.source_type.value == "guideline" for s in snippets)
    # Every snippet is fully cited — the metadata guardrail (W2_ARCH §4.3).
    assert all(s.citation.field_or_chunk_id and s.citation.source_id for s in snippets)
    assert all(s.text == s.citation.quote_or_value for s in snippets)


async def test_fixture_retriever_scopes_by_guideline_filter() -> None:
    # Payload-scope filters back the citation contract's per-guideline scoping. A broken filter
    # that leaks other topics into a scoped query would attach the wrong source to a claim.
    retriever = FixtureEvidenceRetriever.from_corpus(rerank_top_n=10)
    snippets = await retriever.retrieve("risk", guideline="asthma")

    assert snippets
    assert all(s.guideline == "asthma" for s in snippets)


async def test_fixture_retriever_returns_empty_when_nothing_matches() -> None:
    # Empty retrieval must be a clean empty list, not an error — the answer then separates
    # "record says X" from "no guideline evidence retrieved" (W2_ARCH §11) instead of failing.
    retriever = FixtureEvidenceRetriever.from_corpus(rerank_top_n=5)
    assert await retriever.retrieve("qwxyznomatch", guideline="does-not-exist") == []


def test_parse_payload_rejects_a_snippet_without_full_chunk_metadata() -> None:
    # The retriever must never emit a snippet lacking a resolvable citation (W2_ARCH §4.3). A
    # malformed Qdrant payload must raise, not yield an uncitable snippet to the answer model.
    with pytest.raises(RetrievalError):
        _parse_payload({"text": "orphan text with no ids"})
    with pytest.raises(RetrievalError):
        _parse_payload(None)


def test_build_retriever_returns_fixture_in_fixture_mode() -> None:
    # Fixture mode must never construct live network clients — that keeps tests + offline dev
    # fully local, the same guarantee FhirClientMode.FIXTURE gives.
    settings = Settings(
        fhir_client_mode=FhirClientMode.FIXTURE,
        retrieval_mode=RetrievalMode.FIXTURE,
        anthropic_api_key=None,
    )
    retriever = build_retriever(settings)
    assert isinstance(retriever, FixtureEvidenceRetriever)


def test_build_retriever_returns_none_when_live_but_unconfigured() -> None:
    # A live deploy missing its Qdrant URL / Cohere key must degrade (None -> /ready red, tool
    # returns no evidence), NOT crash-loop the service at startup.
    settings = Settings(
        fhir_client_mode=FhirClientMode.FIXTURE,
        retrieval_mode=RetrievalMode.QDRANT,
        qdrant_url=None,
        cohere_api_key=None,
        anthropic_api_key=None,
    )
    assert build_retriever(settings) is None


async def test_build_retriever_constructs_live_retriever_when_configured() -> None:
    # Guards the wiring: with both Qdrant URL and a Cohere key present, live mode must build the
    # real retriever (construction is offline — no network call until a query runs).
    settings = Settings(
        fhir_client_mode=FhirClientMode.FIXTURE,
        retrieval_mode=RetrievalMode.QDRANT,
        qdrant_url="http://qdrant.railway.internal:6333",
        qdrant_api_key="k",
        cohere_api_key="ck",
        anthropic_api_key=None,
    )
    retriever = build_retriever(settings)
    assert isinstance(retriever, QdrantEvidenceRetriever)
    await retriever.aclose()  # release the Qdrant client pool built for this assertion
