from enum import StrEnum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelTier(StrEnum):
    """Claude model tiers the agent may route to.

    Values are the Pydantic AI model identifier strings (``anthropic:<model-id>``).
    The walking skeleton uses ``SONNET`` only; ``HAIKU``/``OPUS`` are declared here so the
    tiered-routing follow-up (prompt ``-02``) has a single source of truth to extend.
    """

    SONNET = "anthropic:claude-sonnet-5"
    HAIKU = "anthropic:claude-haiku-4-5"
    OPUS = "anthropic:claude-opus-4-8"


class FhirClientMode(StrEnum):
    """Which ``FhirClient`` implementation the service wires up.

    ``FIXTURE`` replays recorded FHIR JSON (tests + initial local dev, no live token);
    ``HTTP`` calls a live OpenEMR FHIR R4 endpoint under a SMART patient-scoped token.
    """

    FIXTURE = "fixture"
    HTTP = "http"


class Settings(BaseSettings):
    """Service configuration, sourced entirely from environment variables.

    No secret is ever hard-coded (AUDIT.md secrets-hygiene finding); everything sensitive
    arrives via Railway env vars. See ``.env.example`` for the full list.
    """

    model_config = SettingsConfigDict(env_prefix="COPILOT_", env_file=".env", extra="ignore")

    # Agent
    model_tier: ModelTier = ModelTier.SONNET
    anthropic_api_key: str | None = None

    # FHIR data access (SMART patient/*.read; no DB credentials — ARCHITECTURE.md §4)
    fhir_client_mode: FhirClientMode = FhirClientMode.FIXTURE
    fhir_base_url: str | None = Field(
        default=None, description="OpenEMR FHIR R4 base, e.g. https://host/apis/default/fhir"
    )
    fhir_bearer_token: str | None = Field(
        default=None,
        description="SMART patient/*.read token (env-var stand-in until the PHP module mints it)",
    )
    fhir_timeout_seconds: float = 10.0
    fhir_max_retries: int = 2

    # Observability (Langfuse — ARCHITECTURE.md §10)
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    @property
    def langfuse_enabled(self) -> bool:
        """Whether Langfuse credentials are present and tracing should be wired up."""
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so every request reads the same parsed configuration without re-reading the
    environment. Injected at the FastAPI app edge, never reached into from business logic.

    Returns:
        The parsed and validated ``Settings`` instance.
    """
    return Settings()
