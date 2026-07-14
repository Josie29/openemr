import json
from dataclasses import dataclass

from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage, UsageLimits

from copilot.graph.deps import GraphDeps
from copilot.graph.outputs import ExtractorOutput, RetrieverOutput
from copilot.graph.routing import Route, RouteDecision, build_supervisor_router
from copilot.graph.workers import (
    build_answerer,
    build_evidence_retriever,
    build_intake_extractor,
)
from copilot.observability import TurnTrace
from copilot.schemas import ChatResponse

# A worker's report this turn: its name and the output it handed back (both carry summary + claims).
type WorkerReport = tuple[str, ExtractorOutput | RetrieverOutput]


@dataclass
class CopilotGraph:
    """The supervisor + two workers + final-answer agent, built once and reused across turns.

    Bundling the four agents keeps ``run_graph`` free of construction and lets a test override each
    agent's model independently (``graph.router.override(model=...)``). All four share the same
    ``GraphDeps`` type, so the supervisor threads one deps object — and thus one accumulating pair
    of grounding registries — through the whole graph.
    """

    router: Agent[GraphDeps, RouteDecision]
    extractor: Agent[GraphDeps, ExtractorOutput]
    retriever: Agent[GraphDeps, RetrieverOutput]
    answerer: Agent[GraphDeps, ChatResponse]


def build_graph(model: Model) -> CopilotGraph:
    """Build the full supervisor graph on one model (tests inject a per-agent test double).

    Args:
        model: The Pydantic AI model all four agents run on. (Per-agent model tiering — e.g. a
            cheaper router — is a later optimization; one model keeps the wiring simple.)

    Returns:
        The assembled :class:`CopilotGraph`.
    """
    return CopilotGraph(
        router=build_supervisor_router(model),
        extractor=build_intake_extractor(model),
        retriever=build_evidence_retriever(model),
        answerer=build_answerer(model),
    )


@dataclass(frozen=True)
class GraphResult:
    """The outcome of one supervised turn: the answer, the routing trail, and total token usage.

    ``routes`` is the ordered list of every supervisor hand-off, so the turn's control flow is
    reconstructable in full (the JOS-56 acceptance criterion) — the same trail the observability
    layer emits as child spans under the correlation id. ``usage`` sums the token usage across the
    router, workers, and answerer so the turn's cost is priced over the whole graph, not one agent.
    """

    answer: ChatResponse
    routes: list[RouteDecision]
    usage: RunUsage


async def run_graph(
    graph: CopilotGraph,
    message: str,
    deps: GraphDeps,
    turn: TurnTrace,
    *,
    max_hops: int = 4,
    usage_limits: UsageLimits | None = None,
) -> GraphResult:
    """Run one turn through the supervisor: route, dispatch workers, then compose the answer.

    The procedural heart of the multi-agent surface. Each iteration asks the router for the single
    next :class:`RouteDecision`, logs it as a child span (so the hand-off is inspectable in the
    trace), and dispatches the chosen worker — whose grounded output accumulates into the shared
    ``deps`` registries. The loop ends when the router says ``ANSWER`` or ``max_hops`` is reached;
    the final answer is then composed from the workers' reports and gated against both sources.
    Token usage is summed across every agent run so the caller can price the whole turn.

    Args:
        graph: The built supervisor + workers + answerer.
        message: The physician's question for this turn.
        deps: The per-request dependencies threaded through every agent.
        turn: The turn's trace handle; each route decision is emitted as a child span on it.
        max_hops: Hard ceiling on routing iterations, so a router that never says ``ANSWER`` still
            terminates and composes an answer rather than looping.
        usage_limits: Per-run usage limits (e.g. a tool-call ceiling) applied to every agent run,
            so a worker that loops a tool is stopped and the turn degrades to a refusal upstream.

    Returns:
        A :class:`GraphResult` carrying the composed answer, the ordered routing trail, and the
        summed token usage.
    """
    reports: list[WorkerReport] = []
    routes: list[RouteDecision] = []
    usage = RunUsage()

    for _ in range(max_hops):
        routing = await graph.router.run(
            _router_input(message, reports), deps=deps, usage_limits=usage_limits
        )
        usage += routing.usage
        decision = routing.output
        routes.append(decision)
        turn.routed(decision.route.value, decision.reason)
        if decision.route is Route.ANSWER:
            break
        if decision.route is Route.EXTRACT_INTAKE:
            extracted = await graph.extractor.run(message, deps=deps, usage_limits=usage_limits)
            usage += extracted.usage
            reports.append(("intake-extractor", extracted.output))
        elif decision.route is Route.RETRIEVE_EVIDENCE:
            retrieved = await graph.retriever.run(message, deps=deps, usage_limits=usage_limits)
            usage += retrieved.usage
            reports.append(("evidence-retriever", retrieved.output))

    answered = await graph.answerer.run(
        _answerer_input(message, reports), deps=deps, usage_limits=usage_limits
    )
    usage += answered.usage
    return GraphResult(answer=answered.output, routes=routes, usage=usage)


def _router_input(message: str, reports: list[WorkerReport]) -> str:
    """Format the router's per-hop input: the question and what has already been gathered.

    Args:
        message: The physician's question.
        reports: The worker reports gathered so far this turn.

    Returns:
        The prompt string the router decides its next hand-off from.
    """
    if not reports:
        gathered = "Nothing has been gathered yet."
    else:
        gathered = "Already gathered this turn:\n" + "\n".join(
            f"- {name}: {output.summary}" for name, output in reports
        )
    return f"Question: {message}\n\n{gathered}"


def _answerer_input(message: str, reports: list[WorkerReport]) -> str:
    """Format the answerer's input: the question plus every worker's cited claims.

    The claims are passed as JSON (including their citations) so the answerer restates them with
    the exact same ``SourceRef`` the worker grounded — the composite gate then re-verifies each
    against the shared registries.

    Args:
        message: The physician's question.
        reports: The worker reports gathered this turn.

    Returns:
        The prompt string the answerer composes its final response from.
    """
    if not reports:
        return f"Question: {message}\n\nNo worker findings were gathered. Say so plainly."
    blocks: list[str] = []
    for name, output in reports:
        claims_json = json.dumps([claim.model_dump(mode="json") for claim in output.claims])
        blocks.append(f"### {name}\nsummary: {output.summary}\nclaims: {claims_json}")
    findings = "\n\n".join(blocks)
    return (
        f"Question: {message}\n\nWorker findings to compose from (restate only these, citing the "
        f"same sources):\n\n{findings}"
    )
