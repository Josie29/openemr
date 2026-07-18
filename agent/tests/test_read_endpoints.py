from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from copilot.config import ExtractorMode, FhirClientMode, ModelTier, RetrievalMode, Settings
from copilot.fhir.fixtures import FixtureFhirClient
from copilot.ingestion.extractor import (
    DocumentExtractor,
    ExtractedDocument,
    ExtractionError,
    FixtureOcrBackend,
    resolve_and_extract,
)
from copilot.ingestion.schemas import DocType
from copilot.main import create_app

# Tests for the read-only Week-2 subsystem endpoints (JOS-67 / JOS-63): /documents,
# /documents/{id}/extraction, /evidence. These let a caller exercise one subsystem directly,
# without a /chat LLM turn — so the tests assert the direct HTTP behavior a grader's API collection
# and the contract tests depend on.

_DOCS = Path(__file__).parent / "fixtures" / "documents"
_LAB_PDF = str(_DOCS / "pdfs" / "sergio-angulo-lab-report.pdf")
_LAB_OCR = str(_DOCS / "extractions" / "sergio-angulo-lab-report.ocr.json")
_INTAKE_PDF = str(_DOCS / "pdfs" / "sergio-angulo-intake-form.pdf")
_INTAKE_OCR = str(_DOCS / "extractions" / "sergio-angulo-intake-form.ocr.json")
_MEDLIST_PDF = str(_DOCS / "pdfs" / "sergio-angulo-medication-list.pdf")
_MEDLIST_OCR = str(_DOCS / "extractions" / "sergio-angulo-medication-list.ocr.json")

# The seeded angulo (patient 23) uploads — see fhir/seed/patient-23-angulo.bundle.json.
_PATIENT = "23"
_LAB_DOC_ID = "labreport-2026-07"
_INTAKE_DOC_ID = "intakeform-2026-07"


def _fixture_settings(*, lab: bool = True, intake: bool = True, meds: bool = True) -> Settings:
    """Offline settings with the extractor + document bytes wired to the angulo fixtures.

    ``lab``/``intake``/``meds`` toggle whether each type is configured, so a test can drive the
    partial-configuration failure path (a document whose type has no fixture raises ExtractionError).
    """
    return Settings(
        model_tier=ModelTier.SONNET,
        fhir_client_mode=FhirClientMode.FIXTURE,
        retrieval_mode=RetrievalMode.FIXTURE,
        extractor_mode=ExtractorMode.FIXTURE,
        document_pdf_path_lab_pdf=_LAB_PDF if lab else None,
        ocr_fixture_path_lab_pdf=_LAB_OCR if lab else None,
        document_pdf_path_intake_form=_INTAKE_PDF if intake else None,
        ocr_fixture_path_intake_form=_INTAKE_OCR if intake else None,
        document_pdf_path_medication_list=_MEDLIST_PDF if meds else None,
        ocr_fixture_path_medication_list=_MEDLIST_OCR if meds else None,
        anthropic_api_key=None,
        langfuse_public_key=None,
        langfuse_secret_key=None,
    )


def _http_settings() -> Settings:
    """Live HTTP mode with no token, for the auth-boundary cases (no network is touched)."""
    return Settings(
        fhir_client_mode=FhirClientMode.HTTP,
        fhir_base_url="https://openemr.example/apis/default/fhir",
        fhir_bearer_token=None,
        retrieval_mode=RetrievalMode.FIXTURE,
        anthropic_api_key=None,
        langfuse_public_key=None,
        langfuse_secret_key=None,
    )


# --- GET /documents ----------------------------------------------------------------------------


def test_documents_lists_the_patients_uploaded_documents() -> None:
    # Breaks if the doc-listing endpoint stops surfacing the uploads a grader must pick from to
    # drive extraction (or leaks non-extractable docs like clinical notes).
    client = TestClient(create_app(_fixture_settings()))
    resp = client.get("/documents", params={"patient_id": _PATIENT})
    assert resp.status_code == 200
    body = resp.json()
    assert body["patient_id"] == _PATIENT
    by_type = {d["doc_type"] for d in body["documents"]}
    assert by_type == {"lab_pdf", "intake_form", "medication_list"}


def test_documents_is_empty_for_a_patient_with_no_uploads() -> None:
    # Breaks if "no uploaded documents" surfaces as an error instead of an empty list — the caller
    # must be able to tell "none on file" apart from a failure.
    client = TestClient(create_app(_fixture_settings()))
    resp = client.get("/documents", params={"patient_id": "1"})
    assert resp.status_code == 200
    assert resp.json()["documents"] == []


def test_documents_without_a_token_is_rejected_in_http_mode() -> None:
    # Breaks the auth boundary: in live HTTP mode a tokenless read must 401 before any FHIR call.
    with TestClient(create_app(_http_settings())) as client:
        resp = client.get("/documents", params={"patient_id": _PATIENT})
    assert resp.status_code == 401
    assert resp.json()["error"] == "missing patient-scoped FHIR token"


# --- GET /documents/{id}/extraction ------------------------------------------------------------


def test_extraction_returns_strict_facts_with_boxes_and_confidence() -> None:
    # Breaks if extraction stops returning citable, boxed facts — the click-to-source overlay and
    # the "prove it" story depend on every value carrying a bounding box + confidence.
    client = TestClient(create_app(_fixture_settings()))
    resp = client.get(f"/documents/{_LAB_DOC_ID}/extraction", params={"patient_id": _PATIENT})
    assert resp.status_code == 200
    body = resp.json()
    assert body["doc_type"] == "lab_pdf"  # resolved server-side from the document's category
    results = body["report"]["results"]
    assert results, "expected the lab report to yield results"
    first = results[0]
    assert first["citation"]["bounding_box"] is not None
    assert first["confidence"] is not None


