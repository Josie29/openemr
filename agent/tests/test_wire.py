from copilot.ingestion.extractor import ExtractedDocument
from copilot.ingestion.schemas import (
    AbnormalFlag,
    Allergy,
    BoundingBox,
    Citation,
    CitedText,
    Demographics,
    DocType,
    FamilyHistoryItem,
    IntakeForm,
    LabReport,
    LabResult,
    Medication,
    MedicationList,
)
from copilot.ingestion.wire import derived_facts_for


def _box(page: int = 1) -> BoundingBox:
    return BoundingBox(page=page, x=72.0, y=144.0, width=96.0, height=12.0)


def _citation(value: str, page: int = 1, *, boxed: bool = True) -> Citation:
    return Citation(quote_or_value=value, bounding_box=_box(page) if boxed else None)


def _lab_result(loinc: str | None = "4548-4") -> LabResult:
    return LabResult(
        test_name="Hemoglobin A1c",
        loinc=loinc,
        value="8.2",
        unit=CitedText(value="%", citation=_citation("%")),
        reference_range=CitedText(value="4.0-5.6", citation=_citation("4.0-5.6")),
        abnormal_flag=AbnormalFlag.HIGH,
        citation=_citation("8.2"),
    )


def _lab_document(*results: LabResult, document_id: str = "doc-lab") -> ExtractedDocument:
    return ExtractedDocument(
        document_id=document_id, doc_type=DocType.LAB_PDF, report=LabReport(results=list(results))
    )


def _intake_document(document_id: str = "doc-intake") -> ExtractedDocument:
    """An intake form exercising every section — all of which now persist to a native record."""
    return ExtractedDocument(
        document_id=document_id,
        doc_type=DocType.INTAKE_FORM,
        report=IntakeForm(
            demographics=Demographics(
                full_name=CitedText(value="Sergio", citation=_citation("Sergio"))
            ),
            chief_concern=CitedText(value="cough", citation=_citation("cough")),
            allergies=[
                Allergy(
                    substance="Penicillin",
                    reaction=CitedText(value="hives", citation=_citation("hives")),
                    citation=_citation("Penicillin"),
                )
            ],
            family_history=[
                FamilyHistoryItem(
                    condition="diabetes",
                    relation=CitedText(value="mother", citation=_citation("mother")),
                    citation=_citation("diabetes"),
                )
            ],
        ),
    )


def _medication_list_document(document_id: str = "doc-meds") -> ExtractedDocument:
    """A medication list — the document type that owns medication facts."""
    return ExtractedDocument(
        document_id=document_id,
        doc_type=DocType.MEDICATION_LIST,
        report=MedicationList(
            medications=[
                Medication(
                    name="Metformin",
                    dose=CitedText(value="500 mg", citation=_citation("500 mg")),
                    frequency=CitedText(value="twice daily", citation=_citation("twice daily")),
                    citation=_citation("Metformin"),
                )
            ],
        ),
    )


def test_lab_result_maps_to_the_persist_endpoint_field_names() -> None:
    """The wire keys must match FactPayloadParser exactly.

    Breaks if a field is renamed on one side only — e.g. the endpoint reads `units`/`range` while
    the extractor calls them `unit`/`reference_range`, and every lab fact silently 422s.
    """
    groups = derived_facts_for({"doc-lab": _lab_document(_lab_result())})

    assert len(groups) == 1
    assert groups[0]["document_id"] == "doc-lab"
    assert groups[0]["doc_type"] == "lab_pdf"
    fact = groups[0]["facts"][0]
    assert fact == {
        "type": "lab",
        "loinc": "4548-4",
        "label": "Hemoglobin A1c",
        "value": "8.2",
        "units": "%",
        "range": "4.0-5.6",
        "abnormal": "high",
        "page": 1,
        "bbox": {"x": 72.0, "y": 144.0, "w": 96.0, "h": 12.0},
        "confidence": None,
    }


def test_lab_result_without_a_loinc_code_is_dropped() -> None:
    """A result the extractor could not code cannot be persisted.

    OpenEMR stamps result_code as LOINC unconditionally, so posting an uncoded result would either
    be refused by the endpoint or publish a fabricated code. Drop it here instead.
    """
    groups = derived_facts_for({"doc-lab": _lab_document(_lab_result(loinc=None))})

    assert groups == []


def test_a_document_whose_every_lab_was_dropped_is_omitted_entirely() -> None:
    """No group is emitted for a document whose every fact was dropped — not an empty group.

    (A lab result always has a box — LabResult refuses one without — so the only drop reason is a
    missing LOINC code.)
    """
    groups = derived_facts_for(
        {"doc-lab": _lab_document(_lab_result(loinc=None), _lab_result(loinc=None))}
    )

    assert groups == []


