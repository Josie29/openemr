from unittest.mock import MagicMock

from copilot.observability import TurnTrace


def _score_names(span: MagicMock) -> list[str]:
    """The ``name=`` of every ``score_trace`` call recorded on the span."""
    return [call.kwargs["name"] for call in span.score_trace.call_args_list]


def test_tool_ceiling_is_scored_on_its_own_not_as_a_grounding_miss() -> None:
    # A tool-call-ceiling hit is a resource-limit failure, not a grounding failure. If it emitted
    # verification_grounding=0 it would drag down the A4 grounding-refusal average — a *trust*
    # signal — every time a chart is large enough to exhaust the tool budget, reading as a false
    # trust regression. Guards that limited() emits its own tool_ceiling score and never touches
    # verification_grounding.
    span = MagicMock()

    TurnTrace(span).limited()

    names = _score_names(span)
    assert names == ["tool_ceiling"]
    assert "verification_grounding" not in names
    span.update.assert_called_once_with(level="WARNING")


def test_grounding_refusal_still_scores_verification_grounding() -> None:
    # The grounding-gate refusal is the real trust signal A4 watches; it must keep emitting
    # verification_grounding (0 on refusal) and stay distinct from a tool-ceiling hit.
    span = MagicMock()

    TurnTrace(span).verified(passed=False)

    assert _score_names(span) == ["verification_grounding"]
