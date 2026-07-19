import logging

from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models import Model

from copilot.fhir.models import UploadedDocumentSummary
from copilot.fhir_tools import register_fhir_read_tools
from copilot.graph.budget import budgeted
from copilot.graph.deps import BudgetedTool, GraphDeps
from copilot.graph.gate import enforce_claim_grounding
from copilot.graph.outputs import ExtractorOutput, RetrieverOutput
from copilot.ingestion.extractor import ExtractionError, resolve_and_extract
from copilot.ingestion.registry import DocumentFactHandle
from copilot.observability import score_current_turn
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
- get_patient_summary: the whole structured record — demographics, problems, medications, allergies,
  and recent encounters — in ONE call. This is your read for the structured record, whether the
  question is broad ("who is this / give me the picture") or focused ("what is her DOB?"): call it
  once and use the field the question needs.
- get_lab_observations(code): the patient's laboratory results already in the chart (FHIR
  Observations), oldest first — for "what is their <lab>" and for trends over time. Filter to one
  analyte by its LOINC `code` (e.g. "787-2" for MCV), and prefer the code over the analyte's name,
  since several distinct LOINC codes share a name. These are the chart's structured labs — distinct
  from a lab value that lives only in an uploaded report (see attach_and_extract).
- get_encounter_note(encounter_id): the free-text note for one visit — the narrative the structured
  lists don't hold. Find the visit in get_patient_summary's recent encounters first, then read the
  note for the SPECIFIC encounter the question is about.

You also read UPLOADED documents (values not yet filed in the chart):
- list_documents: the patient's uploaded documents (id, title, date, and `doc_type`, which is one of
  "lab_pdf", "intake_form", or "medication_list"). Metadata only.
