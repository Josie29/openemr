from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import count
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from copilot.fhir.models import ResourceIdentity
from copilot.ingestion.extractor import ExtractedDocument
from copilot.ingestion.schemas import (
    Citation,
    CitedText,
    DocType,
    IntakeForm,
    LabDetail,
    LabReport,
    MedicationList,
)
from copilot.schemas import SourceRef
from copilot.verification import Resolution

# Document facts flow through the SAME SourceRef/grounding machinery as FHIR-record and guideline
# claims: a claim cites ("<resource_type>", "<fact id>") and this registry grounds it. The tags come
# from `resource_type_for` — the fact's eventual write target — so Patient / AllergyIntolerance /
# MedicationRequest are types FHIR read tools ALSO fetch: a document fact and a fetched record can
# now carry the same resource_type. Lab results tag as `Observation`, which `get_lab_observations`
# also fetches — so this applies to labs too.
#
# What keeps the two resolvers from shadowing each other is therefore ID-disjointness, not
# type-disjointness. A document fact's id is `<doc-alias>#<ordinal>` (e.g. `d1#0`) — a short,
# turn-local alias for the document, `#`-suffixed and so never a FHIR uuid — so the FetchLog misses
# every document-fact id and this registry misses every FHIR id (the alias is short so a
# claims-heavy answer fits the model's output budget — see `_doc_aliases`).
# `CompositeResolver` (verification.py:52-69) tries the FetchLog first and falls through on a miss,
# so each citation reaches exactly the one resolver that owns it. This invariant is load-bearing —
# it is what the tags' change cost us — and is covered explicitly by tests/test_document_facts.py.


class FactKind(StrEnum):
    """The kind of fact a document extraction yields — the :data:`DocumentFactHandle` discriminator.

    One member per section the strict schemas can produce: a ``LabReport``'s results, an
    ``IntakeForm``'s demographics, allergies, and family history, and a ``MedicationList``'s
    medications.
    """

    LAB_RESULT = "lab_result"
    DEMOGRAPHIC = "demographic"
    MEDICATION = "medication"
    ALLERGY = "allergy"
    FAMILY_HISTORY = "family_history"


def resource_type_for(kind: FactKind) -> str:
    """The FHIR resource-type tag a document fact of this kind cites as.

    Facts are tagged by their eventual **write target** (tracked in JOS-81), not by the document
    they were read from, so the tag a claim cites today is the resource the fact will round-trip to
    once the write surface lands — no re-tagging and no migration of citations already in flight.

    Two consequences of that rule are deliberate:

    - ``MEDICATION`` maps to ``MedicationRequest``, **not** ``MedicationStatement``: the latter does
      not exist in this OpenEMR fork, so it could never be the write target.
    - ``FAMILY_HISTORY`` maps to ``FamilyMemberHistory``, which has no route, controller, service or
      structured table in this fork (family history is nine fixed free-text columns), so this tag
      alone cannot round-trip. Accepted deliberately: a tag naming the resource the fact *is* keeps
      the set symmetric and honest, where borrowing some other resource's tag would not.

    Args:
        kind: The kind of extracted fact.

    Returns:
        The FHIR resource type the fact's citations carry.
    """
    match kind:
        case FactKind.LAB_RESULT:
            return "Observation"
        case FactKind.DEMOGRAPHIC:
            return "Patient"
        case FactKind.MEDICATION:
            return "MedicationRequest"
        case FactKind.ALLERGY:
            return "AllergyIntolerance"
        case FactKind.FAMILY_HISTORY:
            return "FamilyMemberHistory"


# DEPRECATED — call `resource_type_for(FactKind.LAB_RESULT)` instead. It survives only because it is
# still imported by tests/test_extractor.py and tests/test_overlay_stamp.py. It names the LAB tag
# only: it predates intake extraction, when every document fact was an Observation, and there is no
# longer a single "the document-fact resource type" for it to mean.
DOCUMENT_FACT_RESOURCE_TYPE = resource_type_for(FactKind.LAB_RESULT)


class _FactHandleBase(BaseModel):
    """The citation handle shared by every extracted fact ``attach_and_extract`` returns.

    Carries the handle (``resource_type``/``resource_id``) the model must copy verbatim into a
    claim; each arm adds the human-readable fields it states — mirroring how ``search_guidelines``
    returns snippets the model then cites. The overlay geometry is NOT exposed here: it is stamped
    onto the ``SourceRef`` by the grounding gate (code-authored), never by the model.

    Inheritance is deliberate, mirroring :class:`~copilot.schemas.CitationBase`:
    :data:`DocumentFactHandle` is a discriminated (tagged) union whose arms share this contract and
    differ only by their ``kind`` tag. That is the idiomatic Pydantic tagged-union shape — the same
    sanctioned carve-out from the general compose-over-inherit guidance, which targets domain
    models, not union arms.
    """

    model_config = ConfigDict(frozen=True)

    resource_type: str = Field(description="Cite this verbatim as the claim's source resource_type")
    resource_id: str = Field(description="Cite this verbatim as the claim's source resource_id")


