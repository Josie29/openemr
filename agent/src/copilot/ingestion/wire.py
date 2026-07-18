from typing import Any

from copilot.ingestion.extractor import ExtractedDocument
from copilot.ingestion.schemas import (
    Allergy,
    BoundingBox,
    CitedText,
    Demographics,
    FamilyHistoryItem,
    IntakeForm,
    LabReport,
    LabResult,
    Medication,
    MedicationList,
)

# The Demographics attributes projected to the wire, in the persist endpoint's field vocabulary.
_DEMOGRAPHIC_FIELDS = ("full_name", "date_of_birth", "sex", "address", "phone")


def _box_payload(box: BoundingBox) -> dict[str, Any]:
    """Project a bounding box onto the write-back wire shape.

    The persist endpoint (``FactPayloadParser``) reads ``{x, y, w, h}`` with ``page`` as a sibling
    of the box, whereas the extractor's :class:`BoundingBox` names the sides ``width``/``height``
    and carries ``page`` inside. This is the one place that reconciles the two.

    Returns:
        The box as ``{x, y, w, h}`` — the caller attaches ``page`` alongside it.
    """
    return {"x": box.x, "y": box.y, "w": box.width, "h": box.height}


def _lab_fact(result: LabResult) -> dict[str, Any] | None:
    """Project one lab result onto the wire, or None if it cannot be persisted.

    A ``LabResult`` always carries a bounding box — ``LabResult`` itself refuses one without
    (``_require_bounding_box``) — so the only reason to drop a lab fact here is a missing LOINC
    code: OpenEMR stamps ``procedure_result.result_code`` as LOINC unconditionally, so a result
    without a validated code cannot round-trip (JOS-81). ``loinc`` is None when the report printed
    none or the printed code failed validation — never a guess.

    Args:
        result: One extracted lab analyte.

    Returns:
        The wire dict keyed as the persist endpoint expects, or None to drop an uncoded fact.
    """
    box = result.citation.bounding_box
    if result.loinc is None or box is None:
        return None
    return {
        "type": "lab",
        "loinc": result.loinc,
        "label": result.test_name,
        "value": result.value,
        "units": result.unit or "",
        "range": result.reference_range or "",
        "abnormal": result.abnormal_flag.value,
        "page": box.page,
        "bbox": _box_payload(box),
        "confidence": result.confidence,
    }


def _allergy_fact(allergy: Allergy) -> dict[str, Any]:
    """Project one allergy onto the wire.

    Unlike a lab fact, an intake fact may have no bounding box (only ``LabResult`` requires one), so
    ``bbox``/``page`` are attached only when the citation carries a box.

    Args:
        allergy: One extracted allergy.

    Returns:
        The wire dict keyed as the persist endpoint expects.
    """
    fact: dict[str, Any] = {
        "type": "allergy",
        "substance": allergy.substance,
        "reaction": allergy.reaction,
        "confidence": allergy.confidence,
    }
    _attach_optional_box(fact, allergy.citation.bounding_box)
    return fact


def _medication_fact(medication: Medication) -> dict[str, Any]:
    """Project one medication onto the wire.

    Args:
        medication: One extracted medication.

    Returns:
        The wire dict keyed as the persist endpoint expects.
    """
    fact: dict[str, Any] = {
        "type": "medication",
        "name": medication.name,
        "dose": medication.dose,
        "frequency": medication.frequency,
        "confidence": medication.confidence,
    }
    _attach_optional_box(fact, medication.citation.bounding_box)
    return fact


def _family_history_fact(item: FamilyHistoryItem) -> dict[str, Any] | None:
    """Project one family-history entry onto the wire, or None if it cannot be placed.

    OpenEMR files family history under a per-relative column (``history_mother`` etc.), so a
    relation is required to persist it. An entry with no relation is dropped here rather than sent,
    because the persist parser is all-or-nothing: one unpersistable fact would reject the whole
    document's batch.

    Args:
        item: One extracted family-history entry.

    Returns:
        The wire dict, or None when the entry has no relation to place it on.
    """
    if item.relation is None or item.relation.strip() == "":
        return None
    fact: dict[str, Any] = {
        "type": "family_history",
        "condition": item.condition,
        "relation": item.relation,
        "confidence": item.confidence,
    }
    _attach_optional_box(fact, item.citation.bounding_box)
    return fact


