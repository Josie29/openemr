
from pydantic import BaseModel, ConfigDict, Field


class SourceRef(BaseModel):
    """A citation binding a claim to a specific FHIR resource the agent actually read.

    This is the load-bearing contract for the verification gate (ARCHITECTURE.md §7,
    grounding check): the gate rejects any claim whose ``SourceRef`` does not resolve to a
    resource a tool returned this turn. ``resource_type`` + ``resource_id`` are the FHIR
    coordinates; ``field`` names the element the claim draws from, for one-click traceback.
    """

    model_config = ConfigDict(frozen=True)

    resource_type: str = Field(description="FHIR resource type, e.g. 'Patient'")
    resource_id: str = Field(description="FHIR resource logical id")
    field: str | None = Field(
        default=None, description="Element the claim is drawn from, e.g. 'birthDate'"
    )


class Claim(BaseModel):
    """A single factual statement in the agent's answer, with its supporting citation.

    Structuring the answer as ``list[Claim]`` (rather than free prose) is what makes the
    grounding gate a deterministic code check instead of an LLM judgement.
    """

    model_config = ConfigDict(frozen=True)

    text: str = Field(description="The factual statement, phrased for the physician")
    source: SourceRef = Field(description="The record this statement is traceable to")


class ChatResponse(BaseModel):
    """The agent's structured, verifiable answer to one ``POST /chat`` turn.

    Every factual assertion lives in ``claims`` with a citation; ``summary`` is the
    human-facing prose the physician reads, which must not assert anything not covered by a
    claim. The verification gate runs over ``claims``.
    """

    summary: str = Field(description="Short prose orientation for the physician")
    claims: list[Claim] = Field(description="Every factual statement, each citing a source")


class ChatRequest(BaseModel):
    """The inbound ``POST /chat`` payload for a single agent turn."""

    patient_id: str = Field(description="FHIR Patient logical id the turn is scoped to")
    message: str = Field(description="The physician's question")