class LabFactHandle(_FactHandleBase):
    """The citable view of one extracted lab fact (an analyte and its result)."""

    kind: Literal[FactKind.LAB_RESULT] = FactKind.LAB_RESULT
    test_name: str = Field(description="Analyte/test name as printed on the report")
    value: str = Field(description="Result value verbatim")
    unit: str | None = Field(default=None, description="Unit as printed, if any")
    reference_range: str | None = Field(default=None, description="Reference range, if printed")
    abnormal_flag: str = Field(description="Abnormal indicator: no | yes | high | low")


class DemographicFactHandle(_FactHandleBase):
    """The citable view of one demographic value an intake form states."""

    kind: Literal[FactKind.DEMOGRAPHIC] = FactKind.DEMOGRAPHIC
    field: str = Field(description="Which demographic the form states, e.g. 'date_of_birth'")
    value: str = Field(description="The value verbatim as printed on the form")


class MedicationFactHandle(_FactHandleBase):
    """The citable view of one current medication an intake form reports."""

    kind: Literal[FactKind.MEDICATION] = FactKind.MEDICATION
    name: str = Field(description="Medication name as printed")
    dose: str | None = Field(default=None, description="Dose/strength as printed, if given")
    frequency: str | None = Field(default=None, description="Frequency as printed, if given")


class AllergyFactHandle(_FactHandleBase):
    """The citable view of one allergy an intake form reports."""

    kind: Literal[FactKind.ALLERGY] = FactKind.ALLERGY
    substance: str = Field(description="Allergen/substance as printed")
    reaction: str | None = Field(default=None, description="Reaction as printed, if given")


class FamilyHistoryFactHandle(_FactHandleBase):
    """The citable view of one family-history entry an intake form reports."""

    kind: Literal[FactKind.FAMILY_HISTORY] = FactKind.FAMILY_HISTORY
    condition: str = Field(description="Condition as printed")
    relation: str | None = Field(default=None, description="Affected relative, if given")


DocumentFactHandle = Annotated[
    LabFactHandle
    | DemographicFactHandle
    | MedicationFactHandle
    | AllergyFactHandle
    | FamilyHistoryFactHandle,
    Field(discriminator="kind"),
]


@dataclass(frozen=True)
class _RecordedFact:
    """One recorded fact, normalized to exactly what :meth:`DocumentFactRegistry.resolve` needs.

    Normalizing at record time is what keeps ``resolve`` one shape across both document types: a lab
    result and an intake allergy differ in what a fact *is*, not in how a citation grounds against
    one. Everything arm-specific (which field is the value, what names the record) is decided by the
    recording arm and collapsed into these fields.

    ``lab_detail`` does not breach that rule: it is ONE field, uniformly present, ``None`` for every
    non-lab fact — the shape stays singular. The recording arm still decides everything arm-specific
    and collapses it, into one slot rather than leaking four lab-only scalars that would be dead for
    the other four :class:`FactKind` arms. (Spreading them flat is the version that breaks the
    rule.)
    """

    resource_type: str
    value: str
    identity: ResourceIdentity
    citation: Citation
    document_id: str
    doc_type: DocType
    lab_detail: LabDetail | None = None