- attach_and_extract(document_id): OCR one uploaded document into its individual facts. What is read
  depends on the document's own type — you do not choose it:
  - a `lab_pdf` returns lab results, each with `resource_type` "Observation" and the printed
    `test_name`/`value`/`unit`/`reference_range`/`abnormal_flag`.
  - an `intake_form` returns what the patient wrote at the front desk: demographics
    (`resource_type` "Patient"), allergies ("AllergyIntolerance"), and family history
    ("FamilyMemberHistory"). It does NOT return medications — those come from a medication list.
  - a `medication_list` returns the medications on a pharmacy/discharge medication list, each with
    `resource_type` "MedicationRequest" and the printed `name`/`dose`/`frequency`.
  For lab VALUES and trends, prefer get_lab_observations (the chart's structured labs). But an
  uploaded document is a FIRST-CLASS source, not a last resort: when the question is about one —
  "the med list", "what medications is he on", what the patient reported at intake, a value from an
  uploaded report — call list_documents, then attach_and_extract the relevant document, and answer
  from it. In particular, if the patient has a `medication_list` on file and the question is about
  their medications, read that list; do NOT answer from the chart's medications alone — the list is
  what the patient brought in, it can differ from the chart, and surfacing it (and, where useful,
  noting where it disagrees with the chart) is the point. When unsure whether a relevant document
  exists, call list_documents to check before falling back to the chart. Cite each fact with its
  `resource_type`/`resource_id` and `field` "value" — verbatim from the tool result. Facts from an
  uploaded document are what the PATIENT supplied, not what a clinician has confirmed. Say so when
  it matters — a medication list a patient brought in is not the chart's medication list, and the
  two can disagree.

Read the structured record with get_patient_summary once, add get_lab_observations for lab values or
trends, read a note for a visit's narrative, and read an uploaded document whenever the question is
about one — or when a relevant `medication_list` / `intake_form` is on file for a question about the
patient's medications or what they reported. Answer only from what the tools return, and never
infer a value the record does not hold.

Return an ExtractorOutput: a one-line `summary` and a `claims` list, leading with safety signals (a
high-severity allergy, an anticoagulant).

{_CITATION_RULES}

Do not retrieve guideline evidence and do not write the physician-facing answer — those are the
evidence-retriever's and the supervisor's jobs."""

EVIDENCE_RETRIEVER_PROMPT = f"""You are the evidence-retriever in a clinical Co-Pilot's graph. Your
job is to find guideline evidence relevant to the clinical question and return it as cited claims
for the supervisor to use.

Call `search_guidelines` ONCE, with a focused query built from the CLINICAL TOPIC the question is
about — the condition, the screening subject, the monitoring question. The corpus is a small fixed
set of clinical topics and one search reads all of it, so a second query with different wording
reaches the same content: whatever the first search returns is what the corpus holds. If it does
not cover the question, that is your finding — report it and stop, rather than rephrasing.

Use only de-identified clinical terms; never put patient identifiers (name, MRN, date of birth) or
specific patient values in the query (it is sent to external retrieval/rerank services). Each
returned snippet has just two fields: a `chunk_id` and its `text`. Read them and return a
RetrieverOutput: a one-line `summary` and a `claims` list, each stating one guideline point and
grounding it in a snippet.

{_CITATION_RULES}

Cite guideline snippets with resource_type `guideline` and the snippet's `chunk_id` as resource_id,
and set `quote` to a span copied verbatim from THAT snippet's `text` — word-for-word from the
`text`, nothing else. Surface criteria, thresholds, screening intervals, and monitoring cadence —
never dosing directives. If retrieval returns nothing relevant, say so in the summary and return no
claims rather than inventing evidence.

"""

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

    @agent.tool(prepare=budgeted(BudgetedTool.LIST_DOCUMENTS))
    async def list_documents(ctx: RunContext[GraphDeps]) -> list[UploadedDocumentSummary] | str:
        """List the patient's uploaded documents (id, title, date, doc_type) for extraction.

        Call this whenever the question is about something a patient may have uploaded — their
        medications / "med list", what they reported at intake, a value from an uploaded report —
        before answering from the chart alone. An uploaded `medication_list` is a primary source for
        a medications question, not a fallback.

        Args:
            ctx: The run context (holds the patient-scoped FHIR client).

        Returns:
            The patient's uploaded documents, or a sentence saying there are none.
        """
        documents = await ctx.deps.fhir.get_documents(ctx.deps.patient_id)
        if not documents:
            return "This patient has no uploaded documents on file."
        return documents

    @agent.tool
    async def attach_and_extract(
        ctx: RunContext[GraphDeps], document_id: str
    ) -> list[DocumentFactHandle]:
        """OCR one uploaded document into its individual, citable facts.

        What is read from the document is decided by the document's own type, not by the caller: a
        lab report yields lab results, an intake form yields demographics, allergies, and family
        history, and a medication list yields medications.

        Args:
            ctx: The run context (holds the extractor and the document-fact registry).
            document_id: The DocumentReference id from list_lab_documents to extract.
        """
        if ctx.deps.extractor is None:
            return []
        # Memoized per document id: don't re-fetch + re-OCR a document already extracted this turn.
        # The memo is only populated after a successful lookup+extract below, so checking it before
        # the lookup is behavior-preserving (a hit means the id was already resolved this turn).
        if document_id in ctx.deps.extracted_documents:
            return ctx.deps.extracted_documents[document_id]
        # resolve_and_extract is the shared core (also the /documents/{id}/extraction endpoint): it
        # only extracts a document the patient actually has (rejects a hallucinated/guessed id
        # before the expensive Binary fetch + OCR), and reads the schema off the discovered record's
        # OpenEMR category — so the model chooses WHICH document to read, never how to read it. The
        # bytes ride the request's own patient-scoped FHIR client. All registry side effects stay
        # HERE in the tool; the helper records nothing.
        try:
            extracted = await resolve_and_extract(
                document_id, ctx.deps.patient_id, ctx.deps.extractor, ctx.deps.fhir
            )
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
                extra={"document_id": document_id},
                exc_info=True,
            )
            # SCORE IT TOO. The log line above is invisible to a monitor; the extraction-failure
            # alert (A6) counts this score. Same reasoning as turn_error/tool_error: the failure is
            # caught inside the span, so the span closes clean and would read as a success.
            score_current_turn("extraction_error", 1.0)
            return []
        if extracted is None:
            # Not one of the patient's uploaded documents (a guessed id) — no facts.
            return []
        # Record the extracted facts so the grounding gate can resolve any the worker cites.
        handles = ctx.deps.documents.record(extracted)
        ctx.deps.extracted_documents[document_id] = handles
        # Retain the full typed extraction for the write-back payload: record() keeps only what
        # grounding needs and drops the LOINC code, units, and range the persist endpoint requires.
        ctx.deps.extractions[document_id] = extracted
        # Field-coverage score (JOS-64). Emitted per document, so a multi-document turn produces
        # several scores and Langfuse averages them — per-document granularity is what makes the
        # metric actionable. None (a document stating no fields) emits nothing rather than 0.0.
        pass_rate = extracted.coverage.pass_rate
        if pass_rate is not None:
            score_current_turn("extraction_field_pass_rate", pass_rate)
        return handles

    @agent.output_validator
    async def enforce_grounding(
        ctx: RunContext[GraphDeps], output: ExtractorOutput
    ) -> ExtractorOutput:
        """Reject any claim not grounded in a FHIR read or an extracted fact.

        Also rejects a zero-claim output when facts were extracted — a dropped claims array
        (e.g. a truncated structured output) has no offenders for ``ground_claims`` to catch, so it
        would silently hand the composer an empty answer to fabricate around.

        Raises:
            ModelRetry: When a claim is ungrounded, or facts were extracted but none is cited.
        """
        extracted_count = sum(len(handles) for handles in ctx.deps.extracted_documents.values())
        if extracted_count and not output.claims:
            raise ModelRetry(
                f"You extracted {extracted_count} fact(s) from the document(s) this turn but "
                "returned zero claims. Emit one claim per fact you are reporting, each citing the "
                "fact's resource_type and resource_id verbatim from the tool result."
            )
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

    @agent.tool(prepare=budgeted(BudgetedTool.SEARCH_GUIDELINES))
    async def search_guidelines(
        ctx: RunContext[GraphDeps], query: str
    ) -> list[RetrievedGuideline] | str:
        """Retrieve the top guideline snippets for a query via the hybrid-RAG pipeline.

        Returns only each snippet's ``chunk_id`` and ``text`` — the fields the model cites and
        quotes from. All provenance (source, url, and the verbatim ``anchor_quote`` used for
        deep-linking) is withheld and stamped by the system later, so the model cannot cite a field
        the grounding gate does not check (JOS-89).

        Args:
            ctx: The run context (holds the retriever and the chunk registry).
            query: A focused retrieval query built from the clinical question and patient facts.

        Returns:
            Snippets ranked best-first, or a sentence saying the corpus does not cover the topic.
            That sentence names the corpus as CLOSED.
        """
        snippets = await ctx.deps.retriever.retrieve(query)
        # Record the FULL snippets so the grounding gate can resolve, and the response serializer
        # can stamp provenance for, any chunk the worker cites — even though the model only sees a
        # trimmed view of them.
        ctx.deps.chunks.record_all(snippets)
        # Retrieval scores (JOS-64), per CALL — the prompt permits several searches per turn, so a
        # turn can miss then hit and Langfuse averages them. `retrieve` returns the POST-FLOOR list
        # (retriever._above_floor, settings.retrieval_relevance_floor), so an empty result means
        # nothing cleared the relevance bar — the codebase's own definition of "relevant enough",
        # not a new threshold invented here.
        #
        # Scored BEFORE the empty-corpus early return below: a miss is the case this metric exists
        # for, so returning first would record only the hits and read as a permanent 1.0.
        score_current_turn("retrieval_hit", 1.0 if snippets else 0.0)
        # The top score alongside it separates the two failure modes a bare hit rate conflates: a
        # miss with a near-floor top score means the floor is miscalibrated; a miss with a low one
        # means the corpus does not cover the question.
        score_current_turn(
            "retrieval_top_score", max((s.rerank_score for s in snippets), default=0.0)
        )
        if not snippets:
            return (
                "No guideline in this corpus covers that topic. The corpus is a small fixed set of "
                "clinical topics and this search read all of it, so a reworded query searches the "
                "same content and will return the same nothing. Report that no guideline evidence "
                "was found."
            )
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
