import pytest

from copilot.config import FhirClientMode, RetrievalMode, Settings
from copilot.rag.models import EvidenceSnippet, RetrievedGuideline
from copilot.rag.retriever import (
    FixtureEvidenceRetriever,
    QdrantEvidenceRetriever,
    RetrievalError,
    _above_floor,
    _parse_payload,
    build_retriever,
)
from copilot.schemas import GuidelineCitation


def _scored_snippet(score: float) -> EvidenceSnippet:
    """A minimal reranked snippet carrying just the score the relevance gate reads."""
    return EvidenceSnippet(
        citation=GuidelineCitation(
            source_id="s", page_or_section="sec", field_or_chunk_id=f"c{score}", quote_or_value="t"
        ),
        guideline="g",
        rerank_score=score,
    )


def test_model_facing_projection_withholds_anchor_quote_from_the_model() -> None:
    # Regression (JOS-89): the model must only ever see the field the grounding gate checks against
    # (the chunk `text`), never the verbatim `anchor_quote`. When both were exposed the model copied
    # the anchor — which differs from `text` (here by a leading capital) — into its claim quote, the
    # gate rejected it against `text`, and the turn failed into a refusal. If the projection ever
    # leaks anchor_quote again, that mismatch becomes possible again.
    snippet = EvidenceSnippet(
        citation=GuidelineCitation(
            source_id="uspstf-t2dm-2021",
            page_or_section="Screening Tests",
            field_or_chunk_id="uspstf-t2dm-2021-screening-tests-04",
            quote_or_value="an abnormal screening result: the diagnosis should be confirmed.",
        ),
        guideline="t2dm",
        anchor_quote="The diagnosis should be confirmed.",  # capitalized — NOT a substring of text
        rerank_score=0.5,
    )

    view = RetrievedGuideline.from_snippet(snippet)

    assert view.chunk_id == "uspstf-t2dm-2021-screening-tests-04"
    assert view.text == snippet.text
    # The footgun field is absent from everything the model can see...
    assert "anchor_quote" not in view.model_dump()
    # ...while the full snippet still carries it for the system-side deep-link at serialization.
    assert snippet.anchor_quote == "The diagnosis should be confirmed."


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


def test_above_floor_drops_snippets_below_the_floor() -> None:
    # The upstream relevance gate. If this breaks, an off-topic guideline snippet grounds an
    # answer just because it was retrieved (the lipids-question-for-a-23-year-old failure) instead
    # of being suppressed so no evidence is shown.
    snippets = [_scored_snippet(s) for s in (0.94, 0.71, 0.40, 0.12)]
    kept = _above_floor(snippets, 0.5)
    assert [round(s.rerank_score, 2) for s in kept] == [0.94, 0.71]  # 0.40 and 0.12 gated out


def test_above_floor_preserves_best_first_order() -> None:
    # The gate is a filter, not a re-sort: survivors keep the reranker's order so the top card is
    # still the best match.
    snippets = [_scored_snippet(s) for s in (0.9, 0.8, 0.7)]
    assert _above_floor(snippets, 0.5) == snippets


async def test_fixture_retriever_applies_relevance_floor() -> None:
    # With a floor set, only strongly-overlapping chunks survive — the fixture mirrors the live
    # rerank gate so offline dev/eval sees the same "suppress weak matches" behavior. If the floor
    # were ignored here, gate behavior would silently differ between fixture and live.
    query = "CHA2DS2-VASc stroke risk in atrial fibrillation"
    gated = FixtureEvidenceRetriever.from_corpus(rerank_top_n=10, relevance_floor=0.99)
    snippets = await gated.retrieve(query)

    assert snippets  # the top match (normalized score 1.0) still clears the floor
    assert all(s.rerank_score >= 0.99 for s in snippets)
    ungated = FixtureEvidenceRetriever.from_corpus(rerank_top_n=10)
    assert len(await ungated.retrieve(query)) > len(snippets)  # the floor strictly narrows


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
