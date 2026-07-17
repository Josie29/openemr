import json
from pathlib import Path

from copilot.fhir.models import PatientDemographics
from copilot.ingestion.extractor import ExtractedDocument, map_intake_form, map_lab_report
from copilot.ingestion.geometry.document import DocumentGeometry
from copilot.ingestion.geometry.words import extract_word_boxes
from copilot.ingestion.registry import (
    DOCUMENT_FACT_RESOURCE_TYPE,
    DocumentFactRegistry,
    LabFactHandle,
)
from copilot.ingestion.schemas import AbnormalFlag, BoundingBox, DocType, LabDetail
from copilot.schemas import Claim, FhirCitation, IntakeFormCitation, LabPdfCitation, SourceRef
from copilot.verification import FetchLog, ground_claims

_DOCS = Path(__file__).parent / "fixtures/documents"
_LAB_OCR = _DOCS / "extractions/sergio-angulo-lab-report.ocr.json"
_LAB_PDF = _DOCS / "pdfs/sergio-angulo-lab-report.pdf"
_INTAKE_OCR = _DOCS / "extractions/sergio-angulo-intake-form.ocr.json"
_INTAKE_PDF = _DOCS / "pdfs/sergio-angulo-intake-form.pdf"


def test_stamp_strips_model_authored_box_on_fhir_claim() -> None:
    """Guards click-to-source: a plain FHIR fact must never carry a box the model invented.

    If the gate didn't strip it, the sidebar would draw a fabricated source-overlay rectangle on a
    Patient/Problem fact that was never read from a document.
    """
    fetched = FetchLog()
    fetched.record(
        "Patient", "1", PatientDemographics(resource_id="1", full_name="Marisol Reyes")
    )
    # The model wrongly fills bounding_box on a Patient citation (schema-visible, but system-set).
    claim = Claim(
        text="The patient is Marisol Reyes.",
        source=SourceRef(
            resource_type="Patient",
            resource_id="1",
            field="full_name",
            bounding_box=BoundingBox(page=1, x=10, y=10, width=5, height=5),
        ),
    )

    grounded, offenders = ground_claims([claim], fetched)
    assert not offenders
    stamped = grounded[0].source
    assert stamped.value == "Marisol Reyes"  # grounded normally
    assert stamped.bounding_box is None  # the fabricated box was stripped
    assert stamped.page is None
    assert stamped.document_id is None
    assert isinstance(stamped.to_citation(), FhirCitation)  # not a LabPdfCitation


def test_stamp_keeps_box_on_real_document_fact() -> None:
    """The strip guard must not harm the real path: a genuine extracted lab fact keeps its box.

    Confirms fixing the fabricated-box gap does not regress click-to-source for real document facts.
    """
    ocr = json.loads(_LAB_OCR.read_text())
    words = extract_word_boxes(_LAB_PDF.read_bytes())
    report = map_lab_report(ocr, DocumentGeometry.from_parts(ocr, words))
    registry = DocumentFactRegistry()
    handles = registry.record(
        ExtractedDocument(document_id="doc-1", doc_type=DocType.LAB_PDF, report=report)
    )
    # The registry records whatever kind of fact a document yields, so its handles are a tagged
    # union; this test is about the lab arm.
    handle = next(
        h for h in handles if isinstance(h, LabFactHandle) and h.test_name == "Creatinine"
    )
    assert handle.resource_type == DOCUMENT_FACT_RESOURCE_TYPE
    claim = Claim(
        text="Creatinine was 1.44 mg/dL (high).",
        source=SourceRef(
            resource_type=handle.resource_type, resource_id=handle.resource_id, field="value"
        ),
    )

    grounded, offenders = ground_claims([claim], registry)
    assert not offenders
    stamped = grounded[0].source
    assert stamped.bounding_box is not None  # system-stamped from the extraction
    assert stamped.document_id == "doc-1"
    assert isinstance(stamped.to_citation(), LabPdfCitation)


def _record_lab_fixture() -> DocumentFactRegistry:
    """Record the real lab fixture's facts, so a claim can cite a genuinely extracted lab result."""
    ocr = json.loads(_LAB_OCR.read_text())
    words = extract_word_boxes(_LAB_PDF.read_bytes())
    report = map_lab_report(ocr, DocumentGeometry.from_parts(ocr, words))
    registry = DocumentFactRegistry()
    registry.record(
        ExtractedDocument(document_id="doc-1", doc_type=DocType.LAB_PDF, report=report)
    )
    return registry


