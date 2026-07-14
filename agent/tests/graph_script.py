from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field

from pydantic import BaseModel
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from copilot.graph.routing import Route, RouteDecision
from copilot.graph.supervisor import CopilotGraph
from copilot.rag.models import EvidenceSnippet


@dataclass
class StubRetriever:
    """A deterministic :class:`~copilot.rag.retriever.EvidenceRetriever` for graph tests.

    Returns a fixed set of snippets regardless of the query (and ignores the optional filters), so
    a test can exercise the evidence-retriever's grounding path with an exact, known snippet — no
    corpus content dependency, no Qdrant, no network.
    """

    snippets: Sequence[EvidenceSnippet] = field(default_factory=tuple)

    async def retrieve(
        self,
        query: str,
        *,
        guideline: str | None = None,
        source: str | None = None,
        section: str | None = None,
        top_n: int | None = None,
    ) -> list[EvidenceSnippet]:
        """Return the seeded snippets (query and filters ignored)."""
        return list(self.snippets)

    async def aclose(self) -> None:
        """No-op — the stub holds no resources."""

# Shared scaffolding for driving the supervisor graph deterministically in tests: each of the four
# agents (router, intake-extractor, evidence-retriever, answerer) is overridden with a scripted
# FunctionModel, so a whole turn runs with no live LLM. This is the graph analogue of the
# single-agent `_scripted` helpers the ported tests used to carry inline.


def final_tool_name(info: AgentInfo) -> str:
    """Return the structured-output tool name for the current Pydantic AI version."""
    tools = getattr(info, "output_tools", None) or getattr(info, "result_tools", None) or []
    return tools[0].name if tools else "final_result"


def route_model(routes: Sequence[Route]) -> FunctionModel:
    """A router that emits the given route sequence, one RouteDecision per call (clamps on last).

    Args:
        routes: The routes the supervisor should decide, in order.

    Returns:
        A ``FunctionModel`` producing those routing decisions.
    """
    state = {"i": 0}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        route = routes[min(state["i"], len(routes) - 1)]
        state["i"] += 1
        decision = RouteDecision(route=route, reason="scripted")
        return ModelResponse(
            parts=[ToolCallPart(tool_name=final_tool_name(info), args=decision.model_dump())]
        )

    return FunctionModel(respond)


def worker_model(
    tool_calls: Sequence[tuple[str, dict[str, object]]], output: BaseModel
) -> FunctionModel:
    """A worker/answerer that calls the given tools in order, then emits ``output``.

    Clamps on the final action, so a rejected output is re-emitted on each ModelRetry — driving the
    refusal path when the grounding gate rejects the output.

    Args:
        tool_calls: ``(tool_name, args)`` calls to make before answering, in order.
        output: The structured output to emit (an ``ExtractorOutput``/``RetrieverOutput``/
            ``ChatResponse``).

    Returns:
        A ``FunctionModel`` scripting that sequence.
    """
    actions: list[tuple[str, object]] = [("tool", c) for c in tool_calls] + [("final", output)]
    state = {"i": 0}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        kind, payload = actions[min(state["i"], len(actions) - 1)]
        state["i"] += 1
        if kind == "tool":
            assert isinstance(payload, tuple)
            name, args = payload
            return ModelResponse(parts=[ToolCallPart(tool_name=str(name), args=args)])
        assert isinstance(payload, BaseModel)
        args = payload.model_dump(mode="json")
        return ModelResponse(parts=[ToolCallPart(tool_name=final_tool_name(info), args=args)])

    return FunctionModel(respond)


def looping_tool_model(tool_name: str) -> FunctionModel:
    """A worker that calls one tool forever and never answers — drives the tool-call-cap path."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args={})])

    return FunctionModel(respond)


def raising_model(exc: Exception) -> FunctionModel:
    """A model that raises ``exc`` on invocation — drives the unexpected-error boundary."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise exc

    return FunctionModel(respond)


@contextmanager
def override_graph(
    graph: CopilotGraph,
    *,
    router: FunctionModel,
    extractor: FunctionModel | None = None,
    retriever: FunctionModel | None = None,
    answerer: FunctionModel | None = None,
) -> Iterator[None]:
    """Override each supervisor-graph agent with a scripted model for the duration of the block.

    Args:
        graph: The app's built graph (``app.state.graph``).
        router: The router's scripted model (always needed — the supervisor always routes).
        extractor: The intake-extractor's model, if the turn reaches it.
        retriever: The evidence-retriever's model, if the turn reaches it.
        answerer: The answerer's model, if the turn composes a final answer.
    """
    with ExitStack() as stack:
        stack.enter_context(graph.router.override(model=router))
        if extractor is not None:
            stack.enter_context(graph.extractor.override(model=extractor))
        if retriever is not None:
            stack.enter_context(graph.retriever.override(model=retriever))
        if answerer is not None:
            stack.enter_context(graph.answerer.override(model=answerer))
        yield
