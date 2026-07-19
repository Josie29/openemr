import logging
import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from typing import Any

from langfuse import get_client, propagate_attributes
from langfuse.api import NotFoundError
from langfuse.model import TextPromptClient
from pydantic_ai import Agent

from copilot import __version__
from copilot.config import Settings

logger = logging.getLogger("copilot.observability")


def configure_observability(settings: Settings) -> bool:
    """Wire Langfuse + Pydantic AI OpenTelemetry instrumentation at startup.

    Follows the official Langfuse integration (docs.langfuse.com → Pydantic AI): the Langfuse
    client is the OTel backend, and ``Agent.instrument_all()`` makes every agent run export its
    generation (model, tokens, cost), tool calls, and span hierarchy automatically.

    ``Settings`` accepts the native ``LANGFUSE_*`` names (or ``COPILOT_``-prefixed); here we
    ensure the SDK sees them as its native env vars, then verify auth before instrumenting.

    Args:
        settings: Service settings carrying the Langfuse credentials and host.

    Returns:
        True if instrumentation was enabled; False if Langfuse is unconfigured or setup failed
        (the service then runs untraced — observability must never block a turn or startup).
    """
    if not settings.langfuse_enabled:
        return False

    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key or "")
    os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key or "")
    os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host)
    # Segregates local dev / eval / prod traces within one Langfuse project (the trace
    # `environment` field). The SDK reads this env var when the client initializes below.
    os.environ.setdefault("LANGFUSE_TRACING_ENVIRONMENT", settings.tracing_environment)

    try:
        client = get_client()
        # auth_check() raises on a 401 rather than returning False, so guard it — invalid
        # keys/host must disable tracing, not crash startup.
        authed = client.auth_check()
    except Exception:  # noqa: BLE001 - never let observability setup break startup
        logger.warning(
            "Langfuse setup failed; running untraced. Check the LANGFUSE_* keys and that the "
            "host matches your project region (EU https://cloud.langfuse.com vs US "
            "https://us.cloud.langfuse.com).",
            exc_info=True,
        )
        return False

    if not authed:
        logger.warning("Langfuse auth check returned false; running untraced. Verify keys/host.")
        return False

    Agent.instrument_all()
    return True


@dataclass(frozen=True)
class PromptRef:
    """Reference to a system-prompt version synced to Langfuse Prompt Management.

    Stamped onto every turn's trace so each answer records which prompt version produced it.
    """

    name: str
    version: int


def _fetch_labeled_prompt(name: str, label: str) -> TextPromptClient | None:
    """Fetch the prompt version currently carrying ``label``, or None if none exists yet.

    Caching and retries are disabled so the startup sync always compares against the live server
    state and fails fast rather than blocking boot. A genuine "no such prompt/label" is returned
    as None (the caller creates the first version); any other fetch error propagates so the caller
    skips the sync this boot rather than creating a redundant version on a transient failure.

    Args:
        name: The Langfuse prompt name.
        label: The label whose current version to fetch (the tracing environment).

    Returns:
        The labeled :class:`TextPromptClient`, or None when the prompt/label does not exist yet.

    Raises:
        Exception: Any non-"not found" error fetching the prompt (network, auth, server).
    """
    try:
        prompt = get_client().get_prompt(
            name, label=label, type="text", cache_ttl_seconds=0, max_retries=0
        )
    except NotFoundError:
        return None
    # get_prompt is typed as returning the chat/text union; type="text" guarantees the text client.
    assert isinstance(prompt, TextPromptClient)
    return prompt


def sync_system_prompt(
    enabled: bool, name: str, prompt_text: str, label: str
) -> PromptRef | None:
    """Mirror the code's system prompt into Langfuse Prompt Management, idempotently.

    The code (``agent.SYSTEM_PROMPT``) stays the source of truth — the service never fetches the
    prompt back at runtime, so a Langfuse outage cannot affect a turn. This only registers the
    current prompt in Langfuse for documentation/observability, and returns a reference that
    :func:`observe_turn` stamps onto each trace so every turn records its prompt version.

    Idempotent: the version currently carrying ``label`` is fetched and compared, and a new
    version is created only when the prompt text actually changed — so restarting with unchanged
    code does not churn versions. Like all observability wiring, any failure is swallowed and the
    service runs without the sync rather than failing to start.

    Args:
        enabled: Whether Langfuse is configured (the return of :func:`configure_observability`).
        name: The Langfuse prompt name (``agent.SYSTEM_PROMPT_NAME``).
        prompt_text: The current system prompt text (``agent.SYSTEM_PROMPT``).
        label: The label to move to the synced version — the tracing environment
            (e.g. ``development``/``production``) so dev and prod track versions independently.

    Returns:
        A :class:`PromptRef` for the synced version, or None when tracing is disabled or the
        sync failed.
    """
    if not enabled:
        return None

    try:
        current = _fetch_labeled_prompt(name, label)
        if current is not None and current.prompt == prompt_text:
            return PromptRef(name=name, version=current.version)

        created = get_client().create_prompt(
            name=name,
            prompt=prompt_text,
            labels=[label],
            type="text",
            commit_message=f"Synced from copilot v{__version__}",
        )
        assert isinstance(created, TextPromptClient)
        logger.info(
            "Synced system prompt to Langfuse",
            extra={"prompt_name": name, "version": created.version, "label": label},
        )
        return PromptRef(name=name, version=created.version)
    except Exception:  # noqa: BLE001 - observability must never break startup
        logger.warning("Langfuse system-prompt sync failed; running without it", exc_info=True)
        return None


