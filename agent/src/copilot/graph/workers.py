import logging

from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model

from copilot.fhir.models import UploadedDocumentSummary
from copilot.fhir_tools import register_fhir_read_tools
from copilot.graph.deps import GraphDeps
from copilot.graph.gate import enforce_claim_grounding
from copilot.graph.outputs import ExtractorOutput, RetrieverOutput
from copilot.ingestion.extractor import ExtractionError, FhirBinaryByteSource
from copilot.ingestion.registry import DocumentFactHandle
from copilot.rag.models import RetrievedGuideline
from copilot.schemas import ChatResponse
from copilot.verification import CompositeResolver

logger = logging.getLogger(__name__)

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

Your read tools are each scoped to the one open patient:
- get_patient_summary: demographics, problems, medications, allergies, and recent encounters in ONE
  call. Use this for a broad "who is this / give me the picture" request — one call, not five.
- get_patient / get_problems / get_medications / get_allergies / get_encounters: the same data as
  individual reads. Use these ONLY for a focused question that needs just one of them (e.g. "what is
  her DOB?" → get_patient), so you don't over-fetch. Call independent ones in parallel.
- get_encounter_note(encounter_id): the free-text note for one visit — the narrative the structured
  lists don't hold. Find the visit with get_encounters (or get_patient_summary) first, then read the
  note for the SPECIFIC encounter the question is about. If that visit has no note, say so rather
  than scanning others.

You also read UPLOADED documents (values the FHIR lists don't hold):
- list_documents: the patient's uploaded documents (id, title, date, and `doc_type`, which is either
  "lab_pdf" or "intake_form"). Metadata only.
- attach_and_extract(document_id): OCR one uploaded document into its individual facts. What is read
  depends on the document's own type — you do not choose it:
  - a `lab_pdf` returns lab results, each with `resource_type` "Observation" and the printed
    `test_name`/`value`/`unit`/`reference_range`/`abnormal_flag`.
  - an `intake_form` returns what the patient wrote at the front desk: demographics
    (`resource_type` "Patient"), current medications ("MedicationRequest"), allergies
    ("AllergyIntolerance"), and family history ("FamilyMemberHistory").
  When a question is about lab values, a trend, an uploaded report, or something the patient
  reported on their intake form (a chief concern, a medication they take, a family history), call
  list_documents, then attach_and_extract on the relevant document, and state the facts the question
  needs. Cite each fact with its `resource_type`/`resource_id` and `field` "value" — verbatim from
  the tool result.
  Facts from an intake form are what the PATIENT reported, not what a clinician has confirmed. Say
  so when it matters — an intake medication list is not the chart's medication list, and the two can
  disagree.

For a broad "who is this / give me the picture" request, call get_patient_summary once so the
orientation is complete in a single read; for a focused question, fetch only what it needs. Answer
only from what the tools return — if the record lacks something (e.g. the labs are not in an
uploaded report), say so plainly rather than inferring.

Return an ExtractorOutput: a one-line `summary` and a `claims` list, leading with safety signals (a
high-severity allergy, an anticoagulant).

{_CITATION_RULES}

Do not retrieve guideline evidence and do not write the physician-facing answer — those are the
evidence-retriever's and the supervisor's jobs."""

EVIDENCE_RETRIEVER_PROMPT = f"""You are the evidence-retriever in a clinical Co-Pilot's graph. Your
job is to find guideline evidence relevant to the clinical question and return it as cited claims
for the supervisor to use.

Call `search_guidelines` with a focused query built from the CLINICAL TOPIC the question is about
— the condition, the screening subject, the monitoring question. Use only de-identified clinical
terms; never put patient identifiers (name, MRN, date of birth) or specific patient values in the
query (it is sent to external retrieval/rerank services). Each returned snippet has just two fields:
a `chunk_id` and its `text`. Read them and return a RetrieverOutput: a one-line `summary` and a
`claims` list, each stating one guideline point and grounding it in a snippet.

{_CITATION_RULES}

Cite guideline snippets with resource_type `guideline` and the snippet's `chunk_id` as resource_id,
and set `quote` to a span copied verbatim from THAT snippet's `text` — word-for-word from the
`text`, nothing else. Surface criteria, thresholds, screening intervals, and monitoring cadence —
never dosing directives. If retrieval returns nothing relevant, say so in the summary and return no
claims rather than inventing evidence."""

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
- Synthesize, don't recite: the guideline snippets you were given render beside your answer on their
  own source cards, so give the physician the conclusion in your own words — do NOT restate each
  snippet sentence by sentence. The cards carry the verbatim quotes; your job is the synthesis.

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

    @agent.tool
    async def list_documents(ctx: RunContext[GraphDeps]) -> list[UploadedDocumentSummary]:
        """List the patient's uploaded documents (id, title, date, doc_type) for extraction.

        Memoized per turn: the FHIR discovery read runs once, so repeated calls (a model retrying on
        an empty list) return the cached result instead of hammering FHIR.

        Args:
            ctx: The run context (holds the patient-scoped FHIR client).
        """
        cache = ctx.deps.documents_cache
        if cache is None:
            cache = await ctx.deps.fhir.get_documents(ctx.deps.patient_id)
            ctx.deps.documents_cache = cache
        return cache

    @agent.tool
    async def attach_and_extract(
        ctx: RunContext[GraphDeps], document_id: str
    ) -> list[DocumentFactHandle]:
        """OCR one uploaded document into its individual, citable facts.

        What is read from the document is decided by the document's own type, not by the caller: a
        lab report yields lab results, an intake form yields demographics, medications, allergies,
        and family history.

        Args:
            ctx: The run context (holds the extractor and the document-fact registry).
            document_id: The DocumentReference id from list_lab_documents to extract.
        """
        if ctx.deps.extractor is None:
            return []
        # Only extract a document the patient actually has — i.e. one list_documents returned.
        # Rejects a hallucinated/guessed id and avoids wasting a Binary fetch + OCR (the expensive
        # hop) on a document that isn't there. If discovery hasn't run, there is nothing to extract.
        #
        # This lookup is also what keeps the SCHEMA out of the model's hands: the doc type comes off
        # the discovered record, resolved from its OpenEMR category, so the tool takes no doc_type
        # argument the model could set. The model chooses WHICH document to read, never how to read
        # it.
        summary = next(
            (doc for doc in (ctx.deps.documents_cache or []) if doc.resource_id == document_id),
            None,
        )
        if summary is None:
            return []
        # Memoized per document id: don't re-fetch + re-OCR a document already extracted this turn.
        if document_id in ctx.deps.extracted_documents:
            return ctx.deps.extracted_documents[document_id]
        # Fetch the document's bytes over the request's own patient-scoped FHIR client (Binary),
        # so the bytes are authorized by the open patient's access rights — keyed on the same id
        # the citation + click-to-source viewer use.
        byte_source = FhirBinaryByteSource(ctx.deps.fhir)
        try:
            extracted = await ctx.deps.extractor.extract(document_id, summary.doc_type, byte_source)
        except ExtractionError:
            # The document could not be read — return no facts so the worker reports the gap
            # rather than fabricating facts around a failed OCR.
            #
            # LOG IT. An empty list is indistinguishable from "the agent read this document and it
            # had nothing in it", so swallowing this silently turns every misconfiguration into a
            # plausible-looking answer. FixtureOcrBackend deliberately raises a LOUD error for a
            # doc_type it has no recording for (its docstring: "a turn that looks like 'this
            # document has nothing in it' rather than a misconfiguration") — catching it without a
            # word here is what defeated that intent, and cost a full trace investigation to
            # rediscover.
            logger.warning(
                "document extraction failed; reporting no facts for this document",
                extra={"document_id": document_id, "doc_type": summary.doc_type.value},
                exc_info=True,
            )
            return []
        # Record the extracted facts so the grounding gate can resolve any the worker cites.
        handles = ctx.deps.documents.record(extracted)
        ctx.deps.extracted_documents[document_id] = handles
        # Retain the full typed extraction for the write-back payload: record() keeps only what
        # grounding needs and drops the LOINC code, units, and range the persist endpoint requires.
        ctx.deps.extractions[document_id] = extracted
        return handles

    @agent.output_validator
    async def enforce_grounding(
        ctx: RunContext[GraphDeps], output: ExtractorOutput
    ) -> ExtractorOutput:
        """Reject any intake claim not grounded in a FHIR record read or a lab fact extracted."""
        resolver = CompositeResolver((ctx.deps.fetched, ctx.deps.documents))
        return enforce_claim_grounding(output, resolver, subject="intake-extractor")

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
    async def search_guidelines(
        ctx: RunContext[GraphDeps], query: str
    ) -> list[RetrievedGuideline]:
        """Retrieve the top guideline snippets for a query via the hybrid-RAG pipeline.

        Returns only each snippet's ``chunk_id`` and ``text`` — the fields the model cites and
        quotes from. All provenance (source, url, and the verbatim ``anchor_quote`` used for
        deep-linking) is withheld and stamped by the system later, so the model cannot cite a field
        the grounding gate does not check (JOS-89).

        Args:
            ctx: The run context (holds the retriever and the chunk registry).
            query: A focused retrieval query built from the clinical question and patient facts.
        """
        snippets = await ctx.deps.retriever.retrieve(query)
        # Record the FULL snippets so the grounding gate can resolve, and the response serializer
        # can stamp provenance for, any chunk the worker cites — even though the model only sees a
        # trimmed view of them.
        ctx.deps.chunks.record_all(snippets)
        return [RetrievedGuideline.from_snippet(snippet) for snippet in snippets]

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
        """Reject any final claim not grounded in a FHIR record, guideline chunk, or lab fact."""
        resolver = CompositeResolver((ctx.deps.fetched, ctx.deps.chunks, ctx.deps.documents))
        return enforce_claim_grounding(output, resolver, subject="final answer")

    return agent
