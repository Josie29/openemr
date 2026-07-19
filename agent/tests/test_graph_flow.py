from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext, ToolDefinition
from pydantic_ai.usage import RunUsage, UsageLimits

from copilot.fhir.fixtures import FixtureFhirClient
from copilot.graph.budget import budgeted
from copilot.graph.deps import BudgetedTool, GraphDeps
from copilot.graph.outputs import ExtractorOutput, RetrieverOutput
from copilot.graph.routing import Route, RouteDecision
from copilot.graph.supervisor import build_graph, run_graph
from copilot.graph.workers import build_evidence_retriever
from copilot.ingestion.registry import DocumentFactRegistry
from copilot.observability import TurnTrace, WorkerSpan
from copilot.rag.models import EvidenceSnippet
from copilot.rag.retriever import RetrievalError
from copilot.retrieval import GUIDELINE_RESOURCE_TYPE, ChunkRegistry
from copilot.schemas import ChatResponse, Claim, GuidelineCitation, SourceRef
from copilot.verification import FetchLog
from graph_script import StubRetriever


@dataclass
class _CountingRetriever(StubRetriever):
    """A StubRetriever that records how many times the store was actually queried."""

    calls: int = 0

    async def retrieve(
        self,
        query: str,
        *,
        guideline: str | None = None,
        source: str | None = None,
        section: str | None = None,
        top_n: int | None = None,
    ) -> list[EvidenceSnippet]:
        self.calls += 1
        return await super().retrieve(
            query, guideline=guideline, source=source, section=section, top_n=top_n
        )

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
        tool_budgets={},
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
    return FunctionModel(_extractor_respond())


def _extractor_respond() -> Callable[[list[ModelMessage], AgentInfo], ModelResponse]:
    """The extractor's scripted respond fn, exposed so a test can wrap it (see _marking_model)."""
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

    return respond


def _retriever_model(quote: str) -> FunctionModel:
    """A retriever that searches once, then cites a guideline chunk with ``quote``."""
    return FunctionModel(_retriever_respond(quote))


def _retriever_respond(quote: str) -> Callable[[list[ModelMessage], AgentInfo], ModelResponse]:
    """The retriever's scripted respond fn, exposed so a test can wrap it (see _marking_model)."""
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

    return respond


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


async def test_guideline_search_budget_caps_retrievals_not_just_tool_calls() -> None:
    """A looping retriever performs at most ``max_searches`` real retrievals in a turn.

    The failure this guards is not hypothetical: asked for NSAID guidance in CKD — which the corpus
    does not cover — the evidence-retriever fired NINE rephrased search_guidelines calls, exhausted
    the turn-wide tool-call ceiling, and collapsed a turn whose extraction had already succeeded
    into a refusal. pydantic-ai's UsageLimits is turn-wide and has no per-tool form, so one looping
    tool can starve every other tool in the turn. The budget bounds the expensive half: past it the
    tool short-circuits without touching the store, so a model that keeps calling burns no
    retrieval, no Qdrant/Cohere spend, and no latency.

    Asserts the RETRIEVAL count rather than the tool-call count on purpose — capping calls is what
    the ceiling already does, and it is what fails the turn. Capping work is what makes the loop
    harmless.
    """
    counting = _CountingRetriever(snippets=(_SNIPPET,))
    deps = _deps()
    deps.retriever = counting
    deps.tool_budgets = {BudgetedTool.SEARCH_GUIDELINES: 2}

    # A model that rephrases forever, the way the real one did. Each call carries a DIFFERENT valid
    # query, so nothing is deduplicated and nothing fails validation — the budget is the only thing
    # that can stop the retrievals.
    state = {"n": 0}

    def rephrasing_search(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        state["n"] += 1
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="search_guidelines",
                    args={"query": f"NSAID kidney query {state['n']}"},
                )
            ]
        )

    # Either terminal outcome proves the point, and which one occurs is an artifact of the double:
    # once the budget withholds the tool, this scripted model keeps calling a name no longer in the
    # schema and dies on unknown-tool retries, where a real model simply stops. What the assertion
    # below pins is the invariant common to both — the store was queried a bounded number of times.
    retriever = build_evidence_retriever(TestModel())
    with (
        retriever.override(model=FunctionModel(rephrasing_search)),
        pytest.raises((UsageLimitExceeded, UnexpectedModelBehavior)),
    ):
        await retriever.run(
            "NSAIDs in chronic kidney disease?",
            deps=deps,
            usage_limits=UsageLimits(tool_calls_limit=8),
        )

    assert counting.calls == 2, (
        f"the store was queried {counting.calls} times against a budget of 2 — a corpus that "
        "cannot answer the question must cost a bounded number of retrievals, not one per rephrase"
    )