@dataclass
class TurnTrace:
    """Handle to one agent turn's Langfuse span; every method no-ops when tracing is disabled.

    Encapsulating the optional span here keeps the route free of ``None`` checks and span
    plumbing — it just calls ``turn.verified(...)`` / ``turn.output(...)``.
    """

    _span: Any | None

    def verified(self, *, passed: bool) -> None:
        """Record the grounding gate's outcome as a ``verification_grounding`` score."""
        self._apply(lambda s: s.score_trace(name="verification_grounding", value=float(passed)))

    def limited(self) -> None:
        """Record that the turn hit the tool-call ceiling before it could answer.

        Emits a distinct ``tool_ceiling`` score instead of ``verification_grounding=0``: a turn
        that spent its whole tool budget never reached the grounding gate, so scoring it as a
        grounding failure would pollute the A4 grounding-refusal rate — a *trust* signal — with
        resource-limit hits, which are a cost/runaway signal instead. The span level is set to
        ``WARNING`` so the degraded turn is visible in the trace view without reading as an
        infrastructure error (which ``errored`` reserves for 5xx failures). See
        ``context/planning/alerting.md``.
        """
        self._apply(lambda s: s.score_trace(name="tool_ceiling", value=1.0))
        self._apply(lambda s: s.update(level="WARNING"))

    def errored(self, *, tool_failure: bool) -> None:
        """Flag an infrastructure failure on this turn so the alert monitors can count it.

        Emits numeric scores the same way :meth:`verified` does (the proven monitorable signal),
        because the route catches the failure *inside* the span — so the span itself closes
        cleanly and would otherwise look successful. ``turn_error`` counts every failed turn (feeds
        the error-rate alert); ``tool_error`` additionally marks the subset caused by a FHIR read
        (feeds the tool-failure alert). The span level is set to ``ERROR`` so failures are visible
        in the Langfuse trace view and dashboards, not only in the monitors. See
        ``context/planning/alerting.md`` (A2, A3).

        Args:
            tool_failure: True when the failure was a FHIR tool read (vs an LLM-provider error),
                so the tool-failure alert can be distinguished from a general turn error.
        """
        self._apply(lambda s: s.score_trace(name="turn_error", value=1.0))
        if tool_failure:
            self._apply(lambda s: s.score_trace(name="tool_error", value=1.0))
        self._apply(lambda s: s.update(level="ERROR"))

    def costed(self, *, usd: float) -> None:
        """Record the turn's model cost (USD) as a ``turn_cost`` numeric score.

        Cost is attached by the auto-instrumentation to the per-generation child spans, not the
        ``chat-turn`` root, so a Langfuse Monitor (which reads observation-level cost) cannot
        threshold per-turn cost there. This explicit score gives the cost-spike alert a per-turn
        value to watch — the same monitorable mechanism :meth:`verified`/:meth:`errored` use. See
        ``context/planning/alerting.md`` (A5).

        Args:
            usd: The turn's model cost in US dollars (see ``pricing.turn_cost_usd``).
        """
        self._apply(lambda s: s.score_trace(name="turn_cost", value=usd))

    def output(self, data: object) -> None:
        """Record the turn's response as the trace output."""
        self._apply(lambda s: s.update(output=data))

    def _child_span(self, name: str) -> AbstractContextManager[Any]:
        """Open a child span of the turn, degrading to a no-op context on any tracing failure.

        Returning a context manager (rather than emitting a closed span) is what lets callers keep
        the span *open* across the work it describes, so real child spans nest underneath it. A
        failure to open degrades to :func:`nullcontext`, so the caller's ``with`` body still runs —
        tracing never blocks the turn. An exception raised inside the body propagates through the
        live span, which marks it errored rather than swallowing the failure.

        Args:
            name: The span name.

        Returns:
            The open span's context manager, or a no-op context yielding None.
        """
        if self._span is None:
            return nullcontext(None)
        try:
            return get_client().start_as_current_observation(name=name, as_type="span")
        except Exception:  # noqa: BLE001 - a tracing failure must not affect the turn
            logger.warning("Langfuse child span failed", exc_info=True)
            return nullcontext(None)

    @contextmanager
    def supervising(self) -> Iterator[None]:
        """Open the ``supervisor`` span that every routing hand-off and worker run nests under.

        The Week-2 graph routes procedurally, so without an explicit span the router, its hand-offs
        and the workers would all sit flat under ``chat-turn`` — indistinguishable from Week 1's
        single-agent shape. Wrapping the routing loop gives the trace a supervisor subtree whose
        duration is the orchestration cost, and makes each worker a descendant of the supervisor
        that dispatched it (PRD Week 2: "each worker invocation must be a child span of the
        supervisor span").
        """
        with self._child_span("supervisor"):
            yield

    @contextmanager
    def routing(self, route: str, reason: str) -> Iterator[None]:
        """Open one hand-off span, held open for the duration of the work it dispatches.

        Nesting the dispatched worker inside this span is what makes the hand-off chain
        reconstructable from the trace alone (JOS-56): the span's children *are* the worker run and
        its tool calls, and its wall-clock duration is what that hand-off actually cost — where a
        closed marker span would report zero.

        Args:
            route: The chosen route (e.g. ``"extract_intake"``).
            reason: The supervisor's one-line justification for the hand-off.
        """
        with self._child_span(f"route:{route}") as span:
            if span is not None:
                try:
                    span.update(
                        input={"route": route, "reason": reason}, metadata={"route": route}
                    )
                except Exception:  # noqa: BLE001 - a tracing failure must not affect routing
                    logger.warning("Langfuse route span update failed", exc_info=True)
            yield

    def _apply(self, op: Callable[[Any], None]) -> None:
        """Run a span operation, demoting any failure to a warning (tracing never breaks a turn)."""
        if self._span is None:
            return
        try:
            op(self._span)
        except Exception:  # noqa: BLE001 - a tracing failure must not affect the response
            logger.warning("Langfuse span update failed", exc_info=True)


