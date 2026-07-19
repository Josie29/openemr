import json
import logging
from dataclasses import dataclass

from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage, UsageLimits

from copilot.config import ModelTier
from copilot.graph.deps import GraphDeps
from copilot.graph.outputs import ExtractorOutput, RetrieverOutput
from copilot.graph.routing import Route, RouteDecision, build_supervisor_router
from copilot.graph.workers import (
    build_answerer,
    build_evidence_retriever,
    build_intake_extractor,
)
from copilot.observability import TurnTrace, WorkerSpan
from copilot.pricing import turn_cost_usd, usage_delta
from copilot.schemas import ChatResponse

logger = logging.getLogger(__name__)

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
    model_tier: ModelTier | None = None,
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
        model_tier: The deployed model tier, used to price each worker's own usage onto its span.
            None (the default, used by tests) leaves per-worker cost unpriced; the turn's total
            cost is unaffected either way, since the caller prices ``GraphResult.usage``.

    Returns:
        A :class:`GraphResult` carrying the composed answer, the ordered routing trail, and the
        summed token usage.
    """
    reports: list[WorkerReport] = []
    routes: list[RouteDecision] = []
    # One shared usage accumulator threaded into every agent run this turn, so usage_limits (the
    # tool-call ceiling) is enforced against the cumulative total — a per-TURN cap, not a fresh
    # budget per router hop / worker (which made the effective ceiling ~max_hops x the limit). It
    # also sums token usage across runs for pricing, so nothing needs to be added afterwards.
    usage = RunUsage()

    # A worker runs at most once a turn, and this is enforced here rather than asked for in the
    # router prompt (which already says "do not repeat a step already completed" — and was observed
    # dispatching the retriever three times regardless). The guarantee is structural: a worker is
    # re-run with the SAME `message`, the same tools and the same corpus, so a second dispatch has
    # no new input and cannot produce a different report. The router only reaches for one when the
    # first report came back thin — hoping repetition yields more, which is the same mistake a model
    # makes re-calling a tool that returned nothing.
    dispatched: set[Route] = set()

    # The routing loop runs inside the supervisor span, so every hand-off — and the worker each one
    # dispatches — is a descendant of the supervisor rather than a sibling of it in the trace.
    with turn.supervising():
        for _ in range(max_hops):
            routing = await graph.router.run(
                _router_input(message, reports), deps=deps, usage=usage, usage_limits=usage_limits
            )
            decision = routing.output
            routes.append(decision)
            # Held open across the dispatch below, so the worker run and its tool calls nest under
            # this hand-off and the span's duration is what the hand-off actually cost.
            with turn.routing(decision.route.value, decision.reason) as hop:
                if decision.route is Route.ANSWER:
                    break
                if decision.route in dispatched:
                    # Nothing left to gather: the router asked for a worker that has already
                    # reported, so compose from what there is instead of burning the turn
                    # re-running it.
                    logger.info(
                        "router re-selected a completed worker; composing the answer",
                        extra={
                            "route": decision.route.value,
                            "correlation_id": deps.correlation_id,
                        },
                    )
                    break
                dispatched.add(decision.route)
                # Snapshot the shared accumulator so this hand-off's span can carry the cost of the
                # worker it dispatches, and only that worker's.
                before = usage_delta(RunUsage(), usage)
                if decision.route is Route.EXTRACT_INTAKE:
                    extracted = await graph.extractor.run(
                        message, deps=deps, usage=usage, usage_limits=usage_limits
                    )
                    reports.append(("intake-extractor", extracted.output))
                elif decision.route is Route.RETRIEVE_EVIDENCE:
                    retrieved = await graph.retriever.run(
                        message, deps=deps, usage=usage, usage_limits=usage_limits
                    )
                    reports.append(("evidence-retriever", retrieved.output))
                _record_spend(hop, before, usage, model_tier)

    # The answerer is composed directly rather than dispatched, so it has no hand-off span to nest
    # under and takes one of its own.
    before_answer = usage_delta(RunUsage(), usage)
    with turn.worker("answerer") as span:
        answered = await graph.answerer.run(
            _answerer_input(message, reports), deps=deps, usage=usage, usage_limits=usage_limits
        )
        _record_spend(span, before_answer, usage, model_tier)
    return GraphResult(answer=answered.output, routes=routes, usage=usage)


def _record_spend(
    span: WorkerSpan, before: RunUsage, usage: RunUsage, model_tier: ModelTier | None
) -> None:
    """Attribute one agent run's own token usage and cost to its span.

    The graph threads ONE shared accumulator so the tool-call ceiling stays a per-TURN cap (see
    :func:`run_graph`), which means no single run's usage is directly observable —
    ``AgentRunResult.usage`` returns that same shared object. Diffing a before/after snapshot is
    the only way to attribute usage to one run without regressing the ceiling.

    Args:
        span: The run's span handle.
        before: The accumulator snapshot taken immediately before the run.
        usage: The shared accumulator, now including the run.
        model_tier: The deployed tier, for pricing. None leaves cost unpriced (0.0).
    """
    spent = usage_delta(before, usage)
    span.spent(
        usd=turn_cost_usd(model_tier, spent) if model_tier is not None else 0.0,
        tokens=spent.input_tokens + spent.output_tokens,
        tool_calls=spent.tool_calls,
    )


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
