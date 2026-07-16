import json
from pathlib import Path

from copilot.fhir.models import PatientDemographics
from copilot.ingestion.extractor import ExtractedDocument, map_lab_report
from copilot.ingestion.pdf_geometry import extract_word_boxes
from copilot.ingestion.registry import (
    DOCUMENT_FACT_RESOURCE_TYPE,
    DocumentFactRegistry,
    LabFactHandle,
)
from copilot.ingestion.schemas import BoundingBox, DocType
from copilot.schemas import Claim, FhirCitation, LabPdfCitation, SourceRef
from copilot.verification import FetchLog, ground_claims

_DOCS = Path(__file__).parent / "fixtures/documents"
_LAB_OCR = _DOCS / "extractions/sergio-angulo-lab-report.ocr.json"
_LAB_PDF = _DOCS / "pdfs/sergio-angulo-lab-report.pdf"


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
    words = extract_word_boxes(_LAB_PDF.read_bytes())
    report = map_lab_report(json.loads(_LAB_OCR.read_text()), words)
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
