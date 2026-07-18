import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.usage import UsageLimits

from copilot.api_schemas import (
    DocumentsResponse,
    EvidenceItem,
    EvidenceResponse,
    ExtractionResponse,
)
from copilot.config import FhirClientMode, Settings, get_settings
from copilot.conversation import ConversationStore
from copilot.correlation import CorrelationIdMiddleware, current_correlation_id
from copilot.fhir.client import FhirClient, FhirError, HttpFhirClient
from copilot.fhir.fixtures import FixtureFhirClient
from copilot.fhir.models import LabObservation
from copilot.graph.deps import GraphDeps
from copilot.graph.supervisor import build_graph, run_graph
from copilot.graph.workers import ANSWERER_PROMPT, ANSWERER_PROMPT_NAME
from copilot.health import check_readiness
from copilot.ingestion.extractor import ExtractionError, build_extractor, resolve_and_extract
from copilot.ingestion.schemas import paths_by_doc_type
from copilot.ingestion.wire import derived_facts_for
from copilot.observability import (
    configure_observability,
    observe_turn,
    shutdown_observability,
    sync_system_prompt,
)
from copilot.pricing import turn_cost_usd
from copilot.rag.retriever import FixtureEvidenceRetriever, RetrievalError, build_retriever
from copilot.retrieval import ChunkRegistry
from copilot.schemas import (
    ChatRequest,
    ChatResponse,
    CitationSourceType,
    Evidence,
    LabPoint,
    LabSeries,
)
from copilot.verification import FetchLog

logger = logging.getLogger("copilot")


