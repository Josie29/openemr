import base64
from pathlib import Path

import httpx
import pytest

from copilot.fhir.client import FhirError, HttpFhirClient
from copilot.fhir.fixtures import FixtureFhirClient
from copilot.ingestion.extractor import ExtractionError, FhirBinaryByteSource
from copilot.ingestion.schemas import DocType

_INTAKE_PDF = (
    Path(__file__).parent / "fixtures/documents/pdfs/sergio-angulo-intake-form.pdf"
)
_LAB_PDF = (
    Path(__file__).parent / "fixtures" / "documents" / "pdfs" / "sergio-angulo-lab-report.pdf"
)


def _client_with(handler: object) -> HttpFhirClient:
    """An HttpFhirClient whose transport is mocked (mirrors test_per_request_token's seam)."""
    client = HttpFhirClient("https://fhir.example/apis/default/fhir", "tok")
    client._client = httpx.AsyncClient(
        base_url="https://fhir.example/apis/default/fhir",
        headers=dict(client._client.headers),
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )
    return client


async def test_http_get_document_bytes_returns_raw_stream() -> None:
    """OpenEMR streams the raw file for GET /Binary/{id}; the client returns those bytes verbatim.

    If this broke, OCR would receive an empty or wrong payload and the lab report would silently
    extract nothing in prod.
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7 x"
        )

    client = _client_with(handler)
    data = await client.get_document_bytes("a242fb16-81b3-4b5e-83ae-954b27e42a9a")
    assert data == b"%PDF-1.7 x"
    assert seen["path"].endswith("/Binary/a242fb16-81b3-4b5e-83ae-954b27e42a9a")
    await client.aclose()


async def test_http_get_document_bytes_decodes_fhir_binary_json() -> None:
    """If the server returns a FHIR Binary resource instead of a raw stream, decode its base64 data.

    Keeps the fetch correct whether OpenEMR streams the file or returns a standards Binary resource.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        payload = {"resourceType": "Binary", "contentType": "application/pdf"}
        payload["data"] = base64.b64encode(b"%PDF-json").decode()
        return httpx.Response(200, headers={"content-type": "application/fhir+json"}, json=payload)

    client = _client_with(handler)
    assert await client.get_document_bytes("doc-1") == b"%PDF-json"
    await client.aclose()


async def test_http_get_document_bytes_raises_on_error() -> None:
    """A denied/failed Binary read raises FhirError so extraction degrades, not crashes."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    client = _client_with(handler)
    with pytest.raises(FhirError):
        await client.get_document_bytes("doc-1")
    await client.aclose()


async def test_fixture_client_serves_each_document_its_own_pdf() -> None:
    """Each seeded document's bytes come from the PDF configured for ITS type, not one global file.

    The client used to serve a single PDF for any id. With two document types seeded that silently
    hands the lab report's page to an intake extraction: the recorded intake values are right, but
    none of them can be located on the wrong document's text layer, so every intake fact is dropped
    and the physician is told the form is empty.
    """
    client = FixtureFhirClient.from_seed(
        {DocType.LAB_PDF: str(_LAB_PDF), DocType.INTAKE_FORM: str(_INTAKE_PDF)}
    )

    lab = await client.get_document_bytes("labreport-2026-07")
    intake = await client.get_document_bytes("intakeform-2026-07")

    assert lab[:5] == b"%PDF-" and intake[:5] == b"%PDF-"
    assert lab != intake, "each document must serve its own file"
    assert lab == _LAB_PDF.read_bytes()
    assert intake == _INTAKE_PDF.read_bytes()


async def test_fixture_client_without_pdf_raises() -> None:
    """A fixture client with no PDF configured reports the gap rather than returning empty bytes."""
    client = FixtureFhirClient.from_seed()
    with pytest.raises(FhirError):
        await client.get_document_bytes("labreport-2026-07")


async def test_fixture_client_rejects_an_unseeded_document_id() -> None:
    """An id no seeded document carries reports the gap instead of serving some other document."""
    client = FixtureFhirClient.from_seed({DocType.LAB_PDF: str(_LAB_PDF)})
    with pytest.raises(FhirError):
        await client.get_document_bytes("not-a-real-document")


class _StubBytesClient:
    """Minimal FhirClient stand-in for the byte-source, returning canned bytes or raising."""

    def __init__(self, *, raises: bool = False) -> None:
        self._raises = raises

    async def get_document_bytes(self, document_id: str) -> bytes:
        if self._raises:
            raise FhirError("boom")
        return b"%PDF-stub"


async def test_binary_byte_source_passes_bytes_through() -> None:
    """FhirBinaryByteSource returns exactly what the FHIR client yields for the given id."""
    source = FhirBinaryByteSource(_StubBytesClient())  # type: ignore[arg-type]
    assert await source.fetch("doc-1") == b"%PDF-stub"


async def test_binary_byte_source_wraps_fhir_error_as_extraction_error() -> None:
    """A FHIR failure surfaces as ExtractionError so attach_and_extract degrades to no facts.

    attach_and_extract only catches ExtractionError; a leaked FhirError would crash the whole turn.
    """
    source = FhirBinaryByteSource(_StubBytesClient(raises=True))  # type: ignore[arg-type]
    with pytest.raises(ExtractionError):
        await source.fetch("doc-1")
