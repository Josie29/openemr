from contextlib import ExitStack

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import UsageLimits

from copilot.fhir.fixtures import FixtureFhirClient
from copilot.graph.deps import GraphDeps
from copilot.graph.outputs import ExtractorOutput, RetrieverOutput
from copilot.graph.routing import Route, RouteDecision
from copilot.graph.supervisor import build_graph, run_graph
from copilot.graph.workers import build_evidence_retriever
from copilot.ingestion.registry import DocumentFactRegistry
from copilot.observability import TurnTrace
from copilot.rag.models import EvidenceSnippet
from copilot.retrieval import GUIDELINE_RESOURCE_TYPE, ChunkRegistry
from copilot.schemas import ChatResponse, Claim, GuidelineCitation, SourceRef
from copilot.verification import FetchLog
from graph_script import StubRetriever

# NOTE (same caveat as test_chat_flow): these tests drive each agent with a scripted FunctionModel,
# so they depend on Pydantic AI's message/AgentInfo API. The behavior asserted — the supervisor
# routes, each worker's grounding gate bites, and the final answer only ships grounded claims — is
# the JOS-56 contract that must hold regardless of the model surface.

_SNIPPET = EvidenceSnippet(
    citation=GuidelineCitation(
        source_id="ada-soc-2025",
        page_or_section="Screening",
        field_or_chunk_id="ada-1",
        quote_or_value="Screen adults aged 35 years or older for prediabetes and type 2 diabetes.",
    ),
    guideline="t2dm",
    source_url="https://example.org/ada",
    rerank_score=0.9,
)
_GUIDELINE_QUOTE = "Screen adults aged 35 years or older"


def _final_tool_name(info: AgentInfo) -> str:
    """Return the structured-output tool name for the current Pydantic AI version."""
    tools = getattr(info, "output_tools", None) or getattr(info, "result_tools", None) or []
    return tools[0].name if tools else "final_result"


def _deps() -> GraphDeps:
    """A GraphDeps over the seed patient and a fake retriever seeded with one guideline snippet."""
    return GraphDeps(
        fhir=FixtureFhirClient.from_seed(),
        patient_id="1",
        correlation_id="test-cid",
        retriever=StubRetriever(snippets=(_SNIPPET,)),
        extractor=None,
        fetched=FetchLog(),
        chunks=ChunkRegistry(),
        documents=DocumentFactRegistry(),
    )


def _router_model(routes: list[Route]) -> FunctionModel:
    """A router that emits the given route sequence, one RouteDecision per call."""
    state = {"i": 0}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        route = routes[state["i"]]
        state["i"] += 1
        decision = RouteDecision(route=route, reason=f"scripted step {state['i']}")
        return ModelResponse(
            parts=[ToolCallPart(tool_name=_final_tool_name(info), args=decision.model_dump())]
        )

    return FunctionModel(respond)


def _extractor_model() -> FunctionModel:
    """An extractor that reads the record once, then cites the patient's birth date."""
    state = {"fetched": False}
    output = ExtractorOutput(
        summary="68F.",
        claims=[
            Claim(
                text="Born 1958-03-12.",
                source=SourceRef(resource_type="Patient", resource_id="1", field="birth_date"),
            )
        ],
    )

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not state["fetched"]:
            state["fetched"] = True
            return ModelResponse(parts=[ToolCallPart(tool_name="get_patient_summary", args={})])
        args = output.model_dump(mode="json")
        return ModelResponse(parts=[ToolCallPart(tool_name=_final_tool_name(info), args=args)])

    return FunctionModel(respond)


def _retriever_model(quote: str) -> FunctionModel:
    """A retriever that searches once, then cites a guideline chunk with ``quote``."""
    state = {"searched": False}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not state["searched"]:
            state["searched"] = True
            return ModelResponse(
                parts=[ToolCallPart(tool_name="search_guidelines", args={"query": "diabetes"})]
            )
        output = RetrieverOutput(
            summary="Screen adults 35+.",
            claims=[
                Claim(
                    text="Screen adults 35+ for type 2 diabetes.",
                    source=SourceRef(
                        resource_type=GUIDELINE_RESOURCE_TYPE, resource_id="ada-1", quote=quote
                    ),
                )
            ],
        )
        args = output.model_dump(mode="json")
        return ModelResponse(parts=[ToolCallPart(tool_name=_final_tool_name(info), args=args)])

    return FunctionModel(respond)


