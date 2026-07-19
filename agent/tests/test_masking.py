from typing import Any
from unittest.mock import MagicMock

from langfuse.types import (
    MaskOtelSpansParams,
    MaskOtelSpansResult,
    OtelSpanData,
    OtelSpanIdentifier,
)

from copilot.masking import (
    PHI_SPAN_ATTRIBUTES,
    REDACTED,
    mask_otel_spans,
    mask_payload,
)

_ID = OtelSpanIdentifier(trace_id="a" * 32, span_id="b" * 16)


def _params(attributes: dict[str, Any], span: Any = None) -> MaskOtelSpansParams:
    """One-span batch, using the SDK's own dataclasses so the test binds to the real contract."""
    return MaskOtelSpansParams(
        spans={
            _ID: span
            or OtelSpanData(
                trace_id=_ID.trace_id,
                span_id=_ID.span_id,
                parent_span_id=None,
                name="chat claude-sonnet-5",
                instrumentation_scope_name="pydantic-ai",
                instrumentation_scope_version=None,
                attributes=attributes,
                resource_attributes={},
            )
        }
    )


def _patched(result: MaskOtelSpansResult | None) -> tuple[str, ...]:
    assert result is not None
    patch = result.span_patches[_ID]
    assert patch is not None
    return tuple(patch.delete_attributes or ())


def test_tool_results_never_leave_the_process() -> None:
    # gen_ai.tool.call.result holds raw FHIR records — names, DOBs, clinical values. It is the
    # single heaviest PHI carrier in a trace. If this stops being deleted, every chart read the
    # agent performs ships verbatim to a SaaS backend.
    result = mask_otel_spans(
        params=_params(
            {
                "gen_ai.tool.call.result": '{"name": "Sergio Angulo", "birthDate": "1958-03-02"}',
                "gen_ai.request.model": "claude-sonnet-5",
            }
        )
    )

    deleted = _patched(result)
    assert "gen_ai.tool.call.result" in deleted
    # Non-PHI attributes must survive, or the cost/latency tiles and A1/A5 lose their inputs.
    assert "gen_ai.request.model" not in deleted


def test_prompts_and_completions_are_stripped() -> None:
    # The prompt carries the chart context the model was given; the completion carries answer
    # prose about the patient. Both are PHI even when no identifier appears verbatim.
    deleted = _patched(
        mask_otel_spans(
            params=_params(
                {
                    "gen_ai.input.messages": "[patient context...]",
                    "gen_ai.output.messages": "Her A1c rose to 8.1.",
                }
            )
        )
    )

    assert set(deleted) == {"gen_ai.input.messages", "gen_ai.output.messages"}


def test_clean_span_batch_is_left_untouched() -> None:
    # Patching every span would rewrite the whole batch on each export. Spans with no PHI
    # attributes (route hand-offs, score spans) must pass through unpatched.
    assert mask_otel_spans(params=_params({"gen_ai.request.model": "claude-sonnet-5"})) is None


def test_masking_fails_closed_when_the_hook_raises() -> None:
    # A masking bug must never degrade to exporting raw PHI. If the hook throws mid-batch, every
    # span is patched rather than passed through untouched.
    broken = MagicMock()
    type(broken).attributes = property(lambda _: (_ for _ in ()).throw(RuntimeError("boom")))

    deleted = _patched(mask_otel_spans(params=_params({}, span=broken)))

    assert set(deleted) == set(PHI_SPAN_ATTRIBUTES)


def test_route_survives_masking_but_its_reason_does_not() -> None:
    # The routing decision drives the routing-decisions dashboard tile, so `route` must stay
    # readable. `reason` is model-generated prose that can quote the chart, so it is redacted —
    # the tile keeps working without the free text riding along.
    masked = mask_payload(data={"route": "extract_intake", "reason": "patient's A1c is 8.1"})

    assert masked == {"route": "extract_intake", "reason": REDACTED}


def test_unknown_keys_are_redacted_by_default() -> None:
    # Default-deny: a field added upstream is redacted until someone explicitly allow-lists it,
    # rather than leaking from the day it is introduced.
    masked = mask_payload(data={"summary": "chest pain since Tuesday", "latency_ms": 1200})

    assert masked == {"summary": REDACTED, "latency_ms": 1200}


def test_phi_attribute_names_still_match_pydantic_ai() -> None:
    # The mask is a denylist of exact attribute keys. If Pydantic AI renames one, masking keeps
    # reporting success while silently exporting that field. This pins the contract: on failure,
    # re-read the emitted keys and update PHI_SPAN_ATTRIBUTES.
    from pydantic_ai.models import instrumented

    assert instrumented.__file__ is not None
    with open(instrumented.__file__) as handle:
        text = handle.read()

    for attribute in ("gen_ai.input.messages", "gen_ai.output.messages"):
        assert attribute in text, f"{attribute} no longer emitted by pydantic-ai"
