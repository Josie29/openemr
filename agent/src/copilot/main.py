import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from copilot.agent import CopilotDeps, build_agent
from copilot.config import FhirClientMode, Settings, get_settings
from copilot.correlation import CorrelationIdMiddleware, current_correlation_id
from copilot.fhir.client import FhirError, HttpFhirClient
from copilot.fhir.fixtures import FixtureFhirClient
from copilot.health import check_readiness
from copilot.observability import build_tracer
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
        client = app.state.fhir
        if isinstance(client, HttpFhirClient):
            await client.aclose()

    app = FastAPI(title="AgentForge Clinical Co-Pilot", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CorrelationIdMiddleware)

    app.state.settings = settings
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
        """Answer one agent turn, grounded and traced (ARCHITECTURE.md §6.2)."""
        correlation_id = current_correlation_id()
        settings = request.app.state.settings
        tracer = build_tracer(settings, correlation_id, payload.patient_id, payload.message)
        deps = CopilotDeps(
            fhir=request.app.state.fhir,
            patient_id=payload.patient_id,
            correlation_id=correlation_id,
            fetched=FetchLog(),
        )
        try:
            result = await request.app.state.agent.run(payload.message, deps=deps)
        except UnexpectedModelBehavior:
            # The gate exhausted its retries without an attributable answer — degrade to a
            # refusal rather than ship an unverified claim (ARCHITECTURE.md §7).
            logger.info("verification gate refused", extra={"correlation_id": correlation_id})
            tracer.record_verification(passed=False, retries=-1)
            tracer.finish(status="refused")
            return JSONResponse(status_code=200, content=_UNAVAILABLE_ANSWER.model_dump())
        except FhirError:
            # A data read failed — report the gap, never fabricate around it (§8).
            logger.warning("FHIR read failed", extra={"cid": correlation_id}, exc_info=True)
            tracer.finish(status="error")
            return JSONResponse(
                status_code=502,
                content={
                    "error": "patient data is temporarily unavailable",
                    "correlation_id": correlation_id,
                },
            )

        usage = result.usage
        tracer.record_usage(
            request.app.state.settings.model_tier,
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
        )
        tracer.record_verification(passed=True, retries=0)
        tracer.finish(status="ok")
        return JSONResponse(status_code=200, content=result.output.model_dump())

    return app


app = create_app()
