from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model

from copilot.graph.deps import GraphDeps
from copilot.graph.gate import enforce_claim_grounding
from copilot.graph.outputs import ExtractorOutput, PatientRecordFacts, RetrieverOutput
from copilot.retrieval import EvidenceSnippet
from copilot.schemas import ChatResponse
from copilot.verification import CompositeResolver

# Shared citation discipline both workers and the final answer obey — the contract the grounding
# gate enforces mechanically. Kept in one place so the three prompts cannot drift apart on the one
# rule that must hold identically everywhere.
_CITATION_RULES = """Every factual statement is a Claim carrying a SourceRef.
- Cite the source EXACTLY as it appears in the data you were given: copy `resource_type` and
  `resource_id` verbatim.
- For a structured field, set `field` to the field the statement draws from (e.g. birth_date,
  clinical_status). For a free-text source (a clinical note or a guideline chunk), set `quote` to
  the EXACT verbatim span that supports the statement — copied word-for-word, never paraphrased.
- Leave `value`, `label`, `date` empty — the system fills them from the source.
- If you cannot cite a source you actually saw this turn for a statement, do not make the
  statement. A claim that does not ground is rejected and you will be asked to correct it."""

INTAKE_EXTRACTOR_PROMPT = f"""You are the intake-extractor in a clinical Co-Pilot's worker graph.
Your job is to surface the patient's intake picture as cited facts for the supervisor to use.

Call `get_patient_record` once to read the structured record (demographics, problems, medications,
allergies). Then return an ExtractorOutput: a one-line `summary` and a `claims` list stating the
facts the question needs — who the patient is, their active problems, current medications, and any
allergies (lead with allergies and anticoagulants; they change what the physician does next).

{_CITATION_RULES}

Do not retrieve guideline evidence and do not answer the physician — that is the evidence-retriever
and the supervisor's job. Extract only what the record states."""

EVIDENCE_RETRIEVER_PROMPT = f"""You are the evidence-retriever in a clinical Co-Pilot's graph.
Your job is to find guideline evidence relevant to the clinical question and return it as cited
claims for the supervisor to use.

Call `search_guidelines` with a focused query built from the clinical question (and any patient
facts you were given — e.g. the condition, the screening topic). Read the returned snippets and
return a RetrieverOutput: a one-line `summary` and a `claims` list, each stating one guideline
point and grounding it in a snippet.

{_CITATION_RULES}

Cite guideline snippets by their `GuidelineChunk` resource_type and the snippet's chunk id, with a
verbatim `quote` from the snippet text. Surface criteria, thresholds, screening intervals, and
monitoring cadence — never dosing directives. If retrieval returns nothing relevant, say so in the
summary and return no claims rather than inventing evidence."""

ANSWERER_PROMPT = f"""You are the supervisor writing the final answer in a clinical Co-Pilot.
You are given the intake-extractor's cited patient facts and the evidence-retriever's guideline
evidence (whichever the routing gathered). Compose them into one answer for a physician who has
seconds between rooms.

Return a ChatResponse: a short `summary` that leads with the single most decision-relevant point,
a `claims` list, and two or three specific `follow_ups`.

{_CITATION_RULES}

You may ONLY restate claims already grounded by the workers — cite the same FHIR record or guideline
chunk they did. Do not introduce a new fact, and never assert a relationship between a patient fact
and a guideline unless each side is separately cited; surface it as something for the physician to
consider. If the gathered evidence cannot answer the question, say so plainly."""


def build_intake_extractor(model: Model) -> Agent[GraphDeps, ExtractorOutput]:
    """Build the intake-extractor worker: reads the patient record, returns cited intake facts.

    The worker's output_validator is the shared grounding gate bound to the FHIR ``FetchLog``, so
    an extracted fact not traceable to a record it read this turn is rejected before hand-off.

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

    @agent.tool
    async def get_patient_record(ctx: RunContext[GraphDeps]) -> PatientRecordFacts:
        """Read the open patient's demographics, problems, medications, and allergies at once."""
        patient = await ctx.deps.fhir.get_patient(ctx.deps.patient_id)
        problems = await ctx.deps.fhir.get_problems(ctx.deps.patient_id)
        medications = await ctx.deps.fhir.get_medications(ctx.deps.patient_id)
        allergies = await ctx.deps.fhir.get_allergies(ctx.deps.patient_id)
        # Record everything read so the grounding gate can resolve any field the worker cites.
        ctx.deps.fetched.record_all(patient)
        ctx.deps.fetched.record_all(problems)
        ctx.deps.fetched.record_all(medications)
        ctx.deps.fetched.record_all(allergies)
        return PatientRecordFacts(
            patient=patient, problems=problems, medications=medications, allergies=allergies
        )

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
