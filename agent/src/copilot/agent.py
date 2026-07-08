from dataclasses import dataclass

from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models import Model

from copilot.fhir.client import FhirClient
from copilot.fhir.models import PatientDemographics
from copilot.schemas import ChatResponse
from copilot.verification import FetchLog, resolve_claims

SYSTEM_PROMPT = """You are a Clinical Co-Pilot embedded in an electronic health record. You
help a physician orient on the patient currently open, using ONLY the tools provided to read
that patient's record.

Rules you must follow without exception:
- Answer only from data returned by the tools. Never state a fact you did not read from a tool
  this turn. If the record does not contain something, say so plainly rather than inferring.
- Every factual statement you make must be a Claim carrying a SourceRef that cites the resource
  (resource_type and resource_id) and the exact `field` name from that tool's returned data
  (e.g. full_name, gender, birth_date). If you cannot cite a fetched field for a statement, do
  not make the statement. Leave SourceRef.value empty — the system fills it from the record.
- Keep the summary short and scannable — a physician has seconds. Do not pad a sparse record.
"""


@dataclass
class CopilotDeps:
    """Per-request dependencies injected into the agent run.

    Constructed at the route boundary and passed to ``agent.run`` — the agent never reaches
    into global state. ``fetched`` is mutated by tools and read by the verification gate.
    """

    fhir: FhirClient
    patient_id: str
    correlation_id: str
    fetched: FetchLog


def build_agent(model: Model | str) -> Agent[CopilotDeps, ChatResponse]:
    """Construct the single Clinical Co-Pilot agent (ARCHITECTURE.md §6 — one agent).

    The agent owns one tool (``get_patient``) and one output validator (the grounding gate).
    The model is a parameter so tests can inject ``TestModel``/``FunctionModel`` and run the
    full flow deterministically with no live LLM call.

    Args:
        model: A Pydantic AI model identifier string (e.g. ``"anthropic:claude-sonnet-5"``) or
            a ``Model`` instance (e.g. a test double).

    Returns:
        The configured agent, typed over ``CopilotDeps`` and ``ChatResponse``.
    """
    agent: Agent[CopilotDeps, ChatResponse] = Agent(
        model,
        deps_type=CopilotDeps,
        output_type=ChatResponse,
        system_prompt=SYSTEM_PROMPT,
    )

    @agent.tool
    async def get_patient(ctx: RunContext[CopilotDeps]) -> PatientDemographics:
        """Read the open patient's demographics (name, birth date, sex) from FHIR.

        Records the fetch so the verification gate can confirm any citation to this resource
        is grounded.

        Args:
            ctx: The run context carrying per-request dependencies.

        Returns:
            The patient's typed demographics.

        Raises:
            FhirError: If the FHIR read fails; the caller degrades gracefully.
        """
        demographics = await ctx.deps.fhir.get_patient(ctx.deps.patient_id)
        ctx.deps.fetched.record("Patient", demographics.patient_id, demographics)
        return demographics

    @agent.output_validator
    async def enforce_grounding(ctx: RunContext[CopilotDeps], output: ChatResponse) -> ChatResponse:
        """Reject any claim not grounded in a fetched field, and stamp the real value into the rest.

        This is the deterministic half of ARCHITECTURE.md §7 — the only verification the walking
        skeleton enforces. Each claim's citation is resolved against the actual fetched resource;
        grounded claims get the record's true value stamped in (code-populated, so it is
        trustworthy), and any claim that cannot be resolved forces a retry. Faithfulness (the
        Haiku entailment judge) and domain constraints are deferred to prompt ``-02``.

        Args:
            ctx: The run context (holds the fetch registry).
            output: The agent's candidate structured answer.

        Returns:
            The response with the real record value stamped into every claim's citation.

        Raises:
            ModelRetry: When a claim cites a resource/field that was not read this turn or has no
                value, forcing the model to correct itself.
        """
        grounded, offenders = resolve_claims(output, ctx.deps.fetched)
        if offenders:
            detail = "; ".join(
                f"claim {c.text!r} cites {c.source.resource_type}/{c.source.resource_id}"
                f".{c.source.field} which has no value in the data read this turn"
                for c in offenders
            )
            raise ModelRetry(
                "Every claim must cite a field you actually read via a tool this turn. These do "
                f"not: {detail}. Re-ground them to a real field, or state the information is not "
                "available."
            )
        return grounded

    return agent