def _creatinine_handle(registry: DocumentFactRegistry) -> LabFactHandle:
    """The recorded Creatinine fact — high at 1.44 mg/dL against a 0.70-1.30 range on the
    fixture."""
    ocr = json.loads(_LAB_OCR.read_text())
    words = extract_word_boxes(_LAB_PDF.read_bytes())
    report = map_lab_report(ocr, DocumentGeometry.from_parts(ocr, words))
    handles = registry.record(
        ExtractedDocument(document_id="doc-2", doc_type=DocType.LAB_PDF, report=report)
    )
    return next(
        h for h in handles if isinstance(h, LabFactHandle) and h.test_name == "Creatinine"
    )


def test_stamp_strips_model_authored_lab_detail_on_fhir_claim() -> None:
    """Guards the lab table: a plain FHIR fact must never carry analyte metadata the model invented.

    The sidebar renders lab_detail as system-stamped fact — cells it claims came off the page. If
    the gate did not strip this, a model could attach a reference range to any record fact and the
    table would present the invention as extracted truth.

    This fails the moment anyone re-guards the stamp with `if resolution.lab_detail is not None`.
    """
    fetched = FetchLog()
    fetched.record(
        "Patient", "1", PatientDemographics(resource_id="1", full_name="Marisol Reyes")
    )
    claim = Claim(
        text="The patient is Marisol Reyes.",
        source=SourceRef(
            resource_type="Patient",
            resource_id="1",
            field="full_name",
            lab_detail=LabDetail(
                test_name="Potassium",
                unit="mmol/L",
                reference_range="3.5-5.1",
                abnormal_flag=AbnormalFlag.HIGH,
            ),
        ),
    )

    grounded, offenders = ground_claims([claim], fetched)
    assert not offenders
    stamped = grounded[0].source
    assert stamped.lab_detail is None  # the fabricated analyte metadata was stripped
    assert isinstance(stamped.to_citation(), FhirCitation)


def test_stamp_overwrites_model_authored_lab_detail_on_real_lab_fact() -> None:
    """A model cannot re-label a real abnormal lab as normal by authoring its own reference range.

    The nastier half of the guard, and the one a `is None` check would silently pass: the fact IS a
    real extracted lab result, so a truthiness guard would keep the model's values. Here the model
    forges a range that makes a high creatinine (1.44) read as in-range, and flags it normal. If the
    stamp did not overwrite unconditionally, the sidebar's table would show a physician a fabricated
    range next to a real value and call it extracted — hiding an abnormal renal result.
    """
    registry = _record_lab_fixture()
    handle = _creatinine_handle(registry)
    claim = Claim(
        text="Creatinine was 1.44 mg/dL.",
        source=SourceRef(
            resource_type=handle.resource_type,
            resource_id=handle.resource_id,
            field="value",
            lab_detail=LabDetail(
                test_name="Creatinine",
                unit="mg/dL",
                reference_range="0.0-9.9",  # forged: makes 1.44 look in-range
                abnormal_flag=AbnormalFlag.NO,  # forged: hides the high flag
            ),
        ),
    )

    grounded, offenders = ground_claims([claim], registry)
    assert not offenders
    detail = grounded[0].source.lab_detail
    assert detail is not None
    assert detail.abnormal_flag is AbnormalFlag.HIGH  # the extractor's, not the model's
    assert detail.reference_range == handle.reference_range
    assert detail.reference_range != "0.0-9.9"
    assert detail.test_name == "Creatinine"
    assert detail.unit == "mg/dL"


def test_intake_fact_carries_no_lab_detail() -> None:
    """An intake fact has no analyte metadata, and its wire arm must not advertise the column.

    Pins the decision to hang lab_detail on the LAB_PDF arm rather than the shared document base:
    on the base, every intake citation would ship a permanently-null reference_range that nothing
    can populate, and the sidebar would render a table column that can never fill for a date of
    birth or a penicillin allergy.
    """
    ocr = json.loads(_INTAKE_OCR.read_text())
    pdf = _INTAKE_PDF.read_bytes()
    form = map_intake_form(ocr, DocumentGeometry.from_document(pdf, ocr))
    registry = DocumentFactRegistry()
    handles = registry.record(
        ExtractedDocument(document_id="doc-3", doc_type=DocType.INTAKE_FORM, report=form)
    )
    handle = handles[0]
    claim = Claim(
        text="An intake fact.",
        source=SourceRef(
            resource_type=handle.resource_type, resource_id=handle.resource_id, field="value"
        ),
    )

    grounded, offenders = ground_claims([claim], registry)
    assert not offenders
    stamped = grounded[0].source
    assert stamped.lab_detail is None
    citation = stamped.to_citation()
    assert isinstance(citation, IntakeFormCitation)
    assert not hasattr(citation, "lab_detail")
