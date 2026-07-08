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


def _build_fhir_client(settings: Settings) -> HttpFhirClient | FixtureFhirClient:
    """Construct the FHIR client the mode calls for (dependency injection at the edge).

    Args:
        settings: Service settings selecting the client mode and endpoint.

    Returns:
        A fixture-backed client (dev/tests) or an httpx-backed client (live OpenEMR).

    Raises:
        ValueError: If ``HTTP`` mode is selected without a base URL and bearer token.
    """
    if settings.fhir_client_mode is FhirClientMode.FIXTURE:
        return FixtureFhirClient.from_seed()
    if not settings.fhir_base_url or not settings.fhir_bearer_token:
        raise ValueError("HTTP FHIR mode needs COPILOT_FHIR_BASE_URL and COPILOT_FHIR_BEARER_TOKEN")
    return HttpFhirClient(
        settings.fhir_base_url,
        settings.fhir_bearer_token,
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
        enabled = request.app.state.observability_enabled
        deps = CopilotDeps(
            fhir=request.app.state.fhir,
            patient_id=payload.patient_id,
            correlation_id=correlation_id,
            fetched=FetchLog(),
        )

        status_code = 200
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
                logger.warning("LLM request failed", extra={"cid": correlation_id}, exc_info=True)
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

        return JSONResponse(status_code=status_code, content=content)

    return app


app = create_app()
