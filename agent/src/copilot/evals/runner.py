import logging
import os
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.usage import UsageLimits

from copilot.config import ModelTier, Settings, get_settings
from copilot.fhir.fixtures import FixtureFhirClient
from copilot.fhir.models import UploadedDocumentSummary
from copilot.graph.deps import GraphDeps
from copilot.graph.supervisor import build_graph, run_graph
from copilot.ingestion.extractor import DocumentExtractor, FixtureOcrBackend
from copilot.ingestion.registry import DocumentFactRegistry
from copilot.ingestion.schemas import DocType
from copilot.observability import TurnTrace
from copilot.rag.retriever import FixtureEvidenceRetriever
from copilot.retrieval import ChunkRegistry
from copilot.schemas import ChatResponse
from copilot.verification import FetchLog

logger = logging.getLogger("copilot.evals.runner")

# Override to evaluate a non-default tier (full identifier, e.g. 'anthropic:claude-sonnet-5').
_EVAL_MODEL_TIER_ENV = "COPILOT_EVAL_MODEL_TIER"

# Committed document fixtures the eval replays for the cases whose patient (Sergio Angulo, pid 23)
# carries uploaded documents. Resolved from the source tree (evals always run from the repo, like
# the seed bundles under fhir/seed/): parents[3] is the agent/ package root. These are wired ONLY
# for a patient whose record surfaces a document — every other case keeps extraction disabled.
_DOCUMENTS_DIR = Path(__file__).parents[3] / "tests" / "fixtures" / "documents"
# The demo PDF per document type: the fixture client serves these bytes for whichever seeded
# document has that type, so an intake extraction reads the intake form's own page.
_DOCUMENT_PDF_PATHS = {
    DocType.LAB_PDF: _DOCUMENTS_DIR / "pdfs" / "sergio-angulo-lab-report.pdf",
    DocType.INTAKE_FORM: _DOCUMENTS_DIR / "pdfs" / "sergio-angulo-intake-form.pdf",
}
# One recorded OCR response per document type — the replay is keyed by type, not by patient.
_OCR_FIXTURE_PATHS = {
    DocType.LAB_PDF: _DOCUMENTS_DIR / "extractions" / "sergio-angulo-lab-report.ocr.json",
    DocType.INTAKE_FORM: _DOCUMENTS_DIR / "extractions" / "sergio-angulo-intake-form.ocr.json",
}

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


def _fixture_extractor_for(documents: list[UploadedDocumentSummary]) -> DocumentExtractor | None:
    """Build a deterministic fixture OCR extractor covering the doc types this patient's record has.

    Keeps evals offline and deterministic: a patient whose record surfaces an uploaded document gets
    a ``FixtureOcrBackend`` replaying the recorded response for that document's TYPE — no live
    Mistral call — so a case genuinely exercises ``attach_and_extract``. A patient with no uploaded
    document surfaces none, so extraction stays disabled (``None``).

    Replay is keyed by doc type (one recording per type), which is what lets a lab case and an
    intake case coexist in one process. The invariant that remains, stated honestly: the golden set
    holds at most one document per type, so a per-type recording is unambiguous.

    Args:
        documents: The patient's uploaded document summaries (from ``get_documents``).

    Returns:
        A fixture-backed :class:`DocumentExtractor` when the patient has any extractable document,
        else None.
    """
    paths = {
        doc.doc_type: str(_OCR_FIXTURE_PATHS[doc.doc_type])
        for doc in documents
        if doc.doc_type in _OCR_FIXTURE_PATHS
    }
    if not paths:
        return None
    return DocumentExtractor(FixtureOcrBackend(paths))


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
    retriever over the in-repo corpus, a fixture OCR extractor for a patient with an uploaded lab
    document (deterministic replay, no live OCR), fresh grounding registries, and the same per-run
    tool-call ceiling. A degraded turn (gate refusal or tool-call ceiling) is caught and reported as
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
    # Serve the fixture lab PDF's bytes so attach_and_extract exercises the real OCR pipeline for
    # the one patient with an uploaded lab report; harmless for every other patient, whose record
    # surfaces no lab document to extract. A caller supplying its own fhir client for a lab-document
    # patient must configure its document_pdf_path likewise.
    fhir = fhir or FixtureFhirClient.from_seed(
        {doc_type: str(path) for doc_type, path in _DOCUMENT_PDF_PATHS.items()}
    )
    graph = build_graph(build_eval_model(settings))
    deps = GraphDeps(
        fhir=fhir,
        patient_id=patient_id,
        correlation_id=f"eval-{patient_id}",
        retriever=FixtureEvidenceRetriever.from_corpus(settings.rerank_top_n),
        fetched=FetchLog(),
        chunks=ChunkRegistry(),
        # Extraction is wired deterministically (recorded OCR replay, no live API) only for a
        # patient whose record surfaces an uploaded lab document — the both-tools synthesis case.
        # For every other patient the extractor is None, so the intake-extractor reports no document
        # and the run stays deterministic and OCR-call-free, matching the fixture-only PHI-free
        # contract.
        documents=DocumentFactRegistry(),
        extractor=_fixture_extractor_for(await fhir.get_documents(patient_id)),
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
