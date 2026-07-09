from enum import StrEnum
from functools import lru_cache
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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

    # populate_by_name lets aliased fields still be set by their field name (e.g. tests
    # passing anthropic_api_key=None), not only by the env alias.
    model_config = SettingsConfigDict(
        env_prefix="COPILOT_", env_file=".env", extra="ignore", populate_by_name=True
    )

    # Agent
    model_tier: ModelTier = ModelTier.SONNET
    # Accept the SDK's native ANTHROPIC_API_KEY as well as the COPILOT_-prefixed form.
    anthropic_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "COPILOT_ANTHROPIC_API_KEY"),
    )

    # FHIR data access (SMART patient/*.read; no DB credentials — ARCHITECTURE.md §4).
    # Defaults to HTTP so the deployed service is secure by default (a tokenless /chat is
    # refused, not silently served from fixtures). Local dev opts into fixture via .env.example.
    fhir_client_mode: FhirClientMode = FhirClientMode.HTTP
    fhir_base_url: str | None = Field(
        default=None, description="OpenEMR FHIR R4 base, e.g. https://host/apis/default/fhir"
    )
    fhir_bearer_token: str | None = Field(
        default=None,
        description=(
            "Fallback SMART patient/*.read token, used only when a request carries no "
            "Authorization header. The PHP module mints a per-request token; see /chat."
        ),
    )
    fhir_timeout_seconds: float = 10.0
    fhir_max_retries: int = 2

    # Browser origins allowed to call /chat directly (ARCHITECTURE.md §4: the chat XHR goes from the
    # physician's browser to this service, not proxied through PHP). Empty means no browser may call
    # us — fail closed rather than defaulting to "*", which would let any page spend a stolen token.
    # NoDecode is load-bearing: pydantic-settings JSON-decodes complex fields inside the env source,
    # before any validator runs, so a bare "http://localhost:8301" raises SettingsError at startup.
    # NoDecode hands the raw string to the validator below instead.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Comma-separated browser origins, e.g. http://localhost:8301",
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: object) -> object:
        """Accept a comma-separated string, since env vars cannot carry a JSON list ergonomically.

        Args:
            value: The raw environment value, or an already-parsed list.

        Returns:
            A list of trimmed origins when given a string; the value untouched otherwise.
        """
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    # Observability (Langfuse — ARCHITECTURE.md §10).
    # Accept the SDK's native LANGFUSE_* names (what the Langfuse UI hands you) as well as our
    # COPILOT_-prefixed form, so keys copied straight from Langfuse work without renaming.
    langfuse_public_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LANGFUSE_PUBLIC_KEY", "COPILOT_LANGFUSE_PUBLIC_KEY"),
    )
    langfuse_secret_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LANGFUSE_SECRET_KEY", "COPILOT_LANGFUSE_SECRET_KEY"),
    )
    langfuse_host: str = Field(
        default="https://cloud.langfuse.com",
        validation_alias=AliasChoices(
            "LANGFUSE_HOST", "LANGFUSE_BASE_URL", "COPILOT_LANGFUSE_HOST"
        ),
    )

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