async def test_spent_budget_withholds_the_tool_from_the_model() -> None:
    """A spent budget removes the tool from the schema the model is offered.

    This is the half that actually stops the loop, and it is why the budget is a ``prepare`` hook
    rather than a check inside the tool. A tool that returns "budget exhausted" is still callable,
    and every call it accepts costs a full model round-trip: prod fired nine `list_documents` calls
    at ~$0.024 each — $0.22 of a $0.30 turn — even though the tool was memoized and each repeat was
    a free ~0.4s cache hit. Bounding the tool's work does not bound the loop's cost; withholding the
    tool does.

    If this regresses, the budget still caps the WORK but the model keeps paying to be told no.
    """
    deps = _deps()
    deps.tool_budgets = {BudgetedTool.SEARCH_GUIDELINES: 1}
    prepare = budgeted(BudgetedTool.SEARCH_GUIDELINES)
    tool_def = ToolDefinition(
        name=BudgetedTool.SEARCH_GUIDELINES.value, parameters_json_schema={}
    )

    def ctx_after(calls: int) -> RunContext[GraphDeps]:
        """A run context whose history already holds ``calls`` calls to the budgeted tool."""
        history: list[ModelMessage] = [
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=BudgetedTool.SEARCH_GUIDELINES.value, args={"query": "q"}
                    )
                ]
            )
            for _ in range(calls)
        ]
        return RunContext(deps=deps, model=TestModel(), usage=RunUsage(), messages=history)

    assert await prepare(ctx_after(0), tool_def) is tool_def, (
        "an unspent budget must offer the tool"
    )
    assert await prepare(ctx_after(1), tool_def) is None, (
        "a spent budget still offered the tool to the model — it can keep calling, and each call "
        "costs a model round-trip whether or not the tool does any work"
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


class _RecordingTrace(TurnTrace):
    """A TurnTrace that records span enter/exit order instead of talking to Langfuse.

    Subclassing rather than mocking the Langfuse client keeps the assertion on the contract the
    supervisor actually depends on — that ``supervising`` and ``routing`` are context managers held
    *open* across the work they describe — with no credentials and no network.
    """

    def __init__(self) -> None:
        super().__init__(None)
        self.events: list[str] = []

    @contextmanager
    def supervising(self) -> Iterator[None]:
        self.events.append("enter:supervisor")
        try:
            yield
        finally:
            self.events.append("exit:supervisor")

    @contextmanager
    def routing(self, route: str, reason: str) -> Iterator[WorkerSpan]:
        self.events.append(f"enter:route:{route}")
        try:
            # A spanless handle: the supervisor records the hand-off's cost on it, which is a no-op
            # untraced — this double asserts nesting order, not cost.
            yield WorkerSpan(None)
        finally:
            self.events.append(f"exit:route:{route}")


def _marking_model(
    inner: Callable[[list[ModelMessage], AgentInfo], ModelResponse],
    events: list[str],
    name: str,
) -> FunctionModel:
    """Wrap a worker's scripted respond fn so it records the moment the worker actually runs.

    Takes the raw function rather than a built ``FunctionModel`` because ``FunctionModel.function``
    is typed as an optional sync-or-stream union, which no amount of narrowing makes clean here.
    """

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        events.append(f"ran:{name}")
        return inner(messages, info)

    return FunctionModel(respond)


async def test_worker_runs_nest_inside_their_supervisor_and_route_spans() -> None:
    # Guards the PRD Week-2 tracing contract: "each worker invocation must be a child span of the
    # supervisor span." Parentage in OTel is positional — a child is whatever runs while the parent
    # span is open — so if `routing` ever reverts to emitting an open-and-immediately-closed marker
    # (what it did before this change), every worker silently flattens into a sibling of its own
    # hand-off and the trace stops showing who dispatched whom. No other test would fail.
    graph = build_graph(TestModel())
    deps = _deps()
    trace = _RecordingTrace()

    with ExitStack() as stack:
        stack.enter_context(
            graph.router.override(
                model=_router_model([Route.EXTRACT_INTAKE, Route.RETRIEVE_EVIDENCE, Route.ANSWER])
            )
        )
        stack.enter_context(graph.answerer.override(model=_answerer_model()))
        # Each worker marks the event log the moment it runs, so ordering alone proves the nesting.
        stack.enter_context(
            graph.extractor.override(
                model=_marking_model(_extractor_respond(), trace.events, "intake-extractor")
            )
        )
        stack.enter_context(
            graph.retriever.override(
                model=_marking_model(
                    _retriever_respond(_GUIDELINE_QUOTE), trace.events, "evidence-retriever"
                )
            )
        )
        await run_graph(graph, "Is she due for diabetes screening?", deps, trace)

    for worker, route in (
        ("intake-extractor", "extract_intake"),
        ("evidence-retriever", "retrieve_evidence"),
    ):
        ran = trace.events.index(f"ran:{worker}")
        assert (
            trace.events.index(f"enter:route:{route}")
            < ran
            < trace.events.index(f"exit:route:{route}")
        ), f"{worker} ran outside its route span — it cannot be that hand-off's child in the trace"
        assert (
            trace.events.index("enter:supervisor") < ran < trace.events.index("exit:supervisor")
        ), f"{worker} ran outside the supervisor span — the PRD requires it to be a child of it"
@pytest.mark.anyio
async def test_a_retrieval_that_clears_the_relevance_floor_scores_a_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """retrieval_hit is the only signal that says whether RAG is finding anything usable. Without
    it, a corpus that silently stops matching looks identical to a healthy one — the turn still
    answers, just without evidence."""
    scores: dict[str, float] = {}
    monkeypatch.setattr(
        "copilot.graph.workers.score_current_turn",
        lambda name, value: scores.__setitem__(name, value),
    )
    retriever = build_evidence_retriever(TestModel())
    deps = _deps()

    with retriever.override(model=_retriever_model(_GUIDELINE_QUOTE)):
        await retriever.run("Is he due for diabetes screening?", deps=deps)

    assert scores["retrieval_hit"] == 1.0
    assert scores["retrieval_top_score"] == _SNIPPET.rerank_score


@pytest.mark.anyio
async def test_a_retrieval_returning_nothing_above_the_floor_scores_a_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The empty list here is POST-floor: the retriever applies settings.retrieval_relevance_floor
    before returning, so "no snippets" means nothing cleared the relevance bar — a corpus coverage
    gap, not an error. Nothing else reports that; the turn just answers unevidenced."""
    scores: dict[str, float] = {}
    monkeypatch.setattr(
        "copilot.graph.workers.score_current_turn",
        lambda name, value: scores.__setitem__(name, value),
    )
    retriever = build_evidence_retriever(TestModel())
    deps = _deps()
    deps.retriever = StubRetriever(snippets=())

    with (
        retriever.override(model=_retriever_model(_GUIDELINE_QUOTE)),
        pytest.raises(UnexpectedModelBehavior),
    ):
        await retriever.run("Is he due for diabetes screening?", deps=deps)

    assert scores["retrieval_hit"] == 0.0
    assert scores["retrieval_top_score"] == 0.0


@dataclass
class _FailingRetriever(StubRetriever):
    """A retriever whose store is down — every query raises, as a dead Qdrant/Cohere would."""

    async def retrieve(
        self,
        query: str,
        *,
        guideline: str | None = None,
        source: str | None = None,
        section: str | None = None,
        top_n: int | None = None,
    ) -> list[EvidenceSnippet]:
        raise RetrievalError("qdrant hybrid query failed")


async def test_retrieval_outage_degrades_instead_of_failing_the_turn() -> None:
    # Guards the fallback asymmetry fix: extraction failure has always degraded gracefully, but a
    # retrieval failure used to propagate out of the tool and kill the whole turn — so one dead
    # dependency cost the physician an answer the patient record could fully support. The tool must
    # now report the outage and let the turn complete.
    #
    # It must ALSO not claim the corpus lacks the topic: that wording is a clinical statement a
    # physician may act on, and asserting it because a service was unreachable is a false negative
    # dressed as evidence.
    graph = build_graph(TestModel())
    deps = _deps()
    deps.retriever = _FailingRetriever()

    state = {"searched": False}

    def retriever_reports_outage(
        messages: list[ModelMessage], info: AgentInfo
    ) -> ModelResponse:
        """Search once, then report the outage with no claims — nothing was retrieved to cite."""
        if not state["searched"]:
            state["searched"] = True
            return ModelResponse(
                parts=[ToolCallPart(tool_name="search_guidelines", args={"query": "diabetes"})]
            )
        output = RetrieverOutput(summary="Guideline lookup was unavailable this turn.", claims=[])
        return ModelResponse(
            parts=[
                ToolCallPart(tool_name=_final_tool_name(info), args=output.model_dump(mode="json"))
            ]
        )

    with ExitStack() as stack:
        stack.enter_context(
            graph.router.override(model=_router_model([Route.RETRIEVE_EVIDENCE, Route.ANSWER]))
        )
        stack.enter_context(graph.retriever.override(model=FunctionModel(retriever_reports_outage)))
        # The answerer cites nothing: with retrieval down there is no chunk to ground a guideline
        # claim against, and the gate would (correctly) reject one. A claimless "I could not check
        # the guidelines" IS the right degraded answer.
        stack.enter_context(graph.answerer.override(model=_claimless_answerer_model()))
        result = await run_graph(graph, "Is she due for diabetes screening?", deps, TurnTrace(None))

    assert result.answer.summary, "the turn must still produce an answer when retrieval is down"
    assert [d.route for d in result.routes] == [Route.RETRIEVE_EVIDENCE, Route.ANSWER]


def _claimless_answerer_model() -> FunctionModel:
    """An answerer returning a summary with no claims — the degraded shape when nothing grounds."""
    final = ChatResponse(
        summary="I could not check the guideline corpus this turn; the evidence lookup failed.",
        claims=[],
        follow_ups=[],
    )

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                ToolCallPart(tool_name=_final_tool_name(info), args=final.model_dump(mode="json"))
            ]
        )

    return FunctionModel(respond)