def _answerer_model() -> FunctionModel:
    """An answerer that restates both workers' claims, citing the same sources."""
    final = ChatResponse(
        summary="Marisol Reyes, 68F; ADA advises screening adults 35+ for type 2 diabetes.",
        claims=[
            Claim(
                text="Born 1958-03-12.",
                source=SourceRef(resource_type="Patient", resource_id="1", field="birth_date"),
            ),
            Claim(
                text="ADA: screen adults 35+ for type 2 diabetes.",
                source=SourceRef(
                    resource_type=GUIDELINE_RESOURCE_TYPE,
                    resource_id="ada-1",
                    quote=_GUIDELINE_QUOTE,
                ),
            ),
        ],
        follow_ups=["Is she due for an A1c?"],
    )

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        args = final.model_dump(mode="json")
        return ModelResponse(parts=[ToolCallPart(tool_name=_final_tool_name(info), args=args)])

    return FunctionModel(respond)


async def test_supervisor_routes_workers_and_composes_grounded_answer() -> None:
    # The JOS-56 happy path end-to-end: the supervisor routes extract -> retrieve -> answer, each
    # worker's ported grounding gate stamps real values, and the final answer composes both cited
    # facts. If this breaks, the multi-agent surface either misroutes or ships an unverified claim.
    graph = build_graph(TestModel())
    deps = _deps()

    with ExitStack() as stack:
        stack.enter_context(
            graph.router.override(
                model=_router_model([Route.EXTRACT_INTAKE, Route.RETRIEVE_EVIDENCE, Route.ANSWER])
            )
        )
        stack.enter_context(graph.extractor.override(model=_extractor_model()))
        stack.enter_context(graph.retriever.override(model=_retriever_model(_GUIDELINE_QUOTE)))
        stack.enter_context(graph.answerer.override(model=_answerer_model()))
        result = await run_graph(graph, "Is she due for diabetes screening?", deps, TurnTrace(None))

    # The hand-off chain is reconstructable in full — the acceptance criterion.
    assert [d.route for d in result.routes] == [
        Route.EXTRACT_INTAKE,
        Route.RETRIEVE_EVIDENCE,
        Route.ANSWER,
    ]
    # Both facts survived their gates and the composite gate, with code-stamped real values.
    assert len(result.answer.claims) == 2
    fhir_claim, guideline_claim = result.answer.claims
    assert fhir_claim.source.value == "1958-03-12"  # stamped from the fetched Patient record
    assert guideline_claim.source.value == _GUIDELINE_QUOTE  # matched in the retrieved chunk
    assert guideline_claim.source.label == "ada-soc-2025"  # chunk identity (source id) stamped


async def test_tool_call_ceiling_is_enforced_per_turn() -> None:
    """The tool-call ceiling caps the whole TURN, not each agent run.

    The extractor and retriever each make exactly one real tool call, so their cumulative (2) trips
    a per-turn limit of 1 — whereas the old behavior (fresh limit per agent run) would let every
    single-call run through, letting a runaway spend ~max_hops x the limit before degrading.
    """
    graph = build_graph(TestModel())
    deps = _deps()
    with ExitStack() as stack:
        stack.enter_context(
            graph.router.override(
                model=_router_model([Route.EXTRACT_INTAKE, Route.RETRIEVE_EVIDENCE, Route.ANSWER])
            )
        )
        stack.enter_context(graph.extractor.override(model=_extractor_model()))
        stack.enter_context(graph.retriever.override(model=_retriever_model(_GUIDELINE_QUOTE)))
        stack.enter_context(graph.answerer.override(model=_answerer_model()))
        with pytest.raises(UsageLimitExceeded):
            await run_graph(
                graph, "q", deps, TurnTrace(None), usage_limits=UsageLimits(tool_calls_limit=1)
            )


async def test_evidence_worker_gate_rejects_ungrounded_quote() -> None:
    # The ported gate must bite on the evidence side too: an evidence claim whose quote is NOT in
    # any retrieved chunk is refused, so fabricated guideline text can never leave the worker. The
    # worker retries and, still ungrounded, raises rather than returning the bogus claim.
    retriever = build_evidence_retriever(TestModel())
    deps = _deps()

    with (
        retriever.override(model=_retriever_model("a quote no guideline chunk contains")),
        pytest.raises(UnexpectedModelBehavior),
    ):
        await retriever.run("Is she due for diabetes screening?", deps=deps)
