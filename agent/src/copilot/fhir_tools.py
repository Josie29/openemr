from collections.abc import Sequence
from typing import Any, Protocol

from pydantic_ai import Agent, RunContext

from copilot.fhir.client import FhirClient
from copilot.fhir.models import (
    Allergy,
    Encounter,
    Medication,
    NoteContent,
    PatientDemographics,
    Problem,
)
from copilot.verification import FetchLog, FhirRecordable


class FhirReadDeps(Protocol):
    """The dependency surface the shared FHIR read tools need.

    Structural, so any per-request deps object carrying a patient-scoped ``fhir`` client, the open
    ``patient_id``, and the turn's ``fetched`` registry can host these tools — the single-agent
    ``CopilotDeps`` did, and the Week-2 ``GraphDeps`` does. Registering the tools once here (rather
    than inlining them per agent) keeps "a tool records exactly what it read" in one place.
    """

    fhir: FhirClient
    patient_id: str
    fetched: FetchLog


def _track[D: FhirReadDeps, T: FhirRecordable | Sequence[FhirRecordable]](
    ctx: RunContext[D], result: T
) -> T:
    """Record everything a tool fetched into the turn's registry, then return it unchanged.

    Wrapping every fetch in ``_track`` makes "record what you read" one visual unit at each tool's
    return — a tool cannot fetch data without also grounding it in the ``FetchLog`` the gate reads.

    Args:
        ctx: The run context (holds the fetch registry).
        result: The resource, or list of resources, the tool fetched.

    Returns:
        ``result`` unchanged.
    """
    ctx.deps.fetched.record_all(result)
    return result


def register_fhir_read_tools[D: FhirReadDeps](agent: Agent[D, Any]) -> None:
    """Register the six patient-scoped FHIR read tools on ``agent``.

    Every tool is scoped to the one open patient (the deps carry a patient-scoped client and id),
    and every fetch is recorded via :func:`_track` so the grounding gate can resolve any field a
    claim cites. Shared by any agent that reads the record — today the intake-extractor worker.

    Args:
        agent: The agent to attach the read tools to (its deps must satisfy :class:`FhirReadDeps`).
    """

    @agent.tool
    async def get_patient(ctx: RunContext[D]) -> PatientDemographics:
        """Read the open patient's demographics (name, birth date, sex) from FHIR."""
        return _track(ctx, await ctx.deps.fhir.get_patient(ctx.deps.patient_id))

    @agent.tool
    async def get_problems(ctx: RunContext[D]) -> list[Problem]:
        """Read the open patient's problem list (active and inactive Conditions)."""
        return _track(ctx, await ctx.deps.fhir.get_problems(ctx.deps.patient_id))

    @agent.tool
    async def get_medications(ctx: RunContext[D]) -> list[Medication]:
        """Read the open patient's current medications (deduplicated MedicationRequests)."""
        return _track(ctx, await ctx.deps.fhir.get_medications(ctx.deps.patient_id))

    @agent.tool
    async def get_allergies(ctx: RunContext[D]) -> list[Allergy]:
        """Read the open patient's allergies (AllergyIntolerance resources)."""
        return _track(ctx, await ctx.deps.fhir.get_allergies(ctx.deps.patient_id))

    @agent.tool
    async def get_encounters(ctx: RunContext[D]) -> list[Encounter]:
        """Read the open patient's recent encounters (metadata only — no note bodies)."""
        return _track(ctx, await ctx.deps.fhir.get_encounters(ctx.deps.patient_id))

    @agent.tool
    async def get_encounter_note(ctx: RunContext[D], encounter_id: str) -> list[NoteContent]:
        """Read the free-text clinical note(s) for one encounter — the narrative behind a visit.

        Use for "why"/"what did the note say" questions the structured lists can't answer. Find the
        relevant visit with get_encounters first, then pass its id here.

        Args:
            ctx: The run context.
            encounter_id: The Encounter id whose note to read (from get_encounters).
        """
        return _track(
            ctx, await ctx.deps.fhir.get_encounter_note(ctx.deps.patient_id, encounter_id)
        )
