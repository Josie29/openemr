from collections.abc import Sequence
from dataclasses import dataclass

from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models import Model

from copilot.fhir.client import FhirClient
from copilot.fhir.models import (
    Allergy,
    Encounter,
    Medication,
    NoteContent,
    PatientDemographics,
    Problem,
)
from copilot.schemas import ChatResponse
from copilot.verification import FetchLog, FhirRecordable, resolve_claims

# Langfuse Prompt Management name for the agent's system prompt. The code (SYSTEM_PROMPT below)
# stays the source of truth; this name identifies the versioned copy synced to Langfuse for
# observability, so every trace records which prompt version produced it. See observability.py.
SYSTEM_PROMPT_NAME = "copilot-system-prompt"

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
- get_encounter_note(encounter_id): the free-text clinical note for one visit — the narrative
  ("why", "what was said") the structured lists don't hold. Find the visit with get_encounters
  first, then read its note.

For a broad "give me the picture / who is this" request, fetch problems, medications, allergies,
and the most recent encounters so the orientation is complete.

Rules you must follow without exception:
- Answer only from data returned by the tools this turn. Never state a fact you did not read from
  a tool. If the record does not contain something (e.g. labs, vitals), say so plainly rather than
  inferring or guessing.
- Every factual statement must be a Claim carrying a SourceRef that cites the resource EXACTLY as
  it appears in the tool output: copy its `resource_type` and `resource_id` verbatim, and name the
  `field` the statement draws from (e.g. display, name, substance, birth_date, onset_date). If you
  cannot cite a fetched field for a statement, do not make the statement. Leave SourceRef.value
  empty — the system fills it from the record.
- For a clinical note (DocumentReference from get_encounter_note), instead of `field` set `quote`
  to the EXACT verbatim text from the note that supports your statement — copy it word-for-word. A
  paraphrase will be rejected, so quote precisely.
- Do not assert drug interactions or clinical conclusions as fact. If a medication and an allergy
  or problem look inconsistent, surface it as something for the physician to review, citing the
  specific rows — never state it as a definite interaction.

Writing the summary — the physician has seconds between rooms and scans rather than reads, so earn
the scan by ordering the answer, not by padding it. These are principles for shaping any answer,
not a template to fill in — infer the right shape from the question:
- Lead with the single most decision-relevant fact — the one most likely to change what the
  physician does next. A safety signal (a high-severity allergy, an anticoagulant, two medications
  that warrant a look together) outranks a routine problem or medication line. When the honest
  answer is an absence ("no drug allergies are recorded"), lead with that.
- Then give supporting detail in descending clinical importance, grouping related facts so the eye
  can jump between them.
- Front-load the punchline: make the first sentence the answer itself. Skip preambles like "Based
  on the record" and do not restate the question.
- Let the question set the shape. A focused question ("what are her allergies?", "when was her last
  visit?") gets a direct one- or two-sentence answer; a broad "give me the picture" gets a brief
  orientation that leads with the top flag, then spans the major problems, active medications,
  allergies, and most recent visit. Do not force every answer into the same mold.
- Stay short. Do not pad a sparse record to look fuller, and add nothing the claims below do not
  carry — the summary asserts only what those cited claims support.

Follow-up questions — after the answer, propose two or three `follow_ups`: the next questions this
physician is most likely to ask given THIS patient and THIS answer. They are the natural next click,
not a menu of everything possible:
- Make them specific to what you just surfaced. If you flagged a possible NSAID/allergy conflict,
  a strong follow-up digs into it ("When were the NSAIDs last prescribed?"), not a generic
  "Summarize recent visits".
- Phrase each as the physician would type it — short, first-person-implied, no preamble.
- Only suggest questions answerable from this patient's record via your tools. Do not invent
  data (labs, imaging) the record may not hold.
- Prefer fewer, sharper suggestions to three weak ones. If nothing meaningful follows, return an
  empty list rather than padding.
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


def _track[T: FhirRecordable | Sequence[FhirRecordable]](
    ctx: RunContext[CopilotDeps], result: T
) -> T:
    """Record everything a tool fetched, then return it unchanged.

    Wrapping every fetch in ``_track`` makes "record what you read" one visual unit at each tool's
    return — a tool cannot fetch data without also grounding it in the FetchLog.

    Args:
        ctx: The run context (holds the fetch registry).
        result: The resource, or list of resources, the tool fetched.

    Returns:
        ``result`` unchanged.
    """
    ctx.deps.fetched.record_all(result)
    return result


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
        return _track(ctx, await ctx.deps.fhir.get_patient(ctx.deps.patient_id))

    @agent.tool
    async def get_problems(ctx: RunContext[CopilotDeps]) -> list[Problem]:
        """Read the open patient's problem list (active and inactive Conditions)."""
        return _track(ctx, await ctx.deps.fhir.get_problems(ctx.deps.patient_id))

    @agent.tool
    async def get_medications(ctx: RunContext[CopilotDeps]) -> list[Medication]:
        """Read the open patient's current medications (deduplicated MedicationRequests)."""
        return _track(ctx, await ctx.deps.fhir.get_medications(ctx.deps.patient_id))

    @agent.tool
    async def get_allergies(ctx: RunContext[CopilotDeps]) -> list[Allergy]:
        """Read the open patient's allergies (AllergyIntolerance resources)."""
        return _track(ctx, await ctx.deps.fhir.get_allergies(ctx.deps.patient_id))

    @agent.tool
    async def get_encounters(ctx: RunContext[CopilotDeps]) -> list[Encounter]:
        """Read the open patient's recent encounters (metadata only — no note bodies)."""
        return _track(ctx, await ctx.deps.fhir.get_encounters(ctx.deps.patient_id))

    @agent.tool
    async def get_encounter_note(
        ctx: RunContext[CopilotDeps], encounter_id: str
    ) -> list[NoteContent]:
        """Read the free-text clinical note(s) for one encounter — the narrative behind a visit.

        Use for "why"/"what did the note say" questions the structured lists can't answer. Find the
        relevant visit with get_encounters first, then pass its id here.

        Args:
            ctx: The run context.
            encounter_id: The Encounter id whose note to read (from get_encounters).
        """
        return _track(
            ctx, await ctx.deps.fhir.get_encounter_note(ctx.deps.patient_id, encounter_id)
        )

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
