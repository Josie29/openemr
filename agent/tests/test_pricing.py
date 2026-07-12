import pytest
from pydantic_ai.usage import RunUsage

from copilot.config import ModelTier
from copilot.pricing import turn_cost_usd


class TestTurnCostUsd:
    """The A5 cost-spike alert thresholds on the ``turn_cost`` score these figures produce, so a
    wrong price or a dropped token class would make the alert fire (or stay silent) at the wrong
    dollar amount. See ``context/planning/alerting.md`` (A5)."""

    def test_prices_input_and_output_at_the_tier_rate(self) -> None:
        # 1000 in @ $2/Mtok + 500 out @ $10/Mtok = $0.007 on Sonnet's introductory rate.
        usage = RunUsage(input_tokens=1000, output_tokens=500)
        assert turn_cost_usd(ModelTier.SONNET, usage) == pytest.approx(0.007)

    def test_cheaper_tier_costs_less_for_the_same_usage(self) -> None:
        # Guards the alert against pricing every tier as Sonnet — a mis-routed cheap turn must not
        # read as a spike, and an Opus turn must read as more expensive than Haiku.
        usage = RunUsage(input_tokens=1000, output_tokens=500)
        haiku = turn_cost_usd(ModelTier.HAIKU, usage)
        opus = turn_cost_usd(ModelTier.OPUS, usage)
        assert haiku == pytest.approx(0.0035)
        assert haiku < turn_cost_usd(ModelTier.SONNET, usage) < opus

    def test_cache_tokens_bill_at_anthropic_discounted_rates(self) -> None:
        # Cache reads bill at 0.1x input and writes at 1.25x; billable input becomes
        # 1000 + 0.1*2000 + 1.25*400 = 1700 tokens. Breaks if caching is enabled and cache token
        # classes are ignored (undercount) or billed at the full input rate (overcount).
        usage = RunUsage(
            input_tokens=1000, output_tokens=500, cache_read_tokens=2000, cache_write_tokens=400
        )
        # 1700 in @ $2/Mtok + 500 out @ $10/Mtok = $0.0084.
        assert turn_cost_usd(ModelTier.SONNET, usage) == pytest.approx(0.0084)

    def test_zero_usage_costs_nothing(self) -> None:
        # A turn that never reached the model (e.g. a pre-model failure) must score $0, not error.
        assert turn_cost_usd(ModelTier.SONNET, RunUsage()) == 0.0
