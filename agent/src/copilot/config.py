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
    # Outbound-call budgets (W2_ARCHITECTURE.md §10). Every value sits ABOVE the largest latency
    # observed in production (context/planning/loadtest-results.md), so a call that succeeds today
    # is never cut short — these bound genuine hangs rather than trimming the tail. All are
    # constrained: a timeout field that silently accepts -5 is worse than no field at all.
    fhir_timeout_seconds: float = Field(default=10.0, gt=0, description="Per-request FHIR timeout.")
    fhir_max_retries: int = Field(
        default=2,
        ge=0,
        description=(
            "httpx transport retries for FHIR. Retries CONNECTION failures only — not read "
            "timeouts, not 5xx."
        ),
    )
    llm_timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        description=(
            "Per-generation Anthropic timeout (max observed 41.3s). Deliberately below "
            "turn_deadline_seconds so it can fire on the chat path rather than being dead code; it "
            "also guards the read endpoints, which have no turn deadline."
        ),
    )
    ocr_timeout_seconds: float = Field(
        default=30.0, gt=0, description="Mistral OCR timeout (max observed 16.5s)."
    )
    rerank_timeout_seconds: float = Field(
        default=10.0, gt=0, description="Cohere rerank timeout (max observed 5.2s)."
    )
    rerank_max_retries: int = Field(
        default=2,
        ge=0,
        description=(
            "Cohere SDK-level retries — unlike httpx transport retries, these honour 429/5xx."
        ),
    )
    # int, not float: AsyncQdrantClient types this parameter as ``int | None``.
    qdrant_timeout_seconds: int = Field(
        default=5, gt=0, description="Qdrant query timeout (private-network call)."
    )
    turn_deadline_seconds: float = Field(
        default=85.0,
        gt=0,
        description=(
            "Wall-clock ceiling on one /chat turn, set just UNDER the sidebar's 90s "
            "CHAT_TIMEOUT_MS (ai-copilot.js) so the server degrades before the browser gives up — "
            "otherwise the physician sees 'may be offline' while the turn runs on and bills."
        ),
    )

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
    # One per document type: the fixture FHIR client serves these bytes for whichever seeded
    # document has that type, so an intake extraction reads the intake form's page, not the lab
    # report's. See the id->path resolution in fhir/fixtures.py for why type alone is not enough.
    document_pdf_path_lab_pdf: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "COPILOT_DOCUMENT_PDF_PATH_LAB_PDF", "COPILOT_DOCUMENT_PDF_PATH"
        ),
        description="Path to the demo lab PDF used as the extractor's byte-source.",
    )
    document_pdf_path_intake_form: str | None = Field(
        default=None,
        validation_alias=AliasChoices("COPILOT_DOCUMENT_PDF_PATH_INTAKE_FORM"),
        description="Path to the demo intake form used as the extractor's byte-source.",
    )
    document_pdf_path_medication_list: str | None = Field(
        default=None,
        validation_alias=AliasChoices("COPILOT_DOCUMENT_PDF_PATH_MEDICATION_LIST"),
        description="Path to the demo medication list used as the extractor's byte-source.",
    )
    # FIXTURE mode only: the recorded Mistral OCR response (`*.ocr.json`) the FixtureOcrBackend
    # replays instead of calling the live API, so extraction tests are deterministic and offline.
    # One per document type, since a recording is of a specific document read through a specific
    # schema — replaying a lab response for an intake form yields no facts.
    #
    # Flat scalars rather than a `dict[DocType, str]` field: pydantic-settings JSON-decodes a
    # complex field INSIDE the env source, before any validator runs (the same trap `cors_origins`
    # documents below), so a dict would force JSON into `.env` and turn a typo into a startup
    # SettingsError. DocType is a closed set, and adding a third type already means code changes.
    # The unsuffixed COPILOT_OCR_FIXTURE_PATH alias keeps existing .env/Railway config working.
    ocr_fixture_path_lab_pdf: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "COPILOT_OCR_FIXTURE_PATH_LAB_PDF", "COPILOT_OCR_FIXTURE_PATH"
        ),
        description="FIXTURE mode: the recorded OCR response replayed for a lab_pdf.",
    )
    ocr_fixture_path_intake_form: str | None = Field(
        default=None,
        validation_alias=AliasChoices("COPILOT_OCR_FIXTURE_PATH_INTAKE_FORM"),
        description="FIXTURE mode: the recorded OCR response replayed for an intake_form.",
    )
    ocr_fixture_path_medication_list: str | None = Field(
        default=None,
        validation_alias=AliasChoices("COPILOT_OCR_FIXTURE_PATH_MEDICATION_LIST"),
        description="FIXTURE mode: the recorded OCR response replayed for a medication_list.",
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

    # Per-TOOL call budgets for one turn, distinct from agent_tool_calls_limit (which is turn-wide
    # and shared, so one looping tool starves every other). Enforced by hiding the tool from the
    # model once spent — see copilot.graph.budget.
    #
    # Both are set to the number of calls that can do NEW work, not that number plus slack. The
    # document list is read once and memoized, so a second call is definitionally a retry. Guideline
    # snippets come back ranked best-first over a fixed corpus: one query establishes whether the
    # topic is covered, a second allows one genuine reformulation, and beyond that the corpus is the
    # limit rather than the phrasing — the observed runaway rephrased nine times and never found
    # what was not there.
    agent_max_searches_per_run: int = 2
    agent_max_document_lists_per_run: int = 1

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

    # Per-principal request rate limiting (AF-VULN-0002). Every /chat turn drives a multi-agent LLM
    # pipeline plus external OCR, so unbounded request volume is a cost-amplification / economic-DoS
    # lever on a clinical system. Enabled by default (fail-closed); a caller is keyed by its SMART
    # bearer token (hashed) or, tokenless, its client IP. Limits are per rolling window and split by
    # route class so a /chat flood cannot exhaust the read budget. In-process + single-instance (see
    # rate_limit.py) — the same assumption ConversationStore already makes.
    rate_limit_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("COPILOT_RATE_LIMIT_ENABLED", "RATE_LIMIT_ENABLED"),
    )
    rate_limit_window_seconds: float = Field(
        default=60.0, gt=0, description="Rolling window the per-route limits are counted over."
    )
    rate_limit_chat_per_window: int = Field(
        default=20, gt=0, description="Max POST /chat turns one principal may make per window."
    )
    rate_limit_read_per_window: int = Field(
        default=60,
        gt=0,
        description="Max read/other requests one principal may make per window (looser than chat).",
    )
    rate_limit_max_principals: int = Field(
        default=10_000,
        gt=0,
        description="Cap on tracked principals; the oldest is evicted past it (bounds memory).",
    )

    # Whether to serve the OpenAPI schema (/openapi.json) and interactive docs (/docs, /redoc).
    # Off by default so prod is closed without any Railway change (AF-VULN-0003): publishing the
    # full route/parameter map to anonymous callers on a PHI system is a recon aid. Local dev opts
    # in via COPILOT_EXPOSE_API_DOCS=true (.env.example); spec generation forces it on (openapi.py).
    expose_api_docs: bool = Field(
        default=False,
        validation_alias=AliasChoices("COPILOT_EXPOSE_API_DOCS", "EXPOSE_API_DOCS"),
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

    # PHI must never reach the observability backend (PRD-week-2, HIPAA-minded development).
    # On by default so a missing env var fails closed; turn it off only to debug locally against
    # synthetic data, never where real records are in play.
    phi_masking_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("COPILOT_PHI_MASKING_ENABLED", "PHI_MASKING_ENABLED"),
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
