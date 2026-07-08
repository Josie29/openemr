import logging
from typing import Protocol

from copilot.config import ModelTier, Settings

logger = logging.getLogger("copilot.observability")

# Approximate USD per input/output token by tier (per-Mtok list price / 1e6).
# Verify at build time — prices move (agent-tech-stack.md). Feeds the §12 cost analysis.
_PRICE_PER_TOKEN: dict[ModelTier, tuple[float, float]] = {
    ModelTier.SONNET: (3.0 / 1_000_000, 15.0 / 1_000_000),
    ModelTier.HAIKU: (1.0 / 1_000_000, 5.0 / 1_000_000),
    ModelTier.OPUS: (5.0 / 1_000_000, 25.0 / 1_000_000),
}


def estimate_cost_usd(tier: ModelTier, input_tokens: int, output_tokens: int) -> float:
    """Estimate the USD cost of one turn from token counts.

    Args:
        tier: The model tier that served the turn.
        input_tokens: Prompt tokens consumed.
        output_tokens: Completion tokens produced.

    Returns:
        The estimated cost in USD.
    """
    in_rate, out_rate = _PRICE_PER_TOKEN[tier]
    return input_tokens * in_rate + output_tokens * out_rate


class TurnTracer(Protocol):
    """Records one agent turn to the observability backend.

    A single turn is one trace; the methods below capture what ARCHITECTURE.md §10 requires —
    step order, tokens + cost, and the verification outcome — all keyed by correlation id.
    """

    def record_usage(self, tier: ModelTier, input_tokens: int, output_tokens: int) -> None:
        """Record token usage and derived cost for the turn."""
        ...

    def record_verification(self, *, passed: bool, retries: int) -> None:
        """Record whether the verification gate passed and how many retries it forced."""
        ...

    def finish(self, *, status: str) -> None:
        """Close the trace with a terminal status (e.g. ``"ok"`` / ``"error"``)."""
        ...


class NullTracer:
    """No-op tracer used when Langfuse is not configured (e.g. local dev, tests)."""

    def record_usage(self, tier: ModelTier, input_tokens: int, output_tokens: int) -> None:
        return None

    def record_verification(self, *, passed: bool, retries: int) -> None:
        return None

    def finish(self, *, status: str) -> None:
        return None


class LangfuseTracer:
    """Langfuse-backed tracer for one turn, guarded so observability never breaks the turn.

    Observability is a support concern: a Langfuse outage must not fail a clinical request.
    Every backend call is therefore wrapped and demoted to a logged warning on failure.
    """

    def __init__(self, client: object, correlation_id: str, patient_id: str, message: str) -> None:
        self._client = client
        self._trace = self._safe(
            lambda: client.trace(  # type: ignore[attr-defined]
                name="chat_turn",
                id=correlation_id,
                metadata={"patient_id": patient_id},
                input={"message": message},
            )
        )

    def record_usage(self, tier: ModelTier, input_tokens: int, output_tokens: int) -> None:
        cost = estimate_cost_usd(tier, input_tokens, output_tokens)
        self._safe(
            lambda: self._trace.update(  # type: ignore[union-attr]
                metadata={
                    "model_tier": tier.value,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "estimated_cost_usd": cost,
                }
            )
        )

    def record_verification(self, *, passed: bool, retries: int) -> None:
        self._safe(
            lambda: self._trace.update(  # type: ignore[union-attr]
                metadata={"verification_passed": passed, "verification_retries": retries}
            )
        )

    def finish(self, *, status: str) -> None:
        self._safe(lambda: self._trace.update(metadata={"status": status}))  # type: ignore[union-attr]
        self._safe(lambda: self._client.flush())  # type: ignore[attr-defined]

    def _safe(self, call: object) -> object:
        """Run a Langfuse call, demoting any failure to a warning.

        Args:
            call: A zero-arg callable performing the backend interaction.

        Returns:
            The call's result, or None if it failed or the trace is unavailable.
        """
        if self._trace is None and getattr(call, "__name__", "") != "<lambda>":
            return None
        try:
            return call()  # type: ignore[operator]
        except Exception:  # noqa: BLE001 - observability must never break the turn
            logger.warning("langfuse call failed; continuing without it", exc_info=True)
            return None


def build_tracer(
    settings: Settings, correlation_id: str, patient_id: str, message: str
) -> TurnTracer:
    """Build a tracer for one turn, real or no-op depending on configuration.

    Args:
        settings: Service settings (Langfuse credentials decide which tracer is returned).
        correlation_id: The turn's correlation id (becomes the trace id).
        patient_id: The patient the turn is scoped to.
        message: The physician's question, recorded as trace input.

    Returns:
        A ``LangfuseTracer`` when Langfuse is configured, else a ``NullTracer``.
    """
    if not settings.langfuse_enabled:
        return NullTracer()
    try:
        from langfuse import Langfuse

        client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    except Exception:  # noqa: BLE001 - fall back to no-op if the SDK cannot initialise
        logger.warning("Langfuse configured but client init failed; tracing off", exc_info=True)
        return NullTracer()
    return LangfuseTracer(client, correlation_id, patient_id, message)
