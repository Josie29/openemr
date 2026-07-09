import logging
import os
from dataclasses import dataclass
from typing import Any

from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from copilot.agent import CopilotDeps, build_agent
from copilot.config import ModelTier, Settings, get_settings
from copilot.evals.cases import Tool
from copilot.fhir.fixtures import FixtureFhirClient
from copilot.schemas import ChatResponse
from copilot.verification import FetchLog

logger = logging.getLogger("copilot.evals.runner")

# Override to evaluate a non-default tier (full identifier, e.g. 'anthropic:claude-sonnet-5').
_EVAL_MODEL_TIER_ENV = "COPILOT_EVAL_MODEL_TIER"

# The grounding gate exhausted its retries without an attributable answer — mirrors the /chat
# route's refusal so the eval scores the same degraded output a physician would see.
_REFUSAL = ChatResponse(
    summary="I could not produce an answer I can fully attribute to this patient's record.",
    claims=[],
)

_KNOWN_TOOLS = {tool.value for tool in Tool}


@dataclass(frozen=True)
class AgentRun:
    """The observable result of running one agent turn under eval.

    Args:
        response: The agent's structured answer (or the refusal sentinel if the gate refused).
        tools_called: The FHIR read tools the agent actually invoked this turn, in call order.
        refused: True when the grounding gate exhausted retries and degraded to the refusal.
    """

    response: ChatResponse
    tools_called: list[str]
    refused: bool


def resolve_eval_model_tier() -> ModelTier:
    """Return the Claude tier the agent-under-test runs on during evals.

    Defaults to the cheapest tier (Haiku) so eval runs stay inexpensive — evals here check
    grounding/faithfulness behavior, not the top-tier reasoning the service reserves Sonnet/Opus
    for. Override with ``COPILOT_EVAL_MODEL_TIER`` (a full identifier, e.g.
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
    """Construct the Claude model the agent-under-test runs on during evals.

    Uses the eval tier (cheapest by default — see :func:`resolve_eval_model_tier`), *not* the
    service's configured ``model_tier``, so eval runs stay cheap regardless of the deployed tier.
    The API key is passed explicitly from settings rather than read implicitly, so a missing key
    fails at request time with a clear provider error instead of silently picking up an ambient key.

    Args:
        settings: Settings carrying the Anthropic API key.

    Returns:
        A Pydantic AI ``Model`` for the resolved eval tier.
    """
    model_id = resolve_eval_model_tier().value.partition(":")[2]
    provider = AnthropicProvider(api_key=settings.anthropic_api_key or "not-configured")
    return AnthropicModel(model_id, provider=provider)


def _tools_called(messages: list[Any]) -> list[str]:
    """Extract the FHIR read tools the agent invoked, filtering out the output/result tool.

    Pydantic AI records each tool invocation as a ``ToolCallPart`` on a ``ModelResponse``; the
    final structured output is delivered via its own result tool, which we exclude by keeping only
    names in the agent's declared FHIR tool set.

    Args:
        messages: The message history from ``result.all_messages()``.

    Returns:
        The invoked FHIR tool names in call order (duplicates preserved — a repeated read is a
        signal, not noise).
    """
    called: list[str] = []
    for message in messages:
        if not isinstance(message, ModelResponse):
            continue
        for part in message.parts:
            if isinstance(part, ToolCallPart) and part.tool_name in _KNOWN_TOOLS:
                called.append(part.tool_name)
    return called


async def run_case(
    patient_id: str,
    message: str,
    *,
    settings: Settings | None = None,
    fhir: FixtureFhirClient | None = None,
) -> AgentRun:
    """Run one agent turn against the FHIR fixtures and capture what it did.

    Runs the real agent (real Claude model, real grounding gate) in fixture mode, so the eval
    exercises genuine model behavior with deterministic, PHI-free data. A gate refusal is caught
    and reported as ``refused=True`` rather than raised — a refusal is a scoreable outcome (correct
    for an unanswerable case, a miss for an answerable one), not a harness error.

    Args:
        patient_id: Fixture Patient logical id to scope the turn to.
        message: The physician's question.
        settings: Optional settings override; defaults to the process settings.
        fhir: Optional shared fixture client; one is built from the seed if omitted.

    Returns:
        The agent's response, the tools it called, and whether the gate refused.
    """
    settings = settings or get_settings()
    fhir = fhir or FixtureFhirClient.from_seed()
    agent = build_agent(build_eval_model(settings))
    deps = CopilotDeps(
        fhir=fhir,
        patient_id=patient_id,
        correlation_id=f"eval-{patient_id}",
        fetched=FetchLog(),
    )
    try:
        result = await agent.run(message, deps=deps)
    except UnexpectedModelBehavior:
        return AgentRun(response=_REFUSAL, tools_called=[], refused=True)
    return AgentRun(
        response=result.output,
        tools_called=_tools_called(result.all_messages()),
        refused=False,
    )
