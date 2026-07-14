import logging
import os
from dataclasses import dataclass

from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.usage import UsageLimits

from copilot.config import ModelTier, Settings, get_settings
from copilot.fhir.fixtures import FixtureFhirClient
from copilot.graph.deps import GraphDeps
from copilot.graph.supervisor import build_graph, run_graph
from copilot.observability import TurnTrace
from copilot.rag.retriever import FixtureEvidenceRetriever
from copilot.retrieval import ChunkRegistry
from copilot.schemas import ChatResponse
from copilot.verification import FetchLog

logger = logging.getLogger("copilot.evals.runner")

# Override to evaluate a non-default tier (full identifier, e.g. 'anthropic:claude-sonnet-5').
_EVAL_MODEL_TIER_ENV = "COPILOT_EVAL_MODEL_TIER"

# The grounding gate exhausted its retries (or the turn hit the tool-call ceiling) without an
# attributable answer — mirrors the /chat route's refusal so the eval scores the same degraded
# output a physician would see.
_REFUSAL = ChatResponse(
    summary="I could not produce an answer I can fully attribute to this patient's record.",
    claims=[],
)


@dataclass(frozen=True)
class AgentRun:
    """The observable result of running one graph turn under eval.

    Args:
        response: The composed structured answer (or the refusal sentinel if the turn degraded).
        routes: The ordered supervisor hand-offs this turn (e.g. ``["extract_intake", "answer"]``),
            so a case can be reasoned about by the control flow it took, not just its output.
        refused: True when the grounding gate exhausted retries or the tool-call ceiling was hit and
            the turn degraded to the refusal.
    """

    response: ChatResponse
    routes: list[str]
    refused: bool


def resolve_eval_model_tier() -> ModelTier:
    """Return the Claude tier the graph-under-test runs on during evals.

    Defaults to the cheapest tier (Haiku) so eval runs stay inexpensive — evals here check
    grounding/faithfulness/refusal behavior, not the top-tier reasoning the service reserves
    Sonnet/Opus for. Override with ``COPILOT_EVAL_MODEL_TIER`` (a full identifier, e.g.
    ``anthropic:claude-sonnet-5``) to evaluate the production tier instead.

    Returns:
        The resolved model tier; falls back to Haiku if the override value is not a known tier.
    """
    raw = os.environ.get(_EVAL_MODEL_TIER_ENV)
    if not raw:
        return ModelTier.HAIKU
    try:
        return ModelTier(raw)
    except ValueError:
        logger.warning(
            "Unknown %s; falling back to Haiku",
            _EVAL_MODEL_TIER_ENV,
            extra={"provided": raw, "valid": [tier.value for tier in ModelTier]},
        )
        return ModelTier.HAIKU


def build_eval_model(settings: Settings) -> Model:
    """Construct the Claude model the graph-under-test runs on during evals.

    Uses the eval tier (cheapest by default — see :func:`resolve_eval_model_tier`), *not* the
    service's configured ``model_tier``, so eval runs stay cheap regardless of the deployed tier.
    The API key is passed explicitly from settings rather than read implicitly, so a missing key
    fails at request time with a clear provider error instead of silently picking up an ambient key.

    Args:
        settings: Settings carrying the Anthropic API key.

    Returns:
        A Pydantic AI ``Model`` for the resolved eval tier — every agent in the graph runs on it.
    """
    model_id = resolve_eval_model_tier().value.partition(":")[2]
    provider = AnthropicProvider(api_key=settings.anthropic_api_key or "not-configured")
    return AnthropicModel(model_id, provider=provider)


async def run_case(
    patient_id: str,
    message: str,
    *,
    settings: Settings | None = None,
    fhir: FixtureFhirClient | None = None,
) -> AgentRun:
    """Run one turn through the supervisor graph against the fixtures and capture what it did.

    Runs the real graph (real Claude model, real grounding gate on every worker + the answer) in
    fixture mode, so the eval exercises genuine model behavior with deterministic, PHI-free data.
    The wiring mirrors ``/chat`` (:mod:`copilot.main`): a fixture FHIR client, a fixture evidence
    retriever over the in-repo corpus, fresh grounding registries, and the same per-run tool-call
    ceiling. A degraded turn (gate refusal or tool-call ceiling) is caught and reported as
    ``refused=True`` rather than raised — a refusal is a scoreable outcome (correct for an
    out-of-scope case, a miss for an answerable one), not a harness error.

    Args:
        patient_id: Fixture Patient logical id to scope the turn to.
        message: The physician's question.
        settings: Optional settings override; defaults to the process settings.
        fhir: Optional shared fixture client; one is built from the seed if omitted.

    Returns:
        The composed response, the routing trail, and whether the turn degraded to a refusal.
    """
    settings = settings or get_settings()
    fhir = fhir or FixtureFhirClient.from_seed()
    graph = build_graph(build_eval_model(settings))
    deps = GraphDeps(
        fhir=fhir,
        patient_id=patient_id,
        correlation_id=f"eval-{patient_id}",
        retriever=FixtureEvidenceRetriever.from_corpus(settings.rerank_top_n),
        fetched=FetchLog(),
        chunks=ChunkRegistry(),
    )
    try:
        result = await run_graph(
            graph,
            message,
            deps,
            TurnTrace(None),  # no Langfuse span in the harness; the run is scored on its output
            max_hops=settings.agent_max_hops,
            usage_limits=UsageLimits(tool_calls_limit=settings.agent_tool_calls_limit),
        )
    except (UnexpectedModelBehavior, UsageLimitExceeded):
        return AgentRun(response=_REFUSAL, routes=[], refused=True)
    return AgentRun(
        response=result.answer,
        routes=[decision.route.value for decision in result.routes],
        refused=False,
    )