def test_intake_form_emits_every_persistable_family() -> None:
    """Every intake family now has a native destination and must reach the payload.

    Demographics (accept-gated server-side), chief concern, allergies, and family history each write
    to a native OpenEMR record now (`context/specs/intake-write-back-completion.md`). Medications
    are the exception — they belong to the medication_list document type (JOS-91). If any regresses
    to being omitted, that fact silently never reaches the chart.
    """
    groups = derived_facts_for({"doc-intake": _intake_document()})

    assert len(groups) == 1
    types = sorted(fact["type"] for fact in groups[0]["facts"])
    assert types == ["allergy", "chief_concern", "demographic", "family_history"]


def test_family_history_without_a_relation_is_dropped() -> None:
    """OpenEMR files family history under a per-relative column, so a relation is required.

    An entry with no relation is dropped rather than posted, because the all-or-nothing parser would
    otherwise reject the whole document's batch on that one fact.
    """
    document = ExtractedDocument(
        document_id="doc-intake",
        doc_type=DocType.INTAKE_FORM,
        report=IntakeForm(
            demographics=Demographics(),
            allergies=[],
            family_history=[
                FamilyHistoryItem(condition="asthma", citation=_citation("asthma")),
                FamilyHistoryItem(
                    condition="diabetes",
                    relation=CitedText(value="mother", citation=_citation("mother")),
                    citation=_citation("diabetes"),
                ),
            ],
        ),
    )

    facts = derived_facts_for({"doc-intake": document})[0]["facts"]

    assert [fact["type"] for fact in facts] == ["family_history"]
    assert facts[0]["condition"] == "diabetes"
    assert facts[0]["relation"] == "mother"


def test_demographic_chief_concern_and_family_history_carry_their_fields() -> None:
    groups = derived_facts_for({"doc-intake": _intake_document()})
    facts = {fact["type"]: fact for fact in groups[0]["facts"]}

    assert facts["demographic"]["field"] == "full_name"
    assert facts["demographic"]["value"] == "Sergio"
    assert facts["chief_concern"]["text"] == "cough"
    assert facts["family_history"]["condition"] == "diabetes"
    assert facts["family_history"]["relation"] == "mother"
    assert facts["family_history"]["bbox"] == {"x": 72.0, "y": 144.0, "w": 96.0, "h": 12.0}


def test_intake_allergy_carries_its_fields_and_box() -> None:
    groups = derived_facts_for({"doc-intake": _intake_document()})
    facts = {fact["type"]: fact for fact in groups[0]["facts"]}

    assert facts["allergy"]["substance"] == "Penicillin"
    assert facts["allergy"]["reaction"] == "hives"
    assert facts["allergy"]["bbox"] == {"x": 72.0, "y": 144.0, "w": 96.0, "h": 12.0}
    assert facts["allergy"]["page"] == 1


def test_medication_list_emits_medications() -> None:
    """A medication list persists its medications — the third document type's write surface.

    If this regresses, a med-list document falls through the wire's isinstance chain to an empty
    list and silently persists nothing, so the extracted medications never reach the chart.
    """
    groups = derived_facts_for({"doc-meds": _medication_list_document()})

    assert len(groups) == 1
    assert groups[0]["doc_type"] == "medication_list"
    types = sorted(fact["type"] for fact in groups[0]["facts"])
    assert types == ["medication"]


def test_medication_carries_its_fields_and_box() -> None:
    groups = derived_facts_for({"doc-meds": _medication_list_document()})
    fact = groups[0]["facts"][0]

    assert fact["type"] == "medication"
    assert fact["name"] == "Metformin"
    assert fact["dose"] == "500 mg"
    assert fact["frequency"] == "twice daily"
    assert fact["bbox"] == {"x": 72.0, "y": 144.0, "w": 96.0, "h": 12.0}
    assert fact["page"] == 1


def test_intake_fact_without_a_box_still_persists_without_geometry() -> None:
    """Unlike a lab fact, an intake fact may have no box — it persists, just without an overlay."""
    document = ExtractedDocument(
        document_id="doc-intake",
        doc_type=DocType.INTAKE_FORM,
        report=IntakeForm(
            demographics=Demographics(),
            allergies=[
                Allergy(substance="Latex", citation=_citation("Latex", boxed=False))
            ],
            family_history=[],
        ),
    )

    fact = derived_facts_for({"doc-intake": document})[0]["facts"][0]

    assert fact["type"] == "allergy"
    assert fact["substance"] == "Latex"
    assert "bbox" not in fact
    assert "page" not in fact


def test_multiple_documents_group_independently() -> None:
    groups = derived_facts_for(
        {"doc-lab": _lab_document(_lab_result()), "doc-intake": _intake_document()}
    )

    by_id = {group["document_id"]: group for group in groups}
    assert set(by_id) == {"doc-lab", "doc-intake"}
    assert by_id["doc-lab"]["doc_type"] == "lab_pdf"
    assert by_id["doc-intake"]["doc_type"] == "intake_form"


def test_no_extractions_yields_no_facts() -> None:
    assert derived_facts_for({}) == []
