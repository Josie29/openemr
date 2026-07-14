from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model

from copilot.fhir_tools import register_fhir_read_tools
from copilot.graph.deps import GraphDeps
from copilot.graph.gate import enforce_claim_grounding
from copilot.graph.outputs import ExtractorOutput, RetrieverOutput
from copilot.retrieval import EvidenceSnippet
from copilot.schemas import ChatResponse
from copilot.verification import CompositeResolver

# Shared citation discipline every agent obeys — the contract the grounding gate enforces
# mechanically. Kept in one place so the three prompts cannot drift apart on the one rule that must
# hold identically everywhere. Ported from the Week-1 single-agent system prompt.
_CITATION_RULES = """Every factual statement is a Claim carrying a SourceRef.
- Cite the source EXACTLY as it appears in the data you were given: copy `resource_type` and
  `resource_id` verbatim.
- For a structured field, set `field` to the field the statement draws from (e.g. birth_date,
  clinical_status). For a free-text source (a clinical note or a guideline chunk), set `quote` to
  the EXACT verbatim span that supports the statement — word-for-word, never paraphrased.
- Leave `value`, `label`, `date` empty — the system fills them from the source.
- Cite EVERY record a statement draws on: the primary in `source`, each additional one in
  `supporting`. Prefer atomic statements about a single record.
- Do NOT assert a relationship between two records (that a visit was *for* a diagnosis, that one
  problem caused another) unless a single record's own field states it. State each fact on its own
  and let the physician draw the link.
- Do NOT assert drug interactions or clinical conclusions as fact. If a medication and an allergy or
  problem look inconsistent, surface it for the physician to review, citing the specific rows —
  never state it as a definite interaction.
- If you cannot cite a source you actually saw this turn, do not make the statement. A claim that
  does not ground is rejected and you will be asked to correct it."""

INTAKE_EXTRACTOR_PROMPT = f"""You are the intake-extractor in a clinical Co-Pilot's graph. Your job
is to read THIS patient's record and surface the facts the question needs, as cited claims for the
supervisor to use.

Your read tools are each scoped to the one open patient (call the ones a question needs, in parallel
when independent):
- get_patient: demographics.
- get_problems: the active/inactive problem list.
- get_medications: current medications (deduplicated).
- get_allergies: allergies.
- get_encounters: recent encounters, metadata only (dates, type, reason).
- get_encounter_note(encounter_id): the free-text note for one visit — the narrative the structured
  lists don't hold. Find the visit with get_encounters first, then read the note for the SPECIFIC
  encounter the question is about. If that visit has no note, say so rather than scanning others.

For a broad "who is this / give me the picture" request, fetch problems, medications, allergies, and
the most recent encounters so the orientation is complete; for a focused question, fetch only what
it needs. Answer only from what the tools return — if the record lacks something (e.g. labs,
vitals), say so plainly rather than inferring.

Return an ExtractorOutput: a one-line `summary` and a `claims` list, leading with safety signals (a
high-severity allergy, an anticoagulant).

{_CITATION_RULES}

Do not retrieve guideline evidence and do not write the physician-facing answer — those are the
evidence-retriever's and the supervisor's jobs."""

EVIDENCE_RETRIEVER_PROMPT = f"""You are the evidence-retriever in a clinical Co-Pilot's graph. Your
job is to find guideline evidence relevant to the clinical question and return it as cited claims
for the supervisor to use.

Call `search_guidelines` with a focused query built from the clinical question (and any patient
facts you were given — the condition, the screening topic). Read the returned snippets and return a
RetrieverOutput: a one-line `summary` and a `claims` list, each stating one guideline point and
grounding it in a snippet.

{_CITATION_RULES}

Cite guideline snippets by their `GuidelineChunk` resource_type and the snippet's chunk id, with a
verbatim `quote` from the snippet text. Surface criteria, thresholds, screening intervals, and
monitoring cadence — never dosing directives. If retrieval returns nothing relevant, say so in the
summary and return no claims rather than inventing evidence."""

# Langfuse Prompt Management name for the physician-facing answer prompt. The final answer is the
# turn's user-visible output, so this is the prompt version stamped on each trace (the router and
# worker prompts are internal). The code below stays the source of truth; this only names the copy
# synced to Langfuse for observability. See observability.py.
ANSWERER_PROMPT_NAME = "copilot-answerer-prompt"

ANSWERER_PROMPT = f"""You are the supervisor writing the final answer in a clinical Co-Pilot.
You are given the intake-extractor's cited patient facts and the evidence-retriever's guideline
(whichever the routing gathered). Compose them into one answer for a physician who has seconds
between rooms.

Return a ChatResponse: a `summary`, a `claims` list, and two or three `follow_ups`.

Writing the summary — earn the scan by ordering, not padding:
- Lead with the single most decision-relevant fact — the one most likely to change what the
  physician does next (a safety signal outranks a routine line). When the honest answer is an
  absence ("no drug allergies are recorded"), lead with that.
- Front-load the punchline: make the first sentence the answer itself. Skip preambles like "Based on
  the record" and do not restate the question. Let the question set the shape — a focused question
  gets a one- or two-sentence answer, a broad request a brief orientation. Stay short.

Follow-ups — the next questions THIS physician is most likely to ask given this answer:
- Make them specific to what you just surfaced; phrase each as the physician would type it.
- Only suggest questions answerable from this patient's record or the guideline corpus.
- Prefer fewer, sharper suggestions; always offer at least one unless nothing meaningful follows.

{_CITATION_RULES}

You may ONLY restate claims already grounded by the workers — cite the same FHIR record or guideline
chunk they did. Do not introduce a new fact. If the gathered evidence cannot answer the question,
say so plainly."""


