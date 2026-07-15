from typing import Any, Protocol, Self

from pydantic_ai import ModelRetry

from copilot.schemas import Claim
from copilot.verification import CitationResolver, ground_claims


class _HasClaims(Protocol):
    """A worker/answer output the grounding gate can operate on: it carries citable claims.

    Structural (not a base class), so ``ExtractorOutput``, ``RetrieverOutput``, and the final
    ``ChatResponse`` all qualify without a shared parent — the gate depends on the ``claims`` list
    and Pydantic's ``model_copy``, nothing more.
    """

    @property
    def claims(self) -> list[Claim]:
        """The output's citable claims (read-only so frozen models qualify too)."""
        ...

    def model_copy(self, *, update: dict[str, Any]) -> Self: ...


def enforce_claim_grounding[OutputT: _HasClaims](
    output: OutputT, resolver: CitationResolver, *, subject: str
) -> OutputT:
    """Reject any ungrounded claim in ``output``, else stamp the real values and return it.

    The single grounding gate, applied identically to each worker's output and the supervisor's
    final answer — the "crown jewel survives the port" requirement of
    ``context/decisions/agent-framework-week2.md``. Each agent attaches this via a one-line
    ``@agent.output_validator`` that supplies the resolver its evidence lives in (a FHIR
    ``FetchLog``, a guideline ``ChunkRegistry``, or a composite of both for the final answer).

    Args:
        output: The agent's candidate structured output (carries a ``claims`` list).
        resolver: The source of ground truth to resolve every citation against.
        subject: What is being validated (e.g. ``"intake-extractor"``), named in the retry
            message so the model knows which output to correct.

    Returns:
        ``output`` with every claim's real source value and identity stamped in by code.

    Raises:
        ModelRetry: When any claim cites a source that was not read/retrieved this turn or has no
            resolvable value, forcing the agent to re-ground or drop it.
    """
    grounded, offenders = ground_claims(output.claims, resolver)
    if offenders:
        raise ModelRetry(_offender_message(offenders, subject))
    return output.model_copy(update={"claims": grounded})


def _offender_message(offenders: list[Claim], subject: str) -> str:
    """Build the retry instruction naming each ungrounded claim and the citations that failed.

    Args:
        offenders: The claims that could not be grounded.
        subject: The agent/output being corrected, for the opening line.

    Returns:
        A single instruction string telling the model exactly which claims to re-ground or drop.
    """
    detail = "; ".join(
        f"claim {claim.text!r} cites "
        + ", ".join(
            f"{ref.resource_type}/{ref.resource_id}.{ref.field or 'quote'}"
            for ref in [claim.source, *claim.supporting]
        )
        + " — one of these was not read/retrieved this turn or has no value"
        for claim in offenders
    )
    return (
        f"The {subject} output has claims that are not grounded. Every claim must cite — in "
        f"`source` and every `supporting` entry — a field or quote you actually read (FHIR) or "
        f"retrieved (guideline) this turn. These do not: {detail}. Re-ground each to a real "
        f"source, drop the uncited one, or state the information is not available."
    )