@contextmanager
def observe_turn(
    enabled: bool,
    correlation_id: str,
    conversation_id: str,
    patient_id: str,
    message: str,
    prompt_ref: PromptRef | None = None,
) -> Iterator[TurnTrace]:
    """Open an active ``chat-turn`` span for one agent turn and yield a :class:`TurnTrace`.

    Opening our own current span (``start_as_current_observation``) gives the verification score
    a live span to attach to (the auto-instrumentation's spans close when the run returns) and
    a clean root the agent's generation/tool spans nest under. ``propagate_attributes`` supplies
    the trace-level correlating attributes. When disabled, yields a no-op handle.

    Args:
        enabled: Whether observability is configured.
        correlation_id: The turn's correlation id (per-turn; recorded as trace metadata).
        conversation_id: The conversation id, used as the Langfuse session id so all turns of one
            conversation group under a single session timeline.
        patient_id: The patient the turn is scoped to.
        message: The physician's question, recorded as the trace input.
        prompt_ref: The system-prompt version synced to Langfuse (from :func:`sync_system_prompt`),
            stamped onto the trace metadata so each turn records which prompt produced it. None
            when the prompt was not synced (Langfuse unconfigured or the sync failed).

    Yields:
        A :class:`TurnTrace` wrapping the active span, or a no-op handle when disabled.
    """
    if not enabled:
        yield TurnTrace(None)
        return

    metadata = {
        "correlation_id": correlation_id,
        "conversation_id": conversation_id,
        "patient_id": patient_id,
    }
    if prompt_ref is not None:
        metadata["system_prompt_name"] = prompt_ref.name
        metadata["system_prompt_version"] = str(prompt_ref.version)

    client = get_client()
    with (
        propagate_attributes(
            trace_name="chat-turn",
            session_id=conversation_id,
            metadata=metadata,
            tags=["clinical-copilot", "walking-skeleton"],
            version=__version__,
        ),
        client.start_as_current_observation(name="chat-turn", as_type="span") as span,
    ):
        span.update(input={"message": message})
        yield TurnTrace(span)


def shutdown_observability(enabled: bool) -> None:
    """Flush and shut down the Langfuse client on graceful shutdown.

    The SDK auto-flushes in the background during runtime, so this is the only flush the server
    needs — per-request flushing would block each response on a network round-trip.

    Args:
        enabled: Whether observability is configured.
    """
    if not enabled:
        return
    try:
        get_client().shutdown()
    except Exception:  # noqa: BLE001 - shutdown failure must not break app teardown
        logger.warning("Langfuse shutdown failed", exc_info=True)
