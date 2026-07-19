from dataclasses import dataclass

from pydantic_ai.usage import RunUsage

from copilot.config import ModelTier


@dataclass(frozen=True)
class ModelPricing:
    """USD price per million tokens for one model tier's input and output.

    Anthropic bills cached input separately: cache *reads* at 0.1x the input rate and cache
    *writes* at 1.25x. Prompt caching is not enabled on the agent yet, so those token counts are
    0 today; the multipliers in :func:`turn_cost_usd` apply them anyway so the figure stays
    correct if caching is turned on later. Source: ``context/planning/estimated-token-spend.md``.
    """

    input_per_mtok: float
    output_per_mtok: float


# Prices in USD per million tokens. Sonnet 5 is at its introductory rate ($2/$10) through
# 2026-08-31, reverting to $3/$15 after — bump SONNET then. The A5 cost alert carries ~2x
# headroom over the observed p95, so the reversion will not by itself trip it. Haiku/Opus are
# declared for the tiered-routing follow-up even though the walking skeleton runs one tier per
# deploy. Source: context/planning/estimated-token-spend.md.
MODEL_PRICING: dict[ModelTier, ModelPricing] = {
    ModelTier.HAIKU: ModelPricing(input_per_mtok=1.00, output_per_mtok=5.00),
    ModelTier.SONNET: ModelPricing(input_per_mtok=2.00, output_per_mtok=10.00),
    ModelTier.OPUS: ModelPricing(input_per_mtok=5.00, output_per_mtok=25.00),
}

_CACHE_READ_MULTIPLIER = 0.1  # Anthropic bills a cache read at 10% of the input rate.
_CACHE_WRITE_MULTIPLIER = 1.25  # ...and a cache write at 125% of it.
_TOKENS_PER_MTOK = 1_000_000


def usage_delta(before: RunUsage, after: RunUsage) -> RunUsage:
    """The token usage accrued between two snapshots of the same shared accumulator.

    The graph threads ONE ``RunUsage`` through every agent run so the tool-call ceiling is a
    per-turn cap (see ``supervisor.run_graph``), which means no agent's individual usage is ever
    observable directly — ``AgentRunResult.usage`` returns that same shared object. Subtracting two
    snapshots is the only way to attribute usage to one worker, and ``RunUsage`` defines no
    ``__sub__``, so the field-wise difference lives here beside the pricing that consumes it.

    Args:
        before: Snapshot taken immediately before the run.
        after: Snapshot taken immediately after it.

    Returns:
        A ``RunUsage`` holding only what that run added.
    """
    return RunUsage(
        input_tokens=after.input_tokens - before.input_tokens,
        cache_write_tokens=after.cache_write_tokens - before.cache_write_tokens,
        cache_read_tokens=after.cache_read_tokens - before.cache_read_tokens,
        output_tokens=after.output_tokens - before.output_tokens,
        requests=after.requests - before.requests,
        tool_calls=after.tool_calls - before.tool_calls,
    )


def turn_cost_usd(tier: ModelTier, usage: RunUsage) -> float:
    """Compute one turn's model cost in USD from its token usage.

    Cost is priced against the deployed model tier (the agent runs one tier per deploy).
    Anthropic reports uncached input, cache-read, and cache-write tokens as disjoint counts, so
    summing them does not double-count; cache tokens are 0 until prompt caching is enabled.

    Args:
        tier: The model tier the turn ran on (``Settings.model_tier``).
        usage: The Pydantic AI run usage (token counts) for the turn.

    Returns:
        The turn's model cost in USD.
    """
    price = MODEL_PRICING[tier]
    billable_input_tokens = (
        usage.input_tokens
        + _CACHE_READ_MULTIPLIER * usage.cache_read_tokens
        + _CACHE_WRITE_MULTIPLIER * usage.cache_write_tokens
    )
    input_cost = billable_input_tokens * price.input_per_mtok / _TOKENS_PER_MTOK
    output_cost = usage.output_tokens * price.output_per_mtok / _TOKENS_PER_MTOK
    return input_cost + output_cost
