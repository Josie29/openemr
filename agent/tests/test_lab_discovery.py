from typing import Any

from copilot.fhir.fixtures import FixtureFhirClient
from copilot.fhir.models import LabDocumentSummary


def _lab_pdf(resource_id: str = "a2423e75", category_text: str = "Lab Report") -> dict[str, Any]:
    """A DocumentReference shaped like an uploaded lab PDF (category names a lab; application/pdf).

    Mirrors the live OpenEMR FHIR shape: type is the UNK NullFlavor, so the lab signal is category.
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
        "content": [{"attachment": {"contentType": "application/pdf", "title": "labs.pdf"}}],
    }


def test_lab_pdf_is_discovered() -> None:
    """The real uploaded lab report (category 'Lab Report' + application/pdf) stays discoverable.

    Guards against a discovery filter that is so strict it drops the actual demo document.
    """
    summary = LabDocumentSummary.try_from_fhir(_lab_pdf())
    assert summary is not None
    assert summary.resource_id == "a2423e75"


def test_non_lab_pdf_is_excluded() -> None:
    """A non-lab uploaded PDF (e.g. a referral) must NOT be listed.

    Without the category check it would be OCR'd through the lab schema and its 'facts' presented as
    this patient's labs — the finding-#5 defect.
    """
    referral = _lab_pdf(resource_id="ref-1", category_text="Referral")
    assert LabDocumentSummary.try_from_fhir(referral) is None


def test_clinical_note_is_excluded() -> None:
    """An inline text/plain clinical note has no OCR-able bytes and must be excluded."""
    note = {
        "resourceType": "DocumentReference",
        "id": "note-1",
        "category": [{"text": "Clinical Note"}],
        "content": [{"attachment": {"contentType": "text/plain", "data": "aGVsbG8="}}],
    }
    assert LabDocumentSummary.try_from_fhir(note) is None


def test_lab_category_but_no_binary_is_excluded() -> None:
    """A lab-categorized resource with only a text/plain attachment has nothing to OCR."""
    resource = _lab_pdf(resource_id="x")
    resource["content"] = [{"attachment": {"contentType": "text/plain", "data": "aGVsbG8="}}]
    assert LabDocumentSummary.try_from_fhir(resource) is None


def test_lab_pdf_via_binary_url_is_discovered() -> None:
    """A lab PDF exposed as a Binary url (not inline) is still discovered (the seam reality)."""
    resource = _lab_pdf(resource_id="binref")
    resource["content"] = [
        {"attachment": {"contentType": "application/pdf", "url": "Binary/binref"}}
    ]
    assert LabDocumentSummary.try_from_fhir(resource) is not None


def test_lab_signalled_by_category_coding_display() -> None:
    """The lab signal is also read from a category coding display, not just its text."""
    resource = _lab_pdf(resource_id="coded")
    resource["category"] = [{"coding": [{"display": "Laboratory report"}]}]
    assert LabDocumentSummary.try_from_fhir(resource) is not None


async def test_get_lab_documents_returns_only_lab_pdfs() -> None:
    """End-to-end via the fixture client: a mixed doc list yields only the lab report."""
    lab = _lab_pdf(resource_id="lab-1")
    lab["subject"] = {"reference": "Patient/1"}
    referral = _lab_pdf(resource_id="ref-2", category_text="Referral")
    referral["subject"] = {"reference": "Patient/1"}
    client = FixtureFhirClient(
        {
            "1": {
                "Patient": {"resourceType": "Patient", "id": "1"},
                "DocumentReference": [lab, referral],
            }
        }
    )
    docs = await client.get_lab_documents("1")
    assert [d.resource_id for d in docs] == ["lab-1"]