@dataclass
class DocumentFactRegistry:
    """Registry of the facts a turn extracted from documents — the document-extraction resolver.

    The extraction counterpart to :class:`~copilot.verification.FetchLog` (FHIR records) and
    :class:`~copilot.retrieval.ChunkRegistry` (guideline chunks): ``attach_and_extract`` records the
    facts it read from a document, and this resolves a claim's citation against them so the one
    grounding gate that checks FHIR and guideline claims also checks document facts. A claim grounds
    only when it cites a fact recorded this turn; its value is stamped from the recorded fact (never
    the model's say-so), and the click-to-source overlay provenance — document id, doc type, page
    and box (already in PDF points from the extractor) — is stamped alongside it for the sidebar
    (the JOS-57 seam).
    """

    _facts: dict[str, _RecordedFact] = field(default_factory=dict)
    # document_id -> short turn-local alias ("d1", …): a claim cites `<alias>#<ordinal>`, not
    # `<document_id>#<ordinal>`. Short id = a full report's claims fit the output budget and copy
    # cleanly (the 36-char uuid was truncated AND fabricated). The real document_id rides each
    # `_RecordedFact` for click-to-source.
    _doc_aliases: dict[str, str] = field(default_factory=dict)

    def record(self, extracted: ExtractedDocument) -> list[DocumentFactHandle]:
        """Record an extracted document's facts and return their citable handles.

        Exhaustive over the strict schemas with no default branch, so a new document type is a type
        error here rather than a document that silently records nothing.

        Args:
            extracted: One document's strict extraction (a cited ``LabReport``, ``IntakeForm``, or
                ``MedicationList``).

        Returns:
            One handle per fact — in report order for a lab or a medication list, in the fixed
            section order documented on :meth:`_record_intake` for a form — for the model to state
            and cite.
        """
        ordinals = count()
        report = extracted.report
        match report:
            case LabReport():
                return self._record_lab(extracted, report, ordinals)
            case IntakeForm():
                return self._record_intake(extracted, report, ordinals)
            case MedicationList():
                return self._record_medication_list(extracted, report, ordinals)

    def _record_lab(
        self, extracted: ExtractedDocument, report: LabReport, ordinals: Iterator[int]
    ) -> list[DocumentFactHandle]:
        """Record each lab result as an ``Observation``-tagged fact, in report order.

        Args:
            extracted: The document being recorded.
            report: Its strict lab extraction.
            ordinals: The document's flat ordinal counter.

        Returns:
            One :class:`LabFactHandle` per result.
        """
        handles: list[DocumentFactHandle] = []
        for result in report.results:
            resource_id = self._store(
                extracted,
                next(ordinals),
                FactKind.LAB_RESULT,
                value=result.value,
                identity=ResourceIdentity(
                    label=result.test_name,
                    date=result.collection_date.isoformat() if result.collection_date else None,
                    date_label="Collected",
                ),
                citation=result.citation,
                lab_detail=LabDetail(
                    test_name=result.test_name,
                    unit=result.unit,
                    reference_range=result.reference_range,
                    abnormal_flag=result.abnormal_flag,
                ),
            )
            handles.append(
                LabFactHandle(
                    resource_type=resource_type_for(FactKind.LAB_RESULT),
                    resource_id=resource_id,
                    test_name=result.test_name,
                    value=result.value,
                    unit=result.unit,
                    reference_range=result.reference_range,
                    abnormal_flag=result.abnormal_flag.value,
                )
            )
        return handles

    def _record_intake(
        self, extracted: ExtractedDocument, form: IntakeForm, ordinals: Iterator[int]
    ) -> list[DocumentFactHandle]:
        """Record an intake form's facts in a FIXED section order.

        The section order — demographics in ``Demographics`` declaration order, chief concern,
        allergies, family history — is the contract, not a detail: a fact's citable id is its flat
        ordinal within this one sequence, so the order is the only thing that makes an ordinal name
        a stable fact. Reordering a section silently re-points every id after it at a different
        fact. A field the form does not state consumes no ordinal. Medications are not part of this
        sequence — the ``medication_list`` document type owns them (see
        :meth:`_record_medication_list`).

        Args:
            extracted: The document being recorded.
            form: Its strict intake extraction.
            ordinals: The document's flat ordinal counter, shared with every other section.

        Returns:
            One handle per stated fact, in that order.
        """
        demographics = form.demographics
        # Chief concern is tagged DEMOGRAPHIC (-> Patient) for want of a closer write target; where
        # it actually lands is JOS-81's call.
        cited_fields: tuple[tuple[str, CitedText | None], ...] = (
            ("full_name", demographics.full_name),
            ("date_of_birth", demographics.date_of_birth),
            ("sex", demographics.sex),
            ("address", demographics.address),
            ("phone", demographics.phone),
            ("chief_concern", form.chief_concern),
        )
        handles: list[DocumentFactHandle] = []
        for name, cited in cited_fields:
            if cited is None:
                continue
            resource_id = self._store(
                extracted,
                next(ordinals),
                FactKind.DEMOGRAPHIC,
                value=cited.value,
                identity=ResourceIdentity(label=name),
                citation=cited.citation,
            )
            handles.append(
                DemographicFactHandle(
                    resource_type=resource_type_for(FactKind.DEMOGRAPHIC),
                    resource_id=resource_id,
                    field=name,
                    value=cited.value,
                )
            )
        for allergy in form.allergies:
            resource_id = self._store(
                extracted,
                next(ordinals),
                FactKind.ALLERGY,
                value=allergy.substance,
                identity=ResourceIdentity(label=allergy.substance),
                citation=allergy.citation,
            )
            handles.append(
                AllergyFactHandle(
                    resource_type=resource_type_for(FactKind.ALLERGY),
                    resource_id=resource_id,
                    substance=allergy.substance,
                    reaction=allergy.reaction,
                )
            )
        for entry in form.family_history:
            resource_id = self._store(
                extracted,
                next(ordinals),
                FactKind.FAMILY_HISTORY,
                value=entry.condition,
                identity=ResourceIdentity(label=entry.condition),
                citation=entry.citation,
            )
            handles.append(
                FamilyHistoryFactHandle(
                    resource_type=resource_type_for(FactKind.FAMILY_HISTORY),
                    resource_id=resource_id,
                    condition=entry.condition,
                    relation=entry.relation,
                )
            )
        return handles

    def _record_medication_list(
        self, extracted: ExtractedDocument, report: MedicationList, ordinals: Iterator[int]
    ) -> list[DocumentFactHandle]:
        """Record each medication as a ``MedicationRequest``-tagged fact, in list order.

        Mirrors :meth:`_record_lab`: one fact per row, the flat ordinal is the citable id, and
        ``MedicationRequest`` (via :func:`resource_type_for`) is the resource_type.

        Args:
            extracted: The document being recorded.
            report: Its strict medication-list extraction.
            ordinals: The document's flat ordinal counter.

        Returns:
            One :class:`MedicationFactHandle` per medication.
        """
        handles: list[DocumentFactHandle] = []
        for medication in report.medications:
            resource_id = self._store(
                extracted,
                next(ordinals),
                FactKind.MEDICATION,
                value=medication.name,
                identity=ResourceIdentity(label=medication.name),
                citation=medication.citation,
            )
            handles.append(
                MedicationFactHandle(
                    resource_type=resource_type_for(FactKind.MEDICATION),
                    resource_id=resource_id,
                    name=medication.name,
                    dose=medication.dose,
                    frequency=medication.frequency,
                )
            )
        return handles

    def _doc_alias(self, document_id: str) -> str:
        """This turn's short, stable alias for a document (``d1``, ``d2``, …).

        Memoized per ``document_id`` in first-seen order, so every fact from one document shares an
        alias and a re-extract reuses it — deterministic within a turn.
        """
        alias = self._doc_aliases.get(document_id)
        if alias is None:
            alias = f"d{len(self._doc_aliases) + 1}"
            self._doc_aliases[document_id] = alias
        return alias

    def _store(
        self,
        extracted: ExtractedDocument,
        ordinal: int,
        kind: FactKind,
        *,
        value: str,
        identity: ResourceIdentity,
        citation: Citation,
        lab_detail: LabDetail | None = None,
    ) -> str:
        """Normalize one fact into the registry and return the resource id a claim cites it by.

        Args:
            extracted: The document the fact was read from.
            ordinal: The fact's position in the document's flat recording order.
            kind: Which kind of fact it is; selects the resource-type tag.
            value: The verbatim value the gate will stamp onto a claim citing this fact.
            identity: What names this fact on the evidence card.
            citation: The extractor's citation, carrying the click-to-source box.
            lab_detail: The analyte metadata a lab fact carries beyond its value, stamped onto a
                citing claim for the sidebar's lab table. None for every non-lab fact.

        Returns:
            The fact's short ``<doc-alias>#<ordinal>`` citation id (e.g. ``d1#0``).
        """
        resource_id = f"{self._doc_alias(extracted.document_id)}#{ordinal}"
        self._facts[resource_id] = _RecordedFact(
            resource_type=resource_type_for(kind),
            value=value,
            identity=identity,
            citation=citation,
            document_id=extracted.document_id,
            doc_type=extracted.doc_type,
            lab_detail=lab_detail,
        )
        return resource_id

    def resolve(self, ref: SourceRef) -> Resolution | None:
        """Ground a document-fact citation and return its value plus click-to-source overlay.

        The id is looked up FIRST and the resource type checked only after, because the id namespace
        — not the tag — is what makes a citation this registry's to answer (see the module comment
        on id-disjointness). So a FHIR citation is declined without the tag ever being consulted,
        and a recorded id cited under the wrong tag is refused rather than grounded against a fact
        the claim was not talking about.

        Args:
            ref: The claim's citation (expected to name a recorded document fact).

        Returns:
            The :class:`~copilot.verification.Resolution` (value + identity + document id/type/page/
            box in PDF points) when the fact was recorded this turn; otherwise None (an unrecorded
            id, or a recorded id under the wrong resource type).
        """
        fact = self._facts.get(ref.resource_id)
        if fact is None:
            return None
        if fact.resource_type != ref.resource_type:
            return None
        box = fact.citation.bounding_box  # already in PDF points from the extractor
        return Resolution(
            value=fact.value,
            identity=fact.identity,
            document_id=fact.document_id,
            page=box.page if box is not None else None,
            bounding_box=box,
            doc_type=fact.doc_type,
            lab_detail=fact.lab_detail,
        )
