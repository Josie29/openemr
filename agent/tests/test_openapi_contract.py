import json
from pathlib import Path

from fastapi.testclient import TestClient

from copilot.api_schemas import DocumentsResponse, EvidenceResponse, ExtractionResponse
from copilot.config import ExtractorMode, FhirClientMode, ModelTier, RetrievalMode, Settings
from copilot.main import create_app
from copilot.openapi import OPENAPI_PATH, build_openapi_spec, dump_spec

# Contract tests for the Week-2 HTTP endpoints (JOS-63).
#
# Catches: (1) the committed agent/openapi.json drifting from the implementation — an endpoint or
# response field changing without regenerating the spec, which would ship a lying contract to
# graders/clients; and (2) an endpoint returning a body that no longer validates against the
# response model the spec advertises.

_DOCS = Path(__file__).parent / "fixtures" / "documents"


def _extraction_settings() -> Settings:
    """Fixture settings with the extractor + document bytes wired to the committed angulo fixtures.

    So the FIXTURE FHIR client serves the seeded angulo (patient 23) lab/intake PDFs and the
    extractor replays their recorded OCR — the whole extraction path runs offline.
    """
    return Settings(
        model_tier=ModelTier.SONNET,
        fhir_client_mode=FhirClientMode.FIXTURE,
        retrieval_mode=RetrievalMode.FIXTURE,
        extractor_mode=ExtractorMode.FIXTURE,
        document_pdf_path_lab_pdf=str(_DOCS / "pdfs" / "sergio-angulo-lab-report.pdf"),
        ocr_fixture_path_lab_pdf=str(_DOCS / "extractions" / "sergio-angulo-lab-report.ocr.json"),
        document_pdf_path_intake_form=str(_DOCS / "pdfs" / "sergio-angulo-intake-form.pdf"),
        ocr_fixture_path_intake_form=str(
            _DOCS / "extractions" / "sergio-angulo-intake-form.ocr.json"
        ),
        anthropic_api_key=None,
        langfuse_public_key=None,
        langfuse_secret_key=None,
    )


def test_committed_openapi_spec_matches_the_implementation() -> None:
    """The committed agent/openapi.json is exactly what the app generates today.

    Breaks when an endpoint or a response model changes without regenerating the spec — the guard
    that keeps the published contract honest. Fix: `python scripts/dump_openapi.py`.
    """
    committed = json.loads(OPENAPI_PATH.read_text())
    current = build_openapi_spec()
    assert current == committed, (
        "agent/openapi.json is out of date with the implementation — "
        "regenerate it with `python scripts/dump_openapi.py` and commit the result."
    )


def test_committed_spec_is_in_canonical_form() -> None:
    """The file on disk is the canonical serialization (sorted keys, 2-space indent, trailing NL).

    Guards against a hand-edited or differently-formatted spec that would round-trip-equal but
    produce a noisy diff on the next legitimate regeneration.
    """
    assert OPENAPI_PATH.read_text() == dump_spec(json.loads(OPENAPI_PATH.read_text()))


def test_documents_response_matches_its_advertised_model() -> None:
    """GET /documents returns a body that validates against DocumentsResponse."""
    client = TestClient(create_app(_extraction_settings()))
    resp = client.get("/documents", params={"patient_id": "23"})
    assert resp.status_code == 200
    parsed = DocumentsResponse.model_validate(resp.json())
    # Patient 23 (angulo) has the seeded lab + intake + medication-list uploads.
    assert {d.doc_type.value for d in parsed.documents} == {
        "lab_pdf",
        "intake_form",
        "medication_list",
    }


def test_extraction_response_matches_its_advertised_model() -> None:
    """GET /documents/{id}/extraction returns a body that validates against ExtractionResponse."""
    client = TestClient(create_app(_extraction_settings()))
    resp = client.get(
        "/documents/labreport-2026-07/extraction", params={"patient_id": "23"}
    )
    assert resp.status_code == 200
    ExtractionResponse.model_validate(resp.json())


def test_evidence_response_matches_its_advertised_model() -> None:
    """GET /evidence returns a body that validates against EvidenceResponse (the spec's schema)."""
    client = TestClient(create_app(_extraction_settings()))
    resp = client.get("/evidence", params={"query": "hypertension blood pressure target"})
    assert resp.status_code == 200
    EvidenceResponse.model_validate(resp.json())
