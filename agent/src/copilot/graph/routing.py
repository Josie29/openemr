from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model

from copilot.graph.deps import GraphDeps


class Route(StrEnum):
    """The next step the supervisor can hand off to.

    A closed set, so the supervisor's decision is an exhaustively-matchable enum rather than a
    free string — the routing stays procedural and inspectable (the PRD Core Req 4 requirement).
    """

    EXTRACT_INTAKE = "extract_intake"
    RETRIEVE_EVIDENCE = "retrieve_evidence"
    ANSWER = "answer"


class RouteDecision(BaseModel):
    """One logged supervisor hand-off: which worker comes next, and why.

    This is the structured route event the PRD asks to be inspectable. Emitting it as a typed
    object (not a hidden tool-call) is what lets the routing be reconstructed from the trace: each
    decision becomes a child span carrying its ``route`` and ``reason`` under the turn's
    correlation id.
    """

    model_config = ConfigDict(frozen=True)

    route: Route = Field(description="The next step: extract intake, retrieve evidence, or answer")
    reason: str = Field(
        description="Why this step is next given the question and what has been gathered"
    )


SUPERVISOR_PROMPT = """You are the supervisor of a clinical Co-Pilot's worker graph. You do not
answer the physician yourself and you do not read data yourself — you decide the SINGLE next step,
one hand-off at a time, and explain why.

The steps you can choose:
- `extract_intake`: hand off to the intake-extractor to read the patient's record (demographics,
  problems, medications, allergies). Choose this when the answer needs facts about THIS patient and
  they have not been gathered yet.
- `retrieve_evidence`: hand off to the evidence-retriever to find clinical-guideline evidence
  (criteria, thresholds, screening/monitoring cadence). Choose this when the answer needs guideline
  backing and it has not been gathered yet.
- `answer`: stop routing and let the final answer be composed. Choose this once everything the
  question needs has been gathered — or when neither worker can add anything more.

You are told the question and what has already been gathered this turn. Decide the next step that
moves toward a complete, grounded answer, and do not repeat a step already completed. Return a
RouteDecision with the `route` and a one-line `reason`."""


def build_supervisor_router(model: Model) -> Agent[GraphDeps, RouteDecision]:
    """Build the bounded router agent whose only output is the next :class:`RouteDecision`.

    The LLM's role in routing is deliberately narrow — emit one typed decision — so control flow
    stays in the procedural supervisor loop (``run_graph``) rather than in free-form tool calls.
    That is what makes the routing inspectable on our terms.

    Args:
        model: The Pydantic AI model (or test double) the router runs on.

    Returns:
        The configured router agent, typed over ``GraphDeps`` and ``RouteDecision``.
    """
    return Agent(
        model,
        deps_type=GraphDeps,
        output_type=RouteDecision,
        system_prompt=SUPERVISOR_PROMPT,
        retries=2,
    )