def _configure_logging() -> None:
    """Route the service's own ``copilot`` logs to stdout at INFO.

    Uvicorn configures only its own loggers, so without this the app's ``logger.info`` calls
    (token rejections, gate refusals, FHIR failures) fall through to the root logger's default
    WARNING threshold and never appear. Idempotent — ``basicConfig`` is a no-op once the root
    logger already has handlers, so repeated ``create_app`` calls (tests) don't stack handlers.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )


class ChatFailureReason(StrEnum):
    """Distinct, greppable reason codes for every ``/chat`` failure branch.

    Stamped onto each failure log line as a structured ``reason`` field so operators can tell the
    branches apart in the Railway logs and alert on a specific one — the combined
    ``UnexpectedModelBehavior``/``UsageLimitExceeded`` branch used to log all of these
    indistinguishably. Backed (a :class:`~enum.StrEnum`) because the value is what lands in the log
    record and what a grep/alert query matches.
    """

    # Grounding gate exhausted its retries — no attributable answer.
    GROUNDING_EXHAUSTED = "grounding_exhausted"
    # Per-turn tool-call ceiling hit — a runaway loop.
    TOOL_CEILING = "tool_ceiling"
    # The model provider rejected the call (billing / rate limit / outage).
    LLM_HTTP_ERROR = "llm_http_error"
    # A patient-data (FHIR) read failed.
    FHIR_READ_FAILED = "fhir_read_failed"
    # Any unforeseen failure caught by the route's catch-all boundary.
    UNEXPECTED = "unexpected"


_UNAVAILABLE_ANSWER = ChatResponse(
    summary="I could not produce an answer I can fully attribute to this patient's record.",
    claims=[],
)


def _answer_payload(answer: ChatResponse, chunks: ChunkRegistry, deps: GraphDeps) -> dict[str, Any]:
    """Serialize the final answer: per-claim wire citations, evidence panel, and derived facts.

    The response keeps the answer's own ``claims`` (with their gate-stamped ``source``) and adds,
    per claim, the project-wide :data:`~copilot.schemas.Citation` list the sidebar's click-to-source
    (JOS-57) consumes — each a pure projection of a grounded ``SourceRef`` via
    :meth:`~copilot.schemas.SourceRef.to_citation`. It also adds a top-level ``evidence`` array: the
    distinct guideline sources that grounded the answer, deduped by chunk and ranked by relevance
    (§3.2); a ``derived_facts`` array: the persistable facts extracted from documents this turn,
    grouped by document, which the sidebar posts to the session-authed write-back endpoint (JOS-81);
    and a ``lab_series`` array: the trend charts projected from the lab Observations the agent read
    this turn (JOS-83). All are additive — nothing the current sidebar reads is removed — and stay
    off the LLM-facing ``Claim``/``ChatResponse`` models.

    Args:
        answer: The grounded final answer from the graph.
        chunks: The conversation's chunk registry — the source of the retrieved snippets' rerank
            scores and presentation metadata for the evidence panel.
        deps: The turn's graph deps — the source of the typed extractions the write-back payload
            projects (``deps.extractions``) and the fetch registry the lab charts read
            (``deps.fetched``).

    Returns:
        The JSON-serializable response body, each claim carrying a ``citations`` list and the body
        carrying ``evidence``, ``derived_facts``, and ``lab_series`` lists.
    """
    content: dict[str, Any] = answer.model_dump()
    for claim_dict, claim in zip(content["claims"], answer.claims, strict=True):
        claim_dict["citations"] = [
            ref.to_citation().model_dump(mode="json") for ref in [claim.source, *claim.supporting]
        ]
    content["evidence"] = _build_evidence(answer, chunks)
    content["derived_facts"] = derived_facts_for(deps.extractions)
    content["lab_series"] = _build_lab_series(deps.fetched)
    return content


def _build_evidence(answer: ChatResponse, chunks: ChunkRegistry) -> list[dict[str, Any]]:
    """Build the evidence panel: the distinct guideline sources the answer's claims cite.

    The panel shows SOURCES, not claim sentences — so this collects every guideline chunk the final
    claims cite, resolves each back to the snippet the retriever recorded (for its rerank score and
    presentation metadata), dedupes by chunk id, and orders by relevance. Non-guideline citations
    (FHIR records, lab facts) are not guideline evidence and are skipped. Empty when the answer
    grounds on no guideline chunk — the sidebar then shows no evidence section.

    Args:
        answer: The grounded final answer.
        chunks: The conversation's chunk registry holding this turn's retrieved snippets.

    Returns:
        JSON-serializable evidence entries, most relevant first, one per distinct cited chunk.
    """
    deduped: dict[str, Evidence] = {}
    for claim in answer.claims:
        for ref in (claim.source, *claim.supporting):
            if ref.resource_type != CitationSourceType.GUIDELINE.value:
                continue
            snippet = chunks.get(ref.resource_id)
            if snippet is None or ref.resource_id in deduped:
                continue
            deduped[ref.resource_id] = Evidence(
                source_id=snippet.citation.source_id,
                section=snippet.citation.page_or_section,
                quote=snippet.text,
                chunk_id=ref.resource_id,
                relevance_score=snippet.rerank_score,
                source_url=snippet.source_url,
                year=snippet.year,
                anchor_quote=snippet.anchor_quote,
            )
    ordered = sorted(deduped.values(), key=lambda e: e.relevance_score, reverse=True)
    return [entry.model_dump(mode="json") for entry in ordered]


_MAX_LAB_SERIES = 6


def _build_lab_series(fetched: FetchLog) -> list[dict[str, Any]]:
    """Project the turn's fetched lab Observations into trend series, one per LOINC code.

    Present only when the agent read labs this turn (``get_lab_observations`` recorded them).
    A point needs both a numeric value and a date to be plotted, so valueless questionnaire results
    and undated draws are dropped — never coerced. A series needs two or more such points; one draw
    is not a trend. Grouping by ``code`` keeps similar-named analytes apart and one unit per axis.
    Capped at ``_MAX_LAB_SERIES`` so a broad fetch cannot wall the sidebar.

    Args:
        fetched: The turn's fetch registry.

    Returns:
        JSON-serializable series, each with two or more oldest-first points; empty when no analyte
        has a plottable trend.
    """
    points_by_code: dict[str, list[LabPoint]] = {}
    source_by_code: dict[str, LabObservation] = {}
    for obs in fetched.records_of_type("Observation"):
        if not isinstance(obs, LabObservation):
            continue
        code, value, date = obs.code, obs.value, obs.effective_date
        if code is None or value is None or date is None:
            continue
        points_by_code.setdefault(code, []).append(
            LabPoint(
                date=date,
                value=value,
                status=obs.status or "final",
                observation_id=obs.resource_id,
            )
        )
        source_by_code.setdefault(code, obs)

    series: list[LabSeries] = []
    for code, points in points_by_code.items():
        if len(points) < 2:
            continue
        points.sort(key=lambda p: p.date)
        source = source_by_code[code]
        series.append(
            LabSeries(
                code=code, display=source.display or code, unit=source.unit or "", points=points
            )
        )

    # Most-recent draw first, so the analyte the physician just asked about tends to lead; a stable
    # key also keeps the sidebar deterministic across identical turns.
    series.sort(key=lambda s: s.points[-1].date, reverse=True)
    if len(series) > _MAX_LAB_SERIES:
        logger.info("lab_series truncated", extra={"found": len(series), "kept": _MAX_LAB_SERIES})
        series = series[:_MAX_LAB_SERIES]
    return [s.model_dump(mode="json") for s in series]


def _build_readiness_client(settings: Settings) -> HttpFhirClient | FixtureFhirClient:
    """Construct the app-lifetime FHIR client used by the ``/ready`` probe (and fixture reads).

    In ``HTTP`` mode this client carries no per-patient token — ``/ready`` only pings the
    unauthenticated FHIR ``/metadata`` capability statement. Per-request reads build their own
    token-scoped client from the inbound ``Authorization`` header (in the ``/chat`` route), so the
    readiness client is never used to read patient data. In ``FIXTURE`` mode the same client
    both answers readiness and serves reads (no token exists).

    Args:
        settings: Service settings selecting the client mode and endpoint.

    Returns:
        A fixture-backed client (dev/tests) or a token-less httpx client (live OpenEMR).

    Raises:
        ValueError: If ``HTTP`` mode is selected without a base URL.
    """
    if settings.fhir_client_mode is FhirClientMode.FIXTURE:
        return FixtureFhirClient.from_seed(
            paths_by_doc_type(
                lab_pdf=settings.document_pdf_path_lab_pdf,
                intake_form=settings.document_pdf_path_intake_form,
            )
        )
    # HTTP mode. A missing base URL is a misconfiguration, but we do not raise at startup —
    # crash-looping the deploy hides the cause. Construct a client anyway (empty base URL) so the
    # process starts and the misconfig surfaces as a red /ready FHIR probe; /chat separately 500s
    # with a clear message (see the route). This keeps observability, not opacity, as the failure.
    return HttpFhirClient(
        settings.fhir_base_url or "",
        settings.fhir_bearer_token,  # optional dev fallback; None is fine for the /metadata ping
        timeout_seconds=settings.fhir_timeout_seconds,
        max_retries=settings.fhir_max_retries,
    )


def _bearer_token(request: Request) -> str | None:
    """Extract the SMART patient-scoped token from the ``Authorization: Bearer`` header.

    This is the contract with the PHP module (deployment-strategy.md, Option D): the module mints
    a patient-scoped token and sends it per request, so the agent's FHIR reads can only touch the
    one open patient.

    Args:
        request: The inbound HTTP request.

    Returns:
        The bearer token, or None when no well-formed bearer header is present.
    """
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _build_model(settings: Settings) -> Model:
    """Construct the Claude model for the configured tier (dependency injection at the edge).

    The API key is passed explicitly from settings rather than read from the environment, so
    the model constructs even without a key (tests inject a scripted model and never call the
    real provider). A live call without a real key fails at request time, not construction.

    Args:
        settings: Service settings carrying the model tier and API key.

    Returns:
        A Pydantic AI ``Model`` for the configured Claude tier.
    """
    _, _, model_id = settings.model_tier.value.partition(":")
    provider = AnthropicProvider(api_key=settings.anthropic_api_key or "not-configured")
    return AnthropicModel(model_id, provider=provider)


def _require_token(
    request: Request, settings: Settings, correlation_id: str
) -> JSONResponse | None:
    """Gate a read on the SMART patient-scoped bearer token, without building a FHIR client.

    The token half of :func:`_resolve_request_fhir`, factored out so a read that needs auth but no
    FHIR client (``GET /evidence`` over the non-PHI corpus) shares one source of truth for the 401
    body. In fixture mode no token exists, so the gate is a no-op (matching ``/chat``), which is
    what lets the contract tests run without a token.

    Args:
        request: The inbound request (carries the ``Authorization`` header).
        settings: Service settings (mode, dev token fallback).
        correlation_id: This turn's correlation id, for logging.

    Returns:
        ``None`` when the request may proceed; a 401 ``JSONResponse`` when a token is required and
        absent.
    """
    if settings.fhir_client_mode is FhirClientMode.FIXTURE:
        return None
    if _bearer_token(request) or settings.fhir_bearer_token:
        return None
    logger.info("rejected read with no patient token", extra={"cid": correlation_id})
    return JSONResponse(
        status_code=401,
        content={"error": "missing patient-scoped FHIR token", "correlation_id": correlation_id},
    )


def _resolve_request_fhir(
    request: Request, settings: Settings, correlation_id: str
) -> tuple[FhirClient, HttpFhirClient | None] | JSONResponse:
    """Resolve the FHIR client for one ``/chat`` turn from the request's auth context.

    In HTTP mode the client is scoped to the inbound patient token, so the agent can physically
    read only the one open patient (ARCHITECTURE.md §5). In fixture mode the shared app client
    serves reads (no token exists).

    Args:
        request: The inbound request (carries the ``Authorization`` header and app state).
        settings: Service settings (mode, FHIR endpoint, timeouts).
        correlation_id: This turn's correlation id, for logging.

    Returns:
        On success, a ``(client, per_request_client)`` pair — ``per_request_client`` is the
        closable client the caller must ``aclose`` (HTTP mode) or ``None`` (fixture mode). On a
        bad request, a ``JSONResponse`` (401 no token, 500 misconfigured) to return directly.
    """
    if settings.fhir_client_mode is FhirClientMode.FIXTURE:
        return request.app.state.fhir, None
    denied = _require_token(request, settings, correlation_id)
    if denied is not None:
        return denied
    token = _bearer_token(request) or settings.fhir_bearer_token
    if not settings.fhir_base_url:
        logger.error("HTTP mode without a FHIR base URL", extra={"cid": correlation_id})
        return JSONResponse(
            status_code=500,
            content={"error": "agent misconfigured", "correlation_id": correlation_id},
        )
    client = HttpFhirClient(
        settings.fhir_base_url,
        token,
        timeout_seconds=settings.fhir_timeout_seconds,
        max_retries=settings.fhir_max_retries,
    )
    return client, client


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and wire the Clinical Co-Pilot agent service.

    Args:
        settings: Optional settings override (tests inject their own); production reads env.

    Returns:
        The configured FastAPI application.
    """
    settings = settings or get_settings()
    _configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        shutdown_observability(app.state.observability_enabled)
        # Close the evidence retriever (the Qdrant client, when live; a no-op for the fixture).
        await app.state.retriever.aclose()
        client = app.state.fhir
        if isinstance(client, HttpFhirClient):
            await client.aclose()

    app = FastAPI(title="AgentForge Clinical Co-Pilot", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CorrelationIdMiddleware)
    # Added last, so it wraps outermost and answers the browser's preflight before anything else.
    # The chat call originates in the physician's browser on the OpenEMR origin (ARCHITECTURE.md
    # §4), which is cross-origin to this service in every deployment. Credentials stay off: the
    # SMART token travels in the Authorization header, never in a cookie.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        # POST for /chat; GET for the read-only Week-2 subsystem endpoints (/documents,
        # /documents/{id}/extraction, /evidence). The browser preflights a cross-origin GET that
        # carries an Authorization header, so GET must be allowed explicitly.
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
    )

    app.state.settings = settings
    # Enable Langfuse + Pydantic AI OTel instrumentation before any agent runs.
    app.state.observability_enabled = configure_observability(settings)
    # Register the code's system prompt in Langfuse Prompt Management (documentation/observability
    # only — the agent still runs the in-code SYSTEM_PROMPT, never a fetched copy). The returned
    # ref is stamped onto each turn's trace so every answer records its prompt version.
    app.state.system_prompt_ref = sync_system_prompt(
        app.state.observability_enabled,
        ANSWERER_PROMPT_NAME,
        ANSWERER_PROMPT,
        settings.tracing_environment,
    )
    app.state.fhir = _build_readiness_client(settings)
    # The evidence retriever (JOS-53): the live Qdrant+Cohere hybrid pipeline in QDRANT mode, or an
    # in-process keyword retriever over the in-repo corpus in FIXTURE mode. build_retriever returns
    # None only when QDRANT is selected but unconfigured — degrade to the fixture (real corpus,
    # lower-quality ranking) so /chat still grounds on guidelines rather than failing, and log it;
    # /ready separately surfaces the degraded dependency.
    retriever = build_retriever(settings)
    if retriever is None:
        logger.warning("evidence retriever unconfigured for QDRANT mode; using fixture fallback")
        retriever = FixtureEvidenceRetriever.from_corpus(
            settings.rerank_top_n, relevance_floor=settings.retrieval_relevance_floor
        )
    app.state.retriever = retriever
    # The Week-2 supervisor graph is the only /chat behavior: supervisor routes to the
    # intake-extractor and evidence-retriever, and the grounding gate is enforced on each worker and
    # the final answer (context/decisions/agent-framework-week2.md).
    app.state.graph = build_graph(_build_model(settings))
    # The document extractor (JOS-54): live Mistral OCR in MISTRAL mode, a recorded-response replay
    # in FIXTURE mode, or None when unconfigured (the intake-extractor then reports no uploaded
    # document rather than failing). App-lifetime and stateless; the per-conversation fact registry
    # that its output is grounded against lives on each ConversationSession.
    app.state.extractor = build_extractor(settings)
    # Server-side conversation state so multi-turn history (which contains PHI) stays in the
    # service rather than round-tripping through the client. TTL-evicted; single-instance.
    app.state.conversation_store = ConversationStore(ttl_seconds=1800, max_sessions=1000)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness probe — 200 whenever the process is alive (ARCHITECTURE.md §10)."""
        return {"status": "alive"}

    @app.get("/ready")
    async def ready(request: Request) -> JSONResponse:
        """Readiness probe — 200 only when FHIR, the LLM, and Langfuse are all reachable."""
        report = await check_readiness(request.app.state.settings, request.app.state.fhir)
        status_code = 200 if report.ready else 503
        return JSONResponse(status_code=status_code, content=report.model_dump())

    @app.post("/chat")
    async def chat(request: Request, payload: ChatRequest) -> JSONResponse:
        """Answer one agent turn, grounded and traced (ARCHITECTURE.md §6.2).

        The agent run is wrapped in a Langfuse trace context; the instrumentation captures the
        model, tokens, cost, and tool spans automatically, and the verification outcome is
        recorded as a score. Tracing failures never affect the response.
        """
        correlation_id = current_correlation_id()

        enabled = request.app.state.observability_enabled
        settings: Settings = request.app.state.settings
        store: ConversationStore = request.app.state.conversation_store

        resolved = _resolve_request_fhir(request, settings, correlation_id)
        if isinstance(resolved, JSONResponse):
            return resolved
        fhir, per_request_client = resolved

        content: dict[str, Any] = {}
        status_code = 200
        try:
            # Resolve (or open) the conversation. It is bound to one patient; a mismatch is refused
            # so a follow-up cannot surface a different patient's accumulated history (§5).
            if payload.conversation_id is not None:
                session = store.get(payload.conversation_id)
                if session is None:
                    return JSONResponse(
                        status_code=404,
                        content={
                            "error": "conversation not found",
                            "correlation_id": correlation_id,
                        },
                    )
                if session.patient_id != payload.patient_id:
                    logger.info("conversation/patient mismatch", extra={"cid": correlation_id})
                    return JSONResponse(
                        status_code=403,
                        content={
                            "error": "conversation is bound to a different patient",
                            "correlation_id": correlation_id,
                        },
                    )
                conversation_id = payload.conversation_id
            else:
                conversation_id, session = store.create(payload.patient_id)

            deps = GraphDeps(
                fhir=fhir,
                patient_id=payload.patient_id,
                correlation_id=correlation_id,
                retriever=request.app.state.retriever,
                extractor=request.app.state.extractor,
                # The registries accumulate across the conversation so a follow-up can cite a record
                # read, a guideline chunk retrieved, or a lab fact extracted in an earlier turn.
                fetched=session.fetched,
                chunks=session.chunks,
                documents=session.documents,
            )

            with observe_turn(
                enabled,
                correlation_id,
                conversation_id,
                payload.patient_id,
                payload.message,
                request.app.state.system_prompt_ref,
            ) as turn:
                try:
                    result = await run_graph(
                        request.app.state.graph,
                        payload.message,
                        deps,
                        turn,
                        max_hops=settings.agent_max_hops,
                        # Hard ceiling on tool calls per agent run so a runaway loop (e.g. scanning
                        # every note on a 90+-encounter chart) cannot spend up to pydantic-ai's
                        # default. Hitting it degrades to a refusal below, not a 500.
                        usage_limits=UsageLimits(tool_calls_limit=settings.agent_tool_calls_limit),
                    )
                    turn.verified(passed=True)
                    turn.costed(usd=turn_cost_usd(settings.model_tier, result.usage))
                    content = _answer_payload(result.answer, session.chunks, deps)
                    turn.output(content)
                except UnexpectedModelBehavior:
                    # The grounding gate exhausted its retries without an attributable answer —
                    # degrade to a refusal rather than ship an unverified claim (§8). A user got a
                    # non-answer, so this is a WARNING (not INFO), with its own greppable reason.
                    logger.warning(
                        "agent could not ground an answer within retries",
                        extra={
                            "cid": correlation_id,
                            "reason": ChatFailureReason.GROUNDING_EXHAUSTED,
                        },
                    )
                    turn.verified(passed=False)
                    content = _UNAVAILABLE_ANSWER.model_dump()
                except UsageLimitExceeded:
                    # The turn hit the per-turn tool-call ceiling (a runaway loop) — degrade to a
                    # refusal rather than let the exception 500 (which the browser surfaces as
                    # "Failed to fetch"). A resource-limit failure, distinct from a grounding miss:
                    # scored as `tool_ceiling` (not verification_grounding=0) so the A4 grounding
                    # monitor — a trust signal — isn't polluted by runaway turns.
                    logger.warning(
                        "agent hit the tool-call ceiling before answering",
                        extra={
                            "cid": correlation_id,
                            "reason": ChatFailureReason.TOOL_CEILING,
                        },
                    )
                    turn.limited()
                    content = _UNAVAILABLE_ANSWER.model_dump()
                except ModelHTTPError as exc:
                    # LLM provider rejected the call (billing, rate limit, outage). Always logged;
                    # the specific reason is surfaced too, which aids debugging in this demo system.
                    # (A production PHI deployment would genericize this — see ARCHITECTURE.md §8.)
                    logger.warning(
                        "LLM request failed",
                        extra={
                            "cid": correlation_id,
                            "reason": ChatFailureReason.LLM_HTTP_ERROR,
                        },
                        exc_info=True,
                    )
                    turn.errored(tool_failure=False)
                    status_code = 502
                    content = {"error": str(exc), "correlation_id": correlation_id}
                except FhirError:
                    # A data read failed — report the gap, never fabricate around it (§8).
                    logger.warning(
                        "FHIR read failed",
                        extra={
                            "cid": correlation_id,
                            "reason": ChatFailureReason.FHIR_READ_FAILED,
                        },
                        exc_info=True,
                    )
                    turn.errored(tool_failure=True)
                    status_code = 502
                    content = {
                        "error": "patient data is temporarily unavailable",
                        "correlation_id": correlation_id,
                    }
                except Exception:
                    # Catch-all boundary: any unforeseen failure must return a controlled,
                    # CORS-headed response — never an uncaught exception (which reaches the browser
                    # as a bare 500 or "Failed to fetch"). Log the full traceback so the bug stays
                    # visible; surface only a generic message + correlation id, never internal
                    # detail (audit, §8).
                    logger.error(
                        "unexpected error answering /chat",
                        extra={
                            "cid": correlation_id,
                            "reason": ChatFailureReason.UNEXPECTED,
                        },
                        exc_info=True,
                    )
                    turn.errored(tool_failure=False)
                    status_code = 500
                    content = {
                        "error": "the request could not be completed",
                        "correlation_id": correlation_id,
                    }
        finally:
            # Close the per-request token-scoped client so its connection pool never leaks.
            if per_request_client is not None:
                await per_request_client.aclose()

        # Echo the conversation id on every answered turn so the client keeps the thread.
        content["conversation_id"] = conversation_id
        return JSONResponse(status_code=status_code, content=content)

    # --- Read-only Week-2 subsystem endpoints (JOS-63 / JOS-67) --------------------------------
    # Each exposes ONE Week-2 subsystem directly, bypassing the LLM/graph, so a caller (a grader's
    # API collection, a contract test) can exercise document listing, extraction, or evidence
    # retrieval without paying for a full /chat turn. All three are read-only (no writes, no
    # corpus mutation) and gated by the same SMART patient token as /chat. Handlers return a raw
    # JSONResponse (like /chat) but declare `response_model` so the committed OpenAPI is precise.

    @app.get("/documents", response_model=DocumentsResponse)
    async def documents(request: Request, patient_id: str) -> JSONResponse:
        """List the patient's uploaded, extractable documents (metadata only).

        Backs the ``list_documents`` tool: the same patient-scoped FHIR ``DocumentReference``
        search, filtered to uploaded PDFs whose category resolves to an extraction schema.
        """
        correlation_id = current_correlation_id()
        settings: Settings = request.app.state.settings
        resolved = _resolve_request_fhir(request, settings, correlation_id)
        if isinstance(resolved, JSONResponse):
            return resolved
        fhir, per_request_client = resolved
        try:
            summaries = await fhir.get_documents(patient_id)
        except FhirError:
            logger.warning(
                "document list read failed",
                extra={"cid": correlation_id, "reason": ChatFailureReason.FHIR_READ_FAILED},
                exc_info=True,
            )
            return JSONResponse(
                status_code=502,
                content={
                    "error": "patient data is temporarily unavailable",
                    "correlation_id": correlation_id,
                },
            )
        finally:
            if per_request_client is not None:
                await per_request_client.aclose()
        return JSONResponse(
            content=DocumentsResponse(patient_id=patient_id, documents=summaries).model_dump(
                mode="json"
            )
        )

    @app.get("/documents/{document_id}/extraction", response_model=ExtractionResponse)
    async def document_extraction(
        request: Request, document_id: str, patient_id: str
    ) -> JSONResponse:
        """Extract one uploaded document into its strict-schema facts (synchronous; no LLM).

        Backs ``attach_and_extract`` via the shared ``resolve_and_extract`` core: the document's
        ``doc_type`` is resolved server-side from its OpenEMR category (never a caller input), and
        an id that is not one of the patient's uploaded documents is a 404. The response IS the
        extraction result — there is no async job/status to poll.
        """
        correlation_id = current_correlation_id()
        settings = request.app.state.settings
        extractor = request.app.state.extractor
        if extractor is None:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "document extraction is not available",
                    "correlation_id": correlation_id,
                },
            )
        resolved = _resolve_request_fhir(request, settings, correlation_id)
        if isinstance(resolved, JSONResponse):
            return resolved
        fhir, per_request_client = resolved
        extracted = None
        try:
            # get_documents is also the security control: only a document the patient actually has,
            # whose category maps to a schema, is extractable (no raw by-id path that would bypass
            # the discovery filter).
            summaries = await fhir.get_documents(patient_id)
            extracted = await resolve_and_extract(document_id, summaries, extractor, fhir)
        except ExtractionError:
            logger.warning(
                "document extraction failed",
                extra={"cid": correlation_id, "document_id": document_id},
                exc_info=True,
            )
            return JSONResponse(
                status_code=502,
                content={"error": "could not read the document", "correlation_id": correlation_id},
            )
        except FhirError:
            logger.warning(
                "document read failed for extraction",
                extra={"cid": correlation_id, "reason": ChatFailureReason.FHIR_READ_FAILED},
                exc_info=True,
            )
            return JSONResponse(
                status_code=502,
                content={
                    "error": "patient data is temporarily unavailable",
                    "correlation_id": correlation_id,
                },
            )
        finally:
            if per_request_client is not None:
                await per_request_client.aclose()
        if extracted is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "document not found for this patient",
                    "correlation_id": correlation_id,
                },
            )
        return JSONResponse(
            content=ExtractionResponse.from_extracted(extracted).model_dump(mode="json")
        )

    @app.get("/evidence", response_model=EvidenceResponse)
    async def evidence(request: Request, query: str, top_n: int | None = None) -> JSONResponse:
        """Retrieve ranked guideline chunks for a query (the hybrid-RAG path; no LLM, no PHI).

        Backs ``search_guidelines``: runs the retriever (Qdrant + rerank, or the fixture fallback)
        over the non-PHI corpus. Gated by the bearer token for a uniform surface, though it reads no
        patient data. Corpus indexing stays a CLI dev op — this endpoint never mutates the corpus.
        """
        correlation_id = current_correlation_id()
        settings = request.app.state.settings
        denied = _require_token(request, settings, correlation_id)
        if denied is not None:
            return denied
        retriever = request.app.state.retriever
        try:
            snippets = await retriever.retrieve(query, top_n=top_n)
        except RetrievalError:
            logger.warning(
                "evidence retrieval failed", extra={"cid": correlation_id}, exc_info=True
            )
            return JSONResponse(
                status_code=502,
                content={
                    "error": "evidence retrieval is temporarily unavailable",
                    "correlation_id": correlation_id,
                },
            )
        items = [EvidenceItem.from_snippet(snippet) for snippet in snippets]
        resolved_top_n = top_n if top_n is not None else settings.rerank_top_n
        return JSONResponse(
            content=EvidenceResponse(
                query=query, top_n=resolved_top_n, evidence=items
            ).model_dump(mode="json")
        )

    return app


app = create_app()
