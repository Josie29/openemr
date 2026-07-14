from pydantic import BaseModel, ConfigDict, Field

from copilot.schemas import Claim


class ExtractorOutput(BaseModel):
    """The intake-extractor worker's structured hand-off to the supervisor.

    The patient's intake picture as a set of cited claims — each traceable to a FHIR record the
    worker read this turn (demographics, problems, medications, allergies, and, once JOS-54 lands,
    document-extracted lab/intake Observations). The worker's grounding gate rejects any claim not
    grounded in a fetched record before this is returned, so the supervisor only ever composes
    over verified facts.
    """

    model_config = ConfigDict(frozen=True)

    summary: str = Field(description="One-line orientation on what the intake facts show")
    claims: list[Claim] = Field(
        default_factory=list,
        description="The patient intake facts, each citing the FHIR record it was read from",
    )


class RetrieverOutput(BaseModel):
    """The evidence-retriever worker's structured hand-off to the supervisor.

    The guideline evidence relevant to the turn as a set of cited claims — each grounded in a
    retrieved guideline chunk (cited by ``GuidelineChunk``/chunk-id with a verbatim quote). The
    worker's grounding gate rejects any evidence claim whose quote is not in a chunk it retrieved
    this turn, so unattributable guideline text never reaches the answer.
    """

    model_config = ConfigDict(frozen=True)

    summary: str = Field(description="One-line orientation on what the guideline evidence says")
    claims: list[Claim] = Field(
        default_factory=list,
        description="The guideline evidence, each citing the retrieved chunk it was drawn from",
    )
