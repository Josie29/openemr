import logging
from typing import Any

from langfuse.types import MaskOtelSpansParams, MaskOtelSpansResult, OtelSpanPatch

logger = logging.getLogger("copilot.masking")

REDACTED = "[REDACTED]"

# Span attributes that carry free content — prompts, completions, tool arguments and tool
# results. Tool results are the heaviest: they are raw FHIR records (names, DOBs, addresses,
# clinical values). Deleted at export, so the payload never leaves the process.
PHI_SPAN_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
        "pydantic_ai.all_messages",
        "tool_arguments",
        "tool_response",
    }
)

# Keys whose values are safe to keep on an SDK-set payload. Everything else is redacted, so a
# new field added upstream is redacted by default rather than leaking until someone notices.
SAFE_PAYLOAD_KEYS: frozenset[str] = frozenset({"route"})

_MASK_MARKER = "masking.applied"


def mask_payload(*, data: Any, **_: Any) -> Any:
    """Redact SDK-set observation input/output, keeping only allow-listed structural keys.

    Applies to payloads this service sets explicitly — the route span's ``{route, reason}`` and
    the turn's output. ``reason`` is model-generated prose that can quote the chart, so only
    ``route`` survives. Default-deny: an unrecognised key is redacted.

    Args:
        data: The payload Langfuse is about to export.

    Returns:
        The payload with non-allow-listed content replaced by ``REDACTED``.
    """
    if isinstance(data, dict):
        return {
            key: value if key in SAFE_PAYLOAD_KEYS else mask_payload(data=value)
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [mask_payload(data=item) for item in data]
    if isinstance(data, str):
        return REDACTED
    # Numbers/bools/None carry no free text — latency, token counts, scores stay readable.
    if isinstance(data, (int, float, bool)) or data is None:
        return data
    return REDACTED


def mask_otel_spans(*, params: MaskOtelSpansParams) -> MaskOtelSpansResult | None:
    """Strip PHI-bearing attributes from every OTel span at export.

    The SDK-level ``mask`` only reaches payloads this service sets. The prompts, completions and
    tool results come from Pydantic AI's auto-instrumentation as OTel span attributes, so
    masking without this hook would leave the actual PHI untouched while looking scrubbed.

    Args:
        params: The batch of spans Langfuse is about to export.

    Returns:
        Sparse patches for spans carrying PHI attributes, or None when the batch is clean.
    """
    try:
        patches: dict[Any, OtelSpanPatch] = {}
        for identifier, span in params.spans.items():
            present = PHI_SPAN_ATTRIBUTES.intersection(span.attributes or {})
            if not present:
                continue
            patches[identifier] = OtelSpanPatch(
                delete_attributes=tuple(sorted(present)),
                set_attributes={_MASK_MARKER: True},
            )
        return MaskOtelSpansResult(span_patches=patches) if patches else None
    except Exception:  # noqa: BLE001 - a masking failure must not drop the batch silently
        logger.warning("PHI span masking failed; dropping the batch's content", exc_info=True)
        # Fail closed: patch every span rather than exporting unmasked content.
        return MaskOtelSpansResult(
            span_patches={
                identifier: OtelSpanPatch(
                    delete_attributes=tuple(sorted(PHI_SPAN_ATTRIBUTES)),
                    set_attributes={_MASK_MARKER: True},
                )
                for identifier in params.spans
            }
        )
