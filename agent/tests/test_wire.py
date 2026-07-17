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
        unit="%",
        reference_range="4.0-5.6",
        abnormal_flag=AbnormalFlag.HIGH,
        citation=_citation("8.2"),
    )


def _lab_document(*results: LabResult, document_id: str = "doc-lab") -> ExtractedDocument:
    return ExtractedDocument(
        document_id=document_id, doc_type=DocType.LAB_PDF, report=LabReport(results=list(results))
    )


def _intake_document(document_id: str = "doc-intake") -> ExtractedDocument:
    """An intake form exercising every section — including the three that must NOT be persisted."""
    return ExtractedDocument(
        document_id=document_id,
        doc_type=DocType.INTAKE_FORM,
        report=IntakeForm(
            demographics=Demographics(
                full_name=CitedText(value="Sergio", citation=_citation("Sergio"))
            ),
            chief_concern=CitedText(value="cough", citation=_citation("cough")),
            current_medications=[
                Medication(
                    name="Metformin",
                    dose="500 mg",
                    frequency="twice daily",
                    citation=_citation("Metformin"),
                )
            ],
            allergies=[
                Allergy(substance="Penicillin", reaction="hives", citation=_citation("Penicillin"))
            ],
            family_history=[
                FamilyHistoryItem(condition="diabetes", citation=_citation("diabetes"))
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


def test_intake_form_emits_only_allergies_and_medications() -> None:
    """Demographics, chief concern and family history must never reach the payload.

    They are extracted but have no honest write target, and the endpoint parser is all-or-nothing:
    one unpersistable `type` rejects the whole POST. If this regresses, every intake form 422s.
    """
    groups = derived_facts_for({"doc-intake": _intake_document()})

    assert len(groups) == 1
    types = sorted(fact["type"] for fact in groups[0]["facts"])
    assert types == ["allergy", "medication"]


def test_intake_allergy_and_medication_carry_their_fields_and_box() -> None:
    groups = derived_facts_for({"doc-intake": _intake_document()})
    facts = {fact["type"]: fact for fact in groups[0]["facts"]}

    assert facts["allergy"]["substance"] == "Penicillin"
    assert facts["allergy"]["reaction"] == "hives"
    assert facts["allergy"]["bbox"] == {"x": 72.0, "y": 144.0, "w": 96.0, "h": 12.0}
    assert facts["allergy"]["page"] == 1
    assert facts["medication"]["name"] == "Metformin"
    assert facts["medication"]["dose"] == "500 mg"
    assert facts["medication"]["frequency"] == "twice daily"


def test_intake_fact_without_a_box_still_persists_without_geometry() -> None:
    """Unlike a lab fact, an intake fact may have no box — it persists, just without an overlay."""
    document = ExtractedDocument(
        document_id="doc-intake",
        doc_type=DocType.INTAKE_FORM,
        report=IntakeForm(
            demographics=Demographics(),
            current_medications=[],
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