def test_extraction_of_an_unknown_document_is_a_404() -> None:
    # Breaks the guard that only a document the patient actually has is extractable: a guessed id
    # must 404, never silently extract or 500.
    client = TestClient(create_app(_fixture_settings()))
    resp = client.get("/documents/ghost/extraction", params={"patient_id": _PATIENT})
    assert resp.status_code == 404
    assert resp.json()["error"] == "document not found for this patient"


def test_extraction_reports_502_when_the_document_cannot_be_read() -> None:
    # Breaks if a failed OCR/byte-fetch is dressed up as success: a document whose type is not
    # configured must surface as a controlled 502, never fabricated facts.
    client = TestClient(create_app(_fixture_settings(lab=True, intake=False)))
    resp = client.get(f"/documents/{_INTAKE_DOC_ID}/extraction", params={"patient_id": _PATIENT})
    assert resp.status_code == 502
    assert resp.json()["error"] == "could not read the document"


def test_extraction_reports_503_when_extraction_is_unconfigured() -> None:
    # Breaks the "extraction disabled" degradation: with no extractor wired the endpoint must say
    # so (503), not 500 or pretend the document has no facts.
    settings = _fixture_settings(lab=False, intake=False, meds=False)  # no fixtures -> extractor None
    client = TestClient(create_app(settings))
    resp = client.get(f"/documents/{_LAB_DOC_ID}/extraction", params={"patient_id": _PATIENT})
    assert resp.status_code == 503
    assert resp.json()["error"] == "document extraction is not available"


def test_extraction_without_a_token_is_rejected_in_http_mode() -> None:
    # Breaks the auth boundary for the extraction path (which fetches PDF bytes over Binary).
    with TestClient(create_app(_http_settings())) as client:
        resp = client.get(f"/documents/{_LAB_DOC_ID}/extraction", params={"patient_id": _PATIENT})
    assert resp.status_code == 401
    assert resp.json()["error"] == "missing patient-scoped FHIR token"


# --- GET /evidence -----------------------------------------------------------------------------


def test_evidence_returns_ranked_guideline_chunks() -> None:
    # Breaks if the evidence endpoint stops returning cited, scored guideline chunks — the direct
    # retrieval path a grader tests without a full agent turn.
    client = TestClient(create_app(_fixture_settings()))
    resp = client.get("/evidence", params={"query": "hypertension blood pressure target"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["evidence"], "expected the corpus to match a hypertension query"
    item = body["evidence"][0]
    assert item["topic"] and item["chunk_id"] and item["text"]
    assert 0.0 <= item["score"] <= 1.0


def test_evidence_respects_top_n() -> None:
    # Breaks if top_n is ignored — a caller capping the result set must get at most that many.
    client = TestClient(create_app(_fixture_settings()))
    resp = client.get("/evidence", params={"query": "hypertension", "top_n": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["top_n"] == 1
    assert len(body["evidence"]) <= 1


def test_evidence_without_a_token_is_rejected_in_http_mode() -> None:
    # Breaks the uniform auth surface: /evidence is corpus-only but still requires the bearer token,
    # so a stolen page cannot query it tokenless in live mode.
    with TestClient(create_app(_http_settings())) as client:
        resp = client.get("/evidence", params={"query": "hypertension"})
    assert resp.status_code == 401
    assert resp.json()["error"] == "missing patient-scoped FHIR token"


# --- resolve_and_extract (the shared core) -----------------------------------------------------


async def test_resolve_and_extract_extracts_a_known_document() -> None:
    # Breaks the shared core the /chat tool and the endpoint both call: a known id must OCR into an
    # ExtractedDocument whose doc_type came from the discovered record (never a caller input).
    fhir = FixtureFhirClient.from_seed({DocType.LAB_PDF: _LAB_PDF})
    extractor = DocumentExtractor(FixtureOcrBackend({DocType.LAB_PDF: _LAB_OCR}))
    documents = await fhir.get_documents(_PATIENT)
    result = await resolve_and_extract(_LAB_DOC_ID, documents, extractor, fhir)
    assert isinstance(result, ExtractedDocument)
    assert result.doc_type is DocType.LAB_PDF
    assert result.document_id == _LAB_DOC_ID


async def test_resolve_and_extract_returns_none_for_an_unknown_id() -> None:
    # Breaks the guessed-id guard: an id not in the patient's uploads must return None (no fetch,
    # no OCR), so neither the tool nor the endpoint extracts a hallucinated document.
    fhir = FixtureFhirClient.from_seed({DocType.LAB_PDF: _LAB_PDF})
    extractor = DocumentExtractor(FixtureOcrBackend({DocType.LAB_PDF: _LAB_OCR}))
    documents = await fhir.get_documents(_PATIENT)
    assert await resolve_and_extract("ghost", documents, extractor, fhir) is None


async def test_resolve_and_extract_propagates_extraction_failures() -> None:
    # Breaks the error contract: an unreadable document must raise ExtractionError (which the tool
    # logs and the endpoint maps to 502), not return empty facts that read as "nothing on file".
    fhir = FixtureFhirClient.from_seed({DocType.LAB_PDF: _LAB_PDF})
    extractor = DocumentExtractor(FixtureOcrBackend({}))  # no OCR fixture for any type
    documents = await fhir.get_documents(_PATIENT)
    with pytest.raises(ExtractionError):
        await resolve_and_extract(_LAB_DOC_ID, documents, extractor, fhir)
