from pydantic import BaseModel, ConfigDict, Field


class SourceRef(BaseModel):
    """A citation binding a claim to a specific field of a resource the agent actually read.

    The load-bearing contract for the verification gate (ARCHITECTURE.md §7). Two citation modes,
    both resolved deterministically against the resource a tool returned:

    - **Structured** (coded fields): set ``field``; the gate resolves ``(resource_type,
      resource_id, field)`` to the exact record value.
    - **Free-text note**: set ``quote`` to the verbatim supporting span; the gate checks it is a
      substring of the fetched note's text.

    Either way the gate rejects a claim that does not resolve, and ``value`` is stamped in **by
    code, from the fetched record** — never written by the model.
    """

    model_config = ConfigDict(frozen=True)

    resource_type: str = Field(description="FHIR resource type, e.g. 'Patient'")
    resource_id: str = Field(description="FHIR resource logical id")
    field: str | None = Field(
        default=None,
        description="Field name in the tool's returned data the claim draws from, e.g. birth_date",
    )
    quote: str | None = Field(
        default=None,
        description=(
            "For a free-text note citation only: the EXACT verbatim span from the note that "
            "supports the claim, copied word-for-word (not paraphrased). Use `field` instead for "
            "structured resources."
        ),
    )
    value: str | None = Field(
        default=None,
        description="The actual record value. Leave empty — the system fills this from the record.",
    )
    label: str | None = Field(
        default=None,
        description=(
            "The record's human-recognizable name (e.g. 'Asthma'). Leave empty — the system fills "
            "it from the cited record so the card names the specific record, not just its type."
        ),
    )
    date: str | None = Field(
        default=None,
        description="The cited record's key date (e.g. onset). Leave empty — the system fills it.",
    )
    date_label: str | None = Field(
        default=None,
        description="What `date` means for this record (e.g. 'Onset'). Leave empty — system-set.",
    )


class Claim(BaseModel):
    """A single factual statement in the agent's answer, with its supporting citation.

    Structuring the answer as ``list[Claim]`` (rather than free prose) is what makes the
    grounding gate a deterministic code check instead of an LLM judgement.
    """

    model_config = ConfigDict(frozen=True)

    text: str = Field(description="The factual statement, phrased for the physician")
    source: SourceRef = Field(description="The primary record this statement is traceable to")
    supporting: list[SourceRef] = Field(
        default_factory=list,
        description=(
            "Any ADDITIONAL records this statement also draws on, beyond `source`. If a statement "
            "mentions more than one record (say a visit and a diagnosis), cite the primary one in "
            "`source` and every other one here; the gate verifies all of them, so an uncited or "
            "merely-inferred record is rejected. Prefer atomic statements about one record; leave "
            "this empty then."
        ),
    )


class ChatResponse(BaseModel):
    """The agent's structured, verifiable answer to one ``POST /chat`` turn.

    Every factual assertion lives in ``claims`` with a citation; ``summary`` is the
    human-facing prose the physician reads, which must not assert anything not covered by a
    claim. The verification gate runs over ``claims``. ``follow_ups`` are suggested next
    questions — not factual assertions — so the gate does not touch them.
    """

    summary: str = Field(description="Short prose orientation for the physician")
    claims: list[Claim] = Field(description="Every factual statement, each citing a source")
    follow_ups: list[str] = Field(
        default_factory=list,
        description=(
            "Two or three short next questions this physician is most likely to ask given THIS "
            "patient and THIS answer — the natural next click, phrased as the physician would type "
            "it (e.g. 'Is the epinephrine auto-injector current?'). Each must be answerable from "
            "this patient's record via the available tools. Omit rather than pad; leave empty when "
            "nothing meaningful follows."
        ),
    )


class ChatRequest(BaseModel):
    """The inbound ``POST /chat`` payload for a single agent turn."""

    patient_id: str = Field(description="FHIR Patient logical id the turn is scoped to")
    message: str = Field(description="The physician's question")
    conversation_id: str | None = Field(
        default=None,
        description="Opaque id echoed from a prior turn's response; omit to start a new one",
    )
