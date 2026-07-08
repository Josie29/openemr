import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
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
        return FixtureFhirClient.from_seed()
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

    app.state.settings = settings
    # Enable Langfuse + Pydantic AI OTel instrumentation before any agent runs.
    app.state.observability_enabled = configure_observability(settings)
    app.state.fhir = _build_readiness_client(settings)
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
        enabled = request.app.state.observability_enabled
        settings: Settings = request.app.state.settings

        # Build the FHIR client for this turn. In HTTP mode it is scoped to the inbound
        # patient token, so the agent can physically read only the one open patient (ARCHITECTURE.md
        # §5). In fixture mode the shared app client serves reads (no token exists).
        per_request_client: HttpFhirClient | None = None
        if settings.fhir_client_mode is FhirClientMode.FIXTURE:
            fhir = request.app.state.fhir
        else:
            token = _bearer_token(request) or settings.fhir_bearer_token
            if not token:
                logger.info("rejected /chat with no patient token", extra={"cid": correlation_id})
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": "missing patient-scoped FHIR token",
                        "correlation_id": correlation_id,
                    },
                )
            if not settings.fhir_base_url:
                logger.error("HTTP mode without a FHIR base URL", extra={"cid": correlation_id})
                return JSONResponse(
                    status_code=500,
                    content={"error": "agent misconfigured", "correlation_id": correlation_id},
                )
            per_request_client = HttpFhirClient(
                settings.fhir_base_url,
                token,
                timeout_seconds=settings.fhir_timeout_seconds,
                max_retries=settings.fhir_max_retries,
            )
            fhir = per_request_client

        deps = CopilotDeps(
            fhir=fhir,
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
                    logger.warning("LLM request failed", extra={"cid": correlation_id}, exc_info=True)  # noqa: E501
                    status_code = 502
                    content = {"error": str(exc), "correlation_id": correlation_id}
                except FhirError:
                    # A data read failed — report the gap, never fabricate around it (§8).
                    logger.warning("FHIR read failed", extra={"cid": correlation_id}, exc_info=True)
                    status_code = 502
                    content = {
                        "error": "patient data is temporarily unavailable",
                        "correlation_id": correlation_id,
                    }
        finally:
            # Close the per-request token-scoped client so its connection pool never leaks.
            if per_request_client is not None:
                await per_request_client.aclose()

        return JSONResponse(status_code=status_code, content=content)

    return app


app = create_app()
