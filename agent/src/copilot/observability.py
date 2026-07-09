import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from langfuse import get_client, propagate_attributes
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

    def output(self, data: object) -> None:
        """Record the turn's response as the trace output."""
        self._apply(lambda s: s.update(output=data))

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
    enabled: bool, correlation_id: str, conversation_id: str, patient_id: str, message: str
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

    Yields:
        A :class:`TurnTrace` wrapping the active span, or a no-op handle when disabled.
    """
    if not enabled:
        yield TurnTrace(None)
        return

    client = get_client()
    with (
        propagate_attributes(
            trace_name="chat-turn",
            session_id=conversation_id,
            metadata={
                "correlation_id": correlation_id,
                "conversation_id": conversation_id,
                "patient_id": patient_id,
            },
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
