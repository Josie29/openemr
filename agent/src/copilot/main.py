import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from copilot.agent import CopilotDeps, build_agent
from copilot.config import FhirClientMode, Settings, get_settings
from copilot.correlation import CorrelationIdMiddleware, current_correlation_id
from copilot.fhir.client import FhirError, HttpFhirClient
from copilot.fhir.fixtures import FixtureFhirClient
from copilot.health import check_readiness
from copilot.observability import configure_observability, observe_turn, shutdown_observability
from copilot.schemas import ChatRequest, ChatResponse
from copilot.verification import FetchLog

logger = logging.getLogger("copilot")

_UNAVAILABLE_ANSWER = ChatResponse(
    summary="I could not produce an answer I can fully attribute to this patient's record.",
    claims=[],
)


def _build_fhir_client(settings: Settings) -> HttpFhirClient | FixtureFhirClient:
    """Construct the process-lifetime FHIR client (dependency injection at the edge).

    In ``HTTP`` mode this client exists for the ``/ready`` probe and as the fallback for requests
    that arrive without an ``Authorization`` header. Per-request, patient-scoped reads use a client
    built from that header instead — see :func:`_request_scoped_fhir_client`.

    Args:
        settings: Service settings selecting the client mode and endpoint.

    Returns:
        A fixture-backed client (dev/tests) or an httpx-backed client (live OpenEMR).

    Raises:
        ValueError: If ``HTTP`` mode is selected without a base URL.
    """
    if settings.fhir_client_mode is FhirClientMode.FIXTURE:
        return FixtureFhirClient.from_seed()
    if not settings.fhir_base_url:
        raise ValueError("HTTP FHIR mode needs COPILOT_FHIR_BASE_URL")
    # The bearer token is now optional: the readiness probe hits the unauthenticated /metadata
    # endpoint, and data reads prefer the caller's own token.
    return HttpFhirClient(
        settings.fhir_base_url,
        settings.fhir_bearer_token or "",
        timeout_seconds=settings.fhir_timeout_seconds,
        max_retries=settings.fhir_max_retries,
    )


def _bearer_token(request: Request) -> str | None:
    """Extract the bearer token from the request's ``Authorization`` header.

    Args:
        request: The inbound request.

    Returns:
        The token, or ``None`` when the header is absent or is not a non-empty ``Bearer``
        credential.
    """
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return credential.strip() or None


def _request_scoped_fhir_client(request: Request, settings: Settings) -> HttpFhirClient | None:
    """Build a FHIR client bound to *this caller's* SMART token, if they supplied one.

    This is what makes ARCHITECTURE.md §5's claim true in production rather than only on paper. A
    ``patient/*.read`` token is bound to exactly one patient, so reads through this client
    physically cannot reach another — whereas the process-lifetime client's static token is the
    same for every caller and enforces no per-patient scoping at all.

    Args:
        request: The inbound request, whose ``Authorization`` header carries the SMART token.
        settings: Service settings supplying the FHIR base URL and transport budget.

    Returns:
        A client scoped to the caller's token, or ``None`` when the caller sent no usable token or
        the service is running against fixtures (where tokens are meaningless).
    """
    if settings.fhir_client_mode is not FhirClientMode.HTTP or not settings.fhir_base_url:
        return None
    token = _bearer_token(request)
    if token is None:
        return None
    return HttpFhirClient(
        settings.fhir_base_url,
        token,
        timeout_seconds=settings.fhir_timeout_seconds,
        max_retries=settings.fhir_max_retries,
    )


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


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and wire the Clinical Co-Pilot agent service.

    Args:
        settings: Optional settings override (tests inject their own); production reads env.

    Returns:
        The configured FastAPI application.
    """
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        shutdown_observability(app.state.observability_enabled)
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
        allow_methods=["POST"],
        allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
    )

    app.state.settings = settings
    # Enable Langfuse + Pydantic AI OTel instrumentation before any agent runs.
    app.state.observability_enabled = configure_observability(settings)
    app.state.fhir = _build_fhir_client(settings)
    app.state.agent = build_agent(_build_model(settings))

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
        settings = request.app.state.settings
        enabled = request.app.state.observability_enabled

        # Prefer the caller's SMART token over the process-wide static one, so every FHIR read this
        # turn makes is scoped to the one patient that token permits.
        scoped_fhir = _request_scoped_fhir_client(request, settings)
        deps = CopilotDeps(
            fhir=scoped_fhir or request.app.state.fhir,
            patient_id=payload.patient_id,
            correlation_id=correlation_id,
            fetched=FetchLog(),
        )

        status_code = 200
        try:
            with observe_turn(enabled, correlation_id, payload.patient_id, payload.message) as turn:
                try:
                    result = await request.app.state.agent.run(payload.message, deps=deps)
                    turn.verified(passed=True)
                    content = result.output.model_dump()
                    turn.output(content)
                except UnexpectedModelBehavior:
                    # The gate exhausted its retries without an attributable answer — degrade to a
                    # refusal rather than ship an unverified claim (ARCHITECTURE.md §7).
                    logger.info("verification gate refused", extra={"cid": correlation_id})
                    turn.verified(passed=False)
                    content = _UNAVAILABLE_ANSWER.model_dump()
                except ModelHTTPError as exc:
                    # LLM provider rejected the call (billing, rate limit, outage). Always logged;
                    # the specific reason is surfaced too, which aids debugging in this demo system.
                    # (A production PHI deployment would genericize this — see ARCHITECTURE.md §8.)
                    logger.warning(
                        "LLM request failed", extra={"cid": correlation_id}, exc_info=True
                    )
                    status_code = 502
                    content = {"error": str(exc), "correlation_id": correlation_id}
                except FhirError:
                    # A data read failed — report the gap, never fabricate around it (§8). This also
                    # covers a denied cross-patient read, which OpenEMR surfaces as a bare HTTP 500
                    # rather than a 403 (smart-token-spike-findings.md §1): any non-2xx is a denial.
                    logger.warning("FHIR read failed", extra={"cid": correlation_id}, exc_info=True)
                    status_code = 502
                    content = {
                        "error": "patient data is temporarily unavailable",
                        "correlation_id": correlation_id,
                    }
        finally:
            # The per-request client owns its own connection pool; leaking one per turn would
            # exhaust sockets under load.
            if scoped_fhir is not None:
                await scoped_fhir.aclose()

        return JSONResponse(status_code=status_code, content=content)

    return app


app = create_app()