def _chief_concern_fact(cited: CitedText) -> dict[str, Any]:
    """Project the chief concern onto the wire.

    Args:
        cited: The extracted chief concern with its citation.

    Returns:
        The wire dict keyed as the persist endpoint expects.
    """
    fact: dict[str, Any] = {
        "type": "chief_concern",
        "text": cited.value,
        "confidence": cited.confidence,
    }
    _attach_optional_box(fact, cited.citation.bounding_box)
    return fact


def _demographic_facts(demographics: Demographics) -> list[dict[str, Any]]:
    """Project the present demographic fields onto the wire.

    One fact per non-null field, tagged with the field name the persist endpoint's
    ``DemographicField`` enum expects. Demographics are accept-gated server-side, but that is the
    persister's concern — the wire simply carries every field the extractor read.

    Args:
        demographics: The extracted demographics block.

    Returns:
        The wire dicts for each field present on the form.
    """
    facts: list[dict[str, Any]] = []
    for field in _DEMOGRAPHIC_FIELDS:
        cited: CitedText | None = getattr(demographics, field)
        if cited is None:
            continue
        fact: dict[str, Any] = {
            "type": "demographic",
            "field": field,
            "value": cited.value,
            "confidence": cited.confidence,
        }
        _attach_optional_box(fact, cited.citation.bounding_box)
        facts.append(fact)
    return facts


def _attach_optional_box(fact: dict[str, Any], box: BoundingBox | None) -> None:
    """Attach ``bbox``/``page`` to a document fact when it has geometry.

    Mutates ``fact`` in place. An allergy or medication fact with no box is valid and persists
    without one; the endpoint's ``parseOptionalBox`` accepts an absent ``bbox``.
    """
    if box is None:
        return
    fact["page"] = box.page
    fact["bbox"] = _box_payload(box)


def _facts_for_document(extracted: ExtractedDocument) -> list[dict[str, Any]]:
    """Project one document's persistable facts onto the wire.

    Every extracted family now has a native destination
    (``context/specs/intake-write-back-completion.md``): labs down the procedure chain, allergies to
    ``lists``, family history to ``history_data``, the chief concern to a new encounter, and
    demographics to ``patient_data`` behind an accept gate. Medications belong to the
    ``medication_list`` document type (JOS-91), not the intake form. Match every schema explicitly:
    a fall-through to ``return []`` would silently persist nothing, and only facts that cannot be
    persisted faithfully are dropped — an uncoded lab, a family-history entry with no relation —
    because the endpoint's parser is all-or-nothing and one bad fact would reject the whole batch.

    Args:
        extracted: The full typed extraction for one document.

    Returns:
        The persistable facts, keyed for the persist endpoint; empty when the document yielded none.
    """
    report = extracted.report
    if isinstance(report, LabReport):
        return [fact for result in report.results if (fact := _lab_fact(result)) is not None]
    if isinstance(report, IntakeForm):
        facts: list[dict[str, Any]] = [
            *_demographic_facts(report.demographics),
            *(_allergy_fact(allergy) for allergy in report.allergies),
            *(
                fact
                for item in report.family_history
                if (fact := _family_history_fact(item)) is not None
            ),
        ]
        if report.chief_concern is not None:
            facts.append(_chief_concern_fact(report.chief_concern))
        return facts
    if isinstance(report, MedicationList):
        return [_medication_fact(medication) for medication in report.medications]
    return []


def derived_facts_for(
    extractions: dict[str, ExtractedDocument],
) -> list[dict[str, Any]]:
    """Build the ``derived_facts`` block of a chat response, grouped per source document.

    The sidebar posts one request per document to the session-authed persist endpoint (JOS-81), so
    the payload groups facts by document id and carries the document's type. A document that yielded
    no persistable fact is omitted rather than sent as an empty group.

    Args:
        extractions: The turn's typed extractions, keyed by document id (``deps.extractions``).

    Returns:
        A list of ``{document_id, doc_type, facts}`` groups; empty when nothing persistable was
        extracted this turn.
    """
    groups: list[dict[str, Any]] = []
    for document_id, extracted in extractions.items():
        facts = _facts_for_document(extracted)
        if not facts:
            continue
        groups.append(
            {
                "document_id": document_id,
                "doc_type": extracted.doc_type.value,
                "facts": facts,
            }
        )
    return groups