def build_intake_extractor(model: Model) -> Agent[GraphDeps, ExtractorOutput]:
    """Build the intake-extractor worker: reads the patient record, returns cited intake facts.

    Owns the full patient-scoped FHIR read toolset (via :func:`register_fhir_read_tools`), so it
    subsumes the Week-1 single agent's record-reading capability — including encounters and
    free-text notes. Its output_validator is the shared grounding gate bound to the FHIR
    ``FetchLog``, so a fact not traceable to a record it read this turn is rejected before hand-off.

    Args:
        model: The Pydantic AI model (or test double) the worker runs on.

    Returns:
        The configured intake-extractor agent, typed over ``GraphDeps`` and ``ExtractorOutput``.
    """
    agent: Agent[GraphDeps, ExtractorOutput] = Agent(
        model,
        deps_type=GraphDeps,
        output_type=ExtractorOutput,
        system_prompt=INTAKE_EXTRACTOR_PROMPT,
        retries=2,
    )
    register_fhir_read_tools(agent)

    @agent.output_validator
    async def enforce_grounding(
        ctx: RunContext[GraphDeps], output: ExtractorOutput
    ) -> ExtractorOutput:
        """Reject any intake claim not grounded in a FHIR record read this turn."""
        return enforce_claim_grounding(output, ctx.deps.fetched, subject="intake-extractor")

    return agent


def build_evidence_retriever(model: Model) -> Agent[GraphDeps, RetrieverOutput]:
    """Build the evidence-retriever worker: retrieves guideline snippets, returns cited evidence.

    The worker's output_validator is the shared grounding gate bound to the guideline
    ``ChunkRegistry``, so an evidence claim whose quote is not in a chunk it retrieved this turn is
    rejected before hand-off.

    Args:
        model: The Pydantic AI model (or test double) the worker runs on.

    Returns:
        The configured evidence-retriever agent, typed over ``GraphDeps`` and ``RetrieverOutput``.
    """
    agent: Agent[GraphDeps, RetrieverOutput] = Agent(
        model,
        deps_type=GraphDeps,
        output_type=RetrieverOutput,
        system_prompt=EVIDENCE_RETRIEVER_PROMPT,
        retries=2,
    )

    @agent.tool
    async def search_guidelines(ctx: RunContext[GraphDeps], query: str) -> list[EvidenceSnippet]:
        """Retrieve the top guideline snippets for a query via the hybrid-RAG pipeline.

        Args:
            ctx: The run context (holds the retriever and the chunk registry).
            query: A focused retrieval query built from the clinical question and patient facts.
        """
        snippets = await ctx.deps.retriever.retrieve(query, limit=5)
        # Record what was retrieved so the grounding gate can resolve any chunk the worker cites.
        ctx.deps.chunks.record_all(snippets)
        return snippets

    @agent.output_validator
    async def enforce_grounding(
        ctx: RunContext[GraphDeps], output: RetrieverOutput
    ) -> RetrieverOutput:
        """Reject any evidence claim whose quote is not in a chunk retrieved this turn."""
        return enforce_claim_grounding(output, ctx.deps.chunks, subject="evidence-retriever")

    return agent


def build_answerer(model: Model) -> Agent[GraphDeps, ChatResponse]:
    """Build the supervisor's final-answer agent: composes worker findings into the answer.

    Its output_validator is the shared grounding gate bound to a :class:`CompositeResolver` over
    both the FHIR ``FetchLog`` and the guideline ``ChunkRegistry``, so the final answer may only
    restate claims the workers already grounded — no new, unattributable fact can enter at the
    composition step.

    Args:
        model: The Pydantic AI model (or test double) the answerer runs on.

    Returns:
        The configured answerer agent, typed over ``GraphDeps`` and ``ChatResponse``.
    """
    agent: Agent[GraphDeps, ChatResponse] = Agent(
        model,
        deps_type=GraphDeps,
        output_type=ChatResponse,
        system_prompt=ANSWERER_PROMPT,
        retries=2,
    )

    @agent.output_validator
    async def enforce_grounding(ctx: RunContext[GraphDeps], output: ChatResponse) -> ChatResponse:
        """Reject any final claim not grounded in a FHIR record or guideline chunk this turn."""
        resolver = CompositeResolver((ctx.deps.fetched, ctx.deps.chunks))
        return enforce_claim_grounding(output, resolver, subject="final answer")

    return agent
