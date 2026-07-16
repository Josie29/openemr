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


class ExtractorMode(StrEnum):
    """Which document-extraction backend the service wires up (JOS-54, W2_ARCH §3.1).

    ``MISTRAL`` runs live Mistral OCR (``mistral-ocr-latest``) in schema mode against the document
    bytes. ``FIXTURE`` replays a recorded OCR response (``*.ocr.json``) with no live API call — for
    tests and offline dev, mirroring ``FhirClientMode.FIXTURE`` / ``RetrievalMode.FIXTURE``.
    """

    MISTRAL = "mistral"
    FIXTURE = "fixture"


class RetrievalMode(StrEnum):
    """Which ``EvidenceRetriever`` implementation the service wires up (JOS-53, W2_ARCH §5).

    ``QDRANT`` runs the live hybrid pipeline (FastEmbed dense+sparse -> Qdrant RRF -> Cohere
    rerank) against a reachable Qdrant + Cohere. ``FIXTURE`` runs an in-process keyword
    retriever over the in-repo corpus — no network, no Docker — for tests and offline dev,
    mirroring ``FhirClientMode.FIXTURE``.
    """

    QDRANT = "qdrant"
    FIXTURE = "fixture"


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

    # Hybrid RAG evidence retrieval (JOS-53 — W2_ARCHITECTURE.md §5). Qdrant holds ONLY the
    # non-PHI guideline corpus and Cohere reranks guideline text against the clinical question; no
    # patient identifiers or specific values are sent — the query carries only the de-identified
    # clinical topic (patient facts come from the FHIR tools, not the query). See
    # context/specs/hybrid-rag-pipeline.md. Defaults to QDRANT so the deployed service uses the
    # real pipeline; local dev/tests opt into FIXTURE via .env / the settings override.
    retrieval_mode: RetrievalMode = RetrievalMode.QDRANT
    # Accept the bare QDRANT_URL / QDRANT_API_KEY (what the Railway deploy sets, incl. via a
    # ${{Qdrant.RAILWAY_PRIVATE_DOMAIN}} reference) as well as the COPILOT_-prefixed form.
    qdrant_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("QDRANT_URL", "COPILOT_QDRANT_URL"),
        description="Qdrant REST base URL, e.g. http://qdrant.railway.internal:6333 (plain http)",
    )
    qdrant_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("QDRANT_API_KEY", "COPILOT_QDRANT_API_KEY"),
        description="Qdrant API key (QDRANT__SERVICE__API_KEY on the Qdrant service)",
    )
    qdrant_collection: str = "guidelines"
    # Accept the Cohere SDK's native CO_API_KEY as well as COHERE_API_KEY and our prefixed form,
    # so a key copied from Cohere or set by convention works without renaming (matches the
    # anthropic/langfuse key handling above).
    cohere_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "CO_API_KEY", "COHERE_API_KEY", "COPILOT_COHERE_API_KEY"
        ),
    )
    # FastEmbed model ids (embedded in qdrant-client; no separate embedding service). Dense =
    # semantic, sparse = lexical; fused by Qdrant RRF. Must match between indexer and retriever.
    dense_embedding_model: str = "BAAI/bge-small-en-v1.5"  # 384-dim
    sparse_embedding_model: str = "Qdrant/bm25"
    rerank_model: str = "rerank-v4.0-fast"  # Cohere; v3.5 is deprecated (pin a v4.0 model)
    # Per-leg prefetch depth (dense and sparse each fetch this many before RRF); every fused
    # candidate is reranked, gated by the relevance floor, then capped to rerank_top_n grounded
    # snippets fed to the answer model. Defaults per the decision doc; tune empirically once the
    # 50-case eval set exists.
    retrieval_prefetch_k: int = 20
    rerank_top_n: int = 3
    # Minimum Cohere rerank score a snippet must clear to be shown AND to reach the answer model
    # (upstream gate — a weak match never grounds an answer). Nothing clears it -> no evidence, and
    # the answer is composed without guideline backing rather than on a poor match. NOT calibrated
    # across queries (0.5 on one query != another); a pragmatic floor to tune against the eval set.
    # See context/specs/evidence-gating-and-presentation.md §3.1.
    retrieval_relevance_floor: float = 0.5

    # Document extraction (JOS-54 — W2_ARCHITECTURE.md §3.1). The intake-extractor's
    # attach_and_extract tool OCRs an uploaded lab PDF into cited lab facts. Defaults to MISTRAL so
    # the deployed service runs real OCR; tests/offline dev opt into FIXTURE (replays a recorded
    # response) via the settings override. Accept the Mistral SDK's native MISTRAL_API_KEY as well
    # as the COPILOT_-prefixed form, matching the anthropic/cohere key handling above.
    extractor_mode: ExtractorMode = ExtractorMode.MISTRAL
    mistral_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MISTRAL_API_KEY", "COPILOT_MISTRAL_API_KEY"),
    )
    # The document byte-source for the demo slice: the committed lab PDF whose bytes are fed to the
    # OCR backend when a lab document is extracted. Production fetches the bytes from OpenEMR by
    # document id (deferred — a scope wall, see the seam spec); until then the real document id is
    # discovered live but the bytes come from this fixture. Null disables extraction.
    document_pdf_path: str | None = Field(
        default=None,
        description="Path to the demo lab PDF used as the extractor's byte-source.",
    )
    # FIXTURE mode only: the recorded Mistral OCR response (`*.ocr.json`) the FixtureOcrBackend
    # replays instead of calling the live API, so extraction tests are deterministic and offline.
    ocr_fixture_path: str | None = Field(
        default=None,
        description="Path to a recorded OCR response replayed in FIXTURE extractor mode.",
    )

    # Hard ceiling on tool calls in a single agent turn. Bounds cost/latency: without it the agent
    # can loop a tool (e.g. brute-forcing get_encounter_note across a patient with 90+ encounters)
    # up to pydantic-ai's default request_limit of 50, spending 50 model calls on one question.
    # A legitimate turn reads a handful of resources; hitting this cap means the turn ran away, and
    # /chat degrades it to a refusal rather than letting the cost run.
    agent_tool_calls_limit: int = 12

    # Hard ceiling on supervisor routing hops in one turn (Week-2 multi-agent graph). Each hop is a
    # route decision + at most one worker dispatch; a normal turn resolves in 2-3 (extract,
    # retrieve, answer). Bounding it means a router that never says "answer" still terminates and
    # answer rather than looping worker calls. See copilot.graph.supervisor.run_graph.
    agent_max_hops: int = 4

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
    # Stamped on every Langfuse trace (its `environment` field) so local dev, evals, and Railway
    # prod stay filterable within one project. Defaults to development; Railway sets production.
    # Langfuse requires lowercase [a-z0-9-_] and forbids a leading 'langfuse'.
    tracing_environment: str = Field(
        default="development",
        validation_alias=AliasChoices(
            "LANGFUSE_TRACING_ENVIRONMENT", "COPILOT_TRACING_ENVIRONMENT"
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
