from typing import Any

from copilot.fhir.fixtures import FixtureFhirClient
from copilot.fhir.models import UploadedDocumentSummary
from copilot.ingestion.schemas import DocType


def _uploaded_pdf(
    resource_id: str = "a2423e75", category_text: str = "Lab Report"
) -> dict[str, Any]:
    """A DocumentReference shaped like an uploaded PDF, filed under a category.

    Mirrors the live OpenEMR FHIR shape: type is the UNK NullFlavor, so category is the only signal.
    """
    return {
        "resourceType": "DocumentReference",
        "id": resource_id,
        "type": {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/v3-NullFlavor",
                    "code": "UNK",
                    "display": "unknown",
                }
            ]
        },
        "category": [{"text": category_text}],
        "date": "2026-07-08",
        "content": [{"attachment": {"contentType": "application/pdf", "title": "doc.pdf"}}],
    }


def test_lab_pdf_is_discovered_as_a_lab() -> None:
    """The real uploaded lab report (category 'Lab Report' + application/pdf) stays discoverable.

    Guards against a discovery filter that is so strict it drops the actual demo document.
    """
    summary = UploadedDocumentSummary.try_from_fhir(_uploaded_pdf())
    assert summary is not None
    assert summary.resource_id == "a2423e75"
    assert summary.doc_type is DocType.LAB_PDF


def test_intake_form_is_discovered_as_an_intake_form() -> None:
    """A PDF filed under 'Patient Information' resolves to the intake schema.

    The category is what picks the schema — the model never chooses. If this breaks, an uploaded
    intake form is invisible to the agent and the physician is told the record has nothing in it.
    """
    summary = UploadedDocumentSummary.try_from_fhir(
        _uploaded_pdf(resource_id="intake-1", category_text="Patient Information")
    )
    assert summary is not None
    assert summary.doc_type is DocType.INTAKE_FORM


def test_medication_list_is_discovered_as_a_medication_list() -> None:
    """A PDF filed under 'Medication List' resolves to the medication-list schema.

    This is the third document type's discovery seam. If it breaks, an uploaded medication list is
    invisible to the agent and its medications never reach the chart — the physician is told there
    is nothing to extract.
    """
    summary = UploadedDocumentSummary.try_from_fhir(
        _uploaded_pdf(resource_id="meds-1", category_text="Medication List")
    )
    assert summary is not None
    assert summary.doc_type is DocType.MEDICATION_LIST


def test_medical_record_category_resolves_to_medication_list() -> None:
    """'Medical Record' is the demo fallback for a medication list (the seeded category does not
    reliably render in OpenEMR's Documents tree). If this breaks, the demo upload path stops
    resolving. TRADEOFF documented at resolve_doc_type: any Medical Record upload extracts as a
    medication list — gate or drop before prod.
    """
    summary = UploadedDocumentSummary.try_from_fhir(
        _uploaded_pdf(resource_id="mr-1", category_text="Medical Record")
    )
    assert summary is not None
    assert summary.doc_type is DocType.MEDICATION_LIST


def test_intake_category_is_matched_exactly_not_by_substring() -> None:
    """'Patient Information' matches exactly; its identity CHILDREN do not.

    OpenEMR's tree nests 'Patient ID card' and 'Patient Photograph' under 'Patient Information'.
    A substring match on "patient" would sweep those in and read a driver's licence or a headshot
    through the intake schema. Labs can afford a tolerant match ('Laboratory', 'Labs'); intake
    cannot, because its category is the identity bucket rather than a purpose-built one.
    """
    for category in ("Patient ID card", "Patient Photograph"):
        summary = UploadedDocumentSummary.try_from_fhir(
            _uploaded_pdf(resource_id="id-1", category_text=category)
        )
        assert summary is None, f"{category!r} must not resolve to the intake schema"


def test_document_of_no_extractable_kind_is_excluded() -> None:
    """A PDF whose category names neither schema (e.g. a referral) must NOT be listed.

    Without the category check it would be OCR'd through whichever schema ran, and its 'facts'
    presented as this patient's — the finding-#5 defect.
    """
    referral = _uploaded_pdf(resource_id="ref-1", category_text="Referral")
    assert UploadedDocumentSummary.try_from_fhir(referral) is None


def test_clinical_note_is_excluded() -> None:
    """An inline text/plain clinical note has no OCR-able bytes and must be excluded."""
    note = {
        "resourceType": "DocumentReference",
        "id": "note-1",
        "category": [{"text": "Clinical Note"}],
        "content": [{"attachment": {"contentType": "text/plain", "data": "aGVsbG8="}}],
    }
    assert UploadedDocumentSummary.try_from_fhir(note) is None


def test_known_category_but_no_binary_is_excluded() -> None:
    """A lab-categorized resource with only a text/plain attachment has nothing to OCR."""
    resource = _uploaded_pdf(resource_id="x")
    resource["content"] = [{"attachment": {"contentType": "text/plain", "data": "aGVsbG8="}}]
    assert UploadedDocumentSummary.try_from_fhir(resource) is None


def test_pdf_via_binary_url_is_discovered() -> None:
    """A PDF exposed as a Binary url (not inline) is still discovered (the seam reality)."""
    resource = _uploaded_pdf(resource_id="binref")
    resource["content"] = [
        {"attachment": {"contentType": "application/pdf", "url": "Binary/binref"}}
    ]
    assert UploadedDocumentSummary.try_from_fhir(resource) is not None


def test_lab_signalled_by_category_coding_display() -> None:
    """The lab signal is also read from a category coding display, not just its text."""
    resource = _uploaded_pdf(resource_id="coded")
    resource["category"] = [{"coding": [{"display": "Laboratory report"}]}]
    summary = UploadedDocumentSummary.try_from_fhir(resource)
    assert summary is not None
    assert summary.doc_type is DocType.LAB_PDF


async def test_get_documents_returns_both_types_and_drops_the_rest() -> None:
    """End-to-end via the fixture client: a mixed doc list yields the lab AND the intake form.

    The discovery read is what makes an intake form reachable at all; before this, it was filtered
    out by design so it would not be OCR'd through the lab schema.
    """
    lab = _uploaded_pdf(resource_id="lab-1")
    lab["subject"] = {"reference": "Patient/1"}
    intake = _uploaded_pdf(resource_id="intake-1", category_text="Patient Information")
    intake["subject"] = {"reference": "Patient/1"}
    referral = _uploaded_pdf(resource_id="ref-2", category_text="Referral")
    referral["subject"] = {"reference": "Patient/1"}
    client = FixtureFhirClient(
        {
            "1": {
                "Patient": {"resourceType": "Patient", "id": "1"},
                "DocumentReference": [lab, intake, referral],
            }
        }
    )

    docs = await client.get_documents("1")

    assert {d.resource_id: d.doc_type for d in docs} == {
        "lab-1": DocType.LAB_PDF,
        "intake-1": DocType.INTAKE_FORM,
    }
