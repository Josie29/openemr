import asyncio
from collections.abc import Sequence
from typing import Any, Protocol

from pydantic_ai import Agent, RunContext

from copilot.fhir.client import FhirClient
from copilot.fhir.models import (
    LabObservation,
    NoteContent,
    PatientSummary,
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
    """Register the patient-scoped FHIR read tools on ``agent``.

    Two tools: ``get_patient_summary`` reads the whole structured record (demographics, problems,
    medications, allergies, recent encounters) in one call, and ``get_encounter_note`` reads the
    free-text note for a specific encounter. Both record what they read into the ``FetchLog`` — the
    summary records each sub-resource individually, so a claim citing any Condition or
    MedicationRequest grounds just as if that resource had been read on its own.

    Every tool is scoped to the one open patient (the deps carry a patient-scoped client and id),
    and every fetch is recorded so the grounding gate can resolve any field a claim cites. Shared by
    any agent that reads the record — today the intake-extractor worker.

    Args:
        agent: The agent to attach the read tools to (its deps must satisfy :class:`FhirReadDeps`).
    """

    @agent.tool
    async def get_patient_summary(ctx: RunContext[D]) -> PatientSummary:
        """Read the open patient's whole structured picture in ONE call.

        Returns demographics, the problem list, current medications, allergies, and recent
        encounters together — this is the single read for the structured record, whether the
        question is broad ("who is this / give me the picture") or focused ("what is her DOB?").
        Lab results are separate: use ``get_lab_observations`` for those.

        The five reads run concurrently, and each sub-resource is recorded individually so a claim
        citing any Condition/MedicationRequest/etc. grounds exactly as with a per-resource read.
        """
        pid = ctx.deps.patient_id
        patient, problems, medications, allergies, encounters = await asyncio.gather(
            ctx.deps.fhir.get_patient(pid),
            ctx.deps.fhir.get_problems(pid),
            ctx.deps.fhir.get_medications(pid),
            ctx.deps.fhir.get_allergies(pid),
            ctx.deps.fhir.get_encounters(pid),
        )
        summary = PatientSummary(
            patient=patient,
            problems=problems,
            medications=medications,
            allergies=allergies,
            recent_encounters=encounters,
        )
        # Record each individual record — never the summary wrapper, which carries no id and is
        # never cited — so a claim grounds against the same objects the per-list tools would record.
        ctx.deps.fetched.record_all(
            [
                summary.patient,
                *summary.problems,
                *summary.medications,
                *summary.allergies,
                *summary.recent_encounters,
            ]
        )
        return summary

    @agent.tool
    async def get_lab_observations(
        ctx: RunContext[D], code: str | None = None
    ) -> list[LabObservation]:
        """Read the open patient's laboratory results from the record, oldest first.

        Use for "what is their <lab>" and for trends over time. Returns only laboratory results —
        vitals and social history are excluded. A result with no `value` is normal (OpenEMR records
        some scored questionnaires without a numeric value); report it as unavailable rather than
        inferring a number.

        Args:
            ctx: The run context.
            code: Optional LOINC code to narrow to one analyte, e.g. "787-2" for MCV. Omit to read
                every lab result. Prefer filtering by code over matching on the analyte's name —
                several distinct LOINC codes share similar names (three read as "Platelet...").
        """
        return _track(ctx, await ctx.deps.fhir.get_lab_observations(ctx.deps.patient_id, code=code))

    @agent.tool
    async def get_encounter_note(ctx: RunContext[D], encounter_id: str) -> list[NoteContent]:
        """Read the free-text clinical note(s) for one encounter — the narrative behind a visit.

        Use for "why"/"what did the note say" questions the structured lists can't answer. Find the
        relevant visit in get_patient_summary's recent_encounters first, then pass its id here.

        Args:
            ctx: The run context.
            encounter_id: The Encounter id whose note to read (from get_patient_summary).
        """
        return _track(
            ctx, await ctx.deps.fhir.get_encounter_note(ctx.deps.patient_id, encounter_id)
        )
