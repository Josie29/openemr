from enum import StrEnum
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from copilot.schemas import ChatResponse

# Judges run on the cheapest tier — they make a bounded yes/no rubric judgement, not clinical
# reasoning. This is the "Haiku entailment judge" the agent's grounding docstring anticipates.
_JUDGE_MODEL_ID = "claude-haiku-4-5"


class Verdict(StrEnum):
    """A judge's binary decision on one rubric."""

    PASS = "pass"
    FAIL = "fail"


class JudgeVerdict(BaseModel):
    """A judge's structured decision: the verdict plus a one-line justification for the trace."""

    model_config = ConfigDict(frozen=True)

    verdict: Verdict = Field(description="pass or fail against the rubric")
    reasoning: str = Field(description="One or two sentences justifying the verdict")

    @property
    def passed(self) -> bool:
        """Whether the rubric passed (convenience for turning the verdict into a 1.0/0.0 score)."""
        return self.verdict is Verdict.PASS


_FAITHFULNESS_RUBRIC = """You audit a clinical AI assistant for over-statement. You are given the
CLAIMS the assistant made (each already verified against the patient record and carrying the
record value it cites) and the SUMMARY prose shown to the physician.

Your question: does the SUMMARY assert any POSITIVE clinical fact, value, or conclusion that is NOT
supported by the CLAIMS? Fail if the summary introduces a positive fact beyond the claims — a
diagnosis, a drug-interaction conclusion stated as fact, a specific value, a drug indication, or a
recommendation the claims do not carry. Pass if every positive factual statement traces to a claim.

Two things are explicitly faithful and must NOT be failed:
1. A statement that the record contains NO data of some kind (e.g. "no medications are recorded",
   "no drug allergies are documented"). An absence cannot be cited to a resource, so it needs no
   supporting claim — a correct answer for a sparse record legitimately has zero claims. Fail such
   a statement only if it *also* asserts a positive fact the claims do not support.
2. Surfacing something "for the physician to review" — that is caution, not over-statement.

Ignore tone and completeness — judge only whether the summary stays within what the claims
support, per the rules above."""


@lru_cache(maxsize=4)
def _judge_agent(api_key: str, rubric: str) -> Agent[None, JudgeVerdict]:
    """Build (and cache) a Haiku judge agent for a rubric.

    Cached by ``(api_key, rubric)`` so concurrent evaluators reuse one provider/model rather than
    rebuilding the Anthropic client per case.

    Args:
        api_key: Anthropic API key for the judge model.
        rubric: The system prompt defining the judge's single pass/fail question.

    Returns:
        A Pydantic AI agent that returns a typed ``JudgeVerdict``.
    """
    model = AnthropicModel(_JUDGE_MODEL_ID, provider=AnthropicProvider(api_key=api_key))
    return Agent(model, output_type=JudgeVerdict, system_prompt=rubric)


def _render_claims(response: ChatResponse) -> str:
    """Render an answer's claims as a bulleted list of grounded facts for a judge prompt.

    Args:
        response: The agent's structured answer.

    Returns:
        A bulleted list of ``text (source type/id.field = value)`` lines, or a placeholder when the
        answer carries no claims (a valid state for an absent-data answer).
    """
    if not response.claims:
        return "(no claims — the answer asserts no record-backed facts)"
    lines: list[str] = []
    for claim in response.claims:
        src = claim.source
        lines.append(
            f"- {claim.text} (source {src.resource_type}/{src.resource_id}"
            f".{src.field} = {src.value})"
        )
    return "\n".join(lines)


async def judge_faithfulness(response: ChatResponse, *, api_key: str) -> JudgeVerdict:
    """Judge whether the answer's summary stays within what its claims support.

    Args:
        response: The agent's structured answer.
        api_key: Anthropic API key for the judge model.

    Returns:
        The judge's pass/fail verdict with reasoning.
    """
    agent = _judge_agent(api_key, _FAITHFULNESS_RUBRIC)
    prompt = f"CLAIMS:\n{_render_claims(response)}\n\nSUMMARY:\n{response.summary}"
    result = await agent.run(prompt)
    return result.output
