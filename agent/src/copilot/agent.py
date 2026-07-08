from dataclasses import dataclass

from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models import Model

from copilot.fhir.client import FhirClient
from copilot.fhir.models import Allergy, Encounter, Medication, PatientDemographics, Problem
from copilot.schemas import ChatResponse
from copilot.verification import FetchLog, resolve_claims

SYSTEM_PROMPT = """You are a Clinical Co-Pilot embedded in an electronic health record. You help a
physician orient on the patient currently open, using ONLY the tools provided to read that
patient's record. Every tool is already scoped to the one open patient.

Your tools (each returns typed resources; call the ones a question needs, in parallel when
independent):
- get_patient: demographics (Patient).
- get_problems: the active/inactive problem list (Condition resources).
- get_medications: current medications (MedicationRequest resources), already deduplicated.
- get_allergies: allergies (AllergyIntolerance resources).
- get_encounters: recent encounters, metadata only — dates, type, reason (Encounter resources).

For a broad "give me the picture / who is this" request, fetch problems, medications, allergies,
and the most recent encounters, then give a short scannable orientation with the single most
relevant item flagged.

Rules you must follow without exception:
- Answer only from data returned by the tools this turn. Never state a fact you did not read from
  a tool. If the record does not contain something (e.g. labs, vitals), say so plainly rather than
  inferring or guessing.
- Every factual statement must be a Claim carrying a SourceRef that cites the resource EXACTLY as
  it appears in the tool output: copy its `resource_type` and `resource_id` verbatim, and name the
  `field` the statement draws from (e.g. display, name, substance, birth_date, onset_date). If you
  cannot cite a fetched field for a statement, do not make the statement. Leave SourceRef.value
  empty — the system fills it from the record.
- Do not assert drug interactions or clinical conclusions as fact. If a medication and an allergy
  or problem look inconsistent, surface it as something for the physician to review, citing the
  specific rows — never state it as a definite interaction.
- Keep the summary short and scannable — a physician has seconds. Do not pad a sparse record.
"""


@dataclass
class CopilotDeps:
    """Per-request dependencies injected into the agent run.

    Constructed at the route boundary and passed to ``agent.run`` — the agent never reaches
    into global state. ``fhir`` is built per request from the inbound patient-scoped token, so
    it can only read the one open patient. ``fetched`` is mutated by tools and read by the
    verification gate.
    """

    fhir: FhirClient
    patient_id: str
    correlation_id: str
    fetched: FetchLog


def build_agent(model: Model | str) -> Agent[CopilotDeps, ChatResponse]:
    """Construct the single Clinical Co-Pilot agent (ARCHITECTURE.md §6 — one agent).

    The agent owns the five FHIR read tools and one output validator (the grounding gate). The
    model is a parameter so tests can inject ``TestModel``/``FunctionModel`` and run the full flow
    deterministically with no live LLM call.

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
        """Read the open patient's demographics (name, birth date, sex) from FHIR."""
        demographics = await ctx.deps.fhir.get_patient(ctx.deps.patient_id)
        ctx.deps.fetched.record(demographics.resource_type, demographics.resource_id, demographics)
        return demographics

    @agent.tool
    async def get_problems(ctx: RunContext[CopilotDeps]) -> list[Problem]:
        """Read the open patient's problem list (active and inactive Conditions)."""
        problems = await ctx.deps.fhir.get_problems(ctx.deps.patient_id)
        for problem in problems:
            ctx.deps.fetched.record(problem.resource_type, problem.resource_id, problem)
        return problems

    @agent.tool
    async def get_medications(ctx: RunContext[CopilotDeps]) -> list[Medication]:
        """Read the open patient's current medications (deduplicated MedicationRequests)."""
        medications = await ctx.deps.fhir.get_medications(ctx.deps.patient_id)
        for medication in medications:
            ctx.deps.fetched.record(medication.resource_type, medication.resource_id, medication)
        return medications

    @agent.tool
    async def get_allergies(ctx: RunContext[CopilotDeps]) -> list[Allergy]:
        """Read the open patient's allergies (AllergyIntolerance resources)."""
        allergies = await ctx.deps.fhir.get_allergies(ctx.deps.patient_id)
        for allergy in allergies:
            ctx.deps.fetched.record(allergy.resource_type, allergy.resource_id, allergy)
        return allergies

    @agent.tool
    async def get_encounters(ctx: RunContext[CopilotDeps]) -> list[Encounter]:
        """Read the open patient's recent encounters (metadata only — no note bodies)."""
        encounters = await ctx.deps.fhir.get_encounters(ctx.deps.patient_id)
        for encounter in encounters:
            ctx.deps.fetched.record(encounter.resource_type, encounter.resource_id, encounter)
        return encounters

    @agent.output_validator
    async def enforce_grounding(ctx: RunContext[CopilotDeps], output: ChatResponse) -> ChatResponse:
        """Reject any claim not grounded in a fetched field, and stamp the real value into the rest.

        This is the deterministic half of ARCHITECTURE.md §7. Each claim's citation is resolved
        against the actual fetched resource; grounded claims get the record's true value stamped in
        (code-populated, so it is trustworthy), and any claim that cannot be resolved forces a
        retry. Faithfulness (the Haiku entailment judge) is a later increment.

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
